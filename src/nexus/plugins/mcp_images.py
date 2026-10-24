"""Move MCP image bytes outside the small execution-result envelope."""

import base64
import hashlib
from pathlib import Path

IMAGE_TYPES = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_TOTAL = 8 * 1024 * 1024
# Base64 expands the existing 8 MiB artifact batch; metadata still uses 24 KiB.
MAX_MESSAGE_BYTES = 12 * 1024 * 1024


def extract(value, directory):
    result = value.get('result', {})
    if not isinstance(result, dict) or not isinstance(result.get('content'), list):
        return value
    files = []
    total = 0
    for index, item in enumerate(result['content']):
        if not isinstance(item, dict) or item.get('type') != 'image':
            continue
        media = item.get('mimeType')
        if media not in IMAGE_TYPES or len(files) >= 4:
            raise ValueError('unsupported MCP image type or artifact batch size')
        raw = base64.b64decode(item['data'], validate=True)
        total += len(raw)
        if not 0 < len(raw) <= MAX_IMAGE_BYTES or total > MAX_IMAGE_TOTAL:
            raise ValueError('MCP image exceeds artifact byte capacity')
        name = f'mcp-image-{len(files)+1}.{IMAGE_TYPES[media]}'
        (Path(directory) / name).write_bytes(raw)
        files.append({'name': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'size_bytes': len(raw), 'mime_type': media})
        result['content'][index] = {'type': 'text', 'text':
            f'Image bytes are in files[{len(files)-1}] ({media}); this text is not visual evidence.'}
    if files:
        value['files'] = files
    return value


def upload(runtime, value, directory, deadline, cancel):
    import time
    from nexus.node.process import ExecutionStopped
    from nexus.node.tools.paths import guarded_path, read_handle

    files = value.get('files', [])
    if not files:
        return value
    if not runtime.uploader or not getattr(runtime.invocation, 'identifier', None):
        raise ValueError('MCP images require authenticated artifact transfer')
    total = 0
    for index, item in enumerate(files):
        if cancel.is_set():
            raise ExecutionStopped('cancelled')
        if time.monotonic() >= deadline:
            raise ExecutionStopped('timeout')
        with guarded_path(Path(directory), item['name']) as path, read_handle(path) as stream:
            raw = stream.read(MAX_IMAGE_BYTES + 1)
        total += len(raw)
        if (index >= 4 or len(raw) > MAX_IMAGE_BYTES or total > MAX_IMAGE_TOTAL
                or len(raw) != item['size_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']):
            raise ValueError('MCP image spool evidence mismatch')
        files[index] = runtime.uploader(runtime.invocation.identifier, index, raw)
    return value
