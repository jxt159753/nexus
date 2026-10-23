"""Local standby keyword spotting. No audio leaves the device until a hit."""

from pathlib import Path

from nexus.voice.config import RuntimeConfig


class WakeUnavailable(RuntimeError):
    pass


class WakeDetector:
    def __init__(self, config: RuntimeConfig):
        try:
            import sherpa_onnx
            import numpy as np
        except ImportError as exc:
            raise WakeUnavailable("install sherpa-onnx for local wake word") from exc
        base = Path(config.wake_model_dir)
        paths = {
            "tokens": base / config.wake_tokens,
            "encoder": base / config.wake_encoder,
            "decoder": base / config.wake_decoder,
            "joiner": base / config.wake_joiner,
        }
        for name, path in paths.items():
            if not path.is_file():
                raise WakeUnavailable(f"wake {name} model missing: {path}")
        import hashlib
        import json
        lock = base.parent / "voice-models.lock.json"
        if config.ipc:
            if not lock.is_file():
                raise WakeUnavailable("wake model manifest missing")
            model = next(item for item in json.loads(lock.read_text(encoding="utf-8"))["models"] if item["kind"] == "kws")
            for item in model["files"]:
                path = base / Path(item["path"]).name
                if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                    raise WakeUnavailable("wake model digest mismatch")
        keywords = Path(config.wake_keywords)
        if not keywords.is_file():
            raise WakeUnavailable(f"wake keywords missing: {keywords}")
        self._np = np
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(paths["tokens"]),
            encoder=str(paths["encoder"]),
            decoder=str(paths["decoder"]),
            joiner=str(paths["joiner"]),
            keywords_file=str(keywords),
            num_threads=1,
            keywords_score=config.wake_keywords_score,
            keywords_threshold=config.wake_threshold,
            num_trailing_blanks=config.wake_trailing_blanks,
        )
        self._stream = self._kws.create_stream()
        self.hits: list[str] = []
        self._pushes = 0

    def push(self, pcm16: bytes) -> str | None:
        samples = self._np.frombuffer(pcm16, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        if not len(samples):
            return None
        self._stream.accept_waveform(16000, samples)
        self._pushes += 1
        if self._pushes % 4:
            return None
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            result = self._kws.get_result(self._stream)
            if result:
                self._kws.reset_stream(self._stream)
                self.hits.append(result)
                return str(result)
        return None

    def close(self) -> None:
        try:
            self._kws.reset_stream(self._stream)
        except Exception:
            pass
