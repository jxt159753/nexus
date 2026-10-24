"""Read-only DSH-style clock and jobs tools over existing Core authority."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from nexus.core.cognitive.chat_context import project_result
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp
from nexus.core.storage.tasks import TERMINAL, load_task

NAMES = frozenset({"get_current_time", "job_list", "job_output"})

# Cover tasks created by legacy actions, work operations and fixed Skills.
TASK_IDS = """
SELECT task_id FROM chat_turns WHERE conversation_id=? AND (? IS NULL OR turn_id=?) AND task_id IS NOT NULL
UNION SELECT k.task_id FROM chat_work_tasks k JOIN chat_work_steps w USING(step_id)
 JOIN chat_turns c USING(turn_id) WHERE c.conversation_id=? AND (? IS NULL OR c.turn_id=?)
UNION SELECT s.task_id FROM skill_run_steps s JOIN chat_turns c ON c.skill_run_id=s.run_id
 WHERE c.conversation_id=? AND (? IS NULL OR c.turn_id=?) AND s.task_id IS NOT NULL
UNION SELECT s.task_id FROM skill_run_steps s JOIN chat_work_steps w ON w.skill_run_id=s.run_id
 JOIN chat_turns c USING(turn_id) WHERE c.conversation_id=? AND (? IS NULL OR c.turn_id=?) AND s.task_id IS NOT NULL
"""


async def query(conn, step, grant, intent, arguments, now):
    turn = await one(conn, "SELECT conversation_id,created_at FROM chat_turns WHERE turn_id=? AND owner_id=?",
                     (step["turn_id"], grant["owner_id"]))
    if intent == "get_current_time":
        return {"utc": timestamp(now), "local": now.astimezone(timezone(timedelta(minutes=arguments["utc_offset_minutes"]))).isoformat(),
                "utc_offset_minutes": arguments["utc_offset_minutes"],
                "elapsed_since_turn_seconds": max(0, int((now-datetime.fromisoformat(turn["created_at"])).total_seconds()))}
    turn_id = step["turn_id"] if intent == "job_output" else None
    params = (turn["conversation_id"], turn_id, turn_id) * 4
    base = "SELECT t.* FROM tasks t WHERE t.task_id IN (" + TASK_IDS + ") AND t.owner_id=? AND t.node_id=?"
    params += (grant["owner_id"], grant["node_id"])
    if intent == "job_list":
        rows = await conn.execute_fetchall(base + " ORDER BY t.rowid DESC LIMIT ?", (*params, arguments["limit"]+1))
        keys = ("task_id", "status", "execution_outcome", "verification", "current_attempt_id", "cancel_requested", "created_at", "expires_at")
        return {"observed_at": timestamp(now), "jobs": [
            {**{key: row[key] for key in keys}, "tool": json.loads(row["action_json"])["tool"]}
            for row in rows[:arguments["limit"]]], "truncated": len(rows) > arguments["limit"]}
    target = await one(conn, base + " AND t.task_id=?", (*params, arguments["task_id"]))
    if target is None:
        raise AccessDenied("POLICY_DENIED")
    from nexus.core.cognitive.chat_work import task_feedback
    task = await load_task(conn, UUID(target["task_id"]))
    result = task_feedback(task) if task.status in TERMINAL else {
        "status": task.status, "execution_outcome": task.execution_outcome, "verification": task.verification}
    if task.action.tool == "filesystem.read_image" and isinstance(result.get("data"), dict):
        result["data"]["image"].pop("data_base64", None)
        result["data"]["image"]["notice"] = "此查询仅返回图片元数据；图片由原 read_image 调用提供。"
    return {"task_id": target["task_id"], "observed_at": timestamp(now),
            "output": project_result(result, arguments["max_bytes"])}
