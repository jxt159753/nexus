"""Bounded single-step scheduler. Recovery/retry and global controls are S5."""

import asyncio
import logging
from uuid import UUID

from nexus.contracts.errors import ErrorCode, ProtocolError, create_error
from nexus.contracts.messages import ResultAck
from nexus.contracts.task import TaskRequest
from nexus.core.link.nodes import NodeConnection, NodeLink
from nexus.core.security.types import AccessDenied, NodeScope, Principal
from nexus.core.storage.database import utc_now
from nexus.core.storage.execution import ExecutionRepository
from nexus.core.storage.tasks import TaskRepository, load_task, transition

LOGGER = logging.getLogger(__name__)


class Harness:
    def __init__(self, link: NodeLink):
        self.link = link
        self.security = link.guards.service
        self.tasks = TaskRepository(self.security.database)
        self.executions = ExecutionRepository(self.security.database, self.security, link.config.max_result_bytes)
        self.failed = False
        self.wake = asyncio.Event()

    def scope(self, node_id: UUID) -> NodeScope:
        if node_id not in self.link.config.nodes:
            raise AccessDenied("POLICY_DENIED")
        connection = self.link.connections.get(node_id)
        if connection is not None:
            return connection.scope
        policy = self.link.config.nodes[node_id]
        return NodeScope(node_id, policy.tools, policy.roots, policy.tools, policy.roots, policy.resource_version)

    async def submit(self, principal: Principal, node_id: UUID, request: TaskRequest):
        if self.failed:
            raise AccessDenied("BUSY")
        await self.security.validate_principal(principal)
        if principal.role != "owner" or principal.identity_id != self.link.config.owner_id:
            raise AccessDenied()
        scope = self.scope(node_id)
        task = await self.tasks.create(principal.identity_id, request, node_id, rate_limit=True)
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
        async with self.link._changed:
            self.link._current(connection)
            if message.type in {"command.accepted", "command.started"}:
                await self.executions.receipt(connection.principal, connection.hello.boot_id, message.payload)
            elif message.type == "command.result" or message.type == "command.status_result" and message.payload.result is not None:
                result = message.payload if message.type == "command.result" else message.payload.result
                status = await self.executions.store_result(connection.principal, result)
                await self.executions.verify(result.attempt_id)
                ack = ResultAck(session_id=connection.session_id, attempt_id=result.attempt_id, status=status)
            # Status without a result and transport errors never imply an outcome.
        if ack is not None:
            await self.link.send(connection.principal.identity_id, "result.ack", ack)

    async def tick(self) -> None:
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
                            connection.session_id, connection.hello.boot_id, self.link.config.execution_timeout_seconds)
                    if command is not None:
                        await self.link.dispatch(connection, command)
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
