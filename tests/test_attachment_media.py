"""The owned decoder is stopped on timeout, cancellation and output overflow."""

import asyncio
import sys

import pytest

from nexus.core.cognitive import attachment_media, attachment_http


@pytest.mark.parametrize("reason",["timeout","cancel","overflow"])
def test_real_helper_cleanup_and_credential_free_environment(monkeypatch,reason):
    started=[]; original=asyncio.create_subprocess_exec; real_timeout=asyncio.timeout
    monkeypatch.setenv("DEEPSEEK_API_KEY","private-test-key")
    async def launch(*args,**kwargs):
        assert args[0]==sys.executable and args[-1]=="nexus.media.image_worker"
        assert "DEEPSEEK_API_KEY" not in kwargs["env"]
        source="import sys,time;sys.stdout.buffer.write(b'x'*40000);sys.stdout.flush();time.sleep(60)" if reason=="overflow" else "import time;time.sleep(60)"
        process=await original(sys.executable,"-B","-c",source,**kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(attachment_media.asyncio,"create_subprocess_exec",launch)
    if reason=="timeout":
        monkeypatch.setattr(attachment_media.asyncio,"timeout",lambda seconds:real_timeout(0.2))
    async def run():
        task=asyncio.create_task(attachment_media.normalize(b"source"))
        if reason=="cancel":
            while not started:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)["kind"]=="unparsed"
        assert len(started)==1 and started[0].returncode is not None
    asyncio.run(run())


def test_stalled_download_releases_slot_on_deadline(monkeypatch):
    original=asyncio.timeout
    monkeypatch.setattr(attachment_http.asyncio,"timeout",lambda seconds:original(0.02))
    released=[]
    response=attachment_http.TransferResponse(b"content",release=lambda:released.append(True))
    async def stalled(message):
        await asyncio.sleep(60)
    async def run():
        with pytest.raises(TimeoutError):
            await response({},None,stalled)
    asyncio.run(run())
    assert released==[True] and response.body==b""
