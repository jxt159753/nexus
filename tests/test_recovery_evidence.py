"""Clock rejection and stopped-receipt replay must not deadlock the node."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import pytest

from nexus.contracts.messages import CommandStatusResult
from nexus.contracts.protocol import parse_message
from nexus.core.app import create_app
from nexus.core.storage.control import ControlRepository
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.link import wire_message
from fastapi.testclient import TestClient
from test_harness import dispatched, ready, result_for
from test_node_link import ORIGIN, core_config, grant, handshake, headers
from test_harness_http import login, submission
from test_node_receipts import store_at
from node_support import command
from nexus.node.runtime import NodeRuntime
from nexus.contracts.errors import ErrorCode, ProtocolError
from uuid import uuid4


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("skew", [-6, 3600])
def test_clock_rejected_result_releases_only_its_slot_and_never_succeeds(world, terminal, skew):
    async def scenario():
        repo, item, _ = await dispatched(world)
        for _ in range(64):
            assert await repo.reserve_probe(item.attempt_id)
        if terminal:
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE tasks SET expires_at=? WHERE task_id=?", (timestamp(utc_now()-timedelta(seconds=1)), str(item.task_id)))
            await ControlRepository(world.db).expire()
        original = await world.tasks.get(item.task_id, world.owner_id)
        result = result_for(item, observed_at=(original.created_at+timedelta(seconds=skew)).isoformat())
        assert await repo.store_result(world.node, result) == "already_settled"
        current = await world.tasks.get(item.task_id, world.owner_id)
        assert current.status == ("timed_out" if terminal else "failed")
        assert current.execution_outcome == "unknown" and current.result is None
        assert not await repo.active(world.node_id)
        detail = await repo.detail(item.task_id, world.owner_id)
        assert detail["attempts"][0]["probe_count"] == 64
        assert detail["attempts"][0]["execution_stopped"] == 1
        assert detail["attempts"][0]["observed_at"] is None
        assert await repo.store_result(world.node, result) == "already_settled"
        assert await world.tasks.get(item.task_id, world.owner_id) == current
        async with world.db.connection() as conn:
            assert (await one(conn, "SELECT count(*) FROM audit_events WHERE kind='attempt.result_time_rejected'"))[0] == 1
        other = await ready(world)
        assert await repo.dispatch(other.task_id, world.node, world.scope, uuid4(), uuid4(), 10)
    asyncio.run(scenario())


@pytest.mark.parametrize("phase,stopped", [("unknown", True), ("unknown", False), ("cancelled", True)])
def test_stopped_receipts_replay_and_ack_without_reexecuting(tmp_path, phase, stopped):
    cfg, store = store_at(tmp_path)
    item = command(cfg)
    store.accept(item, uuid4())
    store.start(item.attempt_id, "test-job")
    store.finish(item.attempt_id, phase, stopped=stopped)
    store.initialize()
    if not stopped:
        assert not store.unacknowledged()
        with pytest.raises(ValueError):
            store.acknowledge(item.attempt_id)
        return
    assert [r.attempt_id for r in store.unacknowledged()] == [item.attempt_id]
    store.acknowledge(item.attempt_id)
    store.acknowledge(item.attempt_id)
    assert not store.unacknowledged()
    assert store.get(item.attempt_id).phase == phase


@pytest.mark.parametrize("kind", ["clock", "stopped"])
def test_real_websocket_acknowledges_stop_evidence_and_dispatches_next(world, kind):
    grant(world)
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        auth = login(client, world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            session = handshake(ws, world)
            task = client.post("/api/v1/tasks", json=submission(world), headers=auth).json()["task"]
            item = parse_message(ws.receive_text()).payload
            async def exhaust():
                async with world.db.transaction() as conn:
                    await conn.execute("UPDATE task_attempts SET probe_count=64")
                    await conn.execute("UPDATE tasks SET expires_at=? WHERE task_id=?", (timestamp(utc_now()-timedelta(seconds=1)), str(item.task_id)))
                await ControlRepository(world.db).expire()
            asyncio.run(exhaust())
            if kind == "clock":
                value = result_for(item, observed_at=(utc_now()-timedelta(hours=1)).isoformat())
                message = wire_message("command.result", value)
            else:
                value = CommandStatusResult(session_id=session, attempt_id=item.attempt_id, command_digest=item.command_digest, phase="unknown", execution_stopped=True)
                message = wire_message("command.status_result", value)
            ws.send_text(message)
            ack = parse_message(ws.receive_text())
            assert ack.type == "result.ack" and ack.payload.status == "already_settled"
            detail = client.get("/api/v1/tasks/"+task["task_id"]).json()
            assert detail["task"]["status"] == "timed_out"
            assert detail["task"]["execution_outcome"] == "unknown"
            assert detail["attempts"][0]["execution_stopped"] == 1
            next_task = client.post("/api/v1/tasks", json=submission(world), headers=auth).json()["task"]
            dispatch = parse_message(ws.receive_text())
            assert dispatch.type == "command.execute" and str(dispatch.payload.task_id) == next_task["task_id"]


def test_core_clock_deadline_does_not_use_slow_system_clock(tmp_path):
    cfg, _ = store_at(tmp_path)
    core_now = datetime.now(UTC)+timedelta(hours=1)
    with NodeRuntime(cfg) as runtime:
        runtime.use_core_clock(core_now)
        assert 0 <= (runtime._now()-core_now).total_seconds() < 2
        item = command(cfg, expires_at=core_now-timedelta(seconds=1))
        runtime._worker.run = lambda *_: pytest.fail("expired Core deadline must not execute")
        with pytest.raises(ProtocolError) as error:
            runtime.execute(item)
        assert error.value.error.code == ErrorCode.TOOL_TIMEOUT
        assert runtime.receipts.get(item.attempt_id) is None


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows managed worker")
@pytest.mark.parametrize("hours", [-1, 1])
def test_real_worker_uses_core_time_and_cached_receipt_keeps_it(tmp_path, hours):
    cfg, _ = store_at(tmp_path)
    core_now = datetime.now(UTC)+timedelta(hours=hours)
    with NodeRuntime(cfg) as runtime:
        runtime.use_core_clock(core_now)
        item = command(cfg, expires_at=core_now+timedelta(minutes=1))
        result = runtime.execute(item).result
        assert result.outcome == "succeeded"
        assert 0 <= (result.observed_at-core_now).total_seconds() < 5
        runtime.use_core_clock(core_now+timedelta(hours=3))
        assert runtime.execute(item).result.observed_at == result.observed_at
