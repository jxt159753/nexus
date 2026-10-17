import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Request, WebSocket, WebSocketException
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.base import StrictContract
from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.messages import LinkLimits, NodeHello, NodeWelcome
from nexus.contracts.protocol import Message, parse_message
from nexus.core.config import CoreConfig
from nexus.core.security.http import SecurityHTTP
from nexus.core.security.types import AccessDenied, NodeScope, Principal
from nexus.core.storage.database import audit, utc_now
from nexus.link import MessageRate, session_matches, wire_message


@dataclass
class NodeConnection:
    websocket: WebSocket
    principal: Principal
    hello: NodeHello
    scope: NodeScope
    session_id: UUID = field(default_factory=uuid4)
    last_heartbeat: float = field(default_factory=time.monotonic)
    active_attempt_id: UUID | None = None
    outgoing: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(32))
    incoming: asyncio.Queue[Message] = field(default_factory=lambda: asyncio.Queue(32))


class NodeLink:
    def __init__(self, guards: SecurityHTTP, config: CoreConfig):
        self.guards, self.config = guards, config
        self.connections: dict[UUID, NodeConnection] = {}
        self._pending = 0
        self._handshakes = MessageRate()
        self._changed = asyncio.Lock()
        self.router = APIRouter()
        self.router.add_api_websocket_route("/ws/v1/node", self.endpoint)
        self.router.add_api_route("/api/v1/nodes", self.list_nodes, methods=["GET"])

    async def _audit(self, connection: NodeConnection, kind: str) -> None:
        async with self.guards.service.database.transaction() as conn:
            await audit(conn, str(connection.principal.identity_id), None, kind, utc_now())

    def _current(self, connection: NodeConnection) -> None:
        if self.connections.get(connection.principal.identity_id) is not connection:
            raise ProtocolError(ErrorCode.FORBIDDEN, "Connection was replaced")

    async def _scope(self, connection: NodeConnection) -> NodeScope:
        return await self.guards.service.connection_scope(connection.principal, self.config.owner_id, connection.scope)

    async def _register(self, ws: WebSocket, principal: Principal, hello: NodeHello) -> tuple[NodeConnection, NodeConnection | None]:
        policy = self.config.nodes.get(principal.identity_id)
        if hello.node_id != principal.identity_id or policy is None:
            raise AccessDenied()
        advertised = frozenset(f"{item.tool}@{item.tool_version}" for item in hello.tools)
        trusted = NodeScope(principal.identity_id, advertised, frozenset(hello.roots),
                            policy.tools, policy.roots, policy.resource_version)
        connection = NodeConnection(ws, principal, hello, trusted)
        await self._scope(connection)
        # Never make the connection visible if its durable audit cannot commit.
        await self._audit(connection, "node.connected")
        async with self._changed:
            old = self.connections.get(principal.identity_id)
            self.connections[principal.identity_id] = connection
        return connection, old

    @staticmethod
    async def _close(ws: WebSocket, code: int, reason: str) -> None:
        try:
            async with asyncio.timeout(2):
                await ws.close(code=code, reason=reason)
        except (RuntimeError, WebSocketDisconnect, TimeoutError, OSError):
            pass  # Closing a transport never changes any task outcome.

    async def _sender(self, connection: NodeConnection) -> None:
        while True:
            text = await connection.outgoing.get()
            self._current(connection)
            await self.guards.service.validate_principal(connection.principal)
            async with asyncio.timeout(2):
                await connection.websocket.send_text(text)

    async def send(self, node_id: UUID, kind: str, payload: StrictContract) -> None:
        """Trusted control transport only. P07 must implement durable dispatch."""
        if kind not in {"command.status", "result.ack"}:
            raise ProtocolError(ErrorCode.POLICY_DENIED, "Execution dispatch is not enabled")
        connection = self.connections.get(node_id)
        if connection is None:
            raise ProtocolError(ErrorCode.NODE_OFFLINE, "Node is offline")
        await self.guards.service.validate_principal(connection.principal)
        text = wire_message(kind, payload)
        message = parse_message(text)
        session_matches(message, connection.session_id)
        if len(text.encode()) > self.config.max_message_bytes:
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Negotiated frame limit exceeded")
        async with self._changed:
            self._current(connection)
            try:
                connection.outgoing.put_nowait(text)
            except asyncio.QueueFull:
                raise ProtocolError(ErrorCode.BUSY, "Node send queue full") from None

    async def list_nodes(self, request: Request) -> dict:
        await self.guards.owner_http(request)
        result = []
        for node_id in self.config.nodes:
            connection = self.connections.get(node_id)
            value = dict(node_id=str(node_id), online=False, tools=[], roots=[], session_id=None, boot_id=None)
            if connection is not None:
                try:
                    scope = await self._scope(connection)
                    self._current(connection)
                    value.update(online=time.monotonic() - connection.last_heartbeat < self.config.heartbeat_timeout_seconds,
                                 tools=sorted(scope.tools), roots=sorted(scope.roots),
                                 session_id=str(connection.session_id), boot_id=str(connection.hello.boot_id))
                except (AccessDenied, ProtocolError):
                    pass
            result.append(value)
        return {"nodes": result}

    async def _receive(self, connection: NodeConnection) -> None:
        rate = MessageRate()
        while True:
            remaining = self.config.heartbeat_timeout_seconds - (time.monotonic() - connection.last_heartbeat)
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                text = await self.guards.receive_text(connection.websocket, connection.principal)
            rate.check()
            if len(text.encode("utf-8")) > self.config.max_message_bytes:
                raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Negotiated frame limit exceeded")
            message = parse_message(text)
            session_matches(message, connection.session_id)
            async with self._changed:
                self._current(connection)
                if message.type == "node.heartbeat":
                    connection.last_heartbeat = time.monotonic()
                    connection.active_attempt_id = message.payload.active_attempt_id
                elif message.type in {"command.status_result", "error"}:
                    if message.type == "command.status_result" and message.payload.result is not None:
                        result = message.payload.result.result
                        if result is not None and len(result.model_dump_json().encode("utf-8")) > self.config.max_result_bytes:
                            raise ProtocolError(ErrorCode.RESULT_INVALID, "Negotiated result limit exceeded")
                    # Evidence stays bounded and unacknowledged until a future
                    # durable Harness consumer processes it. No task transitions.
                    try:
                        connection.incoming.put_nowait(message)
                    except asyncio.QueueFull:
                        raise ProtocolError(ErrorCode.BUSY, "Node receive queue full") from None
                else:
                    raise ProtocolError(ErrorCode.FORBIDDEN, "Message is not allowed in this direction")

    async def endpoint(self, websocket: WebSocket) -> None:
        if self._pending >= 32:
            raise WebSocketException(code=1013, reason="connection capacity reached")
        try:
            self._handshakes.check()
        except ProtocolError:
            raise WebSocketException(code=1013, reason="connection rate exceeded") from None
        self._pending += 1
        connection = None
        tasks = []
        try:
            principal = await self.guards.node_ws(websocket)
            await websocket.accept()
            async with asyncio.timeout(5):
                raw = await self.guards.receive_text(websocket, principal)
                if len(raw.encode("utf-8")) > self.config.max_message_bytes:
                    raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Negotiated frame limit exceeded")
                first = parse_message(raw)
            if first.type != "node.hello":
                raise ProtocolError(ErrorCode.FORBIDDEN, "Node hello required")
            connection, old = await self._register(websocket, principal, first.payload)
            if old is not None:
                await self._close(old.websocket, 1008, "connection replaced")
            welcome = NodeWelcome(session_id=connection.session_id,
                heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
                heartbeat_timeout_seconds=self.config.heartbeat_timeout_seconds,
                limits=LinkLimits(max_message_bytes=self.config.max_message_bytes,
                                  max_result_bytes=self.config.max_result_bytes,
                                  execution_timeout_seconds=self.config.execution_timeout_seconds,
                                  max_concurrent_attempts=1))
            connection.outgoing.put_nowait(wire_message("node.welcome", welcome, first.message_id))
            tasks = [asyncio.create_task(self._sender(connection)), asyncio.create_task(self._receive(connection))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketException:
            raise
        except (ProtocolError, AccessDenied, ValueError, KeyError):
            await self._close(websocket, 1008, "protocol or authorization rejected")
        except (TimeoutError, WebSocketDisconnect):
            await self._close(websocket, 1001, "node unavailable")
        except (OSError, RuntimeError, sqlite3.Error):
            await self._close(websocket, 1011, "node transport unavailable")
        finally:
            # Remove ownership before awaiting cleanup. ASGI disconnect can cancel
            # the endpoint repeatedly; shield the bounded child/audit cleanup.
            current = connection is not None and self.connections.get(connection.principal.identity_id) is connection
            if current:
                del self.connections[connection.principal.identity_id]
            self._pending -= 1
            for task in tasks:
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                if current:
                    await self._audit(connection, "node.disconnected")

    async def close(self) -> None:
        await asyncio.gather(*(self._close(c.websocket, 1001, "core stopping") for c in list(self.connections.values())))
