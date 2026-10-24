"""Bounded upload inspection. Raster decoding runs in an owned fixed process."""

import asyncio
import hashlib
import json
import os
import sys

from nexus.contracts.images import ImageSnapshot, MAX_SOURCE_BYTES


def image_media_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


async def normalize(raw: bytes) -> dict:
    environment = {key:value for key,value in os.environ.items() if key.upper() in {"SYSTEMROOT","WINDIR","TEMP","TMP","PYTHONPATH"}}
    environment["PYTHONIOENCODING"] = "utf-8"
    process = await asyncio.create_subprocess_exec(sys.executable,"-B","-m","nexus.media.image_worker",
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
        env=environment,**({"creationflags":0x08000000} if os.name == "nt" else {}))
    try:
        async with asyncio.timeout(10):
            async def write():
                try:
                    process.stdin.write(raw)
                    await process.stdin.drain()
                except (BrokenPipeError,ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
            writer = asyncio.create_task(write())
            try:
                output = bytearray()
                while chunk := await process.stdout.read(4096):
                    if len(output)+len(chunk) > 32768:
                        raise ValueError("image helper output exceeds limit")
                    output.extend(chunk)
                await process.wait()
                await writer
            finally:
                writer.cancel()
                await asyncio.gather(writer,return_exceptions=True)
        if process.returncode:
            raise ValueError("image helper failed")
        data = json.loads(output)
        if data.get("error") in {"IMAGE_DECODER_UNAVAILABLE","IMAGE_PREVIEW_UNAVAILABLE"}:
            return {"kind":"unparsed","reason":data["error"]}
        snapshot = ImageSnapshot.model_validate(data)
        if snapshot.source_sha256 != hashlib.sha256(raw).hexdigest() or snapshot.source_size_bytes != len(raw):
            raise ValueError("image helper source mismatch")
        return {"kind":"image","snapshot":snapshot.model_dump(mode="json")}
    except (ValueError,TimeoutError):
        return {"kind":"unparsed","reason":"IMAGE_PREVIEW_UNAVAILABLE"}
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def inspect(raw: bytes) -> tuple[str,dict]:
    media_type = image_media_type(raw)
    if media_type:
        if len(raw) > MAX_SOURCE_BYTES:
            return media_type,{"kind":"unparsed","reason":"IMAGE_SOURCE_LIMIT"}
        try:
            return media_type,await normalize(raw)
        except OSError:
            return media_type,{"kind":"unparsed","reason":"IMAGE_DECODER_UNAVAILABLE"}
    if raw.startswith((b"%PDF-",b"PK\x03\x04",b"{\\rtf",b"\xd0\xcf\x11\xe0")):
        return "application/octet-stream",{"kind":"unparsed","reason":"FILE_TYPE_NOT_PARSED"}
    # Do not unpack containers, execute files, parse PDF/Office or render HTML.
    if len(raw) <= 1048576:
        try:
            text = raw.decode("utf-8-sig")
            if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in text):
                raise ValueError("binary control bytes")
            encoded = text.encode()
            head = encoded[:8192].decode("utf-8",errors="ignore")
            return "text/plain",{"kind":"text","text":head,"truncated":len(encoded)>8192}
        except (UnicodeDecodeError,ValueError):
            pass
    return "application/octet-stream",{"kind":"unparsed","reason":"FILE_TYPE_NOT_PARSED"}
