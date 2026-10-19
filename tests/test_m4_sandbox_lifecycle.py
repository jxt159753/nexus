"""Controlled transport/process probes; these do not establish runsc isolation."""

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from nexus.contracts.sandbox import SandboxResult, parse_sandbox_message
from nexus.core.link.sandbox import SandboxConnection
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.security.types import AccessDenied
from nexus.sandbox import config as config_module, runtime as runtime_module, node as node_module
from nexus.sandbox.config import SandboxConfig
from nexus.sandbox.node import Receipts, SandboxNode
from pydantic import SecretStr
from nexus.sandbox.runtime import MAX_STDOUT, RunscRuntime
from test_extensions_m4 import FakeSandbox, app_for, generate, run_approved
from test_m4_regressions import approved_skill
from test_sandbox_m4 import hello


@pytest.mark.parametrize("mode", ["overflow", "stderr", "cancel", "timeout", "normal"])
def test_runtime_capture_bounds_and_stops_owned_process(tmp_path, monkeypatch, mode):
    # Fixed trusted probes only. Candidate Tool code is never executed here.
    sources = {
        "overflow": "import sys; sys.stdout.buffer.write(b'x'*1000000); sys.stdout.flush()",
        "stderr": "import sys; sys.stderr.buffer.write(b'x'*1000000); sys.stderr.flush()",
        "cancel": "import time; time.sleep(30)",
        "timeout": "import time; time.sleep(30)",
        "normal": "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
    }
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", sources[mode]],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               bufsize=0, start_new_session=os.name != "nt", cwd=tmp_path)
    if os.name == "nt":
        monkeypatch.setattr(runtime_module.os, "killpg", lambda pid, sig: process.kill(), raising=False)
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    cancel = threading.Event()
    if mode == "cancel":
        cancel.set()
    cleaned = []
    try:
        output, error, stopped = RunscRuntime._exchange(process, b'{"ok":true}', 2, cancel,
                                                       lambda: cleaned.append(process.poll()) is None)
        assert process.poll() is not None
        assert cleaned and cleaned[0] is not None
        assert stopped and len(output) <= MAX_STDOUT
        assert error == {"overflow": "SANDBOX_OUTPUT_LIMIT", "stderr": "SANDBOX_OUTPUT_LIMIT", "cancel": "SANDBOX_CANCELLED", "timeout": "SANDBOX_TIMEOUT", "normal": None}[mode]
        if mode == "normal":
            assert output == b'{"ok":true}'
        assert list(tmp_path.iterdir()) == []  # No stdout/stderr spool files.
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


def test_sandbox_plaintext_requires_explicit_literal_loopback(tmp_path, monkeypatch):
    # Exercise URL validation without pretending this Windows machine is a sandbox.
    monkeypatch.setattr(config_module, "os", SimpleNamespace(name="posix"))
    values = dict(node_id=uuid4(), data_dir=tmp_path / "data", staging_dir=tmp_path / "stage",
                  runsc_path=tmp_path / "runsc", runsc_root=tmp_path / "state",
                  image_rootfs=tmp_path / "rootfs", image_digest="a" * 64)
    for url, flag in [("ws://remote.invalid/ws/v2/sandbox", False),
                      ("ws://remote.invalid/ws/v2/sandbox", True),
                      ("ws://127.0.0.1/ws/v2/sandbox", False),
                      ("ws://localhost/ws/v2/sandbox", True),
                      ("wss://user:secret@remote.invalid/ws/v2/sandbox", False)]:
        with pytest.raises(ValueError):
            SandboxConfig(**values, core_url=url, local_http=flag)
    assert SandboxConfig(**values, core_url="ws://127.0.0.1/ws/v2/sandbox", local_http=True)
    assert SandboxConfig(**values, core_url="wss://remote.invalid/ws/v2/sandbox")


