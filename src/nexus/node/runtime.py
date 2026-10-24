"""Single local execution slot. Caller must authenticate Core before entry."""

import json
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from nexus.contracts.commands import command_digest, command_identity
from nexus.contracts.errors import ErrorCode, ProtocolError, create_error
from nexus.contracts.messages import CommandExecute, CommandResult, CommandStatusResult
from nexus.node.config import NodeConfig, local_directory
from nexus.node.file_transfer import DownloadFailed
from nexus.node.process import ExecutionStopped, ManagedWorker
from nexus.node.receipts import RESULT, ReceiptStore
from nexus.node.tools.registry import REGISTRY, validate_result
from nexus.node.windows import job_stopped
from nexus.locking import DataDirectoryLock
from nexus.node.worker import WorkerRequest

COMMAND = TypeAdapter(CommandExecute)


class NodeRuntime:
    def __init__(self, config: NodeConfig):
        self.config = NodeConfig.model_validate_json(config.model_dump_json())
        self.boot_id = uuid4()
        self._slot = threading.Lock()
        self._state = threading.Lock()
        self._core_clock: tuple[datetime, float] | None = None
        self._active: tuple[UUID, threading.Event] | None = None
        self._closed = False
        self._unhealthy = False
        self._lock = DataDirectoryLock(self.config.data_dir / "node.lock")
        try:
            self.receipts = ReceiptStore(self.config.data_dir / "receipts.sqlite3", config.node_id, config.max_receipts)
            self.receipts.initialize()
            self._recover()
        except BaseException:
            self._lock.close()
            raise
        self._worker = ManagedWorker()
        self.file_downloader = None
        self.plugins = None
        if self.config.plugins.enabled:
            from nexus.plugins.node_runtime import PluginRuntime
            try:
                self.plugins = PluginRuntime(self.config)
            except BaseException:
                self._lock.close()
                raise

    def _recover(self) -> None:
        for receipt in self.receipts.unresolved():
            try:
                stopped = receipt.job_name is not None and job_stopped(receipt.job_name)
            except OSError:
                stopped = False
            self.receipts.finish(receipt.attempt_id, "unknown", stopped=stopped)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        with self._state:
            self._closed = True
            if self._active is not None:
                self._active[1].set()
        if not self._slot.acquire(timeout=15):
            raise RuntimeError("worker stop not confirmed; Node lock retained")
        try:
            if self.plugins is not None:
                self.plugins.close()
            self._lock.close()
        finally:
            self._slot.release()

    def status(self, attempt_id: UUID, session_id: UUID) -> CommandStatusResult:
        receipt = self.receipts.get(attempt_id)
        if receipt is None:
            return CommandStatusResult(session_id=session_id, attempt_id=attempt_id, phase="not_found")
        return receipt.status(session_id)

    def use_core_clock(self, sent_at: datetime) -> None:
        """Called only after the configured Core's authenticated welcome."""
        if sent_at.tzinfo is None or sent_at.utcoffset() != timedelta(0):
            raise ValueError("UTC Core time required")
        with self._state:
            self._core_clock = sent_at, time.monotonic()

    def _now(self) -> datetime:
        with self._state:
            anchor = self._core_clock
        return datetime.now(UTC) if anchor is None else anchor[0] + timedelta(seconds=time.monotonic()-anchor[1])

    def cancel(self, attempt_id: UUID) -> bool:
        self.receipts.cancel_pending(attempt_id)
        with self._state:
            if self._active is not None and self._active[0] == attempt_id:
                self._active[1].set()
                return True  # Request only; status confirms actual stop.
        return False

    def execute(self, command: CommandExecute, on_phase: Callable[[str], None] | None = None) -> CommandStatusResult:
        command = COMMAND.validate_json(command.model_dump_json(warnings="error"))
        old = self.receipts.get(command.attempt_id)
        if old is not None and (old.identity_json != command_identity(command, self.config.node_id)
                                or old.command_digest != command.command_digest):
            raise ProtocolError(ErrorCode.IDEMPOTENCY_CONFLICT, "Attempt identity differs")
        if not secrets.compare_digest(command_digest(command, self.config.node_id), command.command_digest):
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Command digest mismatch")
        if old is not None and old.phase != "received":
            return old.status(command.session_id)
        if not self._slot.acquire(blocking=False):
            if old is not None:
                return self.status(command.attempt_id, command.session_id)
            raise ProtocolError(ErrorCode.BUSY, "Node execution slot occupied")
        try:
            return self._execute_locked(command, on_phase)
        except BaseException as exc:
            # Storage failure is never converted into an in-memory accepted result.
            if not isinstance(exc, ProtocolError):
                self._unhealthy = True
            raise
        finally:
            with self._state:
                self._active = None
            self._slot.release()

    def _execute_locked(self, command: CommandExecute, on_phase: Callable[[str], None] | None = None) -> CommandStatusResult:
        with self._state:
            if self._closed or self._unhealthy:
                raise ProtocolError(ErrorCode.BUSY, "Node unavailable")
            cancel = threading.Event()
            self._active = command.attempt_id, cancel
        self._recover()
        if self.receipts.unresolved():
            raise ProtocolError(ErrorCode.EXECUTION_UNKNOWN, "Previous execution stop is unconfirmed")
        local_directory(self.config.data_dir)
        key = f"{command.tool}@{command.tool_version}"
        if key not in REGISTRY or key not in self.config.allowed_tools:
            raise ProtocolError(ErrorCode.POLICY_DENIED, "Tool is not locally allowed")
        if command.tool != "system.info":
            root = self.config.roots.get(command.arguments.root_id)
            if root is None:
                raise ProtocolError(ErrorCode.POLICY_DENIED, "Resource is not configured")
            try:
                local_directory(root)
            except (OSError, ValueError):
                raise ProtocolError(ErrorCode.POLICY_DENIED, "Resource is not a safe local directory") from None
        remaining = (command.expires_at - self._now()).total_seconds()
        if remaining <= 0:
            raise ProtocolError(ErrorCode.TOOL_TIMEOUT, "Command has expired")
        deadline = time.monotonic() + min(remaining, command.execution_timeout_seconds, self.config.execution_timeout_seconds)
        receipt = self.receipts.accept(command, self.boot_id)
        if on_phase is not None:
            on_phase("command.accepted")
        if receipt.phase != "received":
            return receipt.status(command.session_id)
        job_name = f"Local\\NEXUS-{uuid4().hex}"
        if not self.receipts.start(command.attempt_id, job_name):
            return self.status(command.attempt_id, command.session_id)
        if on_phase is not None:
            on_phase("command.started")
        try:
            budget = min(deadline - time.monotonic(), (command.expires_at - self._now()).total_seconds())
            if budget <= 0:
                raise ExecutionStopped("timeout")
            if command.tool == 'plugin.call':
                if self.plugins is None:
                    raise ValueError('plugin runtime is disabled')
                output = self.plugins.execute(command.arguments, deadline, cancel, job_name)
                value = command.model_dump(mode='json',include={'session_id','task_id','attempt_id','tool','tool_version','command_digest'})
                value.update(observed_at=self._now().isoformat(),outcome='succeeded',result=output.model_dump(mode='json'))
                result = RESULT.validate_json(json.dumps(value))
            elif command.tool == 'filesystem.receive':
                if self.file_downloader is None:
                    raise ValueError('File transfer connection unavailable')
                try:
                    with self.file_downloader(command, deadline, cancel):
                        budget = deadline - time.monotonic()
                        if budget <= 0:
                            raise ExecutionStopped('timeout')
                        raw = self._worker.run(WorkerRequest(command=command, config=self.config, job_name=job_name).model_dump_json().encode('utf-8'), job_name, budget, cancel)
                        result = RESULT.validate_json(raw)
                except DownloadFailed:
                    result = self._failure(command, ErrorCode.RESULT_INVALID, '文件传输或保存校验未完成；请查看执行记录后重试。')
            else:
                raw = self._worker.run(WorkerRequest(command=command, config=self.config, job_name=job_name).model_dump_json().encode("utf-8"), job_name, budget, cancel)
                result = RESULT.validate_json(raw)
            if (result.task_id != command.task_id or result.attempt_id != command.attempt_id
                    or result.command_digest != command.command_digest or result.tool != command.tool
                    or result.tool_version != command.tool_version or result.session_id != command.session_id):
                raise ValueError("worker evidence identity mismatch")
            if result.result is not None:
                validate_result(command, result.result, self.config.max_result_bytes)
        except ExecutionStopped as exc:
            if exc.reason == "cancelled":
                self.receipts.finish(command.attempt_id, "cancelled")
                return self.status(command.attempt_id, command.session_id)
            result = self._failure(command, ErrorCode.TOOL_TIMEOUT, "Tool execution deadline exceeded")
        except (OSError, ValueError):
            # Failed gating, crash and invalid evidence never imply not_started.
            stopped = job_stopped(job_name)
            self.receipts.finish(command.attempt_id, "unknown", stopped=stopped)
            return self.status(command.attempt_id, command.session_id)
        # Persist once in the Core time domain. Cached replay must retain this
        # timestamp across reconnects, rather than applying a new clock offset.
        result = result.model_copy(update={"observed_at": self._now()})
        self.receipts.finish(command.attempt_id, "completed", result)
        return self.status(command.attempt_id, command.session_id)

    @staticmethod
    def _failure(command: CommandExecute, code: ErrorCode, message: str) -> CommandResult:
        value = command.model_dump(mode="json", include={
            "session_id", "task_id", "attempt_id", "command_digest", "tool", "tool_version",
        })
        value.update(observed_at=datetime.now(UTC).isoformat(), outcome="failed", error=create_error(code, message).model_dump(mode="json"))
        return RESULT.validate_json(json.dumps(value))
