"""Deterministic work grants. Models cannot create or widen permission."""

from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp


async def valid_grant(conn, grant_id, now, *, owner_id=None, node_id=None, scope=None):
    grant = await one(conn, "SELECT g.*,c.archived FROM chat_work_grants g JOIN conversations c USING(conversation_id) WHERE grant_id=?", (str(grant_id),))
    if (grant is None or grant["revoked_at"] or grant["archived"] or grant["expires_at"]<=timestamp(now)
            or owner_id is not None and grant["owner_id"]!=str(owner_id) or node_id is not None and grant["node_id"]!=str(node_id)):
        raise AccessDenied("WORK_PERMISSION_EXPIRED")
    if not await one(conn,"SELECT 1 FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL",(grant["owner_id"],)):
        raise AccessDenied("AUTH_REQUIRED")
    if not await one(conn,"SELECT 1 FROM identities WHERE identity_id=? AND role='node' AND revoked_at IS NULL",(grant["node_id"],)):
        raise AccessDenied("POLICY_DENIED")
    if grant["policy_version"]!=(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0]:
        raise AccessDenied("POLICY_DENIED")
    if scope is not None:
        from nexus.core.security.service import scope_digest
        if scope_digest(scope)!=grant["scope_digest"]:
            raise AccessDenied("POLICY_DENIED")
    from nexus.core.security.modes import check_grant
    await check_grant(conn, grant)
    return grant


async def allowed_step(conn, step_id, now, *, scope=None):
    row = await one(conn,"SELECT s.*,t.owner_id,t.work_grant_id,t.work_status,t.recovery_required,t.status AS turn_status,t.expires_at FROM chat_work_steps s JOIN chat_turns t USING(turn_id) WHERE step_id=?",(str(step_id),))
    if row is None or row["recovery_required"] or row["turn_status"] in {"stopped","stopping","failed","interrupted","expired"} or row["status"] not in {"pending","waiting","permission"} or row["work_status"] not in {"waiting","permission"} or row["expires_at"]<=timestamp(now):
        raise AccessDenied("WORK_STOPPED")
    grant = await valid_grant(conn,row["work_grant_id"],now,owner_id=row["owner_id"],scope=scope)
    return row,grant


async def operation(conn, step_id, key, now):
    row,grant=await allowed_step(conn,step_id,now)
    if await one(conn,"SELECT 1 FROM chat_work_operations WHERE grant_id=? AND operation_key=?",(grant["grant_id"],key)):
        return
    if grant["used_operations"]>=grant["max_operations"]:
        raise AccessDenied("WORK_OPERATION_LIMIT")
    await conn.execute("INSERT INTO chat_work_operations VALUES(?,?)",(grant["grant_id"],key))
    await conn.execute("UPDATE chat_work_grants SET used_operations=used_operations+1 WHERE grant_id=?",(grant["grant_id"],))


async def task_step(conn, task_id):
    row=await one(conn,"SELECT step_id FROM chat_work_tasks WHERE task_id=?",(str(task_id),))
    if row:
        return row[0]
    row=await one(conn,"SELECT w.step_id FROM skill_run_steps s JOIN chat_work_steps w ON w.skill_run_id=s.run_id WHERE s.task_id=?",(str(task_id),))
    return row[0] if row else None


async def check_task(conn, task, scope, now):
    identifier=await task_step(conn,task.task_id)
    if identifier is None:
        return False
    step,grant=await allowed_step(conn,identifier,now,scope=scope)
    if grant["node_id"]!=str(task.node_id) or grant["owner_id"]!=str(task.owner_id):
        raise AccessDenied("POLICY_DENIED")
    plan=await one(conn,'SELECT plan_mode FROM conversations WHERE conversation_id=?',(grant['conversation_id'],))
    if plan[0] and task.action.tool in {'command.run','filesystem.write','filesystem.edit','interaction.act'}:
        raise AccessDenied('PLAN_MODE_READ_ONLY')
    if task.current_attempt_id is None and not step["skill_run_id"] and await one(conn,"SELECT 1 FROM chat_controls WHERE turn_id=? AND kind='steer' AND applied_call_id IS NULL",(step["turn_id"],)):
        raise AccessDenied("WORK_STOPPED")
    if grant['permission_mode'] == 'full':
        return True
    if task.action.tool in {"filesystem.write","filesystem.edit"} and step["permission_decision"]!="approve" and not grant["auto_files"]:
        raise AccessDenied("WORK_CONFIRMATION_REQUIRED")
    if task.action.tool=="interaction.act" and step["permission_decision"]!="approve" and (grant["auto_files"] or grant["profile"]!="standard"):
        import json
        prior=await conn.execute_fetchall("SELECT proposal_json FROM chat_work_steps WHERE turn_id=? AND permission_decision='approve'",(step["turn_id"],))
        if not any(json.loads(item[0])["intent"]=="interact_target" and json.loads(item[0])["arguments"]["target_id"]==task.action.arguments.target_id for item in prior):
            raise AccessDenied("WORK_CONFIRMATION_REQUIRED")
    if task.action.tool=="command.run" and grant["profile"]!="standard" and step["permission_decision"]!="approve":
        raise AccessDenied("WORK_CONFIRMATION_REQUIRED")
    return True
