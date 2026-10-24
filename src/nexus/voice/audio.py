"""Bounded native audio I/O with AEC using the samples actually rendered."""

import queue
import threading
import time

from nexus.voice.config import RuntimeConfig


class AudioUnavailable(RuntimeError):
    pass


class AudioEngine:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self._in = queue.Queue(maxsize=100)
        self._far = queue.Queue(maxsize=100)
        self._out = bytearray()
        self._streams = []
        self._output_stream = None
        self._resample_in = self._resample_out = self._aec = None
        self._uplink = bytearray()
        self._closed = False
        self._condition = threading.Condition()
        self._epoch = 0
        self._drain_after = 0.0
        self.failure = None
        self.dropped_input = self.dropped_output = 0
        self.aec_active = False
        self.speech_probability = self.near_rms = 0.0
        self.speech_ms = 0
        self.input_name = self.output_name = ""
        self._default_input = self._default_output = None
        self._endpoint_ids = None

    @staticmethod
    def devices():
        import sounddevice as sd
        apis = sd.query_hostapis()
        return [{"index": i, "name": d["name"], "hostapi": apis[d["hostapi"]]["name"],
                 "input": d["max_input_channels"] > 0, "output": d["max_output_channels"] > 0}
                for i, d in enumerate(sd.query_devices())]

    def _device(self, sd, value, kind):
        if value is None or value == "":
            return None
        if isinstance(value, int):
            device = sd.query_devices(value)
            if device[f"max_{kind}_channels"] < 1:
                raise AudioUnavailable("selected audio device has the wrong direction")
            return value
        candidates = []
        apis = sd.query_hostapis()
        for index, device in enumerate(sd.query_devices()):
            if str(value).lower() in device["name"].lower() and device[f"max_{kind}_channels"] > 0:
                candidates.append((apis[device["hostapi"]]["name"] != "Windows WASAPI", index))
        if candidates:
            return min(candidates)[1]
        if self.config.allow_device_fallback:
            return None
        raise AudioUnavailable(f"configured {kind} device not found")

    def prepare(self):
        if hasattr(self, "_sd"):
            return
        try:
            import numpy as np
            import samplerate
            import sounddevice as sd
        except ImportError as exc:
            raise AudioUnavailable("voice dependencies are not installed") from exc
        self._np, self._sd = np, sd
        self._closed = False
        self._resample_in = samplerate.Resampler("sinc_fastest", channels=1)
        self._resample_out = samplerate.Resampler("sinc_fastest", channels=1)
        self._default_input, self._default_output = tuple(sd.default.device)
        from nexus.voice.devices import default_endpoint_ids
        self._endpoint_ids = default_endpoint_ids()
        if self.config.aec_enabled:
            try:
                from pywebrtc_audio import AudioProcessor
                self._aec = AudioProcessor(sample_rate=self.config.capture_rate, echo_cancellation=True,
                                           noise_suppression=True, auto_gain_control=False, stream_delay_ms=40)
                self.aec_active = True
            except (ImportError, RuntimeError, ValueError):
                self._aec, self.aec_active = None, False

    def open(self, with_output=True):
        self.prepare()
        self._closed = False
        sd = self._sd
        try:
            index = self._device(sd, self.config.input_device, "input")
            inp = sd.RawInputStream(device=index, samplerate=self.config.capture_rate, channels=1,
                                    dtype="int16", blocksize=self.config.capture_rate // 50,
                                    callback=self._input_cb, latency="low")
            self._streams.append(inp)
            inp.start()
            self.input_name = sd.query_devices(index, "input")["name"]
            if with_output:
                self.open_output()
        except Exception as exc:
            self.close()
            raise AudioUnavailable(f"microphone unavailable ({type(exc).__name__})") from exc

    def open_output(self):
        self.prepare()
        if self._output_stream:
            return
        try:
            index = self._device(self._sd, self.config.output_device, "output")
            out = self._sd.RawOutputStream(device=index, samplerate=self.config.output_rate, channels=1,
                                           dtype="int16", blocksize=self.config.output_rate // 50,
                                           callback=self._output_cb, latency="low")
            self._streams.append(out)
            out.start()
            self._output_stream = out
            self.output_name = self._sd.query_devices(index, "output")["name"]
        except Exception as exc:
            raise AudioUnavailable(f"speaker unavailable ({type(exc).__name__})") from exc

    def _input_cb(self, indata, frames, time_info, status):
        if self._closed:
            return
        if status and getattr(status, "input_overflow", False):
            self.failure = "microphone overflow"
        try:
            self._in.put_nowait(bytes(indata))
        except queue.Full:
            self.dropped_input += 1
            self.failure = "microphone queue full"

    def _output_cb(self, outdata, frames, time_info, status):
        need = len(outdata)
        with self._condition:
            count = min(need, len(self._out))
            rendered = bytes(self._out[:count]) + b"\x00" * (need - count)
            del self._out[:count]
            outdata[:] = rendered
            if count:
                latency = float(getattr(self._output_stream, "latency", 0.02) or 0.02)
                self._drain_after = time.monotonic() + latency + 0.02
            self._condition.notify_all()
        # Reference enters the AEC only when those samples reach the renderer,
        # rather than seconds earlier while synthesis fills its playback queue.
        try:
            self._far.put_nowait(rendered)
        except queue.Full:
            try:
                self._far.get_nowait()
                self._far.put_nowait(rendered)
            except (queue.Empty, queue.Full):
                pass

    def read_uplink(self):
        target = self.config.uplink_rate * self.config.uplink_batch_ms // 1000 * 2
        while len(self._uplink) < target:
            if self._closed or self.failure:
                raise AudioUnavailable(self.failure or "audio engine is closed")
            try:
                frame = self._in.get(timeout=0.2)
            except queue.Empty:
                if any(not stream.active for stream in self._streams):
                    raise AudioUnavailable("audio device disconnected")
                continue
            near = self._np.frombuffer(frame, dtype=self._np.int16)
            if self._aec is not None:
                try:
                    far = self._np.frombuffer(self._far.get_nowait(), dtype=self._np.int16)
                except queue.Empty:
                    far = self._np.zeros_like(near)
                if len(far) != len(near):
                    far = self._np.zeros_like(near)
                try:
                    near = self._aec.process(near, far)
                    self.speech_probability = float(self._aec.speech_probability)
                except (RuntimeError, ValueError):
                    self._aec, self.aec_active = None, False
                    self.speech_probability = 0.0
            mono = near.astype(self._np.float32) / 32768.0
            self.near_rms = float(self._np.sqrt(self._np.mean(mono * mono))) if len(mono) else 0.0
            if self.speech_probability >= 0.8 and self.near_rms >= 0.008:
                self.speech_ms += 20
            else:
                self.speech_ms = 0
            converted = self._resample_in.process(mono, self.config.uplink_rate / self.config.capture_rate)
            self._uplink.extend((self._np.clip(converted, -1, 1) * 32767).astype(self._np.int16).tobytes())
        result = bytes(self._uplink[:target])
        del self._uplink[:target]
        return result

    def ready_cue(self):
        """Short local acknowledgement; no TTS request or model call."""
        rate = self.config.downlink_rate
        count = int(rate * 0.075)
        positions = self._np.arange(count) / rate
        envelope = self._np.sin(self._np.pi * self._np.arange(count) / count) ** 2
        tones = [0.08 * envelope * self._np.sin(2 * self._np.pi * hz * positions) for hz in (660, 880)]
        samples = self._np.concatenate((tones[0], self._np.zeros(int(rate * 0.025)), tones[1]))
        self.play((samples * 32767).astype(self._np.int16).tobytes())

    def play(self, pcm):
        if self._closed or not pcm:
            return
        if len(pcm) % 2:
            raise AudioUnavailable("unaligned PCM")
        epoch = self._epoch
        mono = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        converted = self._resample_out.process(mono, self.config.output_rate / self.config.downlink_rate)
        data = (self._np.clip(converted, -1, 1) * 32767).astype(self._np.int16).tobytes()
        ceiling = self.config.output_rate * 2 * 2
        # Producer backpressure preserves every sample and its original order.
        for start in range(0, len(data), 4096):
            chunk = data[start:start + 4096]
            with self._condition:
                while len(self._out) + len(chunk) > ceiling and not self._closed and epoch == self._epoch:
                    self._condition.wait(0.1)
                if self._closed or epoch != self._epoch:
                    return
                self._out.extend(chunk)

    def drain(self):
        with self._condition:
            epoch = self._epoch
            while not self._closed and epoch == self._epoch:
                if not self._out and time.monotonic() >= self._drain_after:
                    return True
                self._condition.wait(0.02)
        return False

    def flush(self):
        with self._condition:
            self._epoch += 1
            self._out.clear()
            self._drain_after = 0
            self._condition.notify_all()
        self.speech_ms = 0

    def devices_changed(self):
        current = tuple(self._sd.default.device)
        from nexus.voice.devices import default_endpoint_ids
        endpoints = default_endpoint_ids()
        if self._endpoint_ids is not None and endpoints is not None:
            if (self.config.input_device in {None, ""} and endpoints[0] != self._endpoint_ids[0] or
                    self.config.output_device in {None, ""} and endpoints[1] != self._endpoint_ids[1]):
                return True
        return (self.config.input_device in {None, ""} and current[0] != self._default_input or
                self.config.output_device in {None, ""} and current[1] != self._default_output or
                any(not stream.active for stream in self._streams))

    def close(self):
        self._closed = True
        self.flush()
        for stream in self._streams:
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass
        self._streams.clear()
        self._output_stream = None
