"""Normalize raster bytes without paths, network access or execution authority."""

import base64
import hashlib
import io

from nexus.contracts.images import ImageSnapshot, MAX_IMAGE_BYTES, MAX_SOURCE_BYTES, SOURCE_MEDIA


def normalize_image(raw: bytes, *, max_dimension: int = 1024) -> ImageSnapshot:
    if not 0 < len(raw) <= MAX_SOURCE_BYTES or not 128 <= max_dimension <= 1024:
        raise ValueError("image source or dimension exceeds the snapshot limit")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        raise ValueError("image decoder unavailable; install the pinned nexus[images] extra on this host") from None
    try:
        with Image.open(io.BytesIO(raw), formats=list(SOURCE_MEDIA)) as source:
            source_format = source.format
            if max(source.size) > 8192 or source.width * source.height > 16_000_000:
                raise ValueError("image dimensions exceed 8192px or 16 million pixels")
            first_frame_only = source_format in {"GIF", "WEBP"} or bool(getattr(source, "is_animated", False))
            source.seek(0)
            source.load()  # Decode before accepting any image evidence.
            oriented = ImageOps.exif_transpose(source)
            original_width, original_height = oriented.size
            # Composite alpha explicitly; normalized snapshots contain no EXIF,
            # profiles or extra frames. Transparent areas are shown on white.
            rgba = oriented.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("invalid or unsupported image") from None
    image.thumbnail((max_dimension, max_dimension))
    for _ in range(10):
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        encoded, media_type = output.getvalue(), "image/png"
        if len(encoded) > MAX_IMAGE_BYTES:
            for quality in (80, 60, 40):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                encoded, media_type = output.getvalue(), "image/jpeg"
                if len(encoded) <= MAX_IMAGE_BYTES:
                    break
        if len(encoded) <= MAX_IMAGE_BYTES:
            return ImageSnapshot(source_sha256=hashlib.sha256(raw).hexdigest(),
                source_size_bytes=len(raw), source_media_type=SOURCE_MEDIA[source_format],
                original_width=original_width, original_height=original_height,
                image={"media_type": media_type, "width": image.width, "height": image.height,
                       "size_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
                       "data_base64": base64.b64encode(encoded).decode("ascii")},
                downscaled=image.size != (original_width, original_height),
                first_frame_only=first_frame_only)
        image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)))
    raise ValueError("image cannot fit the bounded snapshot")
