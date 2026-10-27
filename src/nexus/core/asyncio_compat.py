"""Process-local fixes for the deployed CPython 3.12 selector transport.

gh-115514 clears the writer before buffered TLS shutdown has finished;
gh-136234 lets writelines bypass the connection-loss check. Keep the writer
until connection_lost and use Transport's checked write fallback. The host
interpreter and event-loop exception reporting remain unchanged.
"""

import asyncio
import sys


def configure_transport() -> None:
    if sys.platform == "win32" or sys.version_info[:2] != (3, 12):
        return
    from asyncio.selector_events import _SelectorSocketTransport, _SelectorTransport

    _SelectorSocketTransport.writelines = asyncio.Transport.writelines
    # Detect the affected implementation, including distro backports. This
    # also makes repeated setup harmless without wrapping teardown twice.
    if "_write_ready" not in _SelectorSocketTransport.close.__code__.co_names:
        return
    original_connection_lost = _SelectorSocketTransport._call_connection_lost

    def close(self):
        self._read_ready_cb = None
        _SelectorTransport.close(self)

    def connection_lost(self, exc):
        try:
            original_connection_lost(self, exc)
        finally:
            self._write_ready = None

    _SelectorSocketTransport.close = close
    _SelectorSocketTransport._call_connection_lost = connection_lost
