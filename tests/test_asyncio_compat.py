"""Real selector sockets cover the production Python 3.12 close/write defect."""
import asyncio
import socket
import sys

import pytest

from nexus.core.asyncio_compat import configure_transport


@pytest.mark.skipif(sys.platform=='win32' or sys.version_info[:2]!=(3,12),reason='Linux Python 3.12 transport')
def test_writelines_after_close_does_not_register_none_writer(monkeypatch):
    from asyncio.selector_events import _SelectorSocketTransport
    monkeypatch.setattr(_SelectorSocketTransport,'writelines',_SelectorSocketTransport.writelines)
    configure_transport()
    loop=asyncio.new_event_loop()
    errors=[]
    loop.set_exception_handler(lambda loop,context:errors.append(context))
    async def run():
        left,right=socket.socketpair()
        left.setblocking(False);right.setblocking(False)
        transport=None
        try:
            transport,_=await loop.create_connection(asyncio.Protocol,sock=left)
            transport.writelines([b'first',b'second'])
            assert await loop.sock_recv(right,11)==b'firstsecond'
            transport.close()
            transport.writelines([b'after-close'])
            await asyncio.sleep(.02)
            assert not errors
            assert left.fileno()==-1
        finally:
            if transport:transport.abort()
            right.close()
    try:loop.run_until_complete(run())
    finally:loop.close()
