"""Bounded raw Bailian WebSocket adapters. No SDK dependency or audio files."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from nexus.core.config import VoiceConfig
from nexus.core.voice.providers import AsrProvider, FinalHandler, PartialHandler, TtsProvider


def _endpoint(config: VoiceConfig) -> str:
    return f"wss://{config.workspace_id}.{config.region}.maas.aliyuncs.com/api-ws/v1/inference"


def _headers(config: VoiceConfig, api_key: str) -> dict[str, str]:
    return {"Authorization": f"bearer {api_key}", "X-DashScope-WorkSpace": config.workspace_id,
            "user-agent": "nexus-voice/0.2"}


class ProviderUnavailable(ConnectionError):
    pass


class _AsrSession:
    def __init__(self, task_id, ws, on_partial, on_final):
        self.task_id, self.ws = task_id, ws
        self.on_partial, self.on_final = on_partial, on_final
        self.reader = None
        self.started, self.finished = asyncio.Event(), asyncio.Event()
        self.failure = None
        self.ready = False
        self.finals = set()


class BailianAsrProvider(AsrProvider):
    def __init__(self, config: VoiceConfig, api_key: str | None = None):
        self.config = config
        self.api_key = (api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        if not self.api_key:
            raise ProviderUnavailable("DASHSCOPE_API_KEY is not configured")
        self.sessions: dict[UUID, _AsrSession] = {}

    async def start(self, session_id: UUID, on_partial: PartialHandler, on_final: FinalHandler) -> None:
        await self.stop(session_id)
        task_id = str(uuid4())
        try:
            ws = await connect(_endpoint(self.config), additional_headers=_headers(self.config, self.api_key),
                               max_size=64 * 1024, open_timeout=5, close_timeout=1, ping_interval=20)
        except (OSError, TimeoutError, InvalidHandshake, ConnectionClosed) as exc:
            raise ProviderUnavailable("Bailian ASR connection failed") from exc
        state = _AsrSession(task_id, ws, on_partial, on_final)
        self.sessions[session_id] = state
        state.reader = asyncio.create_task(self._read(session_id, state))
        try:
            await ws.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                            "model": self.config.asr_model,
                            "parameters": {"format": "pcm", "sample_rate": 16000, "language_hints": ["zh"],
                                           "max_sentence_silence": 600, "heartbeat": True,
                                           "punctuation_prediction_enabled": True}, "input": {}}}))
            async with asyncio.timeout(5):
                await state.started.wait()
            if not state.ready or state.failure:
                raise ProviderUnavailable("Bailian ASR task rejected")
        except BaseException:
            await self.stop(session_id)
            raise

    async def _read(self, session_id, state):
        try:
            async for raw in state.ws:
                if isinstance(raw, bytes):
                    raise ProviderUnavailable("Invalid ASR event")
                event = json.loads(raw)
                header = event.get("header", {})
                if header.get("task_id") not in {None, state.task_id}:
                    raise ProviderUnavailable("ASR task mismatch")
                kind = header.get("event")
                if kind == "task-started":
                    state.ready = True
                    state.started.set()
                elif kind == "task-failed":
                    raise ProviderUnavailable("Bailian ASR task failed")
                elif kind == "task-finished":
                    state.finished.set()
                    return
                elif kind == "result-generated":
                    sentence = event.get("payload", {}).get("output", {}).get("sentence") or {}
                    if sentence.get("heartbeat"):
                        continue
                    text = str(sentence.get("text", "")).strip()
                    if not text:
                        continue
                    if sentence.get("sentence_end") is True:
                        key = (sentence.get("sentence_id"), sentence.get("end_time"), text)
                        if key not in state.finals:
                            state.finals.add(key)
                            if len(state.finals) > 64:
                                raise ProviderUnavailable("ASR event limit")
                            await state.on_final(text)
                    else:
                        await state.on_partial(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            state.failure = "VOICE_PROVIDER_ERROR"
        finally:
            if not state.finished.is_set() and not state.failure:
                state.failure = "VOICE_PROVIDER_ERROR"
            state.started.set()

    async def send_audio(self, session_id: UUID, pcm: bytes) -> None:
        state = self.sessions.get(session_id)
        if state is None or state.failure:
            raise ProviderUnavailable("Bailian ASR is unavailable")
        try:
            async with asyncio.timeout(2):
                await state.ws.send(pcm)
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            raise ProviderUnavailable("Bailian ASR send failed") from exc

    async def stop(self, session_id: UUID) -> None:
        state = self.sessions.pop(session_id, None)
        if state is None:
            return
        try:
            async with asyncio.timeout(1):
                await state.ws.send(json.dumps({
                    "header": {"action": "finish-task", "task_id": state.task_id, "streaming": "duplex"},
                    "payload": {"input": {}}}))
                await state.finished.wait()
        except (ConnectionClosed, OSError, TimeoutError):
            pass
        finally:
            if state.reader is not asyncio.current_task():
                state.reader.cancel()
                await asyncio.gather(state.reader, return_exceptions=True)
            await state.ws.close()
        return state.finished.is_set() and state.failure is None

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(key) for key in list(self.sessions)), return_exceptions=True)


class BailianTtsProvider(TtsProvider):
    def __init__(self, config: VoiceConfig, api_key: str | None = None):
        self.config = config
        self.api_key = (api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        if not self.api_key:
            raise ProviderUnavailable("DASHSCOPE_API_KEY is not configured")

    async def synthesize(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        async def source():
            yield text
        async for data in self.stream(source(), voice_id):
            yield data

    async def stream(self, texts: AsyncIterator[str], voice_id: str) -> AsyncIterator[bytes]:
        # Consume the first text before opening a billable task. Core reserves
        # and audits that call while producing this first segment.
        try:
            first = await anext(texts)
        except StopAsyncIteration:
            return
        task_id = str(uuid4())
        try:
            ws = await connect(_endpoint(self.config), additional_headers=_headers(self.config, self.api_key),
                               max_size=1024 * 1024, open_timeout=5, close_timeout=1, ping_interval=20)
        except (OSError, TimeoutError, InvalidHandshake, ConnectionClosed) as exc:
            raise ProviderUnavailable("Bailian TTS connection failed") from exc
        sender = None
        try:
            await ws.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {"task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                            "model": self.config.tts_model,
                            "parameters": {"text_type": "PlainText", "voice": voice_id or self.config.voice_id,
                                           "format": "pcm", "sample_rate": 22050, "volume": self.config.tts_volume,
                                           "rate": self.config.tts_rate, "pitch": self.config.tts_pitch,
                                           "enable_ssml": False}, "input": {}}}))
            async with asyncio.timeout(8):
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        raise ProviderUnavailable("TTS audio before task-started")
                    kind = json.loads(raw).get("header", {}).get("event")
                    if kind == "task-started":
                        break
                    if kind == "task-failed":
                        raise ProviderUnavailable("Bailian TTS task rejected")

            async def send_texts():
                async def send(text):
                    await ws.send(json.dumps({
                        "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
                        "payload": {"input": {"text": text}}}))
                await send(first)
                async for text in texts:
                    await send(text)
                await ws.send(json.dumps({
                    "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {"input": {}}}))

            sender = asyncio.create_task(send_texts())
            remainder = b""
            got_audio = False
            async with asyncio.timeout(self.config.max_tts_seconds):
                while True:
                    if sender.done() and not sender.cancelled() and sender.exception():
                        raise sender.exception()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                    except TimeoutError:
                        if sender.done():
                            raise
                        # A sparse progress update may be followed by tool work
                        # with no speech. Keep the original overall TTS deadline.
                        continue
                    if isinstance(raw, bytes):
                        combined = remainder + raw
                        size = len(combined) // 2 * 2
                        remainder = combined[size:]
                        if size:
                            got_audio = True
                            yield combined[:size]
                        continue
                    event = json.loads(raw)
                    header = event.get("header", {})
                    if header.get("task_id") not in {None, task_id}:
                        raise ProviderUnavailable("TTS task mismatch")
                    if header.get("event") == "task-failed":
                        raise ProviderUnavailable("Bailian TTS task failed")
                    if header.get("event") == "task-finished":
                        await sender
                        if remainder or not got_audio:
                            raise ProviderUnavailable("Incomplete TTS audio")
                        return
        except (ConnectionClosed, OSError, TimeoutError, ValueError) as exc:
            raise ProviderUnavailable("Bailian TTS stream failed") from exc
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            await ws.close()
