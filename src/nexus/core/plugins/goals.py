"""Goal progress is a projection of existing tasks, not another executor."""

import json
from uuid import uuid4

from nexus.contracts.autonomy import GoalCheckpoint, GoalPlan, GoalPolicy
from nexus.contracts.extensions import canonical, digest_value
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict


async def owned(conn, owner_id, conversation_id, goal_id):
    row = await one(conn, "SELECT * FROM plugin_goals WHERE goal_id=? AND owner_id=? AND conversation_id=?",
                    (str(goal_id), str(owner_id), str(conversation_id)))
    if row is None:
        raise LookupError("goal not found")
    return row


async def snapshot(conn, goal_id, *, step_id=None):
    goal = await one(conn, "SELECT * FROM plugin_goals WHERE goal_id=?", (str(goal_id),))
    if goal is None:
        return None
    plan = await one(conn, "SELECT * FROM goal_plans WHERE goal_id=?", (str(goal_id),))
    result = dict(goal)
    result["completion_verified"] = bool(goal["completion_verified"])
    result["verification"] = "declared_task_assertions" if goal["completion_verified"] else "unverified"
    result["plan"] = json.loads(plan["plan_json"]) if plan else None
    result["plan_revision"] = plan["revision"] if plan else 0
    if result['plan'] is not None:
        result['plan']['expected_revision'] = plan['revision']
    result["policy"] = {k: plan[k] for k in ("resume_after_restart", "priority", "policy_revision", "replans", "last_reason")} if plan else {}
    rows = await conn.execute_fetchall("SELECT * FROM goal_steps WHERE goal_id=? ORDER BY rowid", (str(goal_id),))
    result["steps"] = [{key:row[key] for key in ('step_id','status','turn_id')} for row in rows]
    result["evidence"] = [dict(row) for row in await conn.execute_fetchall("SELECT * FROM goal_evidence WHERE goal_id=?", (str(goal_id),))]
    result["checkpoints"] = [json.loads(row[0]) for row in await conn.execute_fetchall(
        "SELECT value_json FROM goal_checkpoints WHERE goal_id=? ORDER BY revision DESC LIMIT 3", (str(goal_id),))]
    result['watches'] = [dict(row) for row in await conn.execute_fetchall(
        'SELECT watch_id,kind,target,priority,paused,expires_at FROM autonomy_watches WHERE goal_id=? ORDER BY priority DESC,rowid DESC LIMIT 16', (str(goal_id),))]
    if step_id is not None:
        if not any(step['step_id']==step_id for step in result['steps']):
            raise LookupError('goal step not found')
        last=await one(conn,'SELECT value_json FROM goal_checkpoints WHERE goal_id=? AND step_id=? ORDER BY revision DESC LIMIT 1',(str(goal_id),step_id))
        result['step_checkpoint']=json.loads(last[0]) if last else None
    return result


def model_view(value):
    """Keep the fixed plan intact; fetch verbose checkpoint notes on demand."""
    if value is None:
        return None
    value=dict(value)
    value['checkpoints']=[{'step_id':item['step_id'],'status':item['status'],'note':item['note'][:120],
                          'remaining_count':len(item['remaining']),'details':'get_goal(step_id='+item['step_id']+')'} for item in value['checkpoints']]
    value['evidence']=[{key:item[key] for key in ('criterion_id','task_id','attempt_id')} for item in value['evidence']]
    value['watches']=[{key:item[key] for key in ('watch_id','kind','priority','paused')} for item in value['watches']]
    return value


