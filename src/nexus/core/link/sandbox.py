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
    SandboxCancelled,
    SandboxCancel,
    SandboxExecute,
    SandboxHello,
    SandboxResult,
    SandboxWelcome,
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


class SandboxLink:
    def __init__(self, guards: SecurityHTTP, config: CoreConfig):
        self.guards, self.config = guards, config
        self.connections: dict[UUID, SandboxConnection] = {}
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

    async def execute(self, node_id: UUID, version, input_value: dict, timeout_seconds: int) -> dict:
        status = self.status(node_id)
        if not status["available"]:
            raise ProtocolError(ErrorCode.NODE_OFFLINE, "Sandbox is not available")
        connection = self.connections.get(node_id)
        environment = json.loads(version["environment_json"])
        if environment.get("image_digest") != status["image_digest"]:
            raise ProtocolError(ErrorCode.POLICY_DENIED, "Sandbox image binding changed")
        execution_id = uuid4()
        future = asyncio.get_running_loop().create_future()
        connection.pending[execution_id] = future
        payload = SandboxExecute(
            session_id=connection.session_id, execution_id=execution_id,
            version_id=UUID(version["version_id"]), artifact_digest=version["artifact_digest"],
            environment_digest=version["environment_digest"], release_json=version["release_json"],
            input_json=json.dumps(input_value, ensure_ascii=False, separators=(",", ":")),
            timeout_seconds=max(1, min(10, timeout_seconds)),
        )
        try:
            await self._send(connection, "sandbox.execute", payload)
            reply = await asyncio.wait_for(future, timeout=timeout_seconds + 2)
        except TimeoutError:
            await self._send(connection, "sandbox.cancel",
                             SandboxCancel(session_id=connection.session_id, execution_id=execution_id))
            return {"outcome": "unknown", "error_code": "SANDBOX_TIMEOUT"}
        except (ProtocolError, OSError, RuntimeError):
            return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_UNKNOWN"}
        finally:
            connection.pending.pop(execution_id, None)
        if reply.outcome == "succeeded":
            if reply.artifact_digest != version["artifact_digest"]:
                return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_MISMATCH"}
            try:
                value = json.loads(reply.output_json)
                return {"outcome": "succeeded", "output": value}
            except (ValueError, TypeError):
                return {"outcome": "unknown", "error_code": "SANDBOX_OUTPUT_INVALID"}
        return {"outcome": reply.outcome, "error_code": reply.error_code, "stopped": reply.stopped}

    async def _send(self, connection: SandboxConnection, kind: str, payload) -> None:
        text = encode_sandbox_message(kind, payload)
        if len(text.encode("utf-8")) > self.config.max_message_bytes:
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "v2 frame exceeds negotiated limit")
        async with connection.send_lock:
            async with asyncio.timeout(2):
                await connection.websocket.send_text(text)

    async def _receive(self, connection: SandboxConnection) -> None:
        while True:
            remaining = self.config.heartbeat_timeout_seconds - (time.monotonic() - connection.last_heartbeat)
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                raw = await self.guards.receive_text(connection.websocket, connection.principal)
            if len(raw.encode("utf-8")) > self.config.max_message_bytes:
                raise ProtocolError(ErrorCode.INVALID_MESSAGE, "v2 frame exceeds negotiated limit")
            message = parse_sandbox_message(raw)
            if message.payload.session_id != connection.session_id:
                raise ProtocolError(ErrorCode.FORBIDDEN, "v2 session mismatch")
            if message.type == "sandbox.heartbeat":
                connection.last_heartbeat = time.monotonic()
            elif message.type == "sandbox.result":
                future = connection.pending.get(message.payload.execution_id)
                if future is not None and not future.done():
                    future.set_result(message.payload)
            elif message.type == "sandbox.cancelled":
                future = connection.pending.get(message.payload.execution_id)
                if future is not None and not future.done():
                    future.set_result(SandboxResult(
                        session_id=connection.session_id, execution_id=message.payload.execution_id,
                        artifact_digest=connection.hello.image_digest, outcome="unknown",
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
            if hello.image_digest != policy.image_digest:
                raise ProtocolError(ErrorCode.POLICY_DENIED, "Sandbox image digest is not registered")
            if not set(hello.verification_profiles) & set(policy.verification_profiles):
                raise ProtocolError(ErrorCode.POLICY_DENIED, "No registered verification profile")
            connection = SandboxConnection(websocket, principal, hello)
            await self._audit(principal, "sandbox.connected")
            self.connections[principal.identity_id] = connection
            await self._send(connection, "sandbox.welcome", SandboxWelcome(
                session_id=connection.session_id,
                heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
                heartbeat_timeout_seconds=self.config.heartbeat_timeout_seconds,
                max_message_bytes=self.config.max_message_bytes,
                max_result_bytes=self.config.max_result_bytes,
                execution_timeout_seconds=self.config.execution_timeout_seconds,
            ))
            tasks = [asyncio.create_task(self._receive(connection))]
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
