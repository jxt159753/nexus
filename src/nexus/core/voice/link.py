"""Authenticated voice control and binary audio endpoints for the Core media proxy."""

import asyncio
import logging
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, WebSocket, WebSocketException
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.voice import (
    VoiceActivate, VoiceBargeIn, VoiceError, VoiceFinal, VoiceHeartbeat, VoiceHello,
    VoiceMessage, VoiceSessionStarted, VoiceSpeakCompleted, VoiceSpeakDone, VoiceStop,
    VoiceStopRequest, VoiceStopped, VoiceWelcome, encode_voice_message, parse_voice_message,
)
from nexus.core.config import CoreConfig
from nexus.core.security.http import SecurityHTTP
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.tasks import Conflict
from nexus.core.voice.service import VoiceService, VoiceSession

LOGGER = logging.getLogger("nexus.core.voice")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class VoiceControl:
    def __init__(self, ws: WebSocket, principal: Principal | None, config: CoreConfig, service: VoiceService):
        self.ws, self.principal, self.config, self.service = ws, principal, config, service
        self.outgoing: asyncio.Queue[str] = asyncio.Queue(32)
        self.session: VoiceSession | None = None
        self.last_heartbeat = asyncio.get_running_loop().time()
        self.closed = False
        self.audio_framing = "pcm16"

    async def send(self, message: VoiceMessage) -> None:
        if self.closed:
            return
        try:
            self.outgoing.put_nowait(encode_voice_message(message))
        except asyncio.QueueFull:
            raise ProtocolError(ErrorCode.BUSY, "Voice control queue full") from None

    async def _sender(self) -> None:
        while True:
            text = await self.outgoing.get()
            try:
                await self.guard.check_ws_message(self.ws, self.principal)
                async with asyncio.timeout(2):
                    await self.ws.send_text(text)
            finally:
                self.outgoing.task_done()

    async def run(self, guard: SecurityHTTP) -> None:
        self.guard = guard
        tasks = []
        try:
            principal = await guard.node_ws(self.ws)
            self.principal = principal
            await self.ws.accept()
            async with asyncio.timeout(5):
                raw = await guard.receive_text(self.ws, principal)
            first = parse_voice_message(raw)
            if not isinstance(first, VoiceHello):
                raise ProtocolError(ErrorCode.FORBIDDEN, "voice.hello required")
            if first.node_id != principal.identity_id:
                raise AccessDenied("POLICY_DENIED")
            policy = self.config.nodes.get(principal.identity_id)
            if policy is None or not policy.voice.enabled:
                raise AccessDenied("VOICE_DISABLED")
            missing = {"capture", "playback"} - set(first.features)
            if missing:
                raise AccessDenied("VOICE_CAPABILITY_MISSING")
            if self.config.voice.provider == "bailian" and first.audio_framing != "nxv2":
                raise AccessDenied("VOICE_CAPABILITY_MISSING")
            self.audio_framing = first.audio_framing
            welcome = VoiceWelcome(
                type="voice.welcome",
                message_id=uuid4(), sent_at=self.service.now(),
                connection_id=uuid4(),
                heartbeat_interval_seconds=self.config.voice.heartbeat_interval_seconds,
                heartbeat_timeout_seconds=self.config.voice.heartbeat_timeout_seconds,
                policy_revision=1,
                max_session_seconds=min(self.config.voice.max_session_seconds, policy.voice.max_session_seconds),
                max_final_chars=self.config.voice.max_final_chars,
            )
            await self.send(welcome)
            if first.activation_request_id is not None:
                session = next((s for s in self.service.sessions.values()
                                if s.request_id == first.activation_request_id and s.node_id == principal.identity_id), None)
                if session is None:
                    raise AccessDenied("MODEL_CONSENT_REQUIRED")
                session = await self.service.open_session(principal, first.activation_request_id, session.trigger)
                session.framed_audio = first.audio_framing == "nxv2"
                self.session = session
                await self.service.attach_control(session, self)
                await self._started(session)
            tasks = [asyncio.create_task(self._sender()), asyncio.create_task(self._receive())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketException:
            raise
        except (ProtocolError, AccessDenied, Conflict, ValueError, KeyError) as exc:
            try:
                await self._error(exc)
            finally:
                await self._close(1008, "voice authorization rejected")
        except (TimeoutError, WebSocketDisconnect):
            await self._close(1001, "voice unavailable")
        except (OSError, RuntimeError):
            await self._close(1011, "voice transport unavailable")
        finally:
            self.closed = True
            for task in tasks:
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.session is not None and self.session.active:
                    try:
                        await self.service.stop(self.session, "disconnect")
                    except Exception:
                        pass

    async def _receive(self) -> None:
        while True:
            remaining = self.config.voice.heartbeat_timeout_seconds - (
                asyncio.get_running_loop().time() - self.last_heartbeat
            )
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                raw = await self.guard.receive_text(self.ws, self.principal)
            message = parse_voice_message(raw)
            if isinstance(message, VoiceHeartbeat):
                self.last_heartbeat = asyncio.get_running_loop().time()
                continue
            if isinstance(message, VoiceActivate):
                await self._activate(message)
                continue
            if isinstance(message, VoiceStop):
                if self.session is not None:
                    await self._require(message)
                    await self.service.stop(self.session, message.reason)
                    async with asyncio.timeout(2):
                        await self.outgoing.join()
                return
            if isinstance(message, VoiceStopRequest):
                if self.session is not None:
                    await self._require(message)
                    self.session.media_stopped.set()
                    await self.service.stop(self.session, message.reason)
                    async with asyncio.timeout(2):
                        await self.outgoing.join()
                return
            if isinstance(message, VoiceStopped):
                if self.session is not None:
                    if message.voice_session_id != self.session.voice_session_id:
                        raise AccessDenied("SESSION_EXPIRED")
                    self.service.require_session(message.voice_session_id, message.session_epoch,
                                                 self.principal.identity_id, active=False)
                    if message.execution_stopped and message.capture_stopped_at and message.playback_stopped_at:
                        self.session.media_stopped.set()
                continue
            if isinstance(message, VoiceBargeIn):
                if self.session is not None:
                    await self._require(message)
                    if (message.generation == self.session.generation
                            and message.utterance_id == self.session.assistant_utterance_id):
                        await self.service.barge_in(self.session, message.segment_index)
                continue
            if isinstance(message, VoiceFinal):
                if self.config.voice.provider != "fake":
                    raise ProtocolError(ErrorCode.FORBIDDEN, "Final text must come from ASR")
                if self.session is not None:
                    await self._require(message)
                    await self.service.accept_final(self.session, message.text)
                continue
            if isinstance(message, VoiceSpeakCompleted):
                if self.session is not None:
                    await self._require(message)
                    await self.service.playback_ack(self.session, message)
                continue
            if isinstance(message, VoiceSpeakDone):
                continue
            if isinstance(message, VoiceError):
                if self.session is not None:
                    await self.service.stop(self.session, "error")
                return
            raise ProtocolError(ErrorCode.FORBIDDEN, "Message is not allowed in this direction")

    async def _activate(self, message: VoiceActivate) -> None:
        if self.session is not None and self.session.active and self.session.state != "activating":
            raise ProtocolError(ErrorCode.BUSY, "Voice session already active")
        if self.session is not None and self.session.active:
            session = self.session
            if message.request_id != session.request_id or message.trigger != session.trigger:
                raise AccessDenied("POLICY_DENIED")
        else:
            session = await self.service.open_session(self.principal, message.request_id, message.trigger, message.device_id)
        self.session = session
        session.framed_audio = self.audio_framing == "nxv2"
        if message.conversation_id and message.conversation_id != session.conversation_id:
            raise AccessDenied("POLICY_DENIED")
        await self.service.attach_control(session, self)
        await self._started(session)
        await self.service.begin_listening(session)

    async def _started(self, session):
        conversation_id = await self.service.ensure_conversation(session)
        await self.send(VoiceSessionStarted(
            type="voice.session.started",
            message_id=uuid4(), sent_at=self.service.now(),
            voice_session_id=session.voice_session_id, session_epoch=session.session_epoch,
            conversation_id=conversation_id,
            mode="wake" if session.trigger == "wake" else "push_to_talk",
            expires_at=session.expires_at,
        ))

    async def _require(self, message) -> None:
        if self.session is None:
            raise AccessDenied("SESSION_EXPIRED")
        self.service.require_session(
            message.voice_session_id, message.session_epoch, self.principal.identity_id
        )
        if self.session.voice_session_id != message.voice_session_id:
            raise AccessDenied("SESSION_EXPIRED")

    async def _error(self, exc: Exception) -> None:
        if isinstance(exc, AccessDenied):
            try:
                code = ErrorCode(exc.code)
            except ValueError:
                code = ErrorCode.POLICY_DENIED
        else:
            code = ErrorCode.INVALID_MESSAGE
        try:
            await self.send(VoiceError(
                type="voice.error",
                message_id=uuid4(), sent_at=self.service.now(),
                code=code, message="Voice operation rejected", retryable=False,
            ))
        except Exception:
            pass

    async def _close(self, code: int, reason: str) -> None:
        try:
            async with asyncio.timeout(2):
                await self.ws.close(code=code, reason=reason)
        except (RuntimeError, WebSocketDisconnect, TimeoutError, OSError):
            pass


class VoiceAudio:
    def __init__(self, ws: WebSocket, session: VoiceSession, service: VoiceService):
        self.ws, self.session, self.service = ws, session, service
        self.outgoing: asyncio.Queue[bytes] = asyncio.Queue(8)
        self.closed = False

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("voice audio disconnected")
        try:
            async with asyncio.timeout(3):
                await self.outgoing.put(data)
        except TimeoutError:
            raise ProtocolError(ErrorCode.BUSY, "Voice audio queue full") from None

    async def _sender(self) -> None:
        while True:
            data = await self.outgoing.get()
            async with asyncio.timeout(2):
                await self.ws.send_bytes(data)

    async def flush(self) -> None:
        while not self.outgoing.empty():
            try:
                self.outgoing.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def run(self, guard: SecurityHTTP, principal: Principal) -> None:
        tasks = []
        try:
            async with asyncio.timeout(5):
                await guard.check_ws_message(self.ws, principal)
            await self.ws.accept()
            await self.service.attach_audio(self.session, self)
            tasks = [asyncio.create_task(self._sender()), asyncio.create_task(self._receive(guard, principal))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (AccessDenied, ProtocolError, ValueError):
            await self._close(1008, "voice audio rejected")
        except (TimeoutError, WebSocketDisconnect):
            await self._close(1001, "voice audio unavailable")
        except (OSError, RuntimeError):
            await self._close(1011, "voice audio unavailable")
        finally:
            self.closed = True
            for task in tasks:
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.session.active:
                    try:
                        await self.service.stop(self.session, "disconnect")
                    except Exception:
                        pass

    async def _receive(self, guard: SecurityHTTP, principal: Principal) -> None:
        while True:
            await guard.check_ws_message(self.ws, principal)
            data = await self.ws.receive_bytes()
            if len(data) > 16 * 1024:
                raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Audio frame exceeds 16 KiB")
            await self.service.send_audio(self.session, data)

    async def _close(self, code: int, reason: str) -> None:
        try:
            async with asyncio.timeout(2):
                await self.ws.close(code=code, reason=reason)
        except (RuntimeError, WebSocketDisconnect, TimeoutError, OSError):
            pass


class VoiceLink:
    def __init__(self, guards: SecurityHTTP, config: CoreConfig, service: VoiceService):
        self.guards, self.config, self.service = guards, config, service
        self.connections: list[VoiceControl | VoiceAudio] = []
        self.router = APIRouter()
        self.router.add_api_websocket_route("/ws/v1/voice", self.control)
        self.router.add_api_websocket_route("/ws/v1/voice/audio", self.audio)

    async def control(self, websocket: WebSocket) -> None:
        connection = VoiceControl(websocket, None, self.config, self.service)  # type: ignore[arg-type]
        self.connections.append(connection)
        try:
            await connection.run(self.guards)
        finally:
            if connection in self.connections:
                self.connections.remove(connection)

    async def audio(self, websocket: WebSocket) -> None:
        connection = None
        try:
            principal = await self.guards.node_ws(websocket)
            session_id = websocket.query_params.get("session_id")
            epoch = websocket.query_params.get("epoch")
            if not session_id or not epoch:
                raise AccessDenied("SESSION_EXPIRED")
            session = self.service.require_session(
                UUID(session_id), int(epoch), principal.identity_id
            )
            connection = VoiceAudio(websocket, session, self.service)
            self.connections.append(connection)
            await connection.run(self.guards, principal)
        except (ValueError, AccessDenied):
            try:
                await websocket.close(code=1008, reason="voice audio rejected")
            except Exception:
                pass
        finally:
            if connection is not None and connection in self.connections:
                self.connections.remove(connection)

    async def close(self) -> None:
        for connection in list(self.connections):
            try:
                await connection._close(1001, "core stopping")
            except Exception:
                pass
        self.connections.clear()
        await self.service.close()
