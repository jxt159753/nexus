"""Chat candidate creation using the existing immutable Skill lifecycle."""

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from nexus.contracts.work import SkillCreateArguments
from nexus.core.security import work as permissions
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import utc_now


async def create_skill(chat, step, principal, node_id):
    if chat.skills is None or chat.skills.extensions.skills is None:
        raise AccessDenied("TEST_UNAVAILABLE")
    extensions = chat.skills.extensions
    scope = chat.harness.scope(node_id)
    async with chat.harness.operation, chat.database.transaction() as conn:
        fresh, grant = await permissions.creation_step(conn, step["step_id"], utc_now(), scope=scope)
        if grant["owner_id"] != str(principal.identity_id) or grant["node_id"] != str(node_id):
            raise AccessDenied("POLICY_DENIED")
        arguments = SkillCreateArguments.model_validate_json(json.dumps(json.loads(fresh["proposal_json"])["arguments"]))
        await permissions.operation(conn, step["step_id"], "skill-create:"+step["step_id"], utc_now())
        version_id = await extensions.accept_chat_skill(conn, principal,
            uuid5(NAMESPACE_URL, "skill-create:"+step["step_id"]), node_id, step["step_id"], arguments)
    version = await extensions.get_version(principal, UUID(version_id))
    if version["test_status"] == "untested":
        # Testing interprets bounded declarations with trusted stand-ins only.
        # Neither Markdown, Shell/MCP nor generated Python is executed here.
        version = await extensions.test_version(principal, UUID(version_id), work_step_id=step["step_id"])
    async with chat.harness.operation, chat.database.transaction() as conn:
        fresh, _ = await permissions.creation_step(conn, step["step_id"], utc_now(), scope=scope)
        data = {key:version[key] for key in ("version_id", "capability_id", "revision", "artifact_digest", "test_status", "approval_status", "enabled")}
        data.update(created=True, approval_required=not version["approval_valid"],
                    approval_available=version["test_status"] == "passed",
                    execution="not_run", test=version["test"],
                    review_url="/api/v1/extensions/versions/"+version_id)
        await chat.work.result(conn, fresh, {"status":"succeeded" if version["test_status"] == "passed" else "failed",
            "verification":"skill_contract_standins_only", "data":data}, utc_now())