def test_unknown_receipt_never_confirms_stop_or_reexecutes(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    execution = uuid4()
    receipts = Receipts(path)
    assert receipts.accept(str(execution), "a" * 64, "b" * 64) is None
    receipts.close()
    receipts = Receipts(path)
    try:
        assert receipts.unsettled()
        assert not receipts.stopped(execution)
        assert not receipts.stopped(uuid4())
        assert receipts.accept(str(execution), "a" * 64, "b" * 64)[3] == "unknown"
        with pytest.raises(ValueError):
            receipts.accept(str(execution), "c" * 64, "b" * 64)
    finally:
        receipts.close()


@pytest.mark.parametrize("reason", ["cancel_all", "disable"])
def test_tool_stop_signal_reaches_transport_before_completion(world, reason):
    async def run():
        app = app_for(world, sandbox=True)
        skill = await approved_skill(app, world)
        root = await app.state.skills.start(world.owner, UUID(skill["version_id"]), world.node_id, {})
        app.state.extensions.sandbox.transport = FakeSandbox()
        tool = await run_approved(app, world, await generate(app, world, "tool", "统计", node_id=world.node_id))
        await app.state.extensions.enable(world.owner, UUID(tool["version_id"]))
        async with world.db.transaction(write=False) as conn:
            version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (tool["version_id"],))
        entered, stopped = asyncio.Event(), asyncio.Event()
        class Transport:
            async def execute(self, *args, cancel_event, permitted, **kwargs):
                entered.set()
                await asyncio.wait_for(cancel_event.wait(), 2)
                stopped.set()
                return {"outcome": "failed", "error_code": "SANDBOX_CANCELLED", "stopped": True}
        controller = app.state.extensions.sandbox
        controller.transport = Transport()
        pending = asyncio.create_task(controller._call(world.node_id, version, {}, 2, skill_run_id=root["run_id"]))
        await asyncio.wait_for(entered.wait(), 2)
        if reason == "cancel_all":
            await app.state.harness.control(world.owner, "cancel_all", uuid4())
        else:
            await app.state.extensions.disable(world.owner, UUID(skill["version_id"]))
        reply = await asyncio.wait_for(pending, 3)
        assert stopped.is_set() and reply["error_code"] == "SANDBOX_CANCELLED"
    asyncio.run(run())


@pytest.mark.parametrize("stopped", [False, True])
def test_link_requires_stop_evidence_and_preserves_execution_identity(world, stopped):
    async def run():
        app = app_for(world, sandbox=True)
        link = app.state.sandbox_link
        execution_id = uuid4()
        class WebSocket:
            async def send_text(self, raw):
                message = parse_sandbox_message(raw)
                assert message.type == "sandbox.execute"
                assert message.payload.execution_id == execution_id
                connection.pending[execution_id].set_result(SandboxResult(
                    session_id=connection.session_id, execution_id=execution_id, artifact_digest="b" * 64,
                    outcome="failed", error_code="PROBE", stopped=stopped))
        connection = SandboxConnection(WebSocket(), world.node, hello(node_id=world.node_id))
        link.connections[world.node_id] = connection
        version = {"version_id": str(uuid4()), "artifact_digest": "b" * 64,
                   "environment_digest": "c" * 64, "release_json": "{}",
                   "environment_json": json.dumps({"image_digest": "a" * 64})}
        reply = await link.execute(world.node_id, version, {}, 1, execution_id=execution_id)
        assert reply["outcome"] == ("failed" if stopped else "unknown")
        assert link.status(world.node_id)["available"] is stopped
        assert not connection.pending
    asyncio.run(run())


def test_tool_recovery_does_not_replay_dispatching_records(world):
    async def run():
        app = app_for(world, sandbox=True)
        skill = await approved_skill(app, world)
        root = await app.state.skills.start(world.owner, UUID(skill["version_id"]), world.node_id, {})
        app.state.extensions.sandbox.transport = FakeSandbox()
        tool = await run_approved(app, world, await generate(app, world, "tool", "统计", node_id=world.node_id))
        execution = str(uuid4())
        now = timestamp(utc_now())
        async with world.db.transaction() as conn:
            await conn.execute("INSERT INTO extension_tool_runs(tool_run_id,owner_id,version_id,approval_id,node_id,skill_run_id,step_index,artifact_digest,environment_digest,input_json,input_digest,status,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,'{}',?,'dispatching',?,?)", (
                execution, str(world.owner_id), tool["version_id"], tool["approval"]["approval_id"],
                str(world.node_id), root["run_id"], tool["artifact_digest"], tool["environment_digest"], "d" * 64, now, now))
        class NeverExecute:
            async def execute(self, *args, **kwargs):
                pytest.fail("Recovery replayed a possibly started execution")
        app.state.extensions.sandbox.transport = NeverExecute()
        await app.state.extensions.sandbox.recover()
        await app.state.extensions.sandbox.recover()
        async with world.db.transaction(write=False) as conn:
            row = await one(conn, "SELECT * FROM extension_tool_runs WHERE tool_run_id=?", (execution,))
            assert row["status"] == "unknown"
            assert json.loads(row["error_json"])["code"] == "CORE_RESTARTED_REQUIRES_RECONCILIATION"
            assert row["artifact_digest"] == tool["artifact_digest"]
            assert (await one(conn, "SELECT count(*) FROM extension_tool_runs"))[0] == 1
    asyncio.run(run())


