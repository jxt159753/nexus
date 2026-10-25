"""Project public progress and final replies from existing durable evidence."""

import json

import aiosqlite

from nexus.contracts.chat import ChatPresentation, ChatProgressText
from nexus.core.cognitive.chat_feedback import fallback


LABELS = {
    "queued": "等待回复", "responding": "正在回复", "working": "正在执行",
    "awaiting_input": "需要你答复", "recovering": "等待核对与恢复",
    "stopping": "正在停止", "completed": "回复已完成", "stopped": "已停止",
    "incomplete": "回复未完整结束", "interrupted": "回复已中断",
    "failed": "回复失败", "expired": "已过期", "unknown": "执行结果未知",
}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


async def execution_state(conn: aiosqlite.Connection, turn: dict) -> tuple[bool, bool, bool, bool, bool]:
    """Include child executions that may still be stopping after a turn ends."""
    tasks = await conn.execute_fetchall("""
        SELECT t.status,t.execution_outcome,EXISTS(
            SELECT 1 FROM task_attempts a WHERE a.task_id=t.task_id AND a.execution_stopped=0
        ) AS unsettled FROM tasks t WHERE t.task_id=? OR t.task_id IN (
            SELECT k.task_id FROM chat_work_tasks k JOIN chat_work_steps s USING(step_id) WHERE s.turn_id=?
            UNION SELECT k.task_id FROM skill_run_steps k JOIN chat_work_steps s ON s.skill_run_id=k.run_id WHERE s.turn_id=?
            UNION SELECT k.task_id FROM skill_run_steps k WHERE k.run_id=?
        )
    """, (turn["task_id"], turn["turn_id"], turn["turn_id"], turn["skill_run_id"]))
    skills = await conn.execute_fetchall("""
        SELECT status FROM skill_runs WHERE run_id=? OR run_id IN (
            SELECT skill_run_id FROM chat_work_steps WHERE turn_id=?
        )
    """, (turn["skill_run_id"], turn["turn_id"]))
    tools = await conn.execute_fetchall("""
        SELECT r.status,e.status AS execution_status FROM extension_tool_runs r
        LEFT JOIN sandbox_executions e ON e.execution_id=r.tool_run_id
        WHERE r.work_step_id IN (SELECT step_id FROM chat_work_steps WHERE turn_id=?)
        OR r.skill_run_id=? OR r.skill_run_id IN (
            SELECT skill_run_id FROM chat_work_steps WHERE turn_id=?
        )
    """, (turn["turn_id"], turn["skill_run_id"], turn["turn_id"]))
    unsettled = (any(task["unsettled"] for task in tasks)
                 or any(skill["status"] in {"running", "cancelling"} for skill in skills)
                 or any(tool["status"] in {"dispatching", "unknown"}
                        or tool["execution_status"] in {"prepared", "sent", "unknown"} for tool in tools))
    unknown = (any(task["execution_outcome"] == "unknown" for task in tasks)
               or any(skill["status"] == "unknown" for skill in skills)
               or any(tool["status"] == "unknown" for tool in tools)
               or any((step["result"] or {}).get("execution_outcome") == "unknown" for step in turn["work_steps"]))
    pending = any(task["status"] not in TERMINAL for task in tasks)
    failed = (any(task["status"] in {"failed", "cancelled", "timed_out"} for task in tasks)
              or any(skill["status"] in {"failed", "cancelled", "timed_out"} for skill in skills)
              or any(tool["status"] == "failed" for tool in tools)
              or any(step["status"] in {"failed", "stopped"} for step in turn["work_steps"]))
    awaiting = any(task["status"] == "awaiting_approval" for task in tasks)
    return unsettled, unknown, pending, failed, awaiting


