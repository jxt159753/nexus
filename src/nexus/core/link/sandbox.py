"""Authenticated v2 Sandbox Link. Core sends only digest-bound Tool releases."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket, WebSocketException
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.sandbox import (
    MAX_V2_BYTES,
    SandboxCancelled,
    SandboxCancel,
    SandboxExecute,
    SandboxHello,
    SandboxResult,
    SandboxWelcome,
    SandboxStatus,
    SandboxResultAck,
    encode_sandbox_message,
    parse_sandbox_message,
)
from nexus.core.config import CoreConfig
from nexus.core.security.http import SecurityHTTP
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import audit, utc_now


@dataclass
class SandboxConnection:
    websocket: WebSocket
    principal: Principal
    hello: SandboxHello
    session_id: UUID = field(default_factory=uuid4)
    last_heartbeat: float = field(default_factory=time.monotonic)
    pending: dict[UUID, asyncio.Future] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    blocked: bool = False
    queries: dict[UUID, asyncio.Future] = field(default_factory=dict)


class SandboxLink:
    def __init__(self, guards: SecurityHTTP, config: CoreConfig, execution_lock=None):
        self.guards, self.config = guards, config
        self.execution_lock = execution_lock or asyncio.Lock()
        self.connections: dict[UUID, SandboxConnection] = {}
        self.result_handler = None
        self.reconcile_handler = None
        self.router = APIRouter()
        self.router.add_api_websocket_route("/ws/v2/sandbox", self.endpoint)

    async def _audit(self, principal: Principal, kind: str) -> None:
        async with self.guards.service.database.transaction() as conn:
            await audit(conn, str(principal.identity_id), None, kind, utc_now())

    def status(self, node_id: UUID) -> dict:
        policy = self.config.sandbox_nodes.get(node_id)
        if policy is None:
            return {"available": False, "reason": "sandbox_not_registered", "image_digest": None}
        connection = self.connections.get(node_id)
        if connection is None:
            return {"available": False, "reason": "sandbox_offline", "image_digest": policy.image_digest}
        hello = connection.hello
        if connection.blocked:
            return {"available": False, "reason": "sandbox_execution_unknown", "image_digest": hello.image_digest}
        online = time.monotonic() - connection.last_heartbeat < self.config.heartbeat_timeout_seconds
        if not online:
            return {"available": False, "reason": "sandbox_offline", "image_digest": policy.image_digest}
        if not hello.available:
            return {"available": False, "reason": hello.reason or "sandbox_unavailable",
                    "image_digest": hello.image_digest, "runtime_version": hello.runtime_version}
        if hello.image_digest != policy.image_digest:
            return {"available": False, "reason": "sandbox_image_mismatch", "image_digest": hello.image_digest}
        return {"available": True, "reason": None, "image_digest": hello.image_digest,
                "runtime_version": hello.runtime_version, "boot_id": str(hello.boot_id)}

    async def execute(self, node_id: UUID, version, input_value: dict, timeout_seconds: int, *,
                      execution_id: UUID | None = None, cancel_event: asyncio.Event | None = None, permitted=None) -> dict:
        status = self.status(node_id)
        if not status["available"]:
            raise ProtocolError(ErrorCode.NODE_OFFLINE, "Sandbox is not available")
        connection = self.connections.get(node_id)
        environment = json.loads(version["environment_json"])
        if environment.get("image_digest") != status["image_digest"]:
            raise ProtocolError(ErrorCode.POLICY_DENIED, "Sandbox image binding changed")
        execution_id = execution_id or uuid4()
        if connection.pending:
            return {"outcome": "failed", "error_code": "SANDBOX_BUSY", "stopped": True}
        future = asyncio.get_running_loop().create_future()
        connection.pending[execution_id] = future
        payload = SandboxExecute(
            session_id=connection.session_id, execution_id=execution_id,
            version_id=UUID(version["version_id"]), artifact_digest=version["artifact_digest"],
            environment_digest=version["environment_digest"], release_json=version["release_json"],
            input_json=json.dumps(input_value, ensure_ascii=False, separators=(",", ":")),
            timeout_seconds=max(1, min(10, timeout_seconds)),
        )
        cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            async with self.execution_lock:
                if (self.connections.get(node_id) is not connection or connection.blocked
                        or cancel_event is not None and cancel_event.is_set() or permitted is not None and not await permitted()):
                    return {"outcome": "failed", "error_code": "SANDBOX_DISPATCH_DENIED", "stopped": True}
                await self._send(connection, "sandbox.execute", payload)
            done, _ = await asyncio.wait([future] + ([cancellation] if cancellation else []),
                                          timeout=timeout_seconds + 2, return_when=asyncio.FIRST_COMPLETED)
            if future not in done:
                await self._send(connection, "sandbox.cancel", SandboxCancel(session_id=connection.session_id, execution_id=execution_id))
                try:
                    reply = await asyncio.wait_for(asyncio.shield(future), 12)
                except TimeoutError:
                    connection.blocked = True
                    return {"outcome": "unknown", "error_code": "SANDBOX_STOP_UNKNOWN", "stopped": False}
            else:
                reply = future.result()
        except asyncio.CancelledError:
            connection.blocked = True
            try:
                await self._send(connection, "sandbox.cancel", SandboxCancel(session_id=connection.session_id, execution_id=execution_id))
            finally:
                raise
        except (ProtocolError, OSError, RuntimeError):
            connection.blocked = True
            return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_UNKNOWN"}
        finally:
            connection.pending.pop(execution_id, None)
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
            if not future.done():
                future.cancel()
        if reply.outcome == "unknown" or not reply.stopped:
            connection.blocked = True
            return {"outcome": "unknown", "error_code": "SANDBOX_STOP_UNKNOWN", "stopped": False}
        if reply.outcome == "succeeded":
            if reply.artifact_digest != version["artifact_digest"]:
                connection.blocked = True
                return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_MISMATCH"}
            try:
                value = json.loads(reply.output_json)
                return {"outcome": "succeeded", "output": value}
            except (ValueError, TypeError):
                connection.blocked = True
                return {"outcome": "unknown", "error_code": "SANDBOX_OUTPUT_INVALID"}
        return {"outcome": reply.outcome, "error_code": reply.error_code, "stopped": reply.stopped}

    async def _send(self, connection: SandboxConnection, kind: str, payload) -> None:
        text = encode_sandbox_message(kind, payload)
        if len(text.encode("utf-8")) > MAX_V2_BYTES:
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "v2 frame exceeds negotiated limit")
        async with connection.send_lock:
            async with asyncio.timeout(2):
                await connection.websocket.send_text(text)

    async def query(self, node_id: UUID, execution_id: UUID):
        connection = self.connections.get(node_id)
        if connection is None:
            return None
        if execution_id in connection.queries or len(connection.queries) >= 10:
            return None
        future = asyncio.get_running_loop().create_future()
        connection.queries[execution_id] = future
        try:
            await self._send(connection, "sandbox.status", SandboxStatus(session_id=connection.session_id, execution_id=execution_id))
            return await asyncio.wait_for(future, 3)
        except (TimeoutError, ProtocolError, OSError, RuntimeError):
            return None
        finally:
            connection.queries.pop(execution_id, None)

    async def acknowledge(self, node_id: UUID, execution_id: UUID, artifact_digest: str):
        connection = self.connections.get(node_id)
        if connection is not None:
            await self._send(connection, "sandbox.result_ack", SandboxResultAck(
                session_id=connection.session_id, execution_id=execution_id, artifact_digest=artifact_digest))

    async def _receive(self, connection: SandboxConnection) -> None:
        while True:
            remaining = self.config.heartbeat_timeout_seconds - (time.monotonic() - connection.last_heartbeat)
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                raw = await self.guards.receive_text(connection.websocket, connection.principal)
            if len(raw.encode("utf-8")) > MAX_V2_BYTES:
                raise ProtocolError(ErrorCode.INVALID_MESSAGE, "v2 frame exceeds negotiated limit")
            message = parse_sandbox_message(raw)
            if self.connections.get(connection.principal.identity_id) is not connection:
                raise ProtocolError(ErrorCode.FORBIDDEN, "replaced Sandbox connection")
            if message.payload.session_id != connection.session_id:
                raise ProtocolError(ErrorCode.FORBIDDEN, "v2 session mismatch")
            if message.type == "sandbox.heartbeat":
                connection.last_heartbeat = time.monotonic()
            elif message.type == "sandbox.result":
                if self.result_handler is not None:
                    await self.result_handler(connection.principal.identity_id, message.payload)
                future = connection.pending.get(message.payload.execution_id)
                if future is not None and not future.done():
                    future.set_result(message.payload)
            elif message.type == "sandbox.status_result":
                future = connection.queries.get(message.payload.execution_id)
                if future is not None and not future.done():
                    future.set_result(message.payload)
            elif message.type == "sandbox.cancelled":
                future = connection.pending.get(message.payload.execution_id)
                if future is not None and not future.done() and message.payload.stopped:
                    future.set_result(SandboxResult(
                        session_id=connection.session_id, execution_id=message.payload.execution_id,
                        artifact_digest=connection.hello.image_digest, outcome="failed",
                        error_code="SANDBOX_CANCELLED", stopped=message.payload.stopped))
            else:
                raise ProtocolError(ErrorCode.FORBIDDEN, "v2 message is not valid in this direction")

    async def endpoint(self, websocket: WebSocket) -> None:
        connection = None
        tasks = []
        try:
            principal = await self.guards.node_ws(websocket)
            await websocket.accept()
            async with asyncio.timeout(5):
                raw = await self.guards.receive_text(websocket, principal)
            first = parse_sandbox_message(raw)
            if first.type != "sandbox.hello":
                raise ProtocolError(ErrorCode.FORBIDDEN, "v2 Sandbox hello required")
            hello = first.payload
            policy = self.config.sandbox_nodes.get(principal.identity_id)
            if hello.node_id != principal.identity_id or policy is None:
                raise AccessDenied()
            if hello.runtime != policy.runtime:
                raise ProtocolError(ErrorCode.POLICY_DENIED, "Sandbox runtime is not registered")
            if hello.platform != policy.platform or hello.runsc_digest != policy.runsc_digest:
                raise ProtocolError(ErrorCode.POLICY_DENIED, "Sandbox runtime binding changed")
            if hello.image_digest != policy.image_digest:
                raise ProtocolError(ErrorCode.POLICY_DENIED, "Sandbox image digest is not registered")
            if not set(hello.verification_profiles) & set(policy.verification_profiles):
                raise ProtocolError(ErrorCode.POLICY_DENIED, "No registered verification profile")
            connection = SandboxConnection(websocket, principal, hello)
            await self._audit(principal, "sandbox.connected")
            old = self.connections.get(principal.identity_id)
            if old is not None:
                await _close(old.websocket, 1001, "sandbox connection replaced")
            self.connections[principal.identity_id] = connection
            await self._send(connection, "sandbox.welcome", SandboxWelcome(
                session_id=connection.session_id,
                heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
                heartbeat_timeout_seconds=self.config.heartbeat_timeout_seconds,
                max_message_bytes=MAX_V2_BYTES,
                max_result_bytes=self.config.max_result_bytes,
                execution_timeout_seconds=self.config.execution_timeout_seconds,
            ))
            tasks = [asyncio.create_task(self._receive(connection))]
            if self.reconcile_handler is not None:
                tasks.append(asyncio.create_task(self.reconcile_handler(principal.identity_id)))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketException:
            raise
        except (ProtocolError, AccessDenied, ValueError, KeyError):
            await _close(websocket, 1008, "sandbox protocol or authorization rejected")
        except (TimeoutError, WebSocketDisconnect):
            await _close(websocket, 1001, "sandbox unavailable")
        except (OSError, RuntimeError):
            await _close(websocket, 1011, "sandbox transport unavailable")
        finally:
            current = connection is not None and self.connections.get(connection.principal.identity_id) is connection
            if current:
                del self.connections[connection.principal.identity_id]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if connection is not None:
                for future in list(connection.pending.values()):
                    if not future.done():
                        future.set_exception(ProtocolError(ErrorCode.NODE_OFFLINE, "Sandbox disconnected"))
                for future in list(connection.queries.values()):
                    if not future.done():
                        future.set_result(None)
                if current:
                    await self._audit(connection.principal, "sandbox.disconnected")

    async def close(self) -> None:
        await asyncio.gather(*(_close(c.websocket, 1001, "core stopping") for c in list(self.connections.values())))


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    try:
        async with asyncio.timeout(2):
            await ws.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect, TimeoutError, OSError):
        pass
