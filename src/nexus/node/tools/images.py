"""Scoped workspace reads reuse the fixed byte-only image normalizer."""

import os
from pathlib import Path

from nexus.contracts.images import ImageReadResult, MAX_SOURCE_BYTES, SOURCE_MEDIA
from nexus.media.images import normalize_image
from nexus.node.tools.paths import guarded_path, read_handle

EXTENSIONS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}


def read_image(args, root):
    extension = Path(args.path).suffix.lower()
    if extension and extension not in EXTENSIONS:
        raise ValueError("read_image accepts PNG/JPEG/GIF/WebP or extensionless images")
    with guarded_path(root, args.path) as path, read_handle(path) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_SOURCE_BYTES:
            raise ValueError("image source exceeds 4 MiB")
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("image source exceeds 4 MiB")
    result = normalize_image(raw, max_dimension=args.max_dimension)
    if extension and SOURCE_MEDIA[EXTENSIONS[extension]] != result.source_media_type:
        raise ValueError("image extension and decoded format differ")
    return ImageReadResult(root_id=args.root_id, path=args.path, **result.model_dump())