async def set_plan(conn, goal, value: GoalPlan):
    if goal["status"] == "complete" or goal["expires_at"] <= timestamp(utc_now()):
        raise Conflict("goal has ended")
    current = await one(conn, "SELECT * FROM goal_plans WHERE goal_id=?", (goal["goal_id"],))
    revision = current["revision"] if current else 0
    if value.expected_revision != revision:
        raise Conflict("goal plan changed")
    old_steps = await conn.execute_fetchall("SELECT * FROM goal_steps WHERE goal_id=?", (goal["goal_id"],))
    new_steps = {step.id: step.model_dump(mode="json") for step in value.steps}
    if current:
        previous = GoalPlan.model_validate_json(current["plan_json"])
        if value.criteria != previous.criteria or value.constraints != previous.constraints:
            raise AccessDenied("GOAL_ACCEPTANCE_IMMUTABLE")
        if current["replans"] >= 3 or not value.reason.strip():
            raise AccessDenied("GOAL_REPLAN_LIMIT_OR_REASON_REQUIRED")
        for step in old_steps:
            if step["status"] in {"running", "succeeded"} and new_steps.get(step["step_id"]) != json.loads(step["definition_json"]):
                raise AccessDenied("GOAL_COMPLETED_OR_RUNNING_WORK_IMMUTABLE")
        # A retry keeps its identity and evidence history. It cannot masquerade
        # as a new step to reset the goal's root round budget.
        old = {step["step_id"]: step for step in old_steps}
        for identifier, definition in new_steps.items():
            if identifier not in old and any(definition["instruction"] == json.loads(step["definition_json"])["instruction"] for step in old_steps):
                raise AccessDenied("GOAL_DUPLICATE_WORK")
        await settled(conn, goal["goal_id"])
        await conn.execute("UPDATE goal_plans SET revision=revision+1,replans=replans+1,plan_json=?,last_reason=? WHERE goal_id=?",
                           (value.model_dump_json(), value.reason, goal["goal_id"]))
    else:
        if (await one(conn, "SELECT count(*) FROM goal_plans"))[0] >= 1000:
            raise AccessDenied("GOAL_STORAGE_FULL")
        await conn.execute("INSERT INTO goal_plans(goal_id,plan_json,last_progress_round) VALUES(?,?,?)", (goal["goal_id"], value.model_dump_json(), goal['rounds']))
    # Keep finished work and its references; only replace unexecuted definitions.
    for step in old_steps:
        if step["step_id"] not in new_steps:
            if step["turn_id"]:
                raise AccessDenied("GOAL_ATTEMPTED_WORK_MUST_REMAIN")
            await conn.execute("DELETE FROM goal_steps WHERE goal_id=? AND step_id=?", (goal["goal_id"], step["step_id"]))
    for identifier, definition in new_steps.items():
        await conn.execute("""INSERT INTO goal_steps(goal_id,step_id,definition_json,status) VALUES(?,?,?,'pending')
            ON CONFLICT(goal_id,step_id) DO UPDATE SET definition_json=excluded.definition_json,
            status=CASE WHEN goal_steps.status IN ('succeeded','running') THEN goal_steps.status ELSE 'pending' END""",
                           (goal["goal_id"], identifier, canonical(definition)))
    await audit(conn, goal["owner_id"], None, "goal.plan_saved", utc_now())
    return await snapshot(conn, goal["goal_id"])


async def policy(conn, goal, value: GoalPolicy):
    plan = await one(conn, "SELECT * FROM goal_plans WHERE goal_id=?", (goal["goal_id"],))
    if plan is None:
        raise ValueError("define acceptance and a plan before enabling recovery")
    if plan["policy_revision"] != value.expected_revision:
        raise Conflict("goal policy changed")
    await conn.execute("UPDATE goal_plans SET policy_revision=policy_revision+1,resume_after_restart=?,priority=? WHERE goal_id=?",
                       (int(value.resume_after_restart), value.priority, goal["goal_id"]))
    await audit(conn, goal["owner_id"], None, "goal.policy_changed", utc_now())
    return await snapshot(conn, goal["goal_id"])


def pointer(value, path):
    for key in path.split("/")[1:] if path else []:
        key = key.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isdigit() or int(key) >= len(value):
                raise ValueError("assertion path unavailable")
            value = value[int(key)]
        elif isinstance(value, dict) and key in value:
            value = value[key]
        else:
            raise ValueError("assertion path unavailable")
    return value


def matches(value, assertions):
    return all(canonical(pointer(value, item.path)) == canonical(item.equals) for item in assertions)


