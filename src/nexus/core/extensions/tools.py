"""Trusted Tool verification and Core-controlled single-attempt execution.

Candidate code is only ever executed by a qualified Sandbox Node. This module
compares actual evidence against Core-owned cases and never imports candidate
source. Missing or mismatched sandbox evidence blocks testing and approval.
"""

import asyncio
import json
import sqlite3
from uuid import UUID, uuid4

from nexus.contracts.extensions import (
    canonical,
    check_json_value,
    digest_value,
    validate_instance,
)
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.extensions.executions import SandboxExecutions


class SandboxUnavailable(Exception):
    pass


def reference_stats(entries: list[dict]) -> dict:
    counts = {"file": 0, "directory": 0, "link": 0, "other": 0}
    for entry in entries:
        kind = entry.get("kind")
        if kind not in counts:
            raise ValueError("unknown entry kind")
        counts[kind] += 1
    return {"counts": counts, "total": len(entries)}


def trusted_cases() -> list[dict]:
    mixed = [
        {"name": "a.txt", "kind": "file"},
        {"name": "b.txt", "kind": "file"},
        {"name": "docs", "kind": "directory"},
        {"name": "shortcut", "kind": "link"},
        {"name": "device", "kind": "other"},
    ]
    full = [{"name": f"entry-{index}", "kind": ("file", "directory", "link", "other")[index % 4]}
            for index in range(200)]
    invalid = [
        {"name": "normal", "input": {"entries": mixed}, "valid": True},
        {"name": "empty", "input": {"entries": []}, "valid": True},
        {"name": "maximum", "input": {"entries": full}, "valid": True},
        {"name": "unknown-kind", "input": {"entries": [{"name": "x", "kind": "socket"}]}, "valid": False},
        {"name": "unknown-field", "input": {"entries": [{"name": "x", "kind": "file", "extra": 1}]}, "valid": False},
        {"name": "too-many", "input": {"entries": [{"name": f"n{index}", "kind": "file"} for index in range(201)]}, "valid": False},
        {"name": "missing-field", "input": {"other": []}, "valid": False},
        {"name": "not-object", "input": [], "valid": False},
    ]
    return invalid