async def presentation(conn: aiosqlite.Connection, turn: dict) -> dict:
    unsettled, unknown, pending, failed, awaiting = await execution_state(conn, turn)
    status = turn["status"]
    if turn["recovery_required"]:
        phase = "recovering"
    elif unsettled and status in {"stopping", "stopped", "interrupted", "failed", "expired"}:
        phase = "stopping"
    elif unknown:
        phase = "unknown"
    elif status in {"stopping", "stopped", "interrupted", "failed", "expired", "incomplete"}:
        phase = status
    elif awaiting or turn["work_status"] == "permission" or turn["action_status"] == "proposed":
        phase = "awaiting_input"
    elif unsettled or pending or turn["work_status"] or turn["auto_result_status"] == "waiting":
        phase = "working"
    elif status in {"queued", "generating"}:
        phase = "queued" if status == "queued" else "responding"
    else:
        phase = "completed"

    # A legacy tool proposal is not its later result summary. Preserve its manual
    # result-consent flow instead of relabelling the proposal as a final report.
    legacy_result = turn["action_status"] == "submitted" and turn["phase"] == "reply"
    final = turn["response_text"] if phase == "completed" and not legacy_result else None
    if turn["phase"] == "summary" and turn["work_grant_id"] and status == "completed":
        final = turn["response_text"]
    if final is None and phase in {"stopped", "incomplete", "interrupted", "failed", "expired", "unknown", "recovering"}:
        final = fallback(turn, phase)
    if final is None and failed and legacy_result:
        final = fallback(turn, "failed")
    live = turn["response_text"] if phase in {
        "responding", "stopping", "stopped", "incomplete", "interrupted", "failed", "expired"
    } else ""
    exchanges = await conn.execute_fetchall("""
        SELECT e.call_id,e.response_json,e.partial_text FROM chat_exchanges e WHERE e.turn_id=?
        AND e.finished_at IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM chat_compactions c WHERE c.call_id=e.call_id
        ) AND NOT EXISTS (SELECT 1 FROM model_calls m WHERE m.call_id=e.call_id AND m.web_step_id IS NOT NULL)
        ORDER BY e.rowid DESC LIMIT 9
    """, (turn["turn_id"],))
    process = []
    for index, exchange in enumerate(exchanges):
        reply = json.loads(exchange["response_json"]) if exchange["response_json"] else None
        text = reply["content"] if reply else exchange["partial_text"]
        if not text or index == 0 and text in {final, live}:
            continue
        process.append(ChatProgressText(id=exchange["call_id"], text=text[:1200], truncated=len(text) > 1200))
    if not exchanges and turn["initial_response_text"]:
        text = turn["initial_response_text"]
        process.append(ChatProgressText(id="initial", text=text[:1200], truncated=len(text) > 1200))
    # Keep the latest bounded updates. Full exchanges and tool evidence remain in
    # the authenticated journal / original work records, never in this projection.
    truncated = len(process) > 8 or len(exchanges) == 9
    process = list(reversed(process[:8]))
    label = LABELS[phase]
    if phase in {"queued", "responding"} and (turn["work_steps"] or turn["phase"] == "summary"):
        label = "正在整理总结" if phase == "responding" else "等待整理总结"
    elif phase == "completed" and legacy_result:
        label = "执行已结束，结果见任务记录"
    elif phase == "completed" and (failed or turn.get("error_code")):
        label = "回复已完成，部分操作未完成"
    from nexus.core.cognitive.chat_files import turn_files
    from nexus.contracts.file_transfer import ChatFile
    from nexus.core.cognitive.conversation_routes import presentation as conversation_sources
    from nexus.contracts.conversation_routes import ConversationLink, ConversationNavigation
    sources, navigation = await conversation_sources(conn, turn)
    return ChatPresentation(
        conversations=[ConversationLink.model_validate_json(json.dumps(source)) for source in sources],
        navigation=ConversationNavigation.model_validate_json(json.dumps(navigation)) if navigation else None,
        files=[ChatFile.model_validate_json(json.dumps(file)) for file in await turn_files(conn,turn)],
        phase=phase, label=label, process=process, process_truncated=truncated,
        live_text=live, final_text=final,
        collapse_process=phase == "completed" and bool(final) and not failed and not turn.get("error_code")
                        and bool(process or turn["work_steps"] or turn["action_status"] == "submitted"),
    ).model_dump(mode="json")
