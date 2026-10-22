"""Actual raster decoding, scoped paths and durable image attempt evidence."""

import base64
import hashlib
import io
import os
from uuid import uuid4

import pytest
from pydantic import ValidationError

Image = pytest.importorskip("PIL.Image")

from nexus.contracts.images import ImageReadArguments, ImageReadResult, MAX_IMAGE_BYTES, MAX_SOURCE_BYTES
from nexus.contracts.messages import ImageReadExecute
from nexus.contracts.tools import ImageReadCall, validate_result
from nexus.node.tools.images import read_image


@pytest.mark.parametrize("format,name", [("PNG", "test.png"), ("JPEG", "test.jpg"), ("GIF", "test.gif"), ("WEBP", "test.webp"), ("PNG", "no-extension")])
def test_real_formats_are_decoded_and_normalized(tmp_path, format, name):
    Image.new("RGB", (80, 40), "red").save(tmp_path/name, format=format)
    args = ImageReadArguments(root_id="workspace", path=name)
    result = read_image(args, tmp_path)
    assert result.source_sha256 == hashlib.sha256((tmp_path/name).read_bytes()).hexdigest()
    assert not result.downscaled and result.first_frame_only == (format in {"GIF", "WEBP"})
    raw = base64.b64decode(result.image.data_base64)
    with Image.open(io.BytesIO(raw)) as decoded:
        decoded.load()
        assert decoded.size == (80, 40)
    checked = validate_result(ImageReadCall(tool="filesystem.read_image", tool_version=1, arguments=args), result, 32768)
    assert checked == result and str(tmp_path) not in result.model_dump_json()


def test_snapshot_is_bounded_and_preserves_dimensions_and_source(tmp_path):
    image = Image.effect_noise((1500, 900), 80).convert("RGB")
    image.save(tmp_path/"noise.png")
    original = (tmp_path/"noise.png").read_bytes()
    result = read_image(ImageReadArguments(root_id="workspace", path="noise.png", max_dimension=512), tmp_path)
    assert result.downscaled and (result.original_width, result.original_height) == (1500, 900)
    assert max(result.image.width, result.image.height) <= 512 and result.image.size_bytes <= MAX_IMAGE_BYTES
    assert (tmp_path/"noise.png").read_bytes() == original


def test_alpha_first_frame_and_exif_orientation(tmp_path):
    Image.new("RGBA", (40, 20), (255, 0, 0, 0)).save(tmp_path/"alpha.png")
    result = read_image(ImageReadArguments(root_id="workspace", path="alpha.png"), tmp_path)
    with Image.open(io.BytesIO(base64.b64decode(result.image.data_base64))) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)
    first, second = Image.new("RGB", (40, 20), "red"), Image.new("RGB", (40, 20), "blue")
    first.save(tmp_path/"animation.gif", save_all=True, append_images=[second], duration=100)
    result = read_image(ImageReadArguments(root_id="workspace", path="animation.gif"), tmp_path)
    with Image.open(io.BytesIO(base64.b64decode(result.image.data_base64))) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0) and result.first_frame_only
    exif = Image.Exif(); exif[274] = 6
    first.save(tmp_path/"rotated.jpg", exif=exif)
    result = read_image(ImageReadArguments(root_id="workspace", path="rotated.jpg"), tmp_path)
    assert (result.original_width, result.original_height) == (20, 40)
    first.save(tmp_path/"animation.png",save_all=True,append_images=[second],duration=100)
    assert read_image(ImageReadArguments(root_id="workspace",path="animation.png"),tmp_path).first_frame_only


@pytest.mark.parametrize("raw", [b"", b"<svg>not raster</svg>", b"\x89PNG\r\n\x1a\ntruncated"])
def test_invalid_bytes_never_become_image_evidence(tmp_path, raw):
    (tmp_path/"bad.png").write_bytes(raw)
    with pytest.raises(ValueError):
        read_image(ImageReadArguments(root_id="workspace", path="bad.png"), tmp_path)


def test_mismatched_extension_source_size_dimensions_and_hardlink_refused(tmp_path):
    Image.new("RGB", (10, 10)).save(tmp_path/"wrong.jpg", format="PNG")
    with pytest.raises(ValueError, match="format differ"):
        read_image(ImageReadArguments(root_id="workspace", path="wrong.jpg"), tmp_path)
    with (tmp_path/"large.png").open("wb") as stream:
        stream.truncate(MAX_SOURCE_BYTES+1)
    with pytest.raises(ValueError, match="4 MiB"):
        read_image(ImageReadArguments(root_id="workspace", path="large.png"), tmp_path)
    Image.new("RGB", (8193, 1)).save(tmp_path/"wide.png")
    with pytest.raises(ValueError, match="dimensions"):
        read_image(ImageReadArguments(root_id="workspace", path="wide.png"), tmp_path)
    os.link(tmp_path/"wrong.jpg", tmp_path/"hard.png")
    with pytest.raises(PermissionError):
        read_image(ImageReadArguments(root_id="workspace", path="hard.png"), tmp_path)


@pytest.mark.parametrize("path", ["../outside.png", "C:/secret.png", "a/../secret.png", "a\\x.png", "//server/file.png", "a:stream", "CON.png"])
def test_image_paths_use_existing_scope_guard(path):
    with pytest.raises(ValidationError):
        ImageReadArguments(root_id="workspace", path=path)


@pytest.mark.parametrize("field,value", [("width", 12), ("sha256", "a"*64), ("size_bytes", 1), ("data_base64", "!!!!")])
def test_tampered_raster_evidence_is_refused(tmp_path, field, value):
    Image.new("RGB", (10, 10)).save(tmp_path/"test.png")
    result = read_image(ImageReadArguments(root_id="workspace", path="test.png"), tmp_path).model_dump(mode="json")
    result["image"][field] = value
    with pytest.raises(ValueError):
        ImageReadResult.model_validate(result)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job and receipt lifecycle")
def test_real_windows_worker_receipt_replay_and_restart(tmp_path):
    from node_support import configuration, sign
    from nexus.node.runtime import NodeRuntime
    root = tmp_path/"workspace"; root.mkdir()
    (tmp_path/"node").mkdir()
    Image.new("RGB", (32, 16), "red").save(root/"test.png")
    config = configuration(tmp_path/"node", roots={"workspace": root}, allowed_tools=frozenset({"filesystem.read_image@1"}))
    from datetime import UTC, datetime, timedelta
    command = sign(ImageReadExecute(tool="filesystem.read_image", tool_version=1,
        arguments=ImageReadArguments(root_id="workspace", path="test.png"), session_id=uuid4(), task_id=uuid4(),
        attempt_id=uuid4(), command_digest="a"*64, grant_id=uuid4(), expires_at=datetime.now(UTC)+timedelta(minutes=1), execution_timeout_seconds=10), config.node_id)
    with NodeRuntime(config) as runtime:
        status = runtime.execute(command)
        assert status.phase == "completed" and status.result.outcome == "succeeded"
        assert status.result.result.image.width == 32
        (root/"test.png").unlink()
        assert runtime.execute(command) == status
    with NodeRuntime(config) as runtime:
        runtime._worker.run = lambda *args: pytest.fail("cached image executed again")
        assert runtime.execute(command) == status