class ToolService:
    def __init__(self, extensions, transport, config):
        self.extensions = extensions
        self.database = extensions.database
        self.transport = transport
        self.config = config
        self.owner_id = extensions.harness.link.config.owner_id
        self.executions = SandboxExecutions(self.database, config.max_sandbox_executions)
        if hasattr(transport, "result_handler"):
            transport.result_handler = self.receive_result
            transport.reconcile_handler = self.reconcile_node

    def _environment_node(self, version) -> tuple[UUID | None, str | None]:
        environment = json.loads(version["environment_json"])
        node = UUID(environment["node_id"]) if environment.get("node_id") else None
        if node is None:
            for candidate, policy in self.extensions.harness.link.config.sandbox_nodes.items():
                if policy.image_digest == environment.get("image_digest"):
                    node = candidate
                    break
        return node, environment.get("image_digest")

    def status(self, node: UUID | None) -> dict:
        if node is None or self.transport is None:
            return {"available": False, "reason": "sandbox_not_configured", "image_digest": None}
        return self.transport.status(node)

    async def _call(self, node: UUID, version, input_value: dict, timeout: int, *,
                    execution_id=None, skill_run_id=None, cancel_cursor=None, test_run_id=None) -> dict:
        cancel_event = asyncio.Event()
        try:
            record = await self.executions.prepare(node, version, input_value, timeout,
                execution_id=execution_id, test_run_id=test_run_id)
        except AccessDenied as exc:
            return {"outcome": "failed", "error_code": str(exc), "stopped": True}
        execution_id = UUID(record["execution_id"])
        if record["status"] == "settled":
            return json.loads(record["result_json"])
        if record["status"] in {"sent", "unknown"}:
            return {"outcome": "unknown", "error_code": "SANDBOX_RECONCILIATION_REQUIRED", "stopped": False}
        async with self.database.transaction(write=False) as conn:
            if cancel_cursor is None:
                cancel_cursor = (await one(conn, "SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]

        async def allowed(*, starting=False):
            from nexus.core.extensions.authorization import skill_run_allowed
            async with self.database.transaction(write=False) as conn:
                now = utc_now()
                if self.extensions.failed or self.extensions.harness.failed or not self.config.enabled:
                    return False
                cursor = (await one(conn, "SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
                if cursor != cancel_cursor or starting and (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    return False
                owner = await one(conn, "SELECT revoked_at FROM identities WHERE identity_id=?", (version["owner_id"],))
                if owner is None or owner[0] is not None:
                    return False
                if skill_run_id is not None:
                    run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=?", (skill_run_id,))
                    current = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (version["version_id"],))
                    if not await skill_run_allowed(conn, run, now):
                        return False
                    try:
                        await self.extensions.current_approval(conn, current, now)
                    except AccessDenied:
                        return False
                return True

        async def watch():
            try:
                while True:
                    if not await allowed():
                        cancel_event.set()
                        return
                    await asyncio.sleep(.05)
            except (OSError, sqlite3.Error):
                self.extensions.failed = self.extensions.harness.failed = True
                cancel_event.set()

        watcher = asyncio.create_task(watch())
        try:
            if not await allowed(starting=True):
                reply = {"outcome": "failed", "error_code": "SANDBOX_DISPATCH_DENIED", "stopped": True}
            else:
                await self.executions.sent(execution_id)
                reply = await self.transport.execute(node, version, input_value, timeout,
                    execution_id=execution_id, cancel_event=cancel_event, permitted=lambda: allowed(starting=True))
            # Real Link already commits inbound results before ack. Test transports
            # exercise exactly the same journal through this path.
            stored = await self.executions.get(execution_id)
            if stored["status"] == "settled":
                return json.loads(stored["result_json"])
            await self.executions.store(node, execution_id, version["artifact_digest"], reply)
            return reply
        except SandboxUnavailable:
            return {"outcome": "failed", "error_code": "SANDBOX_UNAVAILABLE"}
        except (OSError, sqlite3.Error):
            self.extensions.failed = self.extensions.harness.failed = True
            cancel_event.set()
            return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_UNKNOWN"}
        except Exception:
            return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_UNKNOWN"}
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_version(self, version, test_run_id=None) -> tuple[dict, str]:
        try:
            self.extensions.check_environment(version)
        except AccessDenied:
            return {"reason": "sandbox_environment_changed"}, "unavailable"
        node, image = self._environment_node(version)
        status = self.status(node)
        if not status.get("available"):
            return {"reason": status.get("reason", "sandbox_unavailable"), "node_id": str(node) if node else None}, "unavailable"
        if image is None or status.get("image_digest") != image:
            return {"reason": "sandbox_image_mismatch", "node_id": str(node)}, "unavailable"
        manifest = json.loads(version["manifest_json"])
        cases = []
        passed = True
        async with self.database.transaction(write=False) as conn:
            cancel_cursor = (await one(conn, "SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
        deadline = asyncio.get_running_loop().time() + self.config.sandbox_test_timeout_seconds
        for case in trusted_cases():
            record = {"name": case["name"], "valid": case["valid"]}
            try:
                validate_instance(manifest["input_schema"], case["input"])
                schema_ok = True
                expected = reference_stats(case["input"]["entries"])
            except (ValueError, KeyError, TypeError):
                schema_ok = False
                expected = None
            record["schema_accepted"] = schema_ok
            if schema_ok != case["valid"]:
                passed = False
                record["reason"] = "schema_expectation_mismatch"
            elif not schema_ok:
                record["reason"] = "rejected_by_contract"
            else:
                remaining = int(deadline - asyncio.get_running_loop().time())
                if remaining < 1:
                    return {"reason": "test_deadline_exceeded", "cases": cases}, "failed"
                from uuid import NAMESPACE_URL, uuid5
                execution_id = uuid5(NAMESPACE_URL, f"nexus-test:{test_run_id}:{case['name']}") if test_run_id else None
                reply = await self._call(node, version, case["input"], 10,
                                         execution_id=execution_id, cancel_cursor=cancel_cursor, test_run_id=test_run_id)
                record["outcome"] = reply.get("outcome")
                if reply.get("outcome") != "succeeded":
                    passed = False
                    record["reason"] = reply.get("error_code", "sandbox_failure")
                else:
                    output = reply.get("output")
                    try:
                        validate_instance(manifest["output_schema"], output)
                        if output != expected or output.get("total") != len(case["input"]["entries"]):
                            raise ValueError("trusted comparison failed")
                        record["actual"] = output
                    except (ValueError, KeyError, TypeError, AttributeError):
                        passed = False
                        record["reason"] = "output_mismatch"
            cases.append(record)
            if record.get("outcome") == "unknown":
                break
        result = {
            "controller": "trusted-tool-v1",
            "profile": manifest["verification_profile"],
            "image_digest": image,
            "node_id": str(node),
            "cases": cases,
        }
        return result, "passed" if passed else "failed"

    async def execute_skill_step(self, *, owner_id: str, version,
                                 input_value: dict, skill_run_id: str, step_index: int) -> str:
        node_id, image = self._environment_node(version)
        status = self.status(node_id)
        if not status.get("available") or status.get("image_digest") != image:
            return "unavailable"
        manifest = json.loads(version["manifest_json"])
        try:
            validate_instance(manifest["input_schema"], input_value)
            check_json_value(input_value)
        except ValueError:
            return "failed"
        async with self.database.transaction() as conn:
            now = utc_now()
            from nexus.core.extensions.authorization import skill_run_allowed
            run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=?", (skill_run_id,))
            if not await skill_run_allowed(conn, run, now) or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                return "failed"
            try:
                approval = await self.extensions.current_approval(conn, version, now)
            except AccessDenied:
                return "failed"
            if approval["node_id"] is not None and approval["node_id"] != str(node_id):
                return "failed"
            existing = await one(conn, "SELECT * FROM extension_tool_runs WHERE skill_run_id=? AND step_index=?", (skill_run_id, step_index))
            if existing is not None:
                return existing["status"]
            tool_run_id = uuid4()
            await conn.execute("INSERT INTO extension_tool_runs(tool_run_id,owner_id,version_id,approval_id,node_id,skill_run_id,step_index,artifact_digest,environment_digest,input_json,input_digest,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'dispatching',?,?)", (
                str(tool_run_id), owner_id, version["version_id"], approval["approval_id"], str(node_id),
                skill_run_id, step_index, version["artifact_digest"], version["environment_digest"],
                canonical(input_value), digest_value(input_value), timestamp(now), timestamp(now),
            ))
            await audit(conn, owner_id, None, "extension.tool_dispatched", now)
            step = await one(conn, "SELECT * FROM skill_run_steps WHERE run_id=? AND step_index=?", (skill_run_id, step_index))
            if step is not None:
                await conn.execute("UPDATE skill_run_steps SET tool_run_id=?,status='running',updated_at=? WHERE run_id=? AND step_index=?", (
                    str(tool_run_id), timestamp(now), skill_run_id, step_index))
        timeout = min(self.config.sandbox_test_timeout_seconds, 10)
        try:
            reply = await self._call(node_id, version, input_value, timeout,
                                     execution_id=UUID(str(tool_run_id)), skill_run_id=skill_run_id)
        except BaseException:
            async with self.database.transaction() as conn:
                await conn.execute("UPDATE extension_tool_runs SET status='unknown',error_json=?,updated_at=? WHERE tool_run_id=?", (json.dumps({"code": "CORE_INTERRUPTED"}), timestamp(utc_now()), str(tool_run_id)))
            raise
        now = utc_now()
        async with self.database.transaction() as conn:
            # A reconnect may settle the original command while the first waiter
            # returns a disconnect. Persisted evidence wins inside this transaction.
            settled = await one(conn, "SELECT result_json FROM sandbox_executions WHERE execution_id=? AND status='settled'", (str(tool_run_id),))
            if settled is not None:
                reply = json.loads(settled["result_json"])
            if reply.get("outcome") == "succeeded":
                output = reply.get("output")
                try:
                    validate_instance(manifest["output_schema"], output)
                    expected = reference_stats(input_value["entries"])
                    if output != expected or output.get("total") != len(input_value["entries"]):
                        raise ValueError("trusted comparison failed")
                except (ValueError, KeyError, TypeError, AttributeError):
                    await conn.execute("UPDATE extension_tool_runs SET status='failed',error_json=?,updated_at=? WHERE tool_run_id=?", (
                        json.dumps({"code": "RESULT_INVALID"}), timestamp(now), str(tool_run_id)))
                    await conn.execute("UPDATE skill_run_steps SET status='failed',updated_at=? WHERE run_id=? AND step_index=?", (timestamp(now), skill_run_id, step_index))
                    return "failed"
                await conn.execute("UPDATE extension_tool_runs SET status='succeeded',output_json=?,updated_at=? WHERE tool_run_id=?", (canonical(output), timestamp(now), str(tool_run_id)))
                await conn.execute("UPDATE skill_run_steps SET status='succeeded',result_json=?,updated_at=? WHERE run_id=? AND step_index=?", (canonical(output), timestamp(now), skill_run_id, step_index))
                await audit(conn, owner_id, None, "extension.tool_succeeded", now)
                return "succeeded"
            status = "unknown" if reply.get("outcome") == "unknown" else "cancelled" if reply.get("error_code") == "SANDBOX_CANCELLED" and reply.get("stopped") else "failed"
            await conn.execute("UPDATE extension_tool_runs SET status=?,error_json=?,updated_at=? WHERE tool_run_id=?", (
                status, json.dumps({"code": reply.get("error_code", "SANDBOX_FAILED")}), timestamp(now), str(tool_run_id)))
            await conn.execute("UPDATE skill_run_steps SET status=?,updated_at=? WHERE run_id=? AND step_index=?", (
                "unknown" if status == "unknown" else "failed", timestamp(now), skill_run_id, step_index))
            return status

    async def recover(self):
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE extension_tool_runs SET status='unknown',error_json=?,updated_at=? WHERE status='dispatching'", (json.dumps({"code": "CORE_RESTARTED_REQUIRES_RECONCILIATION"}), timestamp(utc_now())))
            await conn.execute("UPDATE extension_test_runs SET status='interrupted',updated_at=? WHERE status='running'", (timestamp(utc_now()),))

    async def receive_result(self, node, result):
        if result.outcome == "succeeded" and result.stopped:
            from nexus.contracts.extensions import parse_strict_json
            reply = {"outcome": "succeeded", "output": parse_strict_json(result.output_json)}
        else:
            reply = {"outcome": result.outcome if result.stopped else "unknown",
                     "error_code": result.error_code or "SANDBOX_STOP_UNKNOWN", "stopped": result.stopped}
        await self.executions.store(node, result.execution_id, result.artifact_digest, reply)
        # Commit is complete before the Node may mark its result acknowledged.
        await self.transport.acknowledge(node, result.execution_id, result.artifact_digest)
        await self._recover_business_result(result.execution_id)

    async def _recover_business_result(self, execution_id):
        record = await self.executions.get(execution_id)
        if record is None or record["status"] != "settled":
            return
        async with self.database.transaction() as conn:
            run = await one(conn, "SELECT * FROM extension_tool_runs WHERE tool_run_id=?", (str(execution_id),))
            if run is None or run["status"] not in {"unknown", "dispatching"}:
                return
            reply = json.loads(record["result_json"])
            status, output, error = "failed", None, reply.get("error_code", "SANDBOX_FAILED")
            if reply["outcome"] == "succeeded":
                version = await one(conn, "SELECT manifest_json FROM extension_versions WHERE version_id=?", (run["version_id"],))
                try:
                    output = reply["output"]
                    validate_instance(json.loads(version[0])["output_schema"], output)
                    if output != reference_stats(json.loads(run["input_json"])["entries"]):
                        raise ValueError("trusted comparison failed")
                    status, error = "succeeded", None
                except (ValueError, KeyError, TypeError):
                    output, error = None, "RESULT_INVALID"
            await conn.execute("UPDATE extension_tool_runs SET status=?,output_json=?,error_json=?,updated_at=? WHERE tool_run_id=?", (
                status, canonical(output) if output is not None else None,
                canonical({"code": error}) if error else None, timestamp(utc_now()), str(execution_id)))

    async def reconcile_node(self, node):
        # Queries never execute code. A not_found reply does not create a new attempt.
        while True:
            for record in await self.executions.unsettled(node):
                if not await self.executions.claim_query(record["execution_id"]):
                    continue
                answer = await self.transport.query(node, UUID(record["execution_id"]))
                if answer is None:
                    continue
                if answer.status != "not_found" and answer.command_digest != record["command_digest"]:
                    raise AccessDenied("SANDBOX_EVIDENCE_MISMATCH")
                if answer.result is not None:
                    await self.receive_result(node, answer.result)
                elif answer.status == "not_found":
                    await self.executions.store(node, UUID(record["execution_id"]), record["artifact_digest"],
                        {"outcome": "failed", "error_code": "SANDBOX_NOT_RECEIVED", "stopped": True})
                    await self._recover_business_result(UUID(record["execution_id"]))
            if not await self.executions.unsettled(node):
                connection = self.transport.connections.get(node)
                if connection is not None:
                    connection.blocked = False
            await asyncio.sleep(2)
