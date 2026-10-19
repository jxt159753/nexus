"""Core-owned sandbox command journal: commit before send, commit before ack."""

import json
from uuid import UUID, uuid4

from nexus.contracts.extensions import canonical, digest_value, parse_strict_json, MAX_OUTPUT_BYTES
from nexus.contracts.sandbox import sandbox_command_digest
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict


class SandboxExecutions:
    def __init__(self, database, limit: int):
        self.database, self.limit = database, limit

    async def prepare(self, node, version, input_value, timeout, *, execution_id=None, test_run_id=None):
        execution_id = execution_id or uuid4()
        command = sandbox_command_digest(version["version_id"], version["artifact_digest"],
                                         version["environment_digest"], input_value, timeout)
        async with self.database.transaction() as conn:
            old = await one(conn, "SELECT * FROM sandbox_executions WHERE execution_id=?", (str(execution_id),))
            if old is not None:
                if old["command_digest"] != command or old["node_id"] != str(node):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return old
            count = await one(conn, "SELECT count(*) FROM sandbox_executions")
            if count[0] >= self.limit:
                raise AccessDenied("SANDBOX_STORAGE_FULL")
            unresolved = await one(conn, "SELECT 1 FROM sandbox_executions WHERE node_id=? AND status IN ('sent','unknown') LIMIT 1", (str(node),))
            if unresolved is not None:
                raise AccessDenied("SANDBOX_RECONCILIATION_REQUIRED")
            now = timestamp(utc_now())
            await conn.execute("INSERT INTO sandbox_executions(execution_id,version_id,node_id,test_run_id,artifact_digest,environment_digest,input_json,timeout_seconds,command_digest,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)", (
                str(execution_id), version["version_id"], str(node), test_run_id,
                version["artifact_digest"], version["environment_digest"], canonical(input_value), timeout, command, now, now))
            return await one(conn, "SELECT * FROM sandbox_executions WHERE execution_id=?", (str(execution_id),))

    async def sent(self, execution_id):
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE sandbox_executions SET status='sent',updated_at=? WHERE execution_id=? AND status='prepared'", (timestamp(utc_now()), str(execution_id)))

    async def store(self, node, execution_id, artifact_digest, reply):
        if reply.get("outcome") == "succeeded":
            encoded = canonical(reply.get("output"))
            if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise ValueError("sandbox output exceeds result limit")
            parse_strict_json(encoded)
        state = "unknown" if reply.get("outcome") == "unknown" else "settled"
        async with self.database.transaction() as conn:
            row = await one(conn, "SELECT * FROM sandbox_executions WHERE execution_id=? AND node_id=?", (str(execution_id), str(node)))
            if row is None or row["artifact_digest"] != artifact_digest:
                raise AccessDenied("SANDBOX_EVIDENCE_MISMATCH")
            if row["status"] == "settled":
                if digest_value(json.loads(row["result_json"])) != digest_value(reply):
                    raise Conflict("SANDBOX_RESULT_CONFLICT")
                return
            await conn.execute("UPDATE sandbox_executions SET status=?,result_json=?,updated_at=? WHERE execution_id=?", (state, canonical(reply), timestamp(utc_now()), str(execution_id)))

    async def unsettled(self, node):
        async with self.database.transaction(write=False) as conn:
            return await conn.execute_fetchall("SELECT * FROM sandbox_executions WHERE node_id=? AND status IN ('sent','unknown') ORDER BY created_at LIMIT 10", (str(node),))

    async def get(self, execution_id):
        async with self.database.transaction(write=False) as conn:
            return await one(conn, "SELECT * FROM sandbox_executions WHERE execution_id=?", (str(execution_id),))

    async def claim_query(self, execution_id) -> bool:
        async with self.database.transaction() as conn:
            cursor = await conn.execute("UPDATE sandbox_executions SET query_count=query_count+1 WHERE execution_id=? AND status IN ('sent','unknown') AND query_count<64", (str(execution_id),))
            return cursor.rowcount == 1