async def task_evidence(conn, goal, criterion, task_id, *, not_before=None):
    task = await one(conn, "SELECT * FROM tasks WHERE task_id=? AND owner_id=?", (str(task_id), goal["owner_id"]))
    if task is None:
        raise AccessDenied("GOAL_EVIDENCE_SCOPE_DENIED")
    linked = await one(conn, """SELECT 1 FROM chat_work_steps s
        JOIN plugin_authorities a ON a.authority_id=?
        WHERE (s.task_id=? OR EXISTS(SELECT 1 FROM chat_work_tasks k WHERE k.step_id=s.step_id AND k.task_id=?)
            OR EXISTS(SELECT 1 FROM skill_run_steps k WHERE k.run_id=s.skill_run_id AND k.task_id=?))
        AND (s.turn_id=a.root_turn_id OR s.turn_id IN (SELECT turn_id FROM plugin_wakeups WHERE authority_id=a.authority_id)) LIMIT 1""",
                       (goal["goal_id"], str(task_id), str(task_id), str(task_id)))
    if not linked or task["created_at"] < goal["created_at"]:
        raise AccessDenied("GOAL_EVIDENCE_SCOPE_DENIED")
    if not_before and task['created_at']<not_before:
        raise AccessDenied('GOAL_EVIDENCE_PRECEDES_DEPENDENCY')
    if task["status"] != "succeeded" or task["execution_outcome"] != "known" or not task["verification"]:
        raise AccessDenied("GOAL_EVIDENCE_UNVERIFIED")
    attempt = await one(conn, "SELECT * FROM task_attempts WHERE attempt_id=? AND task_id=?", (task["current_attempt_id"], str(task_id)))
    if attempt is None or not attempt["execution_stopped"] or attempt["phase"] != "completed" or not attempt["result_json"]:
        raise AccessDenied("GOAL_EVIDENCE_UNSETTLED")
    action, result = json.loads(task["action_json"]), json.loads(task["result_json"])
    if action["tool"] != criterion.tool or not matches(action["arguments"], criterion.arguments) or not matches(result, criterion.assertions):
        raise AccessDenied("GOAL_ASSERTION_FAILED")
    # Empty argument constraints are reasonable only for argument-free tools.
    if action["arguments"] and not criterion.arguments:
        raise AccessDenied("GOAL_ARGUMENT_BINDING_REQUIRED")
    data = result.get("data", result)
    if isinstance(data, dict) and (data.get("status") in {"failed", "unknown", "running", "timed_out"}
                                  or data.get("exit_code", 0) not in (None, 0)
                                  or data.get("execution_stopped") is False):
        raise AccessDenied("GOAL_EFFECT_NOT_FINISHED")
    return {"task_id": str(task_id), "attempt_id": attempt["attempt_id"], "digest": digest_value({
        "action": action, "result": result, "command_digest": attempt["command_digest"], "verification": task["verification"]})}


