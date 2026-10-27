"""Core-owned, consent-bound voice lifecycle. Audio is never persisted."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID, uuid4

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.voice import (
    VoiceError, VoiceFinal, VoiceListenStart, VoicePartial, VoiceSpeak,
    VoiceSpeakCompleted, VoiceSpeakStop, VoiceState, VoiceStop, VoiceStopped, pack_voice_audio,
)
from nexus.core.config import VoiceConfig
from nexus.core.models.config import ModelConfig
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from nexus.core.voice.providers import AsrProvider, TtsProvider
from nexus.core.voice.speech import speech_segments


class ControlSink(Protocol):
    async def send(self, message) -> None: ...


class AudioSink(Protocol):
    async def send(self, data: bytes) -> None: ...
    async def flush(self) -> None: ...


@dataclass
class VoiceSession:
    voice_session_id: UUID
    request_id: UUID
    owner_id: UUID
    node_id: UUID
    trigger: str
    state: str = "activating"
    session_epoch: int = 1
    generation: int = 0
    started_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=lambda: utc_now() + timedelta(minutes=30))
    conversation_id: UUID | None = None
    utterance_id: UUID | None = None
    assistant_utterance_id: UUID | None = None
    chat_turn_id: str | None = None
    control: ControlSink | None = None
    audio: AudioSink | None = None
    response_task: asyncio.Task | None = None
    stopped: bool = False
    consent: dict = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    framed_audio: bool = False
    audio_ready: asyncio.Event = field(default_factory=asyncio.Event)
    playback_done: asyncio.Event = field(default_factory=asyncio.Event)
    media_stopped: asyncio.Event = field(default_factory=asyncio.Event)
    transition: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: int = 0
    partial_seq: int = 0
    listened_bytes: int = 0
    listen_started: float = 0
    last_input: float = 0
    last_control: int = 0
    speech_started_at: datetime | None = None
    owner_principal: Principal | None = None
    proactive: list[dict] = field(default_factory=list)
    proactive_listening: bool = False
    proactive_playback: bool = False
    synthesis_finished: bool = False
    end_after_reply: bool = False

    @property
    def active(self) -> bool:
        return not self.stopped and self.state not in {"stopped", "error"}


def _segments(text: str, limit: int = 120) -> list[str]:
    pieces = []
    while text:
        end = next((i + 1 for i, char in enumerate(text[:limit]) if char in "。！？!?；;"), min(limit, len(text)))
        piece, text = text[:end].strip(), text[end:]
        if piece:
            pieces.append(piece)
    return pieces[:64]


class VoiceService:
    def __init__(self, database, config: VoiceConfig, owner_id: UUID, *, asr: AsrProvider,
                 tts: TtsProvider, responder: Callable | None = None,
                 conversation_factory: Callable | None = None, streamer: Callable | None = None,
                 canceller: Callable | None = None, model_config: ModelConfig | None = None) -> None:
        self.database, self.config, self.owner_id = database, config, owner_id
        self.models = model_config if model_config is not None else ModelConfig()
        self.asr, self.tts = asr, tts
        self.responder, self.streamer = responder, streamer
        self.conversation_factory, self.canceller = conversation_factory, canceller
        self.sessions: dict[UUID, VoiceSession] = {}
        self.lock = asyncio.Lock()
        self.validate_owner = None
        self.node_permitted = None

    @staticmethod
    def now() -> datetime:
        return utc_now()

    async def recover(self) -> None:
        async with self.database.transaction() as conn:
            rows = await conn.execute_fetchall("SELECT voice_session_id,metrics_json FROM voice_sessions "
                                              "WHERE state NOT IN ('stopped','error','interrupted')")
            for row in rows:
                metrics = json.loads(row["metrics_json"] or "{}")
                for call in metrics.get("calls", []):
                    if call["status"] == "reserved":
                        call["status"] = "unknown"
                await conn.execute("UPDATE voice_sessions SET state='interrupted',ended_at=?,end_reason='core_restart',"
                                   "metrics_json=? WHERE voice_session_id=?",
                                   (timestamp(utc_now()), json.dumps(metrics), row["voice_session_id"]))

    async def prepare(self, owner: Principal, node_id: UUID, request_id: UUID, trigger: str,
                      conversation_id: UUID, consent: dict, duration: int) -> VoiceSession:
        if owner.identity_id != self.owner_id or owner.role != "owner":
            raise AccessDenied("FORBIDDEN")
        return await self._create(node_id, request_id, trigger, conversation_id, consent, duration)

    async def _create(self, node_id, request_id, trigger, conversation_id=None, consent=None, duration=None):
        if not self.config.enabled:
            raise AccessDenied("VOICE_DISABLED")
        async with self.lock, self.database.transaction() as conn:
            old = await one(conn, "SELECT * FROM voice_sessions WHERE owner_id=? AND request_id=?",
                            (str(self.owner_id), str(request_id)))
            if old:
                session = self.sessions.get(UUID(old["voice_session_id"]))
                if (session and session.active and session.node_id == node_id and session.trigger == trigger
                        and session.conversation_id == conversation_id and session.consent == (consent or {})):
                    return session
                raise Conflict("IDEMPOTENCY_CONFLICT")
            state = await one(conn, "SELECT paused,revision FROM system_state")
            if state["paused"]:
                raise AccessDenied("PAUSED")
            if sum(s.active or s.state == "stopping" for s in self.sessions.values()) >= self.config.max_concurrent_sessions:
                raise AccessDenied("VOICE_BUSY")
            if (await one(conn, "SELECT count(*) FROM voice_sessions"))[0] >= 10000:
                raise AccessDenied("BUSY")
            now = utc_now()
            session = VoiceSession(
                voice_session_id=uuid4(), request_id=request_id, owner_id=self.owner_id, node_id=node_id,
                trigger=trigger, conversation_id=conversation_id, consent=consent or {},
                started_at=now, expires_at=now + timedelta(seconds=duration or self.config.max_session_seconds),
                last_control=state["revision"],
            )
            await conn.execute("INSERT INTO voice_sessions(voice_session_id,request_id,owner_id,node_id,state,"
                               "session_epoch,generation,conversation_id,trigger,started_at,expires_at,metrics_json) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (str(session.voice_session_id), str(request_id), str(self.owner_id), str(node_id),
                                session.state, 1, 0, str(conversation_id) if conversation_id else None, trigger,
                                timestamp(now), timestamp(session.expires_at),
                                json.dumps({"consent": session.consent, "calls": []})))
            await audit(conn, str(self.owner_id), None, "voice.consent_saved", now)
            # Old durable rows remain queryable; only recent sessions use memory.
            for key, value in list(self.sessions.items()):
                if not value.active and len(self.sessions) >= 128:
                    del self.sessions[key]
            self.sessions[session.voice_session_id] = session
            return session

    async def open_session(self, principal: Principal, request_id: UUID, trigger: str, device_id=None):
        if trigger not in {"button", "wake"}:
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Unsupported activation trigger")
        for session in self.sessions.values():
            if session.request_id == request_id and session.node_id == principal.identity_id and session.active:
                if session.trigger != trigger or session.control is not None or utc_now() >= session.expires_at:
                    raise AccessDenied("SESSION_EXPIRED")
                return session
        if self.config.provider != "fake":
            raise AccessDenied("MODEL_CONSENT_REQUIRED")
        return await self._create(principal.identity_id, request_id, trigger)

    async def ensure_conversation(self, session):
        if session.conversation_id is None:
            conversation_id = await self.conversation_factory(session) if self.conversation_factory else uuid4()
            session.conversation_id = conversation_id
            async with self.database.transaction() as conn:
                await conn.execute("UPDATE voice_sessions SET conversation_id=? WHERE voice_session_id=?",
                                   (str(conversation_id), str(session.voice_session_id)))
        return session.conversation_id

    def require_session(self, session_id, epoch, node_id, *, active=True):
        session = self.sessions.get(session_id)
        if session is None or session.node_id != node_id or session.session_epoch != epoch:
            raise AccessDenied("SESSION_EXPIRED")
        if active and (not session.active or utc_now() >= session.expires_at):
            raise AccessDenied("SESSION_EXPIRED")
        return session

    async def attach_control(self, session, control):
        if not session.active:
            raise AccessDenied("SESSION_EXPIRED")
        session.control = control

    async def attach_audio(self, session, audio):
        if not session.active or session.audio is not None:
            raise AccessDenied("SESSION_EXPIRED")
        session.audio = audio
        session.audio_ready.set()

    async def _persist_metrics(self, session, conn):
        await conn.execute("UPDATE voice_sessions SET metrics_json=? WHERE voice_session_id=?",
                           (json.dumps({"consent": session.consent, "calls": session.calls, "proactive": session.proactive}),
                            str(session.voice_session_id)))

    async def _reserve(self, session, kind: str, units: int) -> int:
        # The independent voice ledger records even silence, failures and unknown calls.
        if session.owner_principal and self.validate_owner:
            await self.validate_owner(session.owner_principal)
        if utc_now() >= session.expires_at:
            raise AccessDenied("SESSION_EXPIRED")
        async with self.lock, self.database.transaction() as conn:
            state = await one(conn, "SELECT paused FROM system_state")
            if state["paused"] or not session.active:
                raise AccessDenied("PAUSED")
            day = utc_now().date().isoformat()
            total = await one(conn, "SELECT coalesce(sum(json_extract(j.value,'$.reserved')),0) "
                              "FROM voice_sessions s,json_each(s.metrics_json,'$.calls') j "
                              "WHERE json_extract(j.value,'$.day')=? AND json_extract(j.value,'$.kind')=?",
                              (day, kind))
            ceiling = self.config.daily_asr_seconds if kind == "asr" else self.config.daily_tts_chars
            if (self.models.cost_limits_enabled and total[0] + units > ceiling) or (self.models.call_limits_enabled and len(session.calls) >= 100):
                raise AccessDenied("BUDGET_EXCEEDED")
            session.calls.append({"kind": kind, "day": day, "reserved": units, "observed": 0,
                                  "status": "reserved", "started_at": timestamp(utc_now()),
                                  "unit_cny_microunits": self.config.asr_cny_microunits_per_second if kind == "asr" else self.config.tts_cny_microunits_per_char,
                                  "estimated_cny_microunits": units * (self.config.asr_cny_microunits_per_second if kind == "asr" else self.config.tts_cny_microunits_per_char)})
            await self._persist_metrics(session, conn)
            await audit(conn, str(self.owner_id), None, f"voice.{kind}_reserved", utc_now())
            return len(session.calls) - 1

    async def _settle(self, session, index, *, known: bool, observed: int):
        async with self.database.transaction() as conn:
            call = session.calls[index]
            call.update(status="estimated" if known else "unknown", observed=observed)
            # Unknown usage retains the original reservation, never zero cost.
            if known:
                call["reserved"] = observed
                call["estimated_cny_microunits"] = observed * call["unit_cny_microunits"]
            await self._persist_metrics(session, conn)

    async def begin_listening(self, session):
        if not session.active:
            return
        if self.models.call_limits_enabled and session.turns >= self.config.max_turns:
            await self.stop(session, "turn_limit")
            return
        session.utterance_id = uuid4()
        session.chat_turn_id = None
        session.partial_seq, session.listened_bytes = 0, 0
        session.speech_started_at = None
        session.listen_started = session.last_input = asyncio.get_running_loop().time()
        session.playback_done.clear()
        session.synthesis_finished = False
        await self._store_utterance(session, session.utterance_id, "user", "")
        await self._reserve(session, "asr", self.config.max_utterance_seconds)
        await self.asr.start(session.voice_session_id,
                             lambda text: self.accept_partial(session, text),
                             lambda text: self.accept_final(session, text))
        await self._set_state(session, "listening")
        await self._send(session, VoiceListenStart(
            type="voice.listen.start", **self._base(), voice_session_id=session.voice_session_id,
            session_epoch=session.session_epoch, utterance_id=session.utterance_id,
            max_seconds=self.config.max_utterance_seconds, language="zh"))

    async def send_audio(self, session, pcm: bytes):
        if not session.active or session.state != "listening":
            return
        if not pcm or len(pcm) > 16 * 1024 or len(pcm) % 2:
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Invalid PCM frame")
        session.listened_bytes += len(pcm)
        if session.listened_bytes > 32000 * self.config.max_utterance_seconds:
            await self.stop(session, "utterance_limit")
            return
        await self.asr.send_audio(session.voice_session_id, pcm)

    async def accept_partial(self, session, text):
        if not session.active or session.state != "listening":
            return
        text = text.strip()[:200]
        if text:
            session.last_input = asyncio.get_running_loop().time()
            session.speech_started_at = session.speech_started_at or utc_now()
            session.partial_seq += 1
            await self._send(session, VoicePartial(
                type="voice.partial", **self._base(), voice_session_id=session.voice_session_id,
                session_epoch=session.session_epoch, utterance_id=session.utterance_id,
                seq=session.partial_seq, text=text, stable=False))

    async def accept_final(self, session, text):
        if not session.active or session.state != "listening":
            return
        text = text.strip()[:self.config.max_final_chars]
        if not text or session.response_task and not session.response_task.done():
            return
        session.turns += 1
        session.proactive_listening = False
        session.proactive_playback = False
        session.generation += 1
        session.assistant_utterance_id = uuid4()
        session.chat_turn_id = None
        session.end_after_reply = False
        await self._store_utterance(session, session.utterance_id, "user", text)
        await self._send(session, VoiceFinal(
            type="voice.final", **self._base(), voice_session_id=session.voice_session_id,
            session_epoch=session.session_epoch, utterance_id=session.utterance_id, text=text,
            started_at=session.speech_started_at or utc_now(), ended_at=utc_now()))
        await self._set_state(session, "thinking")
        session.response_task = asyncio.create_task(self._respond(session, text))

    async def _response_text(self, session, text) -> AsyncIterator[str]:
        if self.streamer is not None:
            async for delta in self.streamer(session, text):
                yield delta
        elif self.responder:
            yield await self.responder(session, text)
        else:
            raise AccessDenied("MODEL_DISABLED")

    async def _respond(self, session, text):
        asr_index = next((index for index in range(len(session.calls) - 1, -1, -1)
                          if session.calls[index]["kind"] == "asr" and session.calls[index]["status"] == "reserved"), None)
        observed = (session.listened_bytes + 31999) // 32000
        async def close_asr():
            known = await self.asr.stop(session.voice_session_id)
            if asr_index is not None:
                await self._settle(session, asr_index, known=known is True, observed=observed)
        closing_asr = asyncio.create_task(close_asr())
        try:
            await self._speak_stream(session, self._response_text(session, text))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if session.active:
                code = getattr(exc, "code", "VOICE_PROVIDER_ERROR")
                try:
                    code = ErrorCode(code)
                except ValueError:
                    code = ErrorCode.VOICE_PROVIDER_ERROR
                await self._send(session, VoiceError(
                    type="voice.error", **self._base(), code=code, message="语音处理失败，请查看聊天记录后重试。",
                    retryable=False, voice_session_id=session.voice_session_id,
                    session_epoch=session.session_epoch))
                await self.stop(session, "error")
        finally:
            await asyncio.shield(closing_asr)

    async def _speak(self, session, text):
        async def source():
            yield text
        await self._speak_stream(session, source())

    async def _speak_stream(self, session, source, *, listen_after=True):
        generation = session.generation
        utterance_id = session.assistant_utterance_id or uuid4()
        session.assistant_utterance_id = utterance_id
        spoken, segments, audio_bytes, call_index = [], 0, 0, None

        async def texts():
            nonlocal segments, call_index
            async for segment in speech_segments(source, self.config.max_tts_chars):
                if not session.active or session.generation != generation:
                    return
                if call_index is None:
                    call_index = await self._reserve(session, "tts", self.config.max_tts_chars)
                    await self._set_state(session, "speaking")
                await self._send(session, VoiceSpeak(
                    type="voice.speak", **self._base(), voice_session_id=session.voice_session_id,
                    session_epoch=session.session_epoch, utterance_id=utterance_id,
                    generation=generation, segment_index=segments, text=segment,
                    final_segment=False, not_after=utc_now() + timedelta(seconds=self.config.max_tts_seconds),
                    voice=self.config.voice_id or None, speed=self.config.tts_rate))
                spoken.append(segment)
                segments += 1
                yield segment

        try:
            async with asyncio.timeout(self.config.max_tts_seconds):
                if session.framed_audio:
                    async with asyncio.timeout(5):
                        await session.audio_ready.wait()
                async for chunk in self.tts.stream(texts(), self.config.voice_id):
                    if not session.active or session.generation != generation:
                        return
                    if len(chunk) % 2:
                        raise ValueError("unaligned provider PCM")
                    for start in range(0, len(chunk), 8192):
                        pcm = chunk[start:start + 8192]
                        if session.audio:
                            await session.audio.send(pack_voice_audio(generation, pcm) if session.framed_audio else pcm)
                        audio_bytes += len(pcm)
                if call_index is not None:
                    await self._settle(session, call_index, known=True, observed=sum(map(len, spoken)))
                await self._store_utterance(session, utterance_id, "assistant", "".join(spoken))
                if session.active and generation == session.generation:
                    session.synthesis_finished = True
                    await self._send(session, VoiceSpeakCompleted(
                        type="voice.speak.completed", **self._base(), voice_session_id=session.voice_session_id,
                        session_epoch=session.session_epoch, utterance_id=utterance_id, generation=generation,
                        segments=segments, total_ms=audio_bytes * 1000 // 44100))
                    if session.framed_audio and session.audio and audio_bytes:
                        await session.audio.send(pack_voice_audio(generation, end=True))
                        await session.playback_done.wait()
        finally:
            if call_index is not None and session.calls[call_index]["status"] == "reserved":
                await asyncio.shield(self._settle(session, call_index, known=False, observed=sum(map(len, spoken))))
        if session.active and generation == session.generation:
            # This happens only after the device confirms that the output drained.
            if session.end_after_reply:
                await self.stop(session, "recovery_required")
            elif listen_after:
                await self.begin_listening(session)
            else:
                await self.standby(session)

    async def standby(self, session):
        await self.asr.stop(session.voice_session_id)
        if session.trigger == "wake" and session.active:
            await self._set_state(session, "activating")
        else:
            await self.stop(session, "timeout")

    def _dnd(self):
        hour = utc_now().astimezone(timezone(timedelta(hours=8))).hour
        start, end = self.config.dnd_start_hour, self.config.dnd_end_hour
        return start <= hour < end if start < end else (hour >= start or hour < end) if start > end else False

    async def announce(self, session, text: str, *, listen=False, event_id=None):
        """Deterministic reminder speech; never invokes a model or grants tools."""
        if not session.consent.get("proactive_speak") or listen and not session.consent.get("proactive_listen"):
            raise AccessDenied("VOICE_NOT_GRANTED")
        if not session.active or utc_now() >= session.expires_at:
            raise AccessDenied("SESSION_EXPIRED")
        if session.state != "activating" or not session.audio_ready.is_set():
            raise AccessDenied("BUSY")
        text = text.strip()
        if not text or len(text) > 500:
            raise ValueError("announcement must contain 1..500 characters")
        event_id = str(event_id or uuid4())
        async with self.lock, self.database.transaction() as conn:
            duplicate = await one(conn, "SELECT 1 FROM voice_sessions s,json_each(s.metrics_json,'$.proactive') p "
                                  "WHERE json_extract(p.value,'$.event_id')=?", (event_id,))
            if duplicate:
                return False
            day = utc_now().date().isoformat()
            total = await one(conn, "SELECT count(*) FROM voice_sessions s,json_each(s.metrics_json,'$.proactive') p "
                              "WHERE json_extract(p.value,'$.day')=? AND json_extract(p.value,'$.status')!='suppressed'", (day,))
            if self.models.call_limits_enabled and total[0] >= self.config.proactive_daily_limit:
                raise AccessDenied("VOICE_QUOTA")
            suppressed = self._dnd()
            session.proactive.append({"event_id": event_id, "day": day,
                                      "status": "suppressed" if suppressed else "claimed",
                                      "created_at": timestamp(utc_now())})
            await self._persist_metrics(session, conn)
            await audit(conn, str(self.owner_id), None,
                        "voice.proactive_suppressed" if suppressed else "voice.proactive_claimed", utc_now())
        if suppressed:
            return False
        session.generation += 1
        session.assistant_utterance_id, session.chat_turn_id = uuid4(), None
        session.proactive_listening = listen
        session.proactive_playback = True
        session.playback_done.clear()
        session.synthesis_finished = False
        await self._set_state(session, "thinking")
        async def speaking():
            async def source():
                yield text
            try:
                await self._speak_stream(session, source(), listen_after=listen)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.stop(session, "error")
        session.response_task = asyncio.create_task(speaking())
        return True

    async def _scheduled_announcement(self, session):
        if not session.consent.get("proactive_speak") or not session.control or not session.audio_ready.is_set():
            return
        async with self.database.transaction(write=False) as conn:
            events = await conn.execute_fetchall(
                "SELECT e.event_id,e.due_at,s.title,s.prompt FROM plugin_schedule_events e "
                "JOIN plugin_schedules s USING(schedule_id) WHERE s.owner_id=? AND s.conversation_id=? "
                "AND e.status='due' AND e.created_at>=? AND e.due_at>=? AND s.deleted=0 "
                "ORDER BY e.created_at LIMIT 20",
                (str(session.owner_id), str(session.conversation_id), timestamp(session.started_at),
                 timestamp(utc_now() - timedelta(seconds=300))))
        claimed = {item["event_id"] for item in session.proactive}
        for event in events:
            if event["event_id"] not in claimed:
                try:
                    await self.announce(session, (event["title"] + "。" + event["prompt"])[:500],
                                        listen=session.consent.get("proactive_listen", False), event_id=event["event_id"])
                except AccessDenied:
                    pass
                break

    async def playback_ack(self, session, message):
        if (message.generation == session.generation and message.utterance_id == session.assistant_utterance_id
                and session.state == "speaking" and session.synthesis_finished):
            session.playback_done.set()

    async def _cancel_response(self, session):
        task = session.response_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.canceller and session.chat_turn_id:
            await self.canceller(session)

    async def barge_in(self, session, segment_index=0):
        async with session.transition:
            if not session.active or session.state not in {"thinking", "speaking"}:
                return
            old_generation = session.generation
            session.generation += 1
            await self._flush_audio(session)
            await self._send(session, VoiceSpeakStop(
                type="voice.speak.stop", **self._base(), voice_session_id=session.voice_session_id,
                session_epoch=session.session_epoch, utterance_id=session.assistant_utterance_id or uuid4(),
                generation=old_generation, reason="barge_in", at_segment=segment_index))
            await self._set_state(session, "interrupted")
            await self._cancel_response(session)
            async with self.database.transaction() as conn:
                await conn.execute("UPDATE voice_utterances SET interrupted=1,barge_in_at=? WHERE utterance_id=?",
                                   (timestamp(utc_now()), str(session.assistant_utterance_id)))
            await self.asr.stop(session.voice_session_id)
            if session.proactive_playback and not session.consent.get("proactive_listen"):
                await self.standby(session)
            else:
                await self.begin_listening(session)

    async def stop(self, session, reason):
        if session.stopped:
            return
        session.stopped, session.state = True, "stopping"
        session.generation += 1
        await self._flush_audio(session)
        await self._send(session, VoiceStop(
            type="voice.stop", **self._base(), voice_session_id=session.voice_session_id,
            session_epoch=session.session_epoch,
            reason=reason if reason in {"user", "core", "timeout", "shutdown", "policy", "disconnect"} else "core"))
        await self._cancel_response(session)
        await self.asr.stop(session.voice_session_id)
        if session.control and reason != "disconnect":
            try:
                async with asyncio.timeout(1.5):
                    await session.media_stopped.wait()
            except TimeoutError:
                pass
        now = utc_now()
        for call in session.calls:
            if call["status"] == "reserved":
                call["status"] = "unknown"
        async with self.database.transaction() as conn:
            await self._persist_metrics(session, conn)
            await conn.execute("UPDATE voice_sessions SET state='stopped',ended_at=?,end_reason=? WHERE voice_session_id=?",
                               (timestamp(now), reason[:100], str(session.voice_session_id)))
            await audit(conn, str(session.node_id), None, "voice.session_stopped", now)
        await self._send(session, VoiceStopped(
            type="voice.session.stopped", **self._base(), voice_session_id=session.voice_session_id,
            session_epoch=session.session_epoch, reason=reason,
            capture_stopped_at=now if session.media_stopped.is_set() else None,
            playback_stopped_at=now if session.media_stopped.is_set() else None,
            execution_stopped=session.media_stopped.is_set(), tts_cancelled=True, asr_cancelled=True))
        session.state, session.control, session.audio = "stopped", None, None

    async def watch(self):
        try:
            while True:
                await asyncio.sleep(0.5)
                async with self.database.transaction(write=False) as conn:
                    control = await one(conn, "SELECT paused,revision FROM system_state")
                for session in list(self.sessions.values()):
                    if not session.active:
                        continue
                    reason = None
                    if self.node_permitted and not self.node_permitted(session.node_id):
                        reason = "policy"
                    if session.owner_principal and self.validate_owner:
                        try:
                            await self.validate_owner(session.owner_principal)
                        except AccessDenied:
                            reason = "policy"
                    if utc_now() >= session.expires_at:
                        reason = "timeout"
                    elif session.control is None and (utc_now() - session.started_at).total_seconds() >= 30:
                        reason = "timeout"
                    elif control["paused"] or control["revision"] != session.last_control:
                        reason = "policy"
                    elif session.state == "listening":
                        age = asyncio.get_running_loop().time() - session.listen_started
                        idle = asyncio.get_running_loop().time() - session.last_input
                        if age >= self.config.max_utterance_seconds or idle >= (8 if session.proactive_listening else self.config.idle_timeout_seconds):
                            await self.standby(session)
                    elif session.state == "activating":
                        await self._scheduled_announcement(session)
                    if reason:
                        await self.stop(session, reason)
        except asyncio.CancelledError:
            raise

    async def close(self):
        for session in list(self.sessions.values()):
            await self.stop(session, "shutdown")
        self.sessions.clear()
        await self.asr.close()
        await self.tts.close()

    @staticmethod
    async def _flush_audio(session):
        if session.audio:
            await session.audio.flush()

    async def _set_state(self, session, state):
        session.state = state
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE voice_sessions SET state=?,generation=? WHERE voice_session_id=?",
                               (state, session.generation, str(session.voice_session_id)))
        await self._send(session, VoiceState(
            type="voice.state", **self._base(), voice_session_id=session.voice_session_id,
            session_epoch=session.session_epoch, state=state, since=utc_now(),
            generation=session.generation, utterance_id=session.assistant_utterance_id))

    async def _store_utterance(self, session, utterance_id, role, text):
        async with self.database.transaction() as conn:
            await conn.execute("INSERT INTO voice_utterances(utterance_id,voice_session_id,role,text,generation,started_at,duration_ms,turn_id) "
                               "VALUES(?,?,?,?,?,?,0,?) ON CONFLICT(utterance_id) DO UPDATE SET text=excluded.text,"
                               "ended_at=excluded.started_at,turn_id=coalesce(excluded.turn_id,voice_utterances.turn_id)",
                               (str(utterance_id), str(session.voice_session_id), role, text, session.generation,
                                timestamp(utc_now()), session.chat_turn_id))

    async def _send(self, session, message):
        if session.control:
            await session.control.send(message)

    @staticmethod
    def _base():
        return {"message_id": uuid4(), "sent_at": utc_now()}