def test_disable_serializes_with_final_sandbox_wire_dispatch(world):
    async def run():
        app = app_for(world, sandbox=True)
        app.state.extensions.sandbox.transport = FakeSandbox()
        tool = await run_approved(app, world, await generate(app, world, "tool", "统计", node_id=world.node_id))
        await app.state.extensions.enable(world.owner, UUID(tool["version_id"]))
        async with world.db.transaction(write=False) as conn:
            version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (tool["version_id"],))
        class NeverSend:
            async def send_text(self, raw):
                pytest.fail("Disabled version reached the wire")
        link = app.state.sandbox_link
        link.connections[world.node_id] = SandboxConnection(NeverSend(), world.node, hello(node_id=world.node_id))
        async def permitted():
            async with world.db.transaction(write=False) as conn:
                current = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (tool["version_id"],))
                try:
                    await app.state.extensions.current_approval(conn, current, utc_now())
                    return True
                except AccessDenied:
                    return False
        async with app.state.harness.operation:
            disabling = asyncio.create_task(app.state.extensions.disable(world.owner, UUID(tool["version_id"])))
            await asyncio.sleep(.02)
            dispatch = asyncio.create_task(link.execute(world.node_id, version, {}, 1, permitted=permitted))
            await asyncio.sleep(.02)
            assert not dispatch.done() and not disabling.done()
        await disabling
        reply = await dispatch
        assert reply["error_code"] == "SANDBOX_DISPATCH_DENIED"
    asyncio.run(run())


def test_node_shutdown_keeps_data_lock_until_owned_connection_cleanup(monkeypatch):
    async def run():
        node = object.__new__(SandboxNode)
        node.config = SimpleNamespace(core_url="wss://example.invalid/ws/v2/sandbox")
        node._token = SecretStr("test-only")
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closed = []
        class Connector:
            async def __aenter__(self):
                return object()
            async def __aexit__(self, *args):
                pass
        monkeypatch.setattr(node_module, "connect", lambda *args, **kwargs: Connector())
        async def connected(ws):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        node._connected = connected
        def close():
            assert release.is_set()
            closed.append(True)
        node.receipts = node.lock = SimpleNamespace(close=close)
        running = asyncio.create_task(node.run(asyncio.Event()))
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            assert not running.done() and not closed
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
        assert len(closed) == 2
    asyncio.run(run())


def test_skill_task_and_step_association_rollback_together(world):
    async def run():
        app = app_for(world)
        version = await approved_skill(app, world)
        root = await app.state.skills.start(world.owner, UUID(version["version_id"]), world.node_id, {})
        async with world.db.transaction() as conn:
            run_row = await one(conn, "SELECT * FROM skill_runs WHERE run_id=?", (root["run_id"],))
            step = await one(conn, "SELECT * FROM skill_run_steps WHERE run_id=? AND step_index=1", (root["run_id"],))
            await conn.execute("CREATE TRIGGER reject_step_link BEFORE UPDATE OF task_id ON skill_run_steps WHEN NEW.task_id IS NOT NULL BEGIN SELECT RAISE(ABORT, 'controlled link failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="controlled link failure"):
            await app.state.skills._step_task(run_row, step, {"tool": "system.info"}, {})
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM tasks"))[0] == 0
            assert (await one(conn, "SELECT count(*) FROM routing_decisions"))[0] == 0
            assert (await one(conn, "SELECT task_id FROM skill_run_steps WHERE run_id=? AND step_index=1", (root["run_id"],)))[0] is None
    asyncio.run(run())