async def settled(conn, goal_id):
    """No new segment or completion may turn an unknown effect into a retry."""
    turns = "SELECT root_turn_id FROM plugin_authorities WHERE authority_id=? UNION SELECT turn_id FROM plugin_wakeups WHERE authority_id=?"
    tasks = await conn.execute_fetchall(f"""WITH goal_turns AS ({turns}), steps AS (
        SELECT step_id,task_id,skill_run_id FROM chat_work_steps WHERE turn_id IN (SELECT * FROM goal_turns)
        ), related AS (
        SELECT task_id FROM steps UNION SELECT k.task_id FROM chat_work_tasks k JOIN steps s USING(step_id)
        UNION SELECT k.task_id FROM skill_run_steps k JOIN steps s ON s.skill_run_id=k.run_id)
        SELECT t.* FROM tasks t JOIN related r USING(task_id)""", (goal_id, goal_id))
    for task in tasks:
        if task["execution_outcome"] == "unknown":
            raise AccessDenied("EXECUTION_UNKNOWN")
        if task["status"] not in {"succeeded", "failed", "cancelled", "timed_out"} or await one(conn, "SELECT 1 FROM task_attempts WHERE task_id=? AND execution_stopped=0", (task["task_id"],)):
            raise Conflict("BUSY")
    if await one(conn, f"""SELECT 1 FROM chat_work_steps s LEFT JOIN extension_tool_runs r
        ON r.work_step_id=s.step_id OR r.skill_run_id=s.skill_run_id LEFT JOIN sandbox_executions e ON e.execution_id=r.tool_run_id
        WHERE s.turn_id IN ({turns}) AND (r.status IN ('dispatching','unknown') OR e.status IN ('prepared','sent','unknown')
        OR EXISTS(SELECT 1 FROM skill_runs sk WHERE sk.run_id=s.skill_run_id AND sk.status IN ('running','cancelling'))) LIMIT 1""", (goal_id, goal_id)):
        raise AccessDenied("EXECUTION_UNKNOWN")
    if await one(conn,f"""SELECT 1 FROM plugin_test_runs r JOIN chat_work_steps s ON s.step_id=r.work_step_id
        WHERE s.turn_id IN ({turns}) AND (r.status IN ('pending','running','unknown')
        OR EXISTS(SELECT 1 FROM plugin_sandbox_executions e WHERE e.test_run_id=r.test_run_id AND e.status IN ('prepared','sent','unknown'))) LIMIT 1""",(goal_id,goal_id)):
        raise AccessDenied('EXECUTION_UNKNOWN')


async def checkpoint(conn, goal, value: GoalCheckpoint, *, turn_id=None):
    plan = await one(conn, "SELECT * FROM goal_plans WHERE goal_id=?", (goal["goal_id"],))
    if plan is None or plan["revision"] != value.expected_revision:
        raise Conflict("goal plan changed")
    if goal["status"] != "active" or goal["expires_at"] <= timestamp(utc_now()):
        raise AccessDenied("GOAL_NOT_ACTIVE")
    if (await one(conn, 'SELECT count(*) FROM goal_checkpoints WHERE goal_id=?', (goal['goal_id'],)))[0] >= 100:
        raise AccessDenied('GOAL_CHECKPOINT_LIMIT')
    step = await one(conn, "SELECT * FROM goal_steps WHERE goal_id=? AND step_id=?", (goal["goal_id"], value.step_id))
    if step is None or step["status"] == "succeeded":
        raise ValueError("step absent or already completed")
    definition = json.loads(step["definition_json"])
    not_before = None
    for parent in definition["depends_on"]:
        row = await one(conn, "SELECT status FROM goal_steps WHERE goal_id=? AND step_id=?", (goal["goal_id"], parent))
        if row[0] != "succeeded":
            raise AccessDenied("GOAL_DEPENDENCY_PENDING")
        checkpoint_time=await one(conn,"SELECT created_at FROM goal_checkpoints WHERE goal_id=? AND step_id=? AND json_extract(value_json,'$.status')='succeeded' ORDER BY revision DESC LIMIT 1",(goal['goal_id'],parent))
        if checkpoint_time is None:
            raise AccessDenied('GOAL_DEPENDENCY_EVIDENCE_MISSING')
        not_before=max(not_before or '',checkpoint_time[0])
    await settled(conn, goal["goal_id"])
    criteria = {item.id: item for item in GoalPlan.model_validate_json(plan["plan_json"]).criteria}
    for binding in value.evidence:
        if binding.criterion_id not in definition["criteria"]:
            raise ValueError("criterion not assigned to step")
        evidence = await task_evidence(conn, goal, criteria[binding.criterion_id], binding.task_id,not_before=not_before)
        await conn.execute("""INSERT INTO goal_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(goal_id,criterion_id)
            DO UPDATE SET task_id=excluded.task_id,attempt_id=excluded.attempt_id,digest=excluded.digest,created_at=excluded.created_at""",
                           (goal["goal_id"], binding.criterion_id, evidence["task_id"], evidence["attempt_id"], evidence["digest"], timestamp(utc_now())))
    if value.status == "succeeded":
        await verify_criteria(conn, goal, plan, definition["criteria"],not_before=not_before)
    await conn.execute("UPDATE goal_steps SET status=?,turn_id=coalesce(turn_id,?) WHERE goal_id=? AND step_id=?",
                       (value.status, turn_id, goal["goal_id"], value.step_id))
    await conn.execute("UPDATE goal_plans SET revision=revision+1,last_reason=?,last_progress_round=CASE WHEN ?='succeeded' THEN ? ELSE last_progress_round END WHERE goal_id=?", (value.note, value.status, goal['rounds'], goal["goal_id"]))
    await conn.execute("INSERT INTO goal_checkpoints VALUES(?,?,?,?,?,?)", (str(uuid4()), goal["goal_id"], plan["revision"] + 1, value.step_id, value.model_dump_json(), timestamp(utc_now())))
    await audit(conn, goal["owner_id"], None, "goal.checkpoint", utc_now())
    return await snapshot(conn, goal["goal_id"])


