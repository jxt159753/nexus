"""Durable extension authorization shared by planning and actual dispatch."""

import json

from nexus.contracts.extensions import canonical, digest_value
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp


async def approved_version(conn, version, now, *, require_enabled=True):
    if require_enabled and not version["enabled"]:
        raise AccessDenied("VERSION_DISABLED")
    owner = await one(conn, "SELECT revoked_at FROM identities WHERE identity_id=? AND role='owner'", (version["owner_id"],))
    if owner is None or owner[0] is not None or version["approval_status"] != "approved":
        raise AccessDenied("VERSION_NOT_APPROVED")
    test = await one(conn, "SELECT * FROM extension_tests WHERE version_id=? ORDER BY rowid DESC LIMIT 1", (version["version_id"],))
    approval = await one(conn, "SELECT * FROM extension_approvals WHERE version_id=? ORDER BY rowid DESC LIMIT 1", (version["version_id"],))
    if (test is None or test["status"] != "passed" or test["controller_version"] != "trusted-controller-v1"
            or test["artifact_digest"] != version["artifact_digest"] or test["environment_digest"] != version["environment_digest"]):
        raise AccessDenied("VERSION_NOT_APPROVED")
    if (approval is None or approval["decision"] != "approve" or approval["test_id"] != test["test_id"]
            or approval["artifact_digest"] != version["artifact_digest"]
            or approval["environment_digest"] != version["environment_digest"] or approval["expires_at"] <= timestamp(now)):
        raise AccessDenied("APPROVAL_EXPIRED")
    policy = (await one(conn, "SELECT version FROM policy_state WHERE singleton=1"))[0]
    scope = canonical({"kind": version["kind"], "node_id": approval["node_id"]})
    parameters = digest_value({"node_id": approval["node_id"], "resource_scope": scope,
                               "input_schema": json.loads(version["manifest_json"])["input_schema"]})
    if approval["policy_version"] != policy or approval["resource_scope"] != scope or approval["parameters_digest"] != parameters:
        raise AccessDenied("POLICY_DENIED")
    return approval


async def skill_run_allowed(conn, run, now):
    if run is None or run["status"] != "running" or run["cancel_requested"] or run["expires_at"] <= timestamp(now):
        return False
    version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (run["version_id"],))
    if version is None or version["artifact_digest"] != run["artifact_digest"] or version["environment_digest"] != run["environment_digest"]:
        return False
    try:
        approval = await approved_version(conn, version, now)
        if run['permission_context_id']:
            from nexus.core.extensions.native_steps import context
            await context(conn,run)
        work = await one(conn,"SELECT step_id FROM chat_work_steps WHERE skill_run_id=?",(run["run_id"],))
        if work:
            from nexus.core.security.work import allowed_step
            await allowed_step(conn,work[0],now)
    except AccessDenied:
        return False
    return (approval["approval_id"] == run["approval_id"]
            and (approval["node_id"] is None or approval["node_id"] == run["node_id"]))


async def skill_task_allowed(conn, task_id, now):
    run = await one(conn, "SELECT r.* FROM skill_run_steps s JOIN skill_runs r USING(run_id) WHERE s.task_id=?", (str(task_id),))
    return run is None or await skill_run_allowed(conn, run, now)
