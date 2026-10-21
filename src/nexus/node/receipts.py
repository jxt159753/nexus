"""Node-only durable evidence; never imports Core storage or task state."""

import hashlib
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from nexus.contracts.commands import command_identity
from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.messages import CommandExecute, CommandResult, CommandStatusResult

RESULT = TypeAdapter(CommandResult)


@dataclass(frozen=True)
class Receipt:
    attempt_id: UUID
    task_id: UUID
    command_digest: str
    identity_json: str
    boot_id: UUID
    phase: str
    job_name: str | None
    execution_stopped: bool
    result_json: str | None
    acknowledged: bool

    def status(self, session_id: UUID) -> CommandStatusResult:
        result = None
        if self.result_json is not None:
            result = RESULT.validate_json(self.result_json).model_copy(update={"session_id": session_id})
        return CommandStatusResult(
            session_id=session_id, attempt_id=self.attempt_id, phase=self.phase,
            command_digest=self.command_digest, result=result,
            execution_stopped=self.execution_stopped,
        )


class ReceiptStore:
    def __init__(self, path: Path, node_id: UUID, max_receipts: int = 10000):
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise ValueError("absolute local receipt path required")
        if path.is_symlink() or path.is_junction():
            raise ValueError("receipt database must not be a link")
        self.path, self.node_id, self.max_receipts = path, node_id, max_receipts

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

    def initialize(self) -> None:
        scripts = [p.read_text(encoding="utf-8") for p in sorted(files("nexus.node").joinpath("migrations").iterdir(), key=lambda p: p.name) if p.name.endswith(".sql")]
        checksums = [hashlib.sha256(script.encode()).hexdigest() for script in scripts]
        # Upgrade offline while holding the Node data-directory lock.
        with closing(sqlite3.connect(self.path)) as check:
            tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if "schema_migrations" not in tables:
                    raise RuntimeError("unversioned receipt database")
                applied = list(check.execute("SELECT version,checksum FROM schema_migrations ORDER BY version"))
                if not applied or applied != list(enumerate(checksums[:len(applied)], 1)):
                    raise RuntimeError("unknown receipt schema or checksum")
                if [r[0] for r in check.execute("SELECT node_id FROM node_identity")] != [str(self.node_id)]:
                    raise RuntimeError("receipt database belongs to another node")
                if len(applied) < len(scripts):
                    backup_path = self.path.with_name(f"{self.path.name}.before-v{len(applied)+1}-{uuid4().hex}.sqlite3")
                    with closing(sqlite3.connect(backup_path)) as backup:
                        check.backup(backup)
                        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise RuntimeError("receipt backup integrity failure")
        with self.transaction() as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names:
                applied = [tuple(r) for r in conn.execute("SELECT version,checksum FROM schema_migrations ORDER BY version")]
                expected = {"schema_migrations", "node_identity", "execution_receipts"}
                if len(applied) >= 2:
                    expected.add("cancellation_tombstones")
                if names != expected:
                    raise RuntimeError("unversioned or unknown receipt database")
                if applied != list(enumerate(checksums[:len(applied)], 1)):
                    raise RuntimeError("unknown receipt schema or checksum")
                if [r[0] for r in conn.execute("SELECT node_id FROM node_identity")] != [str(self.node_id)]:
                    raise RuntimeError("receipt database belongs to another node")
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("receipt database integrity failure")
            else:
                applied = []
                conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)")
            for index in range(len(applied), len(scripts)):
                statement = ""
                for line in scripts[index].splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError("incomplete receipt migration")
                conn.execute("INSERT INTO schema_migrations VALUES(?, ?)", (index + 1, checksums[index]))
            if not names:
                conn.execute("INSERT INTO node_identity VALUES(?)", (str(self.node_id),))

    def cancel_pending(self, attempt_id: UUID) -> None:
        """Fence both absent and received commands before acknowledging a stop."""
        with self.transaction() as conn:
            old = conn.execute("SELECT 1 FROM cancellation_tombstones WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            if old is None and conn.execute("SELECT count(*) FROM cancellation_tombstones").fetchone()[0] >= self.max_receipts:
                raise ProtocolError(ErrorCode.BUSY, "Cancellation receipt capacity reached")
            conn.execute("INSERT OR IGNORE INTO cancellation_tombstones VALUES(?)", (str(attempt_id),))
            conn.execute("UPDATE execution_receipts SET phase='cancelled',execution_stopped=1 WHERE attempt_id=? AND phase='received'", (str(attempt_id),))

    @staticmethod
    def _receipt(row: sqlite3.Row | None) -> Receipt | None:
        if row is None:
            return None
        return Receipt(
            UUID(row["attempt_id"]), UUID(row["task_id"]), row["command_digest"],
            row["identity_json"], UUID(row["boot_id"]), row["phase"], row["job_name"],
            bool(row["execution_stopped"]), row["result_json"], bool(row["acknowledged"]),
        )

    def get(self, attempt_id: UUID) -> Receipt | None:
        with self.transaction() as conn:
            return self._receipt(conn.execute("SELECT * FROM execution_receipts WHERE attempt_id=?", (str(attempt_id),)).fetchone())

    def accept(self, command: CommandExecute, boot_id: UUID) -> Receipt:
        identity = command_identity(command, self.node_id)
        with self.transaction() as conn:
            old = self._receipt(conn.execute("SELECT * FROM execution_receipts WHERE attempt_id=?", (str(command.attempt_id),)).fetchone())
            if old is not None:
                if old.identity_json != identity or old.command_digest != command.command_digest:
                    raise ProtocolError(ErrorCode.IDEMPOTENCY_CONFLICT, "Attempt identity differs")
                return old
            if conn.execute("SELECT count(*) FROM execution_receipts").fetchone()[0] >= self.max_receipts:
                raise ProtocolError(ErrorCode.BUSY, "Local receipt capacity reached")
            conn.execute("INSERT INTO execution_receipts(attempt_id, task_id, command_digest, identity_json, boot_id, phase) VALUES(?, ?, ?, ?, ?, 'received')", (
                str(command.attempt_id), str(command.task_id), command.command_digest, identity, str(boot_id),
            ))
            if conn.execute("SELECT 1 FROM cancellation_tombstones WHERE attempt_id=?", (str(command.attempt_id),)).fetchone():
                conn.execute("UPDATE execution_receipts SET phase='cancelled' WHERE attempt_id=?", (str(command.attempt_id),))
            row = conn.execute("SELECT * FROM execution_receipts WHERE attempt_id=?", (str(command.attempt_id),)).fetchone()
            return self._receipt(row)

    def start(self, attempt_id: UUID, job_name: str) -> bool:
        with self.transaction() as conn:
            if conn.execute("SELECT phase FROM execution_receipts WHERE attempt_id=?", (str(attempt_id),)).fetchone()[0] == "cancelled":
                return False
            cursor = conn.execute("UPDATE execution_receipts SET phase='started', job_name=?, execution_stopped=0 WHERE attempt_id=? AND phase='received'", (job_name, str(attempt_id)))
            if cursor.rowcount != 1:
                raise RuntimeError("attempt cannot start")
            return True

    def finish(self, attempt_id: UUID, phase: str, result: CommandResult | None = None, *, stopped: bool = True) -> None:
        if phase not in {"completed", "cancelled", "unknown"} or (phase == "completed") != (result is not None):
            raise ValueError("invalid receipt completion")
        encoded = None if result is None else RESULT.validate_json(result.model_dump_json()).model_dump_json()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM execution_receipts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            if row is None or row["phase"] not in {"received", "started", "unknown"}:
                raise RuntimeError("receipt already settled or absent")
            if result is not None and (str(result.attempt_id) != row["attempt_id"] or str(result.task_id) != row["task_id"] or result.command_digest != row["command_digest"]):
                raise ValueError("result identity mismatch")
            conn.execute("UPDATE execution_receipts SET phase=?, result_json=?, execution_stopped=? WHERE attempt_id=?", (phase, encoded, int(stopped), str(attempt_id)))

    def unresolved(self) -> list[Receipt]:
        with self.transaction() as conn:
            return [self._receipt(r) for r in conn.execute("SELECT * FROM execution_receipts WHERE phase='started' OR execution_stopped=0 LIMIT ?", (self.max_receipts,))]

    def unacknowledged(self, *, after: str = "", limit: int = 100) -> list[Receipt]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("page size must be 1..100")
        with self.transaction() as conn:
            return [self._receipt(r) for r in conn.execute("SELECT * FROM execution_receipts WHERE (phase='completed' OR phase='cancelled' OR (phase='unknown' AND execution_stopped=1)) AND acknowledged=0 AND attempt_id>? ORDER BY attempt_id LIMIT ?", (after, limit))]

    def acknowledge(self, attempt_id: UUID) -> None:
        with self.transaction() as conn:
            if conn.execute("UPDATE execution_receipts SET acknowledged=1 WHERE attempt_id=? AND (phase='completed' OR phase='cancelled' OR (phase='unknown' AND execution_stopped=1))", (str(attempt_id),)).rowcount != 1:
                raise ValueError("no settled receipt to acknowledge")
