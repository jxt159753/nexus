"""Native Windows voice process. Authenticated Core channels and bounded IPC."""

import asyncio
import json
import os
import sys
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from nexus.contracts.voice import (
    VoiceActivate, VoiceBargeIn, VoiceError, VoiceFinal, VoiceHeartbeat, VoiceHello,
    VoiceListenStart, VoicePartial, VoiceSessionStarted, VoiceSpeak, VoiceSpeakCompleted,
    VoiceSpeakStop, VoiceState, VoiceStop, VoiceStopRequest, VoiceStopped, VoiceWelcome,
    encode_voice_message, parse_voice_message, unpack_voice_audio,
)
from nexus.voice.audio import AudioEngine, AudioUnavailable
from nexus.voice.config import RuntimeConfig
from nexus.voice.wake import WakeDetector


class VoiceRuntime:
    def __init__(self, config: RuntimeConfig, token: str):
        if not token:
            raise ValueError("NEXUS_NODE_TOKEN is required")
        self.config, self.token = config, token
        self.audio = AudioEngine(config)
        self.control = self.audio_ws = None
        self.session = self.welcome = None
        self.tasks = []
        self.stop_event, self.activated, self.listening = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.boot_id = uuid4()
        self.audio_open = self.speaking = self.core_stopping = False
        self.wake = WakeDetector(config) if config.wake_enabled else None
        self.current_utterance_id = None
        self.current_generation, self.blocked_generation = 0, -1
        self.segment_count = 0
        self.state = "activating"
        self.preroll = deque(maxlen=50)
        self.barge_pending = False
        self.started = time.monotonic()
        self.core_time = None
        self.core_time_monotonic = None
        self.metrics = {"wake_hit": None, "activated_ms": None, "first_partial_ms": None,
                        "first_speak_ms": None, "first_audio_ms": None, "barge_in_ms": None, "stopped_ms": None}

    def _mark(self, name):
        if self.metrics.get(name) is None:
            self.metrics[name] = round((time.monotonic() - self.started) * 1000)

    def _emit(self, kind, **value):
        event = {"event": kind, **value}
        if self.config.ipc:
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
        elif kind not in {"partial", "final", "speak"}:
            # Transcript text is deliberately absent from default diagnostics.
            print("voice " + json.dumps(event, ensure_ascii=False), flush=True)

    def _headers(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.config.origin:
            headers["Origin"] = self.config.origin
        return headers

    def _base(self):
        return self.config.core_url.rstrip("/")

    def _envelope(self):
        return {"message_id": uuid4(), "sent_at": self.now()}

    def now(self):
        if self.core_time is not None:
            return self.core_time + timedelta(seconds=time.monotonic() - self.core_time_monotonic)
        return datetime.now(UTC)

    async def _send(self, message):
        if self.control:
            await self.control.send(encode_voice_message(message))

    async def run(self):
        # Initialize native DSP before starting the blocking stdin reader.
        # Frozen NumPy initialization can deadlock against Windows CRT input
        # locks; no device is opened by prepare(). Network heartbeats also must
        # not be blocked by first-time native imports.
        self.audio.prepare()
        async with connect(self._base() + "/ws/v1/voice", additional_headers=self._headers(),
                           max_size=16 * 1024, open_timeout=8, close_timeout=1, ping_interval=20) as control:
            self.control = control
            await self._send(VoiceHello(
                type="voice.hello", **self._envelope(), node_id=self.config.node_id,
                voice_version="0.2", boot_id=self.boot_id, audio_framing="nxv2",
                activation_request_id=self.config.request_id,
                features=["capture", "playback", "streaming_asr", "streaming_tts"] +
                         (["wake"] if self.wake else []) + (["aec"] if self.config.aec_enabled else [])))
            async with asyncio.timeout(8):
                self.welcome = parse_voice_message(await control.recv())
            if not isinstance(self.welcome, VoiceWelcome):
                raise RuntimeError("voice handshake rejected")
            self.core_time, self.core_time_monotonic = self.welcome.sent_at, time.monotonic()
            try:
                self.tasks = [asyncio.create_task(self._heartbeat_loop()),
                              asyncio.create_task(self._control_loop()),
                              asyncio.create_task(self._session_flow()),
                              asyncio.create_task(self._device_watch())]
                if self.config.ipc:
                    self.tasks.append(asyncio.create_task(self._command_loop()))
                done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                await self._shutdown()

    async def _open_audio(self):
        if not self.audio_open:
            self.audio.open(with_output=False)
            self.audio_open = True
            self._emit("devices", input=self.audio.input_name, output=self.audio.output_name,
                       aec_active=self.audio.aec_active,
                       duplex="full" if self.audio.aec_active else "half")

    async def _session_flow(self):
        if not self.wake and not self.config.start_on_launch:
            await asyncio.to_thread(input, "Press Enter to start listening...")
        if self.config.request_id is None:
            await self._activate()
        async with asyncio.timeout(12):
            await self.activated.wait()
        self._mark("activated_ms")
        self.audio.open_output()
        url = f"{self._base()}/ws/v1/voice/audio?session_id={self.session.voice_session_id}&epoch={self.session.session_epoch}"
        self.audio_ws = await connect(url, additional_headers=self._headers(), max_size=16 * 1024,
                                      open_timeout=8, close_timeout=1)
        await self._open_audio()
        if self.wake:
            self.state = "standby"
            self._emit("state", state="standby", detail="本地监听唤醒词，音频尚未上传")
        elif self.config.request_id is not None:
            await self._activate()
        audio_tasks = [asyncio.create_task(self._audio_loop()), asyncio.create_task(self._uplink_loop())]
        self.tasks.extend(audio_tasks)
        done, _ = await asyncio.wait(audio_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    async def _activate(self):
        await self._send(VoiceActivate(
            type="voice.activate", **self._envelope(), request_id=self.config.request_id or uuid4(),
            trigger="wake" if self.wake else "button", confidence=None,
            conversation_id=self.config.conversation_id))

    async def _control_loop(self):
        async for raw in self.control:
            message = parse_voice_message(raw)
            if isinstance(message, VoiceSessionStarted):
                self.session = message
                self.activated.set()
                self._emit("session", voice_session_id=str(message.voice_session_id),
                           conversation_id=str(message.conversation_id), expires_at=message.expires_at.isoformat())
            elif isinstance(message, VoiceListenStart):
                self.speaking = False
                self.state = "listening"
                self.listening.set()
                self._emit("state", state="listening")
            elif isinstance(message, VoicePartial):
                self._mark("first_partial_ms")
                self._emit("partial", text=message.text)
            elif isinstance(message, VoiceFinal):
                self._emit("final", text=message.text)
            elif isinstance(message, VoiceState):
                self.state = "standby" if message.state == "activating" and self.wake else message.state
                self.current_generation = max(self.current_generation, message.generation)
                if message.utterance_id:
                    self.current_utterance_id = message.utterance_id
                if message.state in {"thinking", "speaking"}:
                    self.listening.clear()
                elif message.state == "activating":
                    self.listening.clear()
                    self.speaking = False
                self._emit("state", state=self.state, detail=message.detail)
            elif isinstance(message, VoiceSpeak):
                if (self.session is None or message.voice_session_id != self.session.voice_session_id
                        or message.session_epoch != self.session.session_epoch
                        or message.generation <= self.blocked_generation
                        or message.not_after <= self.now()):
                    continue
                self._mark("first_speak_ms")
                self.speaking = True
                self.state = "speaking"
                self.listening.clear()
                self.current_utterance_id, self.current_generation = message.utterance_id, message.generation
                self.segment_count = message.segment_index + 1
                self._emit("speak", text=message.text, segment_index=message.segment_index)
            elif isinstance(message, VoiceSpeakStop):
                self.blocked_generation = max(self.blocked_generation, message.generation)
                self.speaking = False
                self.audio.flush()
                self._emit("state", state="interrupted")
            elif isinstance(message, VoiceSpeakCompleted):
                # Synthesis finished; playback remains active until the binary
                # end marker and the actual output-device drain.
                self.segment_count = message.segments
            elif isinstance(message, VoiceStop):
                self.core_stopping = True
                self.audio.close()
                self.audio_open = self.speaking = False
                now = self.now()
                await self._send(VoiceStopped(
                    type="voice.session.stopped", **self._envelope(), voice_session_id=message.voice_session_id,
                    session_epoch=message.session_epoch, reason=message.reason,
                    capture_stopped_at=now, playback_stopped_at=now, execution_stopped=True))
                self._emit("state", state="stopped", media_stopped=True)
                self.stop_event.set()
                return
            elif isinstance(message, VoiceStopped):
                self._mark("stopped_ms")
                self.stop_event.set()
                return
            elif isinstance(message, VoiceError):
                self._emit("error", code=message.code.value, message=message.message)
                return
        raise ConnectionError("voice control disconnected")

    async def _audio_loop(self):
        async for packet in self.audio_ws:
            if isinstance(packet, str):
                raise AudioUnavailable("invalid audio channel")
            generation, pcm, end = unpack_voice_audio(packet)
            if generation <= self.blocked_generation or generation < self.current_generation:
                continue
            # Control and audio are independent ordered channels.
            async with asyncio.timeout(2):
                while generation > self.current_generation and not self.stop_event.is_set():
                    await asyncio.sleep(0.01)
            if generation != self.current_generation or generation <= self.blocked_generation:
                continue
            if end:
                drained = await asyncio.to_thread(self.audio.drain)
                if drained and generation == self.current_generation and generation > self.blocked_generation:
                    self.speaking = False
                    await self._send(VoiceSpeakCompleted(
                        type="voice.speak.completed", **self._envelope(),
                        voice_session_id=self.session.voice_session_id, session_epoch=self.session.session_epoch,
                        utterance_id=self.current_utterance_id, generation=generation,
                        segments=self.segment_count, total_ms=0))
            else:
                self._mark("first_audio_ms")
                await asyncio.to_thread(self.audio.play, pcm)
        raise ConnectionError("voice audio disconnected")

    async def interrupt(self, source="button"):
        if self.session is None or self.state not in {"thinking", "speaking"} or self.current_utterance_id is None:
            return
        self._mark("barge_in_ms")
        self.blocked_generation = max(self.blocked_generation, self.current_generation)
        self.audio.flush()
        if not self.audio.aec_active:
            self.preroll.clear()
        self.speaking = False
        self.listening.clear()
        self.barge_pending = True
        await self._send(VoiceBargeIn(
            type="voice.barge_in", **self._envelope(), voice_session_id=self.session.voice_session_id,
            session_epoch=self.session.session_epoch, utterance_id=self.current_utterance_id,
            generation=self.current_generation, detected_at=self.now(),
            playback_position_ms=0, segment_index=max(0, self.segment_count - 1),
            confidence=float(self.audio.speech_probability), source=source))
        self.state = "interrupted"
        self._emit("state", state="interrupted", local_playback_stopped=True)

    async def _uplink_loop(self):
        while not self.stop_event.is_set():
            pcm = await asyncio.to_thread(self.audio.read_uplink)
            if self.audio.aec_active or self.listening.is_set() or self.state == "standby":
                self.preroll.append(pcm)
            if self.wake and self.state == "standby":
                hit = self.wake.push(pcm)
                if hit:
                    self.metrics["wake_hit"] = hit
                    self.state = "activating"
                    self.listening.clear()
                    self.barge_pending = True
                    await self._activate()
            elif self.listening.is_set():
                if self.barge_pending:
                    for previous in list(self.preroll)[:-1]:
                        await self.audio_ws.send(previous)
                    self.barge_pending = False
                await self.audio_ws.send(pcm)
            elif (self.state in {"thinking", "speaking"} and self.audio.aec_active
                  and self.current_generation > self.blocked_generation
                  and getattr(self.audio, "speech_ms", 0) >= 120):
                await self.interrupt(source="vad")
            await asyncio.sleep(0)

    async def _heartbeat_loop(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(self.welcome.heartbeat_interval_seconds)
            await self._send(VoiceHeartbeat(
                type="voice.heartbeat", **self._envelope(),
                voice_session_id=self.session.voice_session_id if self.session else None,
                session_epoch=self.session.session_epoch if self.session else None,
                state=self.state, capture_active=self.audio_open, playback_active=self.speaking))

    async def _device_watch(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(1)
            if self.audio_open and self.audio.devices_changed():
                raise AudioUnavailable("audio device changed; restart voice to use the selected device")

    async def _command_loop(self):
        import queue
        import threading
        lines = queue.Queue(maxsize=8)
        failed = threading.Event()

        def read_commands():
            while True:
                line = sys.stdin.readline(4097)
                try:
                    lines.put(line, timeout=0.5)
                except queue.Full:
                    failed.set()
                    return
                if not line or len(line) > 4096:
                    return

        # A blocked console read must not hold asyncio.run's executor shutdown
        # open after a transport/device failure. The parent owns this pipe.
        threading.Thread(target=read_commands, daemon=True).start()
        while not self.stop_event.is_set():
            if failed.is_set():
                raise ValueError("voice command queue full")
            try:
                line = lines.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            if not line:
                return
            if len(line) > 4096:
                raise ValueError("voice command exceeds limit")
            command = json.loads(line)
            if command == {"command": "stop"}:
                return
            if command == {"command": "interrupt"}:
                await self.interrupt()
            else:
                raise ValueError("unknown voice command")

    async def _shutdown(self):
        self.stop_event.set()
        # Local shutdown is first, before any network waits.
        self.audio.close()
        self.audio_open = self.speaking = False
        try:
            if self.session and not self.core_stopping:
                now = self.now()
                await self._send(VoiceStopRequest(
                    type="voice.stop.request", **self._envelope(), voice_session_id=self.session.voice_session_id,
                    session_epoch=self.session.session_epoch, reason="user", local_stopped_at=now))
        except (ConnectionClosed, OSError, TimeoutError):
            pass
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(*(t for t in self.tasks if t is not current), return_exceptions=True)
        if self.audio_ws:
            await self.audio_ws.close()
        if self.wake:
            self.wake.close()
        self._emit("state", state="stopped", media_stopped=True)
        self.metrics.update(input_name=self.audio.input_name, output_name=self.audio.output_name,
                            aec_active=self.audio.aec_active, dropped_input=self.audio.dropped_input,
                            dropped_output=self.audio.dropped_output)
        self._emit("metrics", **self.metrics)


async def main_async(config, token):
    # A broken session never silently opens the microphone or repeats paid work.
    await VoiceRuntime(config, token).run()


def main():
    # PyInstaller ignores PYTHONIOENCODING on Chinese Windows. Native IPC is
    # always UTF-8 in both directions, including device names and ASR text.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    import argparse
    from pathlib import Path
    from nexus.voice.config import load_runtime_config
    parser = argparse.ArgumentParser(description="NEXUS Windows voice runtime")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--ipc", action="store_true")
    parser.add_argument("--devices", action="store_true")
    args = parser.parse_args()
    try:
        if args.devices:
            print(json.dumps(AudioEngine.devices(), ensure_ascii=False))
            return
        if args.ipc:
            raw = sys.stdin.readline(8193)
            if len(raw) > 8192:
                raise ValueError("voice startup exceeds limit")
            value = json.loads(raw)
            from uuid import UUID
            for key in ("node_id", "request_id", "conversation_id"):
                if value.get(key):
                    value[key] = UUID(value[key])
            config = RuntimeConfig(**value, ipc=True)
        elif args.config:
            config = load_runtime_config(args.config)
        else:
            raise ValueError("--config or --ipc is required")
        asyncio.run(main_async(config, os.environ.get(config.token_env, "")))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Error diagnostics contain only a class/code, never provider contents,
        # credentials, URLs or transcripts.
        print(json.dumps({"event": "error", "code": type(exc).__name__,
                          "message": "语音连接或设备不可用，请检查设置后重新开始。"}, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
