"""Process-local workaround for CPython 3.12's closed writelines transport.

CPython gh-136234: writelines() bypasses write()'s connection-loss check and
can register a None writer after close(), spinning the selector indefinitely.
Use the public Transport fallback until Core runs on a fixed Python version;
do not modify the host interpreter or suppress event-loop exceptions.
"""

import asyncio
import sys


def configure_transport():
    if sys.platform == 'win32' or sys.version_info[:2] != (3, 12):
        return
    from asyncio.selector_events import _SelectorSocketTransport
    _SelectorSocketTransport.writelines = asyncio.Transport.writelines
