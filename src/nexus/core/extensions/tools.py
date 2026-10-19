"""Trusted Tool verification and Core-controlled single-attempt execution.

Candidate code is only ever executed by a qualified Sandbox Node. This module
compares actual evidence against Core-owned cases and never imports candidate
source. Missing or mismatched sandbox evidence blocks testing and approval.
"""

import json
from uuid import UUID, uuid4

from nexus.contracts.extensions import (
    canonical,
    check_json_value,
    digest_value,
    validate_instance,
)
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now


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

    def _environment_node(self, version) -> tuple[UUID | None, str | None]:
        environment = json.loads(version["environment_json"])
        node = UUID(environment["node_id"]) if environment.get("node_id") else None
        if node is None:
            for candidate, policy in self.extensions.harness.link.config.sandbox_nodes.items():
                if policy.image_digest == environment.get("image_digest"):
                    node = candidate
                    break
        return node, environment.get("image_digest")

    def _status(self, node: UUID | None) -> dict:
        if node is None or self.transport is None:
            return {"available": False, "reason": "sandbox_not_configured", "image_digest": None}
        return self.transport.status(node)

    async def _call(self, node: UUID, version, input_value: dict, timeout: int) -> dict:
        try:
            return await self.transport.execute(node, version, input_value, timeout)
        except SandboxUnavailable:
            return {"outcome": "failed", "error_code": "SANDBOX_UNAVAILABLE"}
        except Exception:
            return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_UNKNOWN"}

    async def test_version(self, version) -> tuple[dict, str]:
        node, image = self._environment_node(version)
        status = self._status(node)
        if not status.get("available"):
            return {"reason": status.get("reason", "sandbox_unavailable"), "node_id": str(node) if node else None}, "unavailable"
        if image is None or status.get("image_digest") != image:
            return {"reason": "sandbox_image_mismatch", "node_id": str(node)}, "unavailable"
        manifest = json.loads(version["manifest_json"])
        cases = []
        passed = True
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
                reply = await self._call(node, version, case["input"], self.config.sandbox_test_timeout_seconds)
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
        result = {
            "controller": "trusted-tool-v1",
            "profile": manifest["verification_profile"],
            "image_digest": image,
            "node_id": str(node),
            "cases": cases,
        }
        return result, "passed" if passed else "failed"

    async def execute_skill_step(self, *, owner_id: str, version, node_id: UUID,
                                 input_value: dict, skill_run_id: str, step_index: int) -> str:
        environment = json.loads(version["environment_json"])
        image = environment.get("image_digest")
        status = self._status(node_id)
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
        reply = await self._call(node_id, version, input_value, timeout)
        now = utc_now()
        async with self.database.transaction() as conn:
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
            status = "unknown" if reply.get("outcome") == "unknown" else "failed"
            await conn.execute("UPDATE extension_tool_runs SET status=?,error_json=?,updated_at=? WHERE tool_run_id=?", (
                status, json.dumps({"code": reply.get("error_code", "SANDBOX_FAILED")}), timestamp(now), str(tool_run_id)))
            await conn.execute("UPDATE skill_run_steps SET status=?,updated_at=? WHERE run_id=? AND step_index=?", (
                "unknown" if status == "unknown" else "failed", timestamp(now), skill_run_id, step_index))
            return status
