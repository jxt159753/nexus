"""Durable single-step dispatch and evidence settlement. No network I/O."""

from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from nexus.contracts.commands import command_digest
from nexus.contracts.errors import ErrorCode, ProtocolError, create_error
from nexus.contracts.messages import CommandExecute, CommandReceipt, CommandResult
from nexus.contracts.tools import validate_result
from nexus.core.security.service import SecurityService, assert_principal, scope_digest
from nexus.core.security.types import NodeScope, Principal
from nexus.core.storage.database import Database, one, timestamp, utc_now
from nexus.core.storage.tasks import TERMINAL, canonical, load_task, transition

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
            rows = await conn.execute_fetchall("SELECT task_id FROM tasks WHERE node_id=? AND status IN ('ready','awaiting_approval') AND expires_at>? ORDER BY rowid LIMIT 100", (str(node_id), timestamp(utc_now())))
            return [UUID(row[0]) for row in rows]

    async def dispatch(self, task_id: UUID, node: Principal, scope: NodeScope, session_id: UUID, boot_id: UUID, timeout: int) -> CommandExecute | None:
        now = utc_now()
        async with self.database.transaction() as conn:
            task = await load_task(conn, task_id)
            authorization = await self.security.authorize_dispatch(conn, task, node, scope, now)
            if task.current_attempt_id is not None:
                return None  # S4 never creates a retry or a second execution.
            if await one(conn, "SELECT 1 FROM task_attempts WHERE node_id=? AND phase!='completed'", (str(node.identity_id),)):
                return None
            grant_id, attempt_id = uuid4(), uuid4()
            expires = min(task.expires_at, authorization.expires_at or task.expires_at)
            value = task.action.model_dump(mode="json")
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
            await conn.execute("INSERT INTO task_attempts(attempt_id, task_id, node_id, grant_id, attempt_no, dispatch_count, phase, command_json, command_digest, boot_id, created_at) VALUES (?, ?, ?, ?, 1, 1, 'dispatching', ?, ?, ?, ?)", (
                str(attempt_id), str(task_id), str(node.identity_id), str(grant_id), command.model_dump_json(),
                command.command_digest, str(boot_id), timestamp(now),
            ))
            await conn.execute("UPDATE tasks SET current_attempt_id=?, execution_outcome='unknown' WHERE task_id=?", (str(attempt_id), str(task_id)))
            await transition(conn, task, "dispatching", task.owner_id, now, execution=True)
            return command  # transaction context must commit before caller sends

    async def _active(self, conn, node: Principal, attempt_id: UUID):
        await assert_principal(conn, node, utc_now())
        row = await one(conn, "SELECT * FROM task_attempts WHERE attempt_id=?", (str(attempt_id),))
        if node.role != "node" or row is None or row["node_id"] != str(node.identity_id):
            raise invalid()
        task = await load_task(conn, UUID(row["task_id"]))
        if task.current_attempt_id != attempt_id:
            raise invalid()
        return task, row

    async def receipt(self, node: Principal, boot_id: UUID, receipt: CommandReceipt) -> None:
        async with self.database.transaction() as conn:
            task, row = await self._active(conn, node, receipt.attempt_id)
            if receipt.task_id != task.task_id or receipt.command_digest != row["command_digest"] or receipt.boot_id != boot_id:
                raise invalid()
            if task.status == "dispatching":
                await conn.execute("UPDATE task_attempts SET phase='running' WHERE attempt_id=?", (str(receipt.attempt_id),))
                await transition(conn, task, "running", node.identity_id, utc_now(), execution=True)

    async def store_result(self, node: Principal, result: CommandResult) -> str:
        result = RESULT.validate_json(result.model_dump_json())
        if result.result is not None and len(result.result.model_dump_json().encode("utf-8")) > self.max_result_bytes:
            raise invalid()
        async with self.database.transaction() as conn:
            task, row = await self._active(conn, node, result.attempt_id)
            command = COMMAND.validate_json(row["command_json"])
            if (result.task_id != task.task_id or result.command_digest != command.command_digest
                    or result.tool != command.tool or result.tool_version != command.tool_version):
                raise invalid()
            if row["result_json"] is not None:
                if evidence_json(RESULT.validate_json(row["result_json"])) != evidence_json(result):
                    raise invalid()
                return "already_settled" if task.status in TERMINAL else "stored"
            if task.status not in {"dispatching", "running"}:
                raise invalid()
            now = utc_now()
            if not (task.created_at - timedelta(seconds=5) <= result.observed_at <= min(now, command.expires_at) + timedelta(seconds=5)):
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
            await conn.execute("UPDATE tasks SET result_json=?, error_json=?, verification=?, execution_outcome='known' WHERE task_id=?", (
                output.model_dump_json() if output else None, error.model_dump_json() if error else None,
                verification, str(task.task_id),
            ))
            await conn.execute("UPDATE task_attempts SET phase='completed' WHERE attempt_id=?", (str(attempt_id),))
            await transition(conn, task, "succeeded" if succeeded else "failed", task.owner_id, utc_now(), execution=True)

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
            attempts = await conn.execute_fetchall("SELECT attempt_id, attempt_no, dispatch_count, phase, command_digest, boot_id, created_at, result_json FROM task_attempts WHERE task_id=? ORDER BY attempt_no LIMIT 2", (str(task_id),))
            approvals = await conn.execute_fetchall("SELECT approval_id, expires_at, decision FROM approvals WHERE task_id=? ORDER BY rowid DESC LIMIT 100", (str(task_id),))
            events = await conn.execute_fetchall("SELECT event_id, revision, kind, created_at FROM task_events WHERE task_id=? ORDER BY event_id LIMIT 200", (str(task_id),))
            evidence = []
            for row in attempts:
                value = dict(row)
                raw = value.pop("result_json")
                value["observed_at"] = RESULT.validate_json(raw).observed_at.isoformat() if raw else None
                evidence.append(value)
            return {"task": task, "attempts": evidence, "approvals": [dict(r) for r in approvals], "events": [dict(r) for r in events]}