async def verify_criteria(conn, goal, plan, identifiers=None, *, not_before=None):
    for criterion in GoalPlan.model_validate_json(plan["plan_json"]).criteria:
        if identifiers is not None and criterion.id not in identifiers:
            continue
        saved = await one(conn, "SELECT * FROM goal_evidence WHERE goal_id=? AND criterion_id=?", (goal["goal_id"], criterion.id))
        if saved is None:
            raise AccessDenied("GOAL_EVIDENCE_MISSING")
        current = await task_evidence(conn, goal, criterion, saved["task_id"],not_before=not_before)
        if current["digest"] != saved["digest"] or current["attempt_id"] != saved["attempt_id"]:
            raise AccessDenied("GOAL_EVIDENCE_CHANGED")


async def verify_completion(conn, goal):
    if goal['status']!='active' or goal['expires_at']<=timestamp(utc_now()):
        raise AccessDenied('GOAL_NOT_ACTIVE')
    plan = await one(conn, "SELECT * FROM goal_plans WHERE goal_id=?", (goal["goal_id"],))
    if plan is None:
        raise AccessDenied("GOAL_ACCEPTANCE_REQUIRED")
    await settled(conn, goal["goal_id"])
    if await one(conn, "SELECT 1 FROM goal_steps WHERE goal_id=? AND status!='succeeded'", (goal["goal_id"],)):
        raise AccessDenied("GOAL_STEPS_INCOMPLETE")
    await verify_criteria(conn, goal, plan)
    return True


async def next_step(conn, goal_id):
    rows = await conn.execute_fetchall("SELECT * FROM goal_steps WHERE goal_id=? ORDER BY rowid", (goal_id,))
    states = {row["step_id"]: row["status"] for row in rows}
    for row in rows:
        if row["status"] in {"pending", "running"} and all(states[parent] == "succeeded" for parent in json.loads(row["definition_json"])["depends_on"]):
            return row
    return None


async def continuation(conn, goal):
    plan = await one(conn, "SELECT * FROM goal_plans WHERE goal_id=?", (goal["goal_id"],))
    await settled(conn, goal["goal_id"])
    if plan is None:
        return None, None
    if goal['rounds'] - plan['last_progress_round'] >= 3:
        raise AccessDenied('GOAL_NO_VERIFIED_PROGRESS')
    step = await next_step(conn, goal["goal_id"])
    value = json.loads(plan["plan_json"])
    instruction = json.loads(step["definition_json"])["instruction"] if step else "检查失败步骤，按原验收条件重规划；全部证据通过后完成目标。"
    text = ("继续目标 " + goal["goal_id"] + "。先调用 builtin.goal.get_goal 读取完整计划、检查点和原任务证据，勿重复已完成操作。\n"
            + "目标：" + goal["objective"][:350] + "\n持续约束：" + "；".join(value["constraints"])
            + "\n本段：" + instruction + "\n用 goal_checkpoint 保存真实任务证据和未完成事项；失败须有原因并保留已完成步骤，不能放宽验收。")
    return text, step["step_id"] if step else None
