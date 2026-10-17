import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.node.receipts import ReceiptStore
from nexus.node.runtime import NodeRuntime
from node_support import command, configuration, sign


def store_at(tmp_path, *, max_receipts=10000):
    cfg = configuration(tmp_path)
    store = ReceiptStore(tmp_path / "receipts.sqlite3", cfg.node_id, max_receipts)
    store.initialize()
    return cfg, store


def test_durable_receive_start_result_ack_and_session_rebinding(tmp_path):
    cfg, store = store_at(tmp_path)
    item = command(cfg)
    boot = uuid4()
    received = store.accept(item, boot)
    assert received.phase == "received" and received.execution_stopped
    assert store.accept(item, uuid4()) == received
    store.start(item.attempt_id, "test-job")
    assert store.get(item.attempt_id).phase == "started"
    result = NodeRuntime._failure(item, ErrorCode.RESULT_INVALID, "Test error")
    store.finish(item.attempt_id, "completed", result)
    reopened = ReceiptStore(store.path, cfg.node_id)
    reopened.initialize()
    session = uuid4()
    status = reopened.get(item.attempt_id).status(session)
    assert status.phase == "completed" and status.result.session_id == session
    assert status.result.observed_at == result.observed_at
    assert status.result.attempt_id == item.attempt_id
    assert len(reopened.unacknowledged()) == 1
    reopened.acknowledge(item.attempt_id)
    reopened.acknowledge(item.attempt_id)
    assert not reopened.unacknowledged()
    assert reopened.get(item.attempt_id).result_json is not None


@pytest.mark.parametrize("field,value", [
    ("task_id", uuid4()), ("grant_id", uuid4()), ("execution_timeout_seconds", 1),
])
def test_conflicting_immutable_metadata_rejected(tmp_path, field, value):
    cfg, store = store_at(tmp_path)
    item = command(cfg)
    store.accept(item, uuid4())
    changed = sign(item.model_copy(update={field: value}), cfg.node_id)
    with pytest.raises(ProtocolError) as exc:
        store.accept(changed, uuid4())
    assert exc.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
    changed = item.model_copy(update={"session_id": uuid4()})
    assert store.accept(changed, uuid4()).attempt_id == item.attempt_id


def test_capacity_blocks_new_attempts_preserves_replays(tmp_path):
    cfg, store = store_at(tmp_path, max_receipts=1)
    item = command(cfg)
    store.accept(item, uuid4())
    with pytest.raises(ProtocolError) as exc:
        store.accept(command(cfg), uuid4())
    assert exc.value.error.code == ErrorCode.BUSY
    assert store.accept(item, uuid4()).phase == "received"


@pytest.mark.parametrize("kind", ["unversioned", "version", "checksum", "identity"])
def test_receipt_database_incompatibility_preserves_records(tmp_path, kind):
    cfg, store = store_at(tmp_path)
    item = command(cfg)
    store.accept(item, uuid4())
    with closing(sqlite3.connect(store.path)) as conn, conn:
        if kind == "unversioned":
            conn.execute("DROP TABLE schema_migrations")
        elif kind == "version":
            conn.execute("UPDATE schema_migrations SET version=2")
        elif kind == "checksum":
            conn.execute("UPDATE schema_migrations SET checksum='tampered'")
        else:
            conn.execute("UPDATE node_identity SET node_id=?", (str(uuid4()),))
    with pytest.raises(RuntimeError):
        store.initialize()
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT count(*) FROM execution_receipts").fetchone()[0] == 1


def test_invalid_finish_and_identity_are_rejected(tmp_path):
    cfg, store = store_at(tmp_path)
    item = command(cfg)
    store.accept(item, uuid4())
    with pytest.raises(ValueError):
        store.finish(item.attempt_id, "completed")
    with pytest.raises(ValueError):
        store.finish(item.attempt_id, "completed", NodeRuntime._failure(command(cfg), ErrorCode.RESULT_INVALID, "Other"))
    with pytest.raises(ValueError):
        store.acknowledge(item.attempt_id)
    assert store.get(item.attempt_id).phase == "received"
    store.finish(item.attempt_id, "cancelled")
    with pytest.raises(RuntimeError):
        store.start(item.attempt_id, "no")
    for limit in (0, 101, True):
        with pytest.raises(ValueError):
            store.unacknowledged(limit=limit)


def test_failed_receipt_write_rolls_back(tmp_path):
    cfg, store = store_at(tmp_path)
    with store.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_receive BEFORE INSERT ON execution_receipts BEGIN SELECT RAISE(ABORT, 'injected'); END")
    item = command(cfg)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.accept(item, uuid4())
    assert store.get(item.attempt_id) is None
