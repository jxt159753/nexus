"""Exercise close with real backpressure and reentrant protocol writes."""

import asyncio
import socket
import sys

import pytest

from nexus.core.asyncio_compat import configure_transport


@pytest.fixture
def transport_fix(monkeypatch):
    if sys.platform == "win32" or sys.version_info[:2] != (3, 12):
        pytest.skip("production Linux Python 3.12 selector transport")
    from asyncio.selector_events import _SelectorSocketTransport
    for name in ("writelines", "close", "_call_connection_lost"):
        monkeypatch.setattr(_SelectorSocketTransport, name, getattr(_SelectorSocketTransport, name))
    configure_transport()


@pytest.mark.parametrize("method", ["write", "writelines"])
def test_buffered_close_flushes_reentrant_protocol_writes(transport_fix, method):
    loop = asyncio.new_event_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    initial, trailing = b"a" * 131072, b"b" * 131072

    class BufferedProtocol(asyncio.Protocol):
        resumed = 0
        lost = 0

        def connection_made(self, transport):
            self.transport = transport

        def resume_writing(self):
            self.resumed += 1
            if self.resumed == 1:
                # SSLProtocol similarly flushes its own remaining output here.
                if method == "write":
                    self.transport.write(trailing)
                else:
                    self.transport.writelines([trailing[:65536], trailing[65536:]])

        def connection_lost(self, exc):
            self.lost += 1

    async def exercise():
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        transport = None
        try:
            transport, protocol = await loop.create_connection(BufferedProtocol, sock=left)
            transport.set_write_buffer_limits(high=1024, low=0)
            transport.write(initial)
            assert transport.get_write_buffer_size() > 0
            transport.close()
            received = bytearray()
            async with asyncio.timeout(3):
                while chunk := await loop.sock_recv(right, 65536):
                    received.extend(chunk)
            await asyncio.sleep(0)
            assert received == initial + trailing
            assert protocol.resumed >= 1 and protocol.lost == 1
            assert left.fileno() == -1
            assert transport._write_ready is None
            assert not errors
        finally:
            if transport is not None and left.fileno() != -1:
                transport.abort()
                await asyncio.sleep(0)
            left.close()
            right.close()

    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()


def test_writelines_after_close_does_not_register_writer(transport_fix):
    async def exercise():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        transport, _ = await loop.create_connection(asyncio.Protocol, sock=left)
        try:
            transport.writelines([b"first", b"second"])
            assert await loop.sock_recv(right, 11) == b"firstsecond"
            transport.close()
            transport.writelines([b"after-close"])
            await asyncio.sleep(0)
            assert left.fileno() == -1
            assert not errors
        finally:
            transport.abort()
            right.close()
    asyncio.run(exercise())


def test_transport_setup_is_idempotent(transport_fix):
    from asyncio.selector_events import _SelectorSocketTransport
    before = (_SelectorSocketTransport.close, _SelectorSocketTransport._call_connection_lost)
    configure_transport()
    assert before == (_SelectorSocketTransport.close, _SelectorSocketTransport._call_connection_lost)


def test_windows_transport_is_unchanged(monkeypatch):
    from asyncio.selector_events import _SelectorSocketTransport
    before = (_SelectorSocketTransport.close, _SelectorSocketTransport.writelines)
    monkeypatch.setattr(sys, "platform", "win32")
    configure_transport()
    assert before == (_SelectorSocketTransport.close, _SelectorSocketTransport.writelines)
