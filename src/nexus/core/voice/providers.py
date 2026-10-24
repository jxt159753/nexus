"""Voice provider interfaces and deterministic fakes for Core-only tests."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID


PartialHandler = Callable[[str], Awaitable[None]]
FinalHandler = Callable[[str], Awaitable[None]]


class AsrProvider:
    async def start(self, session_id: UUID, on_partial: PartialHandler, on_final: FinalHandler) -> None:
        raise NotImplementedError

    async def send_audio(self, session_id: UUID, pcm: bytes) -> None:
        raise NotImplementedError

    async def stop(self, session_id: UUID) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class TtsProvider:
    async def stream(self, texts: AsyncIterator[str], voice_id: str) -> AsyncIterator[bytes]:
        async for text in texts:
            async for chunk in self.synthesize(text, voice_id):
                yield chunk

    async def synthesize(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield b""  # pragma: no cover - makes this an async generator for type checkers

    async def close(self) -> None:
        pass


@dataclass
class _FakeAsrState:
    on_partial: PartialHandler
    on_final: FinalHandler
    buffer: bytearray = field(default_factory=bytearray)
    partial_sent: bool = False
    final_sent: bool = False


class FakeAsrProvider(AsrProvider):
    """Emits one partial/final after a fixed amount of 16 kHz PCM.

    This is deliberately deterministic: it proves the Core control/audio lifecycle
    without pretending to be a speech recognizer or making a paid provider call.
    """

    def __init__(self, *, final_text: str = "你好，NEXUS，这是一次语音测试。") -> None:
        self.final_text = final_text
        self.sessions: dict[UUID, _FakeAsrState] = {}

    async def start(self, session_id: UUID, on_partial: PartialHandler, on_final: FinalHandler) -> None:
        self.sessions[session_id] = _FakeAsrState(on_partial, on_final)

    async def send_audio(self, session_id: UUID, pcm: bytes) -> None:
        state = self.sessions.get(session_id)
        if state is None or state.final_sent:
            return
        state.buffer.extend(pcm)
        if not state.partial_sent and len(state.buffer) >= 8000:
            state.partial_sent = True
            await state.on_partial("你好")
        if len(state.buffer) >= 32000:
            state.final_sent = True
            await state.on_final(self.final_text)

    async def stop(self, session_id: UUID) -> None:
        self.sessions.pop(session_id, None)

    async def close(self) -> None:
        self.sessions.clear()


class FakeTtsProvider(TtsProvider):
    """Returns bounded silence in 22.05 kHz PCM16 chunks.

    The audio path is real (binary WebSocket forwarding), but there is no model
    call. The real Bailian adapter will replace only this provider.
    """

    def __init__(self, *, sample_rate: int = 22050, seconds: float = 0.5) -> None:
        self.sample_rate = sample_rate
        self.frames = max(1, int(sample_rate * seconds))

    async def synthesize(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        # 20 ms of silence per yield, bounded by the configured duration.
        chunk = 2 * max(1, self.sample_rate // 50)
        remaining = 2 * self.frames
        while remaining > 0:
            size = min(chunk, remaining)
            yield b"\x00" * size
            remaining -= size
            await asyncio.sleep(0)
