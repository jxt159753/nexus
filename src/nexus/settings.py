
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int


def load_settings() -> Settings:
    data_dir_raw = os.environ.get("NEXUS_DATA_DIR")
    if not data_dir_raw:
        raise ValueError("NEXUS_DATA_DIR is required")

    data_dir = Path(data_dir_raw).expanduser()

    if not data_dir.is_absolute():
        raise ValueError("NEXUS_DATA_DIR must be an absolute path")

    host = os.environ.get("NEXUS_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        raise ValueError("S1 only permits localhost binding")

    port_raw = os.environ.get("NEXUS_PORT", "8000")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError("NEXUS_PORT must be an integer") from exc

    if not 1 <= port <= 65535:
        raise ValueError("NEXUS_PORT must be between 1 and 65535")

    return Settings(
        data_dir=data_dir,
        host=host,
        port=port,
    )
