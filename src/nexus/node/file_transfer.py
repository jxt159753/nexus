"""Authenticated, bounded staging in the trusted parent; no token reaches workers."""

import hashlib
import os
import ssl
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler

from nexus.node.process import ExecutionStopped
from nexus.node.tools.paths import guarded_path
from nexus.plugins.transfer import NoRedirect


class DownloadFailed(Exception):
    """Staging failed before the file-writing worker was started."""


def staging_name(attempt_id):
    return f'receive-{attempt_id}.tmp'


def downloader(config, token):
    endpoint = urlsplit(config.core_url)
    base = urlunsplit(('https' if endpoint.scheme == 'wss' else 'http',endpoint.netloc,'','',''))
    context = ssl.create_default_context(cafile=str(config.ca_file) if config.ca_file else None)
    opener = build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=context))

    @contextmanager
    def stage(command, deadline, cancel):
        def check():
            if cancel.is_set():
                raise ExecutionStopped('cancelled')
            if time.monotonic() >= deadline:
                raise ExecutionStopped('timeout')
        check()
        request = Request(f'{base}/api/v1/file-transfers/{command.attempt_id}/content',headers={'Authorization':'Bearer '+token.get_secret_value(),'Accept-Encoding':'identity'})
        with guarded_path(config.data_dir,staging_name(command.attempt_id)) as path:
            # The attempt receipt prevents retry after a crash. Never replace an old staging file.
            with path.open('xb') as stream:
                try:
                    digest, total = hashlib.sha256(), 0
                    with opener.open(request,timeout=max(.1,min(2,deadline-time.monotonic()))) as response:
                        if (response.headers.get('X-Content-SHA256') != command.arguments.sha256
                                or response.headers.get('Content-Encoding','identity') != 'identity'):
                            raise ValueError('Unexpected file response')
                        while True:
                            check()
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > command.arguments.size_bytes:
                                raise ValueError('File exceeds declared size')
                            stream.write(chunk)
                            digest.update(chunk)
                    check()
                    if total != command.arguments.size_bytes or digest.hexdigest() != command.arguments.sha256:
                        raise ValueError('File integrity mismatch')
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException as exc:
                    stream.close()
                    path.unlink()
                    if isinstance(exc, (OSError, ValueError)):
                        raise DownloadFailed() from exc
                    raise
            try:
                yield
            finally:
                path.unlink(missing_ok=True)
    return stage
