"""Durable single-step dispatch and evidence settlement. No network I/O."""

from nexus.contracts.basic import MUTATING_TOOLS

from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from nexus.contracts.commands import command_digest
from nexus.contracts.errors import ErrorCode, ProtocolError, create_error
from nexus.contracts.messages import CommandExecute, CommandReceipt, CommandResult, CommandStatusResult
from nexus.contracts.tools import validate_result
from nexus.core.security.service import SecurityService, assert_principal, scope_digest
from nexus.core.security.types import AccessDenied, NodeScope, Principal
from nexus.core.storage.database import Database, audit, one, timestamp, utc_now
from nexus.core.storage.control import expire_task, task_event
from nexus.core.storage.tasks import TERMINAL, canonical, load_task, transition
from nexus.core.extensions.authorization import skill_task_allowed

COMMAND = TypeAdapter(CommandExecute)
RESULT = TypeAdapter(CommandResult)


def invalid() -> ProtocolError:
    return ProtocolError(ErrorCode.RESULT_INVALID, "Evidence does not match the active attempt")


def evidence_json(result: CommandResult) -> str:
    # Reconnecting may change only transport identity, never execution evidence.
    return canonical(result.model_dump(mode="json", exclude={"session_id"}))


class ExecutionRepository:
    def __init__(self, database: Database, security: SecurityService, max_result_bytes: int):
        self.database, self.security, self.max_result_bytes = database, security, max_result_bytes

    async def pending(self, node_id: UUID) -> list[UUID]:
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT task_id FROM tasks WHERE node_id=? AND status IN ('ready','awaiting_approval','recovering') AND cancel_requested=0 AND expires_at>? AND (current_attempt_id IS NULL OR EXISTS(SELECT 1 FROM task_attempts a WHERE a.attempt_id=tasks.current_attempt_id AND a.phase='completed' AND a.execution_stopped=1)) ORDER BY rowid LIMIT 100", (str(node_id), timestamp(utc_now())))
            return [UUID(row[0]) for row in rows]

    async def dispatch(self, task_id: UUID, node: Principal, scope: NodeScope, session_id: UUID, boot_id: UUID, timeout: int, *, mcp_timeout: int = 10) -> CommandExecute | None:
        now = utc_now()
        async with self.database.transaction() as conn:
            task = await load_task(conn, task_id)
            if not await skill_task_allowed(conn, task_id, now):
                return None
            authorization = await self.security.authorize_dispatch(conn, task, node, scope, now)
            if task.cancel_requested or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                return None
            count = (await one(conn, "SELECT count(*) FROM task_attempts WHERE task_id=?", (str(task_id),)))[0]
            if count >= (1 if task.action.tool in MUTATING_TOOLS else 2):
                return None
            if task.current_attempt_id is not None:
                previous = await one(conn, "SELECT * FROM task_attempts WHERE attempt_id=?", (str(task.current_attempt_id),))
                if previous is None or previous["phase"] != "completed" or not previous["execution_stopped"]:
                    return None
            if await one(conn, "SELECT 1 FROM task_attempts WHERE node_id=? AND phase!='completed'", (str(node.identity_id),)):
                return None
            grant_id, attempt_id = uuid4(), uuid4()
            expires = min(task.expires_at, authorization.expires_at or task.expires_at)
            value = task.action.model_dump(mode="json")
            if task.action.tool == "plugin.call" and task.action.arguments.plugin_id == "builtin.mcp":
                timeout = mcp_timeout
            value.update(session_id=str(session_id), task_id=str(task_id), attempt_id=str(attempt_id),
                         grant_id=str(grant_id), command_digest="0" * 64,
                         expires_at=timestamp(expires), execution_timeout_seconds=timeout)
            command = COMMAND.validate_json(canonical(value))
            command = command.model_copy(update={"command_digest": command_digest(command, node.identity_id)})
            await conn.execute("INSERT INTO execution_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                str(grant_id), str(task_id), str(node.identity_id), authorization.policy_version,
                scope_digest(scope), str(authorization.approval_id) if authorization.approval_id else None,
                command.command_digest, timestamp(expires),
            ))
            await conn.execute("INSERT INTO task_attempts(attempt_id, task_id, node_id, grant_id, attempt_no, dispatch_count, phase, command_json, command_digest, boot_id, created_at) VALUES (?, ?, ?, ?, ?, 1, 'dispatching', ?, ?, ?, ?)", (
                str(attempt_id), str(task_id), str(node.identity_id), str(grant_id), count + 1, command.model_dump_json(),
                command.command_digest, str(boot_id), timestamp(now),
            ))
            await conn.execute("UPDATE tasks SET current_attempt_id=?, execution_outcome='unknown',error_json=NULL WHERE task_id=?", (str(attempt_id), str(task_id)))
            await transition(conn, task, "dispatching", task.owner_id, now, execution=True)
            return command  # transaction context must commit before caller sends

    async def _active(self, conn, node: Principal, attempt_id: UUID, *, historical=False):
        await assert_principal(conn, node, utc_now())
        row = await one(conn, "SELECT * FROM task_attempts WHERE attempt_id=?", (str(attempt_id),))
        if node.role != "node" or row is None or row["node_id"] != str(node.identity_id):
            raise invalid()
        task = await load_task(conn, UUID(row["task_id"]))
        if task.current_attempt_id != attempt_id and not (historical and row["phase"] == "completed"):
            raise invalid()
        return task, row

    async def receipt(self, node: Principal, boot_id: UUID, receipt: CommandReceipt) -> None:
        async with self.database.transaction() as conn:
            task, row = await self._active(conn, node, receipt.attempt_id, historical=True)
            if receipt.task_id != task.task_id or receipt.command_digest != row["command_digest"] or receipt.boot_id != boot_id:
                raise invalid()
            if task.current_attempt_id == receipt.attempt_id and task.status in {"dispatching", "recovering"} and row["phase"] != "completed":
                await conn.execute("UPDATE task_attempts SET phase='running' WHERE attempt_id=?", (str(receipt.attempt_id),))
                await transition(conn, task, "running", node.identity_id, utc_now(), execution=True)

    async def store_result(self, node: Principal, result: CommandResult) -> str:
        result = RESULT.validate_json(result.model_dump_json())
        if result.result is not None and len(result.result.model_dump_json().encode("utf-8")) > self.max_result_bytes:
            raise invalid()
        async with self.database.transaction() as conn:
            task, row = await self._active(conn, node, result.attempt_id, historical=True)
            command = COMMAND.validate_json(row["command_json"])
            if (result.task_id != task.task_id or result.command_digest != command.command_digest
                    or result.tool != command.tool or result.tool_version != command.tool_version):
                raise invalid()
            if row["result_json"] is not None:
                if evidence_json(RESULT.validate_json(row["result_json"])) != evidence_json(result):
                    raise invalid()
                return "already_settled" if task.status in TERMINAL or row["phase"] == "completed" else "stored"
            now = utc_now()
            if not (task.created_at - timedelta(seconds=5) <= result.observed_at <= min(now, command.expires_at) + timedelta(seconds=5)):
                if row["phase"] == "completed":
                    return "already_settled"
                # An authenticated, identity-bound completion proves the worker
                # ended even when device clock skew invalidates its output time.
                # Reject the business result without permanently holding the slot.
                await audit(conn, str(node.identity_id), str(task.task_id), "attempt.result_time_rejected", now)
                await self._ended(conn, task, row, now, outcome="unknown", retry=False)
                return "already_settled"
            task = await expire_task(conn, task, now)
            if task.status in TERMINAL or task.current_attempt_id != result.attempt_id or row["phase"] == "completed":
                await conn.execute("UPDATE task_attempts SET result_json=?, phase='completed', execution_stopped=1 WHERE attempt_id=?", (result.model_dump_json(), str(result.attempt_id)))
                await audit(conn, str(node.identity_id), str(task.task_id), "attempt.late_result", now)
                if task.current_attempt_id == result.attempt_id and task.execution_outcome == "unknown":
                    await conn.execute("UPDATE tasks SET execution_outcome='known' WHERE task_id=?", (str(task.task_id),))
                    await task_event(conn, task, "task.execution_reconciled", node.identity_id, now)
                return "already_settled"
            if task.status not in {"dispatching", "running", "recovering"}:
                raise invalid()
            await conn.execute("UPDATE task_attempts SET result_json=?, phase='verifying' WHERE attempt_id=?", (result.model_dump_json(), str(result.attempt_id)))
            await transition(conn, task, "verifying", node.identity_id, now, execution=True)
        return "stored"

    async def verify(self, attempt_id: UUID) -> None:
        async with self.database.transaction() as conn:
            row = await one(conn, "SELECT * FROM task_attempts WHERE attempt_id=?", (str(attempt_id),))
            if row is None:
                raise LookupError("attempt not found")
            task = await load_task(conn, UUID(row["task_id"]))
            if task.status != "verifying" or task.current_attempt_id != attempt_id:
                return
            now = utc_now()
            task = await expire_task(conn, task, now)
            if task.status in TERMINAL:
                await conn.execute("UPDATE task_attempts SET phase='completed',execution_stopped=1 WHERE attempt_id=?", (str(attempt_id),))
                await conn.execute("UPDATE tasks SET execution_outcome='known' WHERE task_id=?", (str(task.task_id),))
                return
            result = RESULT.validate_json(row["result_json"])
            command = COMMAND.validate_json(row["command_json"])
            output, error, verification = None, result.error, None
            if result.outcome == "succeeded":
                try:
                    output = validate_result(command, result.result, self.max_result_bytes)
                    verification = "node_report_schema_checked"
                except ValueError:
                    error = create_error(ErrorCode.RESULT_INVALID, "Tool evidence failed validation")
            succeeded = output is not None
            if not succeeded and not task.cancel_requested and task.action.tool not in MUTATING_TOOLS and error is not None and error.code in {ErrorCode.TOOL_TIMEOUT, ErrorCode.BUSY}:
                await self._ended(conn, task, row, now, outcome="known", retry=True)
                return
            await conn.execute("UPDATE tasks SET result_json=?, error_json=?, verification=?, execution_outcome='known' WHERE task_id=?", (
                output.model_dump_json() if output else None, error.model_dump_json() if error else None,
                verification, str(task.task_id),
            ))
            await conn.execute("UPDATE task_attempts SET phase='completed', execution_stopped=1 WHERE attempt_id=?", (str(attempt_id),))
            await transition(conn, task, "succeeded" if succeeded else "failed", task.owner_id, utc_now(), execution=True)

    async def _ended(self, conn, task, row, now, *, outcome: str, retry: bool) -> None:
        """Only call with positive evidence the old execution cannot continue."""
        await conn.execute("UPDATE task_attempts SET phase='completed',execution_stopped=1 WHERE attempt_id=?", (row["attempt_id"],))
        await conn.execute("UPDATE tasks SET execution_outcome=? WHERE task_id=?", (outcome, str(task.task_id)))
        if task.status in TERMINAL:
            if task.execution_outcome != outcome:
                await task_event(conn, task, "task.execution_reconciled", task.owner_id, now)
            return
        task = await expire_task(conn, task, now)
        if task.status in TERMINAL:
            return
        if task.cancel_requested:
            await transition(conn, task, "cancelled", task.owner_id, now, execution=True)
        elif retry and row["attempt_no"] < 2 and task.action.tool not in MUTATING_TOOLS:
            if task.status != "recovering":
                await transition(conn, task, "recovering", task.owner_id, now, execution=True)
            else:
                await task_event(conn, task, "attempt.retry_ready", task.owner_id, now)
        else:
            exhausted = retry and task.action.tool not in MUTATING_TOOLS
            code = ErrorCode.BUDGET_EXCEEDED if exhausted else ErrorCode.EXECUTION_UNKNOWN
            await conn.execute("UPDATE tasks SET error_json=? WHERE task_id=?", (create_error(code, "Execution recovery attempts exhausted" if exhausted else "Execution stopped without a verified result; it was not repeated").model_dump_json(), str(task.task_id)))
            await transition(conn, task, "failed", task.owner_id, now, execution=True)

    async def active(self, node_id: UUID) -> list[dict]:
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT a.*,t.cancel_requested,t.status AS task_status FROM task_attempts a JOIN tasks t USING(task_id) WHERE a.node_id=? AND a.phase!='completed' LIMIT 1", (str(node_id),))
            return [dict(row) for row in rows]

    async def reserve_probe(self, attempt_id: UUID) -> bool:
        async with self.database.transaction() as conn:
            cursor = await conn.execute("UPDATE task_attempts SET probe_count=probe_count+1 WHERE attempt_id=? AND phase!='completed' AND probe_count<64", (str(attempt_id),))
            return cursor.rowcount == 1

    async def recovering(self, attempt_id: UUID) -> None:
        async with self.database.transaction() as conn:
            row = await one(conn, "SELECT * FROM task_attempts WHERE attempt_id=?", (str(attempt_id),))
            task = await load_task(conn, UUID(row["task_id"]))
            if task.status in {"dispatching", "running"}:
                await transition(conn, task, "recovering", task.owner_id, utc_now(), execution=True)

    async def status(self, node: Principal, status: CommandStatusResult) -> str:
        """Returns a transport recommendation; it never starts an execution."""
        async with self.database.transaction() as conn:
            task, row = await self._active(conn, node, status.attempt_id, historical=True)
            if status.phase != "not_found" and status.command_digest != row["command_digest"]:
                raise invalid()
            if row["phase"] in {"completed", "verifying"}:
                return "already_settled" if row["phase"] == "completed" and (status.phase == "cancelled" or status.phase == "unknown" and status.execution_stopped) else "none"
            now = utc_now()
            task = await expire_task(conn, task, now)
            if status.phase == "cancelled":
                await self._ended(conn, task, row, now, outcome="known", retry=not task.cancel_requested)
                return "already_settled"
            if status.phase == "unknown":
                if status.execution_stopped:
                    await self._ended(conn, task, row, now, outcome="unknown", retry=True)
                    return "already_settled"
                await conn.execute("UPDATE task_attempts SET phase='unknown' WHERE attempt_id=?", (str(status.attempt_id),))
            if task.status in TERMINAL or task.cancel_requested or row["stop_reason"]:
                return "none"  # Periodic stop requests avoid a status/cancel feedback loop.
            if status.phase in {"not_found", "received"}:
                return "resend"
            if status.phase == "started":
                await conn.execute("UPDATE task_attempts SET phase='running' WHERE attempt_id=?", (str(status.attempt_id),))
                if task.status in {"dispatching", "recovering"}:
                    await transition(conn, task, "running", node.identity_id, now, execution=True)
            elif task.status in {"dispatching", "running"}:
                await transition(conn, task, "recovering", node.identity_id, now, execution=True)
            return "none"

    async def cancelled(self, node: Principal, attempt_id: UUID) -> None:
        async with self.database.transaction() as conn:
            task, row = await self._active(conn, node, attempt_id, historical=True)
            if row["phase"] == "completed":
                return
            if not task.cancel_requested and task.status not in TERMINAL and not row["stop_reason"]:
                raise invalid()
            await self._ended(conn, task, row, utc_now(), outcome="known", retry=False)

    async def may_send(self, node: Principal, scope: NodeScope, command: CommandExecute) -> bool:
        async with self.database.transaction(write=False) as conn:
            if not await skill_task_allowed(conn, command.task_id, utc_now()):
                return False
            task, row = await self._active(conn, node, command.attempt_id)
            if (task.status not in {"dispatching", "running", "recovering"} or task.cancel_requested or row["phase"] in {"completed", "verifying"}
                    or row["stop_reason"] or utc_now() >= command.expires_at
                    or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]):
                return False
            try:
                auth = await self.security.authorize_dispatch(conn, task.model_copy(update={"status": "ready"}), node, scope, utc_now())
            except AccessDenied:
                return False
            grant = await one(conn, "SELECT * FROM execution_grants WHERE grant_id=?", (str(command.grant_id),))
            return grant["policy_version"] == auth.policy_version and grant["scope_digest"] == scope_digest(scope)

    async def resend(self, node: Principal, scope: NodeScope, attempt_id: UUID, session_id: UUID) -> CommandExecute | None:
        async with self.database.transaction() as conn:
            task, row = await self._active(conn, node, attempt_id)
            now = utc_now()
            if not await skill_task_allowed(conn, task.task_id, now):
                return None
            if task.status not in {"dispatching", "running", "recovering"} or task.cancel_requested or row["phase"] in {"completed", "verifying"} or row["stop_reason"]:
                return None
            if (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                return None
            if row["dispatch_count"] >= 3:
                await conn.execute("UPDATE task_attempts SET stop_reason='dispatch_limit' WHERE attempt_id=?", (str(attempt_id),))
                await conn.execute("UPDATE tasks SET error_json=? WHERE task_id=?", (create_error(ErrorCode.BUDGET_EXCEEDED, "Command dispatch budget exhausted").model_dump_json(), str(task.task_id)))
                await transition(conn, task, "failed", task.owner_id, now, execution=True)
                return None
            command = COMMAND.validate_json(row["command_json"]).model_copy(update={"session_id": session_id})
            try:
                auth = await self.security.authorize_dispatch(conn, task.model_copy(update={"status": "ready"}), node, scope, now)
                grant = await one(conn, "SELECT * FROM execution_grants WHERE grant_id=?", (str(command.grant_id),))
                if now >= command.expires_at or grant["policy_version"] != auth.policy_version or grant["scope_digest"] != scope_digest(scope):
                    raise AccessDenied("POLICY_DENIED")
            except AccessDenied:
                await conn.execute("UPDATE task_attempts SET stop_reason='authorization' WHERE attempt_id=?", (str(attempt_id),))
                await conn.execute("UPDATE tasks SET error_json=? WHERE task_id=?", (create_error(ErrorCode.POLICY_DENIED, "Original command authorization expired or changed").model_dump_json(), str(task.task_id)))
                await transition(conn, task, "failed", task.owner_id, now, execution=True)
                return None
            await conn.execute("UPDATE task_attempts SET dispatch_count=dispatch_count+1 WHERE attempt_id=?", (str(attempt_id),))
            await audit(conn, str(node.identity_id), str(task.task_id), "attempt.retransmitted", now)
            return command

    async def resume_verification(self) -> None:
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT attempt_id FROM task_attempts WHERE phase='verifying' LIMIT 100")
        for row in rows:
            await self.verify(UUID(row[0]))

    async def detail(self, task_id: UUID, owner_id: UUID) -> dict:
        async with self.database.transaction(write=False) as conn:
            task = await load_task(conn, task_id)
            if task.owner_id != owner_id:
                raise PermissionError("task owner mismatch")
            attempts = await conn.execute_fetchall("SELECT attempt_id, attempt_no, dispatch_count, phase, command_digest, boot_id, created_at, result_json, execution_stopped, stop_reason, probe_count FROM task_attempts WHERE task_id=? ORDER BY attempt_no LIMIT 2", (str(task_id),))
            approvals = await conn.execute_fetchall("SELECT approval_id, expires_at, decision FROM approvals WHERE task_id=? ORDER BY rowid DESC LIMIT 100", (str(task_id),))
            events = await conn.execute_fetchall("SELECT event_id, revision, kind, created_at FROM task_events WHERE task_id=? ORDER BY event_id LIMIT 200", (str(task_id),))
            evidence = []
            for row in attempts:
                value = dict(row)
                raw = value.pop("result_json")
                value["observed_at"] = RESULT.validate_json(raw).observed_at.isoformat() if raw else None
                evidence.append(value)
            return {"task": task, "attempts": evidence, "approvals": [dict(r) for r in approvals], "events": [dict(r) for r in events]}
