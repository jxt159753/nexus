"""Real frozen EXE regression: native imports, UTF-8 IPC and bounded failure exit."""
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from websockets.asyncio.server import serve


@pytest.mark.skipif(not os.environ.get("NEXUS_TEST_VOICE_EXE"), reason="opt-in Windows frozen binary")
@pytest.mark.parametrize("wake", [False, True])
def test_frozen_device_failure_exits_while_parent_keeps_stdin_open(wake):
    if wake and not os.environ.get("NEXUS_TEST_WAKE_MODEL_DIR"):
        pytest.skip("opt-in installed wake resources")
    async def scenario():
        async def handler(ws):
            await ws.recv()
            envelope = {"message_id": str(uuid4()), "sent_at": datetime.now(UTC).isoformat()}
            await ws.send(json.dumps({"type": "voice.welcome", **envelope,
                "connection_id": str(uuid4()), "heartbeat_interval_seconds": 1,
                "heartbeat_timeout_seconds": 5, "policy_revision": 1,
                "max_session_seconds": 60, "max_final_chars": 2000}))
            await ws.recv()
            await ws.send(json.dumps({"type": "voice.session.started", **envelope,
                "voice_session_id": str(uuid4()), "session_epoch": 1,
                "conversation_id": str(uuid4()), "mode": "push_to_talk",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}))
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            process = await asyncio.create_subprocess_exec(os.environ["NEXUS_TEST_VOICE_EXE"], "--ipc",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NEXUS_NODE_TOKEN": "local-test-token"})
            try:
                config = {"core_url": f"ws://127.0.0.1:{port}", "node_id": str(uuid4()),
                          "output_device": "不存在的语音测试设备 / deliberately absent", "aec_enabled": True}
                if wake:
                    from pathlib import Path
                    models = Path(os.environ["NEXUS_TEST_WAKE_MODEL_DIR"])
                    config.update(wake_enabled=True, request_id=str(uuid4()), conversation_id=str(uuid4()),
                                  wake_model_dir=str(models / "kws"), wake_keywords=str(models / "keywords.txt"))
                process.stdin.write((json.dumps(config, ensure_ascii=False) + "\n").encode("utf-8"))
                await process.stdin.drain()
                await asyncio.wait_for(process.wait(), 12)
                events = [json.loads(line) for line in (await process.stdout.read()).decode("utf-8").splitlines()]
                assert process.returncode == 1
                assert any(e.get("media_stopped") is True for e in events)
                assert events[-1]["code"] == "AudioUnavailable"
                assert "语音" in events[-1]["message"]
            finally:
                process.stdin.close()
                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except TimeoutError:
                        import subprocess
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                        await process.wait()
    asyncio.run(scenario())
