"""Bounded single-step scheduler, recovery and durable user controls."""

import asyncio
import logging
import time
from uuid import UUID

from nexus.contracts.errors import ErrorCode, ProtocolError, create_error
from nexus.contracts.messages import CommandCancel, CommandExecute, CommandStatus, ResultAck
from nexus.contracts.task import TaskRequest
from nexus.core.link.nodes import NodeConnection, NodeLink
from nexus.core.security.types import AccessDenied, NodeScope, Principal
from nexus.core.storage.database import one, utc_now
from nexus.core.storage.execution import ExecutionRepository
from nexus.core.storage.control import ControlRepository
from nexus.core.storage.tasks import TaskRepository, load_task, transition

LOGGER = logging.getLogger(__name__)


class Harness:
    def __init__(self, link: NodeLink):
        self.link = link
        self.security = link.guards.service
        self.tasks = TaskRepository(self.security.database)
        self.executions = ExecutionRepository(self.security.database, self.security, link.config.max_result_bytes)
        self.controls = ControlRepository(self.security.database)
        self.operation = link.execution_lock
        self.probes: dict[UUID, tuple[UUID, UUID, bool, float]] = {}
        link.execution_permitted = self.permitted
        self.failed = False
        self.wake = asyncio.Event()

    async def permitted(self, connection: NodeConnection, command: CommandExecute) -> bool:
        return not self.failed and await self.executions.may_send(connection.principal, connection.scope, command)

    async def cancel(self, principal: Principal, task_id: UUID) -> dict:
        if self.failed:
            raise AccessDenied("BUSY")
        async with self.operation:
            result = await self.controls.cancel(principal, task_id)
        self.wake.set()
        return result

    async def control(self, principal: Principal, kind: str, request_id: UUID) -> dict:
        if self.failed:
            raise AccessDenied("BUSY")
        async with self.operation:
            result = await self.controls.apply(principal, kind, request_id)
        self.wake.set()
        return result

    async def decide(self, principal: Principal, approval_id: UUID, decision: str):
        if self.failed:
            raise AccessDenied("BUSY")
        async with self.security.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT node_id FROM approvals WHERE approval_id=? AND owner_id=?", (str(approval_id), str(principal.identity_id)))
            if row is None:
                raise LookupError("approval not found")
        task = await self.security.decide(approval_id, principal, decision, self.scope(UUID(row[0])))
        self.wake.set()
        return task

    def scope(self, node_id: UUID) -> NodeScope:
        if node_id not in self.link.config.nodes:
            raise AccessDenied("POLICY_DENIED")
        connection = self.link.connections.get(node_id)
        if connection is not None:
            return connection.scope
        policy = self.link.config.nodes[node_id]
        return NodeScope(node_id, policy.tools, policy.roots, policy.tools, policy.roots, policy.resource_version, policy.command_digests, policy.interaction_digests,policy.plugin_digest)

    async def submit(self, principal: Principal, node_id: UUID, request: TaskRequest, *, language_id: UUID | None = None, chat_turn_id: UUID | None = None, rate_limit: bool = True, skill_run_id: UUID | None = None, skill_step_index: int | None = None, work_step_id: UUID | None = None, work_role: str = "execute"):
        if self.failed:
            raise AccessDenied("BUSY")
        await self.security.validate_principal(principal)
        if principal.role != "owner" or principal.identity_id != self.link.config.owner_id:
            raise AccessDenied()
        scope = self.scope(node_id)
        task = await self.tasks.create(principal.identity_id, request, node_id, rate_limit=rate_limit, language_id=language_id,chat_turn_id=chat_turn_id, skill_run_id=skill_run_id, skill_step_index=skill_step_index, work_step_id=work_step_id, work_role=work_role)
        if task.status in {"queued", "awaiting_approval", "ready"} and task.expires_at > utc_now():
            try:
                await self.security.prepare(task.task_id, principal, scope)
            except AccessDenied as exc:
                if exc.code == "AUTH_REQUIRED":
                    raise
                # A denied submission has durable history but no executable attempt.
                async with self.security.database.transaction() as conn:
                    current = await load_task(conn, task.task_id)
                    if current.status in {"queued", "awaiting_approval", "ready"}:
                        await conn.execute("UPDATE tasks SET error_json=? WHERE task_id=?", (create_error(ErrorCode.POLICY_DENIED, "Action is not authorized").model_dump_json(), str(task.task_id)))
                        await transition(conn, current, "failed", principal.identity_id, utc_now())
        self.wake.set()
        return await self.tasks.get(task.task_id, principal.identity_id)

    async def consume(self, connection: NodeConnection, message) -> None:
        ack = None
        followup = "none"
        async with self.link._changed:
            self.link._current(connection)
            if message.type in {"command.accepted", "command.started"}:
                await self.executions.receipt(connection.principal, connection.hello.boot_id, message.payload)
            elif message.type == "command.result" or message.type == "command.status_result" and message.payload.result is not None:
                result = message.payload if message.type == "command.result" else message.payload.result
                status = await self.executions.store_result(connection.principal, result)
                await self.executions.verify(result.attempt_id)
                ack = ResultAck(session_id=connection.session_id, attempt_id=result.attempt_id, status=status)
            elif message.type == "command.status_result":
                followup = await self.executions.status(connection.principal, message.payload)
                if followup == "already_settled":
                    ack = ResultAck(session_id=connection.session_id, attempt_id=message.payload.attempt_id, status="already_settled")
            elif message.type == "command.cancelled":
                await self.executions.cancelled(connection.principal, message.payload.attempt_id)
            # Transport errors alone never prove an execution stopped.
        if ack is not None:
            await self.link.send(connection.principal.identity_id, "result.ack", ack)
        if followup == "resend":
            command = await self.executions.resend(connection.principal, connection.scope, message.payload.attempt_id, connection.session_id)
            if command is not None:
                await self.link.dispatch(connection, command)

    async def stop_attempt(self, connection: NodeConnection, attempt_id: UUID) -> None:
        await self.link.send(connection.principal.identity_id, "command.cancel", CommandCancel(
            session_id=connection.session_id, attempt_id=attempt_id, reason="Core requested execution stop"))

    async def tick(self) -> None:
        async with self.operation:
            await self._tick()

    async def _tick(self) -> None:
        await self.controls.expire()
        self.probes = {node: probe for node, probe in self.probes.items() if node in self.link.connections}
        for connection in list(self.link.connections.values()):
            if not connection.ready:
                continue
            try:
                for _ in range(32):
                    try:
                        incoming = connection.incoming.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.consume(connection, incoming)
                active = await self.executions.active(connection.principal.identity_id)
                if active:
                    row = active[0]
                    attempt_id = UUID(row["attempt_id"])
                    prior = self.probes.get(connection.principal.identity_id)
                    now = time.monotonic()
                    stopping = bool(row["stop_reason"] or row["cancel_requested"] or row["task_status"] in {"failed", "timed_out", "cancelled"})
                    # Cancellation is immediate; repeated stop/status traffic is bounded.
                    signature = (connection.session_id, attempt_id, stopping)
                    if prior is None or prior[:3] != signature or now - prior[3] >= 5:
                        if await self.executions.reserve_probe(attempt_id):
                            if stopping:
                                await self.stop_attempt(connection, attempt_id)
                            else:
                                await self.executions.recovering(attempt_id)
                                await self.link.send(connection.principal.identity_id, "command.status", CommandStatus(session_id=connection.session_id, attempt_id=attempt_id))
                        self.probes[connection.principal.identity_id] = (*signature, now)
                    continue
                self.probes.pop(connection.principal.identity_id, None)
                if (await self.controls.state())["paused"]:
                    continue
                for task_id in await self.executions.pending(connection.principal.identity_id):
                    async with self.link._changed:
                        self.link._current(connection)
                        try:
                            authorization = await self.security.prepare_pending(task_id, connection.principal, connection.scope)
                        except AccessDenied:
                            continue  # A denied action must not starve other permitted tools.
                        if authorization.mode != "allow":
                            continue
                        command = await self.executions.dispatch(task_id, connection.principal, connection.scope,
                            connection.session_id, connection.hello.boot_id, self.link.config.execution_timeout_seconds,
                            mcp_timeout=min(self.link.config.mcp_execution_timeout_seconds, connection.hello.mcp_execution_timeout_seconds))
                    if command is not None:
                        await self.link.dispatch(connection, command)
                        self.probes[connection.principal.identity_id] = (connection.session_id, command.attempt_id, False, time.monotonic())
                    break  # one active execution per node
            except AccessDenied:
                continue  # Revoked permission never drives a new execution.
            except ProtocolError:
                await self.link._close(connection.websocket, 1008, "execution evidence or session rejected")

    async def run(self) -> None:
        try:
            while not self.failed:
                self.wake.clear()
                await self.tick()
                try:
                    async with asyncio.timeout(0.2):
                        await self.wake.wait()
                except TimeoutError:
                    pass
        except Exception as exc:
            self.failed = True  # No more submissions/dispatch after storage failure.
            LOGGER.error("Harness stopped (%s)", type(exc).__name__)
            await self.link.close()
