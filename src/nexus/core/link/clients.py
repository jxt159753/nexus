"""Authenticated browser transport. No task authority or unbounded fan-out."""

import asyncio
import sqlite3
from dataclasses import dataclass, field
from uuid import UUID

import anyio
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketException
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.messages import TaskAccepted, WireError
from nexus.contracts.protocol import parse_message
from nexus.core.harness.service import Harness
from nexus.core.security.http import SecurityHTTP
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.client_events import ClientEvents
from nexus.core.storage.tasks import Conflict
from nexus.link import MessageRate, wire_message


class SlowClient(Exception):
    pass


@dataclass
class BrowserConnection:
    websocket: WebSocket
    principal: Principal
    node_id: UUID | None
    cursor: int = 0
    outgoing: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(32))

    def enqueue(self, kind: str, payload, correlation_id: UUID | None = None) -> None:
        try:
            self.outgoing.put_nowait(wire_message(kind, payload, correlation_id))
        except asyncio.QueueFull:
            raise SlowClient() from None


class ClientLink:
    def __init__(self, harness: Harness, guards: SecurityHTTP):
        self.harness, self.guards = harness, guards
        self.events = ClientEvents(guards.service.database)
        self.connections: list[BrowserConnection] = []
        self.pending = 0
        self.handshakes = MessageRate()
        self.router = APIRouter()
        self.router.add_api_websocket_route("/ws/v1/client", self.endpoint)
        self.router.add_api_route("/api/v1/client/snapshot", self.snapshot, methods=["GET"])

    async def snapshot(self, request: Request, before: int = Query(2**63-1, ge=1, le=2**63-1)) -> dict:
        principal = await self.guards.owner_http(request)
        return await self.events.snapshot(principal.identity_id, before)

    async def sender(self, connection: BrowserConnection) -> None:
        while True:
            raw = await connection.outgoing.get()
            await self.guards.check_ws_message(connection.websocket, connection.principal)
            try:
                async with asyncio.timeout(2):
                    await connection.websocket.send_text(raw)
            except TimeoutError:
                raise SlowClient() from None

    async def tail(self, connection: BrowserConnection) -> None:
        while True:
            await self.guards.check_ws_message(connection.websocket, connection.principal)
            messages, cursor = await self.events.page(connection.principal.identity_id, connection.cursor)
            for kind, payload in messages:
                connection.enqueue(kind, payload)
            connection.cursor = cursor
            await asyncio.sleep(0.25)

    async def receiver(self, connection: BrowserConnection) -> None:
        rate = MessageRate()
        while True:
            raw = await self.guards.receive_text(connection.websocket, connection.principal)
            message = None
            try:
                rate.check()
                if len(raw.encode("utf-8")) > self.harness.link.config.max_message_bytes:
                    raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Frame too large")
                message = parse_message(raw)
                payload = message.payload
                if message.type == "task.submit":
                    if connection.node_id is None:
                        raise AccessDenied("POLICY_DENIED")
                    task = await self.harness.submit(connection.principal, connection.node_id, payload)
                    connection.enqueue("task.accepted", TaskAccepted(task_id=task.task_id, status=task.status), message.message_id)
                elif message.type == "task.cancel":
                    await self.harness.cancel(connection.principal, payload.task_id)
                elif message.type == "approval.decide":
                    await self.harness.decide(connection.principal, payload.approval_id, payload.decision)
                elif message.type in {"control.pause", "control.resume", "control.cancel_all"}:
                    await self.harness.control(connection.principal, message.type.split(".")[1], payload.request_id)
                else:
                    raise WebSocketException(code=1008, reason="client direction rejected")
            except (ProtocolError, AccessDenied, Conflict, LookupError) as exc:
                if isinstance(exc, ProtocolError):
                    error = WireError(**exc.error.model_dump())
                else:
                    code = exc.code if isinstance(exc, AccessDenied) else "IDEMPOTENCY_CONFLICT" if isinstance(exc, Conflict) and str(exc) != "BUSY" else "BUSY" if isinstance(exc, Conflict) else "FORBIDDEN"
                    if code == "AUTH_REQUIRED":
                        raise WebSocketException(code=1008, reason="session expired") from None
                    error = WireError(code=ErrorCode(code), message="Operation rejected", retryable=code == "BUSY")
                connection.enqueue("error", error, message.message_id if message else None)
                if error.code == ErrorCode.BUSY:
                    raise WebSocketException(code=1013, reason="client rate or capacity exceeded")

    async def endpoint(self, websocket: WebSocket) -> None:
        if self.pending >= 10:
            raise WebSocketException(code=1013, reason="client capacity reached")
        try:
            self.handshakes.check()
        except ProtocolError:
            raise WebSocketException(code=1013, reason="connection rate exceeded") from None
        self.pending += 1
        tasks = []
        connection = None
        try:
            principal = await self.guards.browser_ws(websocket)
            if principal.identity_id != self.harness.link.config.owner_id:
                raise WebSocketException(code=1008, reason="owner rejected")
            node_text = websocket.query_params.get("node_id")
            node_id = UUID(node_text) if node_text else next(iter(self.harness.link.config.nodes)) if len(self.harness.link.config.nodes) == 1 else None
            if node_id is not None:
                self.harness.scope(node_id)
            await websocket.accept()
            async with asyncio.timeout(5):
                raw = await self.guards.receive_text(websocket, principal)
                if len(raw.encode("utf-8")) > self.harness.link.config.max_message_bytes:
                    raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Frame too large")
                first = parse_message(raw)
            if first.type != "client.sync":
                raise WebSocketException(code=1008, reason="client sync required")
            start, synced = await self.events.sync(principal.identity_id, first.payload.event_id)
            connection = BrowserConnection(websocket, principal, node_id, start)
            connection.enqueue("client.synced", synced, first.message_id)
            self.connections.append(connection)
            tasks = [asyncio.create_task(self.sender(connection)), asyncio.create_task(self.tail(connection)), asyncio.create_task(self.receiver(connection))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except SlowClient:
            await self.harness.link._close(websocket, 1013, "slow client; resynchronize")
        except (WebSocketException, AccessDenied, ProtocolError, ValueError, KeyError) as exc:
            await self.harness.link._close(websocket, getattr(exc, "code", 1008) if isinstance(exc, WebSocketException) else 1008, "protocol or authorization rejected")
        except sqlite3.Error:
            self.harness.failed = True
            self.harness.wake.set()
            await self.harness.link.close()
            await self.harness.link._close(websocket, 1011, "storage unavailable")
        except (WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
            await self.harness.link._close(websocket, 1001, "client unavailable")
        finally:
            self.pending -= 1
            if connection in self.connections:
                self.connections.remove(connection)
            for task in tasks:
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        await asyncio.gather(*(self.harness.link._close(c.websocket, 1001, "core stopping") for c in list(self.connections)))
