"""Authenticated outbound connection, heartbeat and bounded reconnect."""

import asyncio
import logging
import random
import ssl
import sqlite3
from dataclasses import dataclass
from uuid import UUID

from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.messages import CommandCancelled, CommandReceipt, NodeHeartbeat, NodeHello, ToolCapability, WireError
from nexus.contracts.protocol import parse_message
from nexus.link import MessageRate, session_matches, wire_message
from nexus.node.runtime import NodeRuntime
from nexus.node.service_config import NodeServiceConfig

LOGGER = logging.getLogger("nexus.node.websocket")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False  # Handshake debug logs must never expose Bearer headers.


@dataclass(frozen=True)
class LinkState:
    phase: str
    session_id: UUID | None = None
    reconnect_count: int = 0


class NodeClient:
    def __init__(self, config: NodeServiceConfig, runtime: NodeRuntime, token: SecretStr):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token.get_secret_value()):
            raise ValueError("valid node credential required")
        self.config = NodeServiceConfig.model_validate_json(config.model_dump_json())
        if config.node_id != runtime.config.node_id:
            raise ValueError("runtime identity differs")
        self.runtime, self._token = runtime, token
        if runtime.plugins:
            from nexus.plugins.transfer import uploader
            runtime.plugins.uploader=uploader(config,token)
        self.state = LinkState("offline")
        self._active_attempt_id: UUID | None = None

    async def _send(self, ws, kind, payload, *, correlation=None, limit=65536) -> None:
        text = wire_message(kind, payload, correlation)
        if len(text.encode("utf-8")) > limit:
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Negotiated frame limit exceeded")
        async with asyncio.timeout(2):
            await ws.send(text)

    async def _connected(self, ws) -> None:
        hello = NodeHello(node_id=self.config.node_id, node_version="0.1.0", boot_id=self.runtime.boot_id,plugin_digest=self.runtime.config.plugin_digest(),plugin_tools=self.runtime.config.plugins.features(self.runtime.config.interaction_targets),
            tools=[ToolCapability(tool=key.split("@")[0], tool_version=1) for key in sorted(self.runtime.config.allowed_tools)],
            roots=sorted(self.runtime.config.roots), interaction_digests={key:value.digest() for key,value in self.runtime.config.interaction_targets.items()}, command_digests={key: value.digest() for key,value in self.runtime.config.commands.items()})
        # Only actual local configuration is advertised; claims never grant rights.
        await self._send(ws, "node.hello", hello)
        async with asyncio.timeout(5):
            message = parse_message(await ws.recv())
        if message.type != "node.welcome":
            raise ProtocolError(ErrorCode.FORBIDDEN, "Core welcome required")
        self.runtime.use_core_clock(message.sent_at)
        welcome = message.payload
        self.state = LinkState("online", welcome.session_id, self.state.reconnect_count)
        rate = MessageRate()
        execution = None

        async def send_status(status, correlation):
            if status.result is not None:
                if status.result.result is not None and len(status.result.result.model_dump_json().encode("utf-8")) > welcome.limits.max_result_bytes:
                    raise ProtocolError(ErrorCode.RESULT_INVALID, "Negotiated result limit exceeded")
                await self._send(ws, "command.result", status.result, correlation=correlation, limit=welcome.limits.max_message_bytes)
            else:
                await self._send(ws, "command.status_result", status, correlation=correlation, limit=welcome.limits.max_message_bytes)

        async def execute(command, correlation):
            phases = asyncio.Queue(2)
            loop = asyncio.get_running_loop()
            self._active_attempt_id = command.attempt_id
            worker = asyncio.create_task(asyncio.to_thread(self.runtime.execute, command,
                lambda phase: loop.call_soon_threadsafe(phases.put_nowait, phase)))
            waiting = None
            try:
                while not worker.done() or not phases.empty():
                    waiting = asyncio.create_task(phases.get())
                    done, _ = await asyncio.wait([worker, waiting], return_when=asyncio.FIRST_COMPLETED)
                    if waiting in done:
                        receipt = CommandReceipt(session_id=welcome.session_id, task_id=command.task_id,
                            attempt_id=command.attempt_id, command_digest=command.command_digest, boot_id=self.runtime.boot_id)
                        await self._send(ws, waiting.result(), receipt, correlation=correlation, limit=welcome.limits.max_message_bytes)
                    else:
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                status = worker.result()
                self._active_attempt_id = None
                await send_status(status, correlation)
            except ProtocolError as exc:
                await self._send(ws, "error", WireError(**exc.error.model_dump(), session_id=welcome.session_id),
                                 correlation=correlation, limit=welcome.limits.max_message_bytes)
            except (OSError, sqlite3.Error, ValueError):
                await ws.close(code=1011, reason="node execution unavailable")
            finally:
                if waiting is not None:
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                # Disconnecting the transport must not abandon the managed worker.
                try:
                    await asyncio.shield(asyncio.gather(worker, return_exceptions=True))
                finally:
                    self._active_attempt_id = None

        async def heartbeat():
            while True:
                await self._send(ws, "node.heartbeat", NodeHeartbeat(session_id=welcome.session_id, active_attempt_id=self._active_attempt_id),
                                 limit=welcome.limits.max_message_bytes)
                # A protocol ping proves this connection is still responsive;
                # application heartbeat remains the Core's online authority.
                waiter = await ws.ping()
                async with asyncio.timeout(welcome.heartbeat_timeout_seconds):
                    await waiter
                await asyncio.sleep(welcome.heartbeat_interval_seconds)

        async def receive():
            nonlocal execution
            while True:
                text = await ws.recv()
                rate.check()
                raw_size = len(text.encode("utf-8")) if isinstance(text, str) else len(text)
                if raw_size > welcome.limits.max_message_bytes:
                    raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Negotiated frame limit exceeded")
                incoming = parse_message(text)
                session_matches(incoming, welcome.session_id)
                if incoming.type == "command.status":
                    result = await asyncio.to_thread(self.runtime.status, incoming.payload.attempt_id, welcome.session_id)
                    if result.result is not None and result.result.result is not None:
                        if len(result.result.result.model_dump_json().encode("utf-8")) > welcome.limits.max_result_bytes:
                            raise ProtocolError(ErrorCode.RESULT_INVALID, "Negotiated result limit exceeded")
                    await self._send(ws, "command.status_result", result, correlation=incoming.message_id,
                                     limit=welcome.limits.max_message_bytes)
                elif incoming.type == "result.ack":
                    await asyncio.to_thread(self.runtime.receipts.acknowledge, incoming.payload.attempt_id)
                elif incoming.type == "command.cancel":
                    await asyncio.to_thread(self.runtime.cancel, incoming.payload.attempt_id)
                    status = await asyncio.to_thread(self.runtime.status, incoming.payload.attempt_id, welcome.session_id)
                    if status.phase == "not_found":
                        await self._send(ws, "command.cancelled", CommandCancelled(session_id=welcome.session_id,
                            attempt_id=incoming.payload.attempt_id, stopped_phase="received"), limit=welcome.limits.max_message_bytes)
                    else:
                        await send_status(status, incoming.message_id)
                elif incoming.type == "command.execute":
                    if execution is not None and not execution.done() and self._active_attempt_id is not None:
                        old = await asyncio.to_thread(self.runtime.status, incoming.payload.attempt_id, welcome.session_id)
                        if self._active_attempt_id == incoming.payload.attempt_id and old.phase != "not_found":
                            try:
                                # Runtime checks the complete immutable identity, then
                                # returns the durable receipt without a second worker.
                                status = await asyncio.to_thread(self.runtime.execute, incoming.payload)
                                await send_status(status, incoming.message_id)
                            except ProtocolError as exc:
                                await self._send(ws, "error", WireError(**exc.error.model_dump(), session_id=welcome.session_id),
                                                 correlation=incoming.message_id, limit=welcome.limits.max_message_bytes)
                        else:
                            error = WireError(code=ErrorCode.BUSY, message="Node execution slot occupied", retryable=False, session_id=welcome.session_id)
                            await self._send(ws, "error", error, correlation=incoming.message_id, limit=welcome.limits.max_message_bytes)
                    else:
                        if execution is not None:
                            await execution  # finish prior result send before starting the next worker
                        self._active_attempt_id = incoming.payload.attempt_id
                        execution = asyncio.create_task(execute(incoming.payload, incoming.message_id))
                else:
                    raise ProtocolError(ErrorCode.FORBIDDEN, "Message is not allowed in this direction")

        async def replay():
            cursor = ""
            counts: dict[UUID, int] = {}
            while True:
                await asyncio.sleep(5)
                receipts = await asyncio.to_thread(self.runtime.receipts.unacknowledged, after=cursor, limit=4)
                for receipt in receipts:
                    if counts.get(receipt.attempt_id, 0) < 3:
                        await send_status(receipt.status(welcome.session_id), None)
                        counts[receipt.attempt_id] = counts.get(receipt.attempt_id, 0) + 1
                cursor = str(receipts[-1].attempt_id) if len(receipts) == 4 else ""

        tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(receive()), asyncio.create_task(replay())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if execution is not None:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

    async def run(self, stop: asyncio.Event) -> None:
        context = ssl.create_default_context(cafile=None if self.config.ca_file is None else str(self.config.ca_file)) if self.config.core_url.startswith("wss:") else None
        delay = 1.0
        reconnects = 0
        while not stop.is_set():
            self.state = LinkState("connecting", reconnect_count=reconnects)
            try:
                # This endpoint is configured explicitly; do not follow redirects.
                connector = connect(self.config.core_url,
                    additional_headers={"Authorization": "Bearer " + self._token.get_secret_value()},
                    ssl=context, proxy=None, compression=None, ping_interval=None,
                    open_timeout=5, close_timeout=2, max_size=65536, max_queue=16,
                    write_limit=32768, logger=LOGGER)
                connector.process_redirect = lambda exc: exc
                async with connector as ws:
                    connected = asyncio.create_task(self._connected(ws))
                    stopping = asyncio.create_task(stop.wait())
                    try:
                        done, _ = await asyncio.wait([connected, stopping], return_when=asyncio.FIRST_COMPLETED)
                        if connected in done:
                            connected.result()
                    finally:
                        connected.cancel()
                        stopping.cancel()
                        await asyncio.gather(connected, stopping, return_exceptions=True)
                    delay = 1.0
            except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake, ProtocolError, ValueError):
                pass  # Only bounded status is exposed, never rejected input or credentials.
            finally:
                if self.state.phase == "online":
                    delay = 1.0
                reconnects = min(reconnects + 1, 2**31 - 1)
                self.state = LinkState("offline", reconnect_count=reconnects)
            if not stop.is_set():
                try:
                    async with asyncio.timeout(random.uniform(delay, min(30.0, delay * 1.25))):
                        await stop.wait()
                except TimeoutError:
                    pass
                delay = min(30.0, delay * 2)
