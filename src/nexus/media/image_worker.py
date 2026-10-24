"""Fixed image-only helper. No Core/Node authority, files or model credentials."""

import json
import sys

from nexus.contracts.images import MAX_SOURCE_BYTES
from nexus.media.images import normalize_image


def main() -> int:
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    raw = sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)
    try:
        result = normalize_image(raw)
        print(result.model_dump_json())
    except ValueError as exc:
        code = "IMAGE_DECODER_UNAVAILABLE" if "decoder unavailable" in str(exc) else "IMAGE_PREVIEW_UNAVAILABLE"
        print(json.dumps({"error": code}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
