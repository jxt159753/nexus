"""Trusted Skill testing and bounded three-step execution.

The test controller never executes generated code. Generated Tool steps are
represented by a fixed double during tests; real Tool steps require an approved
version and a qualified Sandbox Node.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from nexus.contracts.extensions import (
    RESULT_FIELDS,
    canonical,
    validate_instance,
)
from nexus.contracts.task import ListDirectoryArguments, ListDirectoryRequest, SystemInfoArguments, SystemInfoRequest
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, load_task

LOGGER = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
SKILL_VERIFICATION = "skill_steps_verified"

SYSTEM_RESULT = {
    "os_name": "Windows",
    "os_version": "test-double",
    "architecture": "AMD64",
    "python_version": "3.13",
}
DIRECTORY_ENTRIES = [
    {"name": "alpha.txt", "kind": "file"},
    {"name": "beta.txt", "kind": "file"},
    {"name": "docs", "kind": "directory"},
]


def _directory_result(truncated: bool) -> dict:
    return {"root_id": "workspace", "entries": [dict(item) for item in DIRECTORY_ENTRIES], "truncated": truncated}


def _stats(entries: list[dict]) -> dict:
    counts = {"file": 0, "directory": 0, "link": 0, "other": 0}
    for entry in entries:
        kind = entry.get("kind")
        if kind not in counts:
            raise ValueError("unknown entry kind")
        counts[kind] += 1
    return {"counts": counts, "total": len(entries)}


def _resolve(arguments: dict, results: dict) -> dict:
    value = {}
    for key, item in arguments.items():
        if isinstance(item, dict) and set(item) == {"$ref"}:
            reference = item["$ref"]
            step_id, field = reference["step"], reference["field"]
            if step_id not in results:
                raise ValueError("reference source is not verified")
            result = results[step_id]
            if field not in result:
                raise ValueError("reference field is absent")
            value[key] = result[field]
        else:
            value[key] = item
    return value


class SkillService:
    def __init__(self, harness, extension_service, tools=None):
        self.harness = harness
        self.database = harness.security.database
        self.extensions = extension_service
        self.tools = tools
        self.wake = asyncio.Event()
        self.failed = False

    async def test_version(self, row) -> tuple[dict, str]:
        cases = []
        try:
            release = json.loads(row["release_json"])
            steps = release["steps"]
            manifest = release["manifest"]
            for truncated in (False, True):
                results: dict[str, dict] = {}
                observed = []
                for step in steps:
                    arguments = _resolve(step.get("arguments", {}), results)
                    if step.get("tool_version_id"):
                        tool = await self._tool_version(step["tool_version_id"])
                        if tool is None:
                            raise ValueError("referenced Tool version is unavailable")
                        tool_manifest = json.loads(tool["manifest_json"])
                        validate_instance(tool_manifest["input_schema"], arguments)
                        if tool_manifest["verification_profile"] != "directory_entry_stats_v1":
                            raise ValueError("no trusted test double for this Tool profile")
                        output = _stats(arguments["entries"])
                        validate_instance(tool_manifest["output_schema"], output)
                    elif step.get("tool") == "system.info":
                        if arguments:
                            raise ValueError("system.info takes no arguments")
                        output = dict(SYSTEM_RESULT)
                    elif step.get("tool") == "filesystem.list":
                        ListDirectoryArguments.model_validate(arguments)
                        output = _directory_result(truncated)
                    else:
                        raise ValueError("unsupported built-in tool")
                    results[step["id"]] = output
                    observed.append({"id": step["id"], "output": output})
                validate_instance(manifest["output_schema"], results)
                cases.append({"name": "truncated" if truncated else "normal", "truncated": truncated,
                              "observed": observed})
            return {"passed": True, "controller": "trusted-skill-v1", "cases": cases}, "passed"
        except (ValueError, KeyError, TypeError, LookupError) as exc:
            return {"passed": False, "controller": "trusted-skill-v1", "cases": cases,
                    "reason": type(exc).__name__}, "failed"

    async def _tool_version(self, version_id: str):
        async with self.database.transaction(write=False) as conn:
            return await one(conn, "SELECT * FROM extension_versions WHERE version_id=? AND kind='tool'", (str(version_id),))

    async def _owner_principal(self, owner_id: str) -> Principal:
        async with self.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT token_hash,role,revoked_at FROM identities WHERE identity_id=?", (owner_id,))
            if row is None or row["role"] != "owner" or row["revoked_at"] is not None:
                raise AccessDenied()
            return Principal(UUID(owner_id), "owner", row["token_hash"])

    async def start(self, principal: Principal, version_id: UUID, node_id: UUID, input_value: dict) -> dict:
        self.extensions.require_enabled()
        input_value = json.loads(json.dumps(input_value, allow_nan=False))
        if type(input_value) is not dict:
            raise ValueError("Skill input must be an object")
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                now = utc_now()
                await self.extensions.owner(conn, principal)
                version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=? AND owner_id=? AND kind='skill'", (str(version_id), str(principal.identity_id)))
                if version is None:
                    raise LookupError("skill version not found")
                approval = await self.extensions.current_approval(conn, version, now)
                if approval["node_id"] is not None and approval["node_id"] != str(node_id):
                    raise AccessDenied("POLICY_DENIED")
                await self.extensions._dependencies_enabled(conn, version, now)
                manifest = json.loads(version["manifest_json"])
                validate_instance(manifest["input_schema"], input_value)
                if (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    raise AccessDenied("BUSY")
                policy = self.harness.link.config.nodes.get(node_id)
                if policy is None:
                    raise AccessDenied("POLICY_DENIED")
                steps = json.loads(version["steps_json"])
                run_id = uuid4()
                expires = now + timedelta(minutes=5)
                await conn.execute("INSERT INTO skill_runs(run_id,owner_id,version_id,approval_id,artifact_digest,environment_digest,node_id,input_json,status,current_step,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,'running',1,?,?,?)", (
                    str(run_id), str(principal.identity_id), str(version_id), approval["approval_id"],
                    version["artifact_digest"], version["environment_digest"], str(node_id),
                    canonical(input_value), timestamp(now), timestamp(now), timestamp(expires),
                ))
                for index, step in enumerate(steps, start=1):
                    await conn.execute("INSERT INTO skill_run_steps(run_id,step_index,step_id,status,created_at,updated_at) VALUES(?,?,?,'pending',?,?)", (
                        str(run_id), index, step["id"], timestamp(now), timestamp(now),
                    ))
                await audit(conn, str(principal.identity_id), None, "extension.skill_started", now)
        self.wake.set()
        return await self.get(principal, run_id)

    async def get(self, principal: Principal, run_id: UUID) -> dict:
        async with self.database.transaction(write=False) as conn:
            await self.extensions.owner(conn, principal)
            run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=? AND owner_id=?", (str(run_id), str(principal.identity_id)))
            if run is None:
                raise LookupError("skill run not found")
            steps = await conn.execute_fetchall("SELECT * FROM skill_run_steps WHERE run_id=? ORDER BY step_index", (str(run_id),))
            version = await one(conn, "SELECT capability_id,revision,artifact_digest FROM extension_versions WHERE version_id=?", (run["version_id"],))
            value = dict(run)
            value["input"] = json.loads(value.pop("input_json"))
            value["result"] = json.loads(value["result_json"]) if value["result_json"] else None
            value["error"] = json.loads(value["error_json"]) if value["error_json"] else None
            value.pop("result_json"); value.pop("error_json")
            value["verification"] = SKILL_VERIFICATION if run["status"] == "succeeded" else None
            value["capability_id"] = version["capability_id"] if version else None
            value["version_revision"] = version["revision"] if version else None
            value["steps"] = []
            for step in steps:
                item = dict(step)
                item["result"] = json.loads(item["result_json"]) if item["result_json"] else None
                item.pop("result_json")
                value["steps"].append(item)
            return value

    async def list(self, principal: Principal, before: int = 2**63 - 1) -> list[dict]:
        async with self.database.transaction(write=False) as conn:
            await self.extensions.owner(conn, principal)
            rows = await conn.execute_fetchall("SELECT run_id FROM skill_runs WHERE owner_id=? AND rowid<? ORDER BY rowid DESC LIMIT 20", (str(principal.identity_id), before))
        return [await self.get(principal, UUID(row[0])) for row in rows]

    async def cancel(self, principal: Principal, run_id: UUID) -> dict:
        self.extensions.require_enabled()
        async with self.database.transaction() as conn:
            now = utc_now()
            await self.extensions.owner(conn, principal)
            run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=? AND owner_id=?", (str(run_id), str(principal.identity_id)))
            if run is None:
                raise LookupError("skill run not found")
            if run["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                terminal = True
            else:
                terminal = False
                await conn.execute("UPDATE skill_runs SET cancel_requested=1,status='cancelling',revision=revision+1,updated_at=? WHERE run_id=?", (timestamp(now), str(run_id)))
                current = await one(conn, "SELECT task_id FROM skill_run_steps WHERE run_id=? AND step_index=?", (str(run_id), run["current_step"]))
        if terminal:
            return await self.get(principal, run_id)
        if current is not None and current["task_id"]:
            try:
                await self.harness.cancel(principal, UUID(current["task_id"]))
            except (AccessDenied, LookupError):
                pass
        self.wake.set()
        return await self.get(principal, run_id)

    async def _step_task(self, run, step, definition: dict, arguments: dict):
        principal = await self._owner_principal(run["owner_id"])
        request_id = uuid5(NAMESPACE_URL, f"nexus-skill:{run['run_id']}:{step['step_index']}")
        if definition.get("tool") == "system.info":
            request = SystemInfoRequest(request_id=request_id, intent="get_system_info", arguments=SystemInfoArguments())
        else:
            request = ListDirectoryRequest(request_id=request_id, intent="list_directory", arguments=ListDirectoryArguments.model_validate(arguments))
        return await self.harness.submit(principal, UUID(run["node_id"]), request, rate_limit=False)

    async def _finish(self, run_id: str, status: str, *, result: dict | None = None, error: str | None = None) -> None:
        async with self.database.transaction() as conn:
            run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=?", (run_id,))
            if run is None or run["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                return
            await conn.execute("UPDATE skill_runs SET status=?,result_json=?,error_json=?,revision=revision+1,updated_at=? WHERE run_id=?", (
                status, canonical(result) if result is not None else None,
                json.dumps({"code": error}) if error else None, timestamp(utc_now()), run_id,
            ))
            await audit(conn, run["owner_id"], None, f"extension.skill_{status}", utc_now())

    async def _advance(self, run_id: str) -> None:
        async with self.database.transaction(write=False) as conn:
            run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=?", (run_id,))
            if run is None or run["status"] not in {"running", "cancelling"}:
                return
            version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (run["version_id"],))
            step = await one(conn, "SELECT * FROM skill_run_steps WHERE run_id=? AND step_index=?", (run_id, run["current_step"]))
            steps = await conn.execute_fetchall("SELECT * FROM skill_run_steps WHERE run_id=? ORDER BY step_index", (run_id,))
            paused = bool((await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0])
        now = utc_now()
        if run["expires_at"] <= timestamp(now):
            if step is not None and step["task_id"]:
                try:
                    await self.harness.cancel(await self._owner_principal(run["owner_id"]), UUID(step["task_id"]))
                except (AccessDenied, LookupError):
                    pass
            await self._finish(run_id, "timed_out", error="DEADLINE_EXCEEDED")
            return
        if version is None or not version["enabled"]:
            await self._finish(run_id, "failed", error="VERSION_DISABLED")
            return
        if not await self._approval_ok(version, now):
            await self._finish(run_id, "failed", error="APPROVAL_EXPIRED")
            return
        if run["cancel_requested"]:
            if step is not None and step["status"] in {"pending", "dispatchable", "approval", "running"} and step["task_id"]:
                if step["task_id"]:
                    try:
                        await self.harness.cancel(await self._owner_principal(run["owner_id"]), UUID(step["task_id"]))
                    except (AccessDenied, LookupError):
                        pass
                task = await self._task(step["task_id"])
                if task is not None and task["status"] not in TERMINAL:
                    return
            await self._finish(run_id, "cancelled")
            return
        if step is None:
            results = {item["step_id"]: json.loads(item["result_json"]) for item in steps if item["result_json"]}
            release = json.loads(version["release_json"])
            try:
                validate_instance(release["manifest"]["output_schema"], results)
            except ValueError:
                await self._finish(run_id, "failed", error="RESULT_INVALID")
                return
            await self._finish(run_id, "succeeded", result=results)
            return
        # A step with persisted evidence is settled before any new dispatch.
        if step["task_id"]:
            task = await self._task(step["task_id"])
            if task is None:
                await self._finish(run_id, "failed", error="TASK_MISSING")
                return
            if task["status"] == "succeeded":
                await self._step_succeeded(run, step, task["result_json"])
                return
            if task["status"] in {"failed", "cancelled", "timed_out"}:
                await self._finish(run_id, "cancelled" if task["status"] == "cancelled" else "failed",
                                   error=task["error_code"] or "STEP_FAILED")
                return
            if task["status"] == "awaiting_approval":
                await self._step_status(run_id, step["step_index"], "approval")
            else:
                await self._step_status(run_id, step["step_index"], "running")
            return
        if step["tool_run_id"]:
            tool_run = await self._tool_run(step["tool_run_id"])
            if tool_run is None:
                await self._finish(run_id, "failed", error="TOOL_EVIDENCE_MISSING")
                return
            if tool_run["status"] == "succeeded":
                await self._step_succeeded(run, step, tool_run["output_json"])
                return
            if tool_run["status"] in {"failed", "unknown", "cancelled"}:
                await self._finish(run_id, "failed", error=json.loads(tool_run["error_json"])["code"] if tool_run["error_json"] else "TOOL_FAILED")
                return
            await self._step_status(run_id, step["step_index"], "running")
            return
        if paused:
            return
        release = json.loads(version["release_json"])
        definition = release["steps"][step["step_index"] - 1]
        try:
            previous = {item["step_id"]: json.loads(item["result_json"]) for item in steps if item["result_json"]}
            arguments = _resolve(definition.get("arguments", {}), previous)
        except (ValueError, KeyError, TypeError):
            await self._finish(run_id, "failed", error="REFERENCE_INVALID")
            return
        if definition.get("tool_version_id"):
            tool = await self._tool_version(definition["tool_version_id"])
            if tool is None:
                await self._finish(run_id, "failed", error="DEPENDENCY_UNAVAILABLE")
                return
            if self.tools is None:
                await self._finish(run_id, "failed", error="SANDBOX_UNAVAILABLE")
                return
            status = await self.tools.execute_skill_step(
                owner_id=run["owner_id"], version=tool, node_id=UUID(run["node_id"]),
                input_value=arguments, skill_run_id=run_id, step_index=step["step_index"])
            if status == "started":
                return
            step_row = await self._read_step(run_id, step["step_index"])
            if step_row and step_row["status"] == "succeeded":
                await self._advance(run_id)
            else:
                await self._finish(run_id, "failed", error="TOOL_STEP_FAILED")
            return
        try:
            task = await self._step_task(run, step, definition, arguments)
        except (AccessDenied, Conflict, LookupError, ValueError) as exc:
            LOGGER.warning("skill step submit failed: %s %s", type(exc).__name__, exc)
            await self._finish(run_id, "failed", error="STEP_SUBMIT_FAILED")
            return
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE skill_run_steps SET task_id=?,status=? ,updated_at=? WHERE run_id=? AND step_index=? AND task_id IS NULL", (
                str(task.task_id), "approval" if task.status == "awaiting_approval" else "dispatchable",
                timestamp(utc_now()), run_id, step["step_index"]))
        self.wake.set()

    async def _approval_ok(self, version, now) -> bool:
        async with self.database.transaction(write=False) as conn:
            try:
                await self.extensions.current_approval(conn, version, now)
                return True
            except AccessDenied:
                return False

    async def _step_status(self, run_id: str, index: int, status: str) -> None:
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE skill_run_steps SET status=?,updated_at=? WHERE run_id=? AND step_index=?", (status, timestamp(utc_now()), run_id, index))

    async def _step_succeeded(self, run, step, result_json: str) -> None:
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE skill_run_steps SET status='succeeded',result_json=?,updated_at=? WHERE run_id=? AND step_index=?", (
                result_json, timestamp(utc_now()), run["run_id"], step["step_index"]))
            total = await one(conn, "SELECT count(*) FROM skill_run_steps WHERE run_id=?", (run["run_id"],))
            if step["step_index"] >= total[0]:
                results = await conn.execute_fetchall("SELECT step_id,result_json FROM skill_run_steps WHERE run_id=? ORDER BY step_index", (run["run_id"],))
                value = {item["step_id"]: json.loads(item["result_json"]) for item in results}
                version = await one(conn, "SELECT release_json FROM extension_versions WHERE version_id=?", (run["version_id"],))
                try:
                    validate_instance(json.loads(version["release_json"])["manifest"]["output_schema"], value)
                except (ValueError, KeyError, TypeError):
                    await conn.execute("UPDATE skill_runs SET status='failed',error_json=?,revision=revision+1,updated_at=? WHERE run_id=?", (
                        json.dumps({"code": "RESULT_INVALID"}), timestamp(utc_now()), run["run_id"]))
                    return
                await conn.execute("UPDATE skill_runs SET status='succeeded',result_json=?,revision=revision+1,updated_at=? WHERE run_id=?", (canonical(value), timestamp(utc_now()), run["run_id"]))
            else:
                await conn.execute("UPDATE skill_runs SET current_step=?,revision=revision+1,updated_at=? WHERE run_id=?", (step["step_index"] + 1, timestamp(utc_now()), run["run_id"]))
        self.wake.set()

    async def _task(self, task_id: str):
        async with self.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT status,result_json,error_json FROM tasks WHERE task_id=?", (task_id,))
            if row is None:
                return None
            error = json.loads(row["error_json"])["code"] if row["error_json"] else None
            return {"status": row["status"], "result_json": row["result_json"], "error_code": error}

    async def _tool_run(self, tool_run_id: str):
        async with self.database.transaction(write=False) as conn:
            return await one(conn, "SELECT * FROM extension_tool_runs WHERE tool_run_id=?", (tool_run_id,))

    async def _read_step(self, run_id: str, index: int):
        async with self.database.transaction(write=False) as conn:
            return await one(conn, "SELECT * FROM skill_run_steps WHERE run_id=? AND step_index=?", (run_id, index))

    async def tick(self) -> None:
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT run_id FROM skill_runs WHERE status IN ('running','cancelling') ORDER BY created_at LIMIT 10")
        for row in rows:
            try:
                await self._advance(row[0])
            except (sqlite3.Error, OSError):
                raise
            except Exception:
                LOGGER.exception("skill run stopped by an internal error")
                await self._finish(row[0], "failed", error="INTERNAL_ERROR")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except (sqlite3.Error, OSError):
                self.failed = self.harness.failed = True
                self.harness.wake.set()
                return
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), .3)
            except TimeoutError:
                pass
