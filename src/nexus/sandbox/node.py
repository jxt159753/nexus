"""Sandbox Node process: authenticated v2 link, durable receipts, runsc only."""

import asyncio
import json
import logging
import os
import random
import sqlite3
import ssl
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from nexus.contracts.extensions import canonical, digest_value, parse_strict_json
from nexus.contracts.sandbox import (
    SandboxCancelled,
    SandboxExecute,
    SandboxHeartbeat,
    SandboxHello,
    SandboxResult,
    SandboxStatusResult,
    sandbox_command_digest,
    encode_sandbox_message,
    parse_sandbox_message,
)
from nexus.sandbox.config import SandboxConfig
from nexus.sandbox.runtime import RunscRuntime, SandboxUnavailable
from nexus.locking import DataDirectoryLock

LOGGER = logging.getLogger("nexus.sandbox")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class Receipts:
    def __init__(self, path: Path, limit: int = 10000):
        self.path, self.limit = path, limit
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS sandbox_receipts(
            execution_id TEXT PRIMARY KEY, artifact_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
            status TEXT NOT NULL, output_json TEXT, error_code TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        self.connection.commit()
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(sandbox_receipts)")}
        if "command_digest" not in columns:
            backup_path = path.with_name(path.name + ".before-v2-" + uuid4().hex)
            with closing(sqlite3.connect(backup_path)) as backup:
                self.connection.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.Error("receipt backup failed")
            with self.connection:
                self.connection.execute("ALTER TABLE sandbox_receipts ADD COLUMN command_digest TEXT")
                self.connection.execute("ALTER TABLE sandbox_receipts ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1))")
        self.connection.execute("UPDATE sandbox_receipts SET status='unknown',error_code='NODE_RESTARTED',updated_at=? WHERE status IN ('received','running')", (datetime.now(UTC).isoformat(),))
        self.connection.commit()

    def accept(self, execution_id: str, artifact_digest: str, input_digest: str, command_digest: str | None = None) -> sqlite3.Row | None:
        row = self.connection.execute("SELECT * FROM sandbox_receipts WHERE execution_id=?", (execution_id,)).fetchone()
        if row is not None:
            if row[1] != artifact_digest or row[2] != input_digest or row[8] != command_digest:
                raise ValueError("execution identity conflict")
            return row
        count = self.connection.execute("SELECT count(*) FROM sandbox_receipts").fetchone()[0]
        if count >= self.limit:
            raise sqlite3.Error("sandbox receipt store is full")
        now = datetime.now(UTC).isoformat()
        self.connection.execute("INSERT INTO sandbox_receipts(execution_id,artifact_digest,input_digest,status,created_at,updated_at,command_digest) VALUES(?,?,?,'received',?,?,?)",
                                (execution_id, artifact_digest, input_digest, now, now, command_digest))
        self.connection.commit()
        return None

    def finish(self, execution_id: str, status: str, output_json: str | None, error_code: str | None) -> None:
        self.connection.execute("UPDATE sandbox_receipts SET status=?,output_json=?,error_code=?,updated_at=? WHERE execution_id=?",
                                (status, output_json, error_code, datetime.now(UTC).isoformat(), execution_id))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def unsettled(self) -> bool:
        return self.connection.execute("SELECT 1 FROM sandbox_receipts WHERE status='unknown' LIMIT 1").fetchone() is not None

    def stopped(self, execution_id: UUID) -> bool:
        row = self.connection.execute("SELECT status FROM sandbox_receipts WHERE execution_id=?", (str(execution_id),)).fetchone()
        return row is not None and row[0] in {"succeeded", "failed"}

    def get(self, execution_id):
        return self.connection.execute("SELECT * FROM sandbox_receipts WHERE execution_id=?", (str(execution_id),)).fetchone()

    def acknowledge(self, execution_id, artifact_digest):
        row = self.get(execution_id)
        if row is None or row[1] != artifact_digest:
            raise ValueError("result acknowledgement mismatch")
        if row[3] in {"succeeded", "failed"}:
            with self.connection:
                self.connection.execute("UPDATE sandbox_receipts SET acknowledged=1 WHERE execution_id=?", (str(execution_id),))

    def unacknowledged(self):
        return self.connection.execute("SELECT * FROM sandbox_receipts WHERE acknowledged=0 AND status IN ('succeeded','failed') ORDER BY created_at LIMIT 4").fetchall()


class SandboxNode:
    def __init__(self, config: SandboxConfig, token: SecretStr):
        self.config = config
        self.runtime = RunscRuntime(config)
        self._token = token
        self.boot_id = uuid4()
        self._available = False
        self._reason: str | None = "preflight not run"
        self._version = "unavailable"
        self._active: UUID | None = None
        self._result_limit = config.max_result_bytes
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = DataDirectoryLock(config.data_dir / "sandbox.lock")
        try:
            self.receipts = Receipts(config.data_dir / "receipts.sqlite3")
        except BaseException:
            self.lock.close()
            raise
        try:
            self.runtime.preflight()
            self._available = True
            self._reason = None
            self._version = self.runtime.version
            for row in self.receipts.connection.execute("SELECT execution_id FROM sandbox_receipts WHERE status='unknown'").fetchall():
                if self.runtime.reconcile(UUID(row[0]).hex):
                    self.receipts.finish(row[0], "failed", None, "EXECUTION_RESULT_UNKNOWN")
            if self.receipts.unsettled():
                self._available = False
                self._reason = "Unreconciled execution; confirm the old sandbox stopped before accepting new work"
        except SandboxUnavailable as exc:
            self._reason = str(exc)[:300]
        except Exception:
            self._reason = "sandbox preflight failed"

    def hello(self) -> SandboxHello:
        return SandboxHello(
            node_id=self.config.node_id, node_version="0.1.0", boot_id=self.boot_id,
            runtime="runsc", runtime_version=self._version, image_digest=self.config.image_digest,
            platform=self.config.platform, runsc_digest=self.config.runsc_digest,
            available=self._available, reason=self._reason,
            verification_profiles=["directory_entry_stats_v1"],
        )

    async def _send(self, ws, kind: str, payload, correlation=None) -> None:
        text = encode_sandbox_message(kind, payload, correlation_id=correlation)
        async with asyncio.timeout(2):
            await ws.send(text)

    async def _execute(self, ws, session_id: UUID, payload: SandboxExecute, correlation,
                       cancel_event: threading.Event) -> None:
        if not self._available:
            await self._send(ws, "sandbox.result", SandboxResult(
                session_id=session_id, execution_id=payload.execution_id,
                artifact_digest=payload.artifact_digest, outcome="failed",
                error_code="SANDBOX_UNAVAILABLE", stopped=True), correlation)
            return
        try:
            release = parse_strict_json(payload.release_json)
            if canonical(release) != payload.release_json or digest_value(release) != payload.artifact_digest:
                raise ValueError("release digest mismatch")
            if digest_value(release["environment"]) != payload.environment_digest:
                raise ValueError("environment digest mismatch")
            environment = release["environment"]
            if (environment.get("image_digest") != self.config.image_digest
                    or environment.get("platform") != self.config.platform
                    or environment.get("runsc_digest") != self.config.runsc_digest
                    or environment.get("policy") != "runsc-host-limits-v2"
                    or environment.get("node_id") != str(self.config.node_id)):
                raise ValueError("sandbox local environment binding changed")
            if release.get("kind") != "tool":
                raise ValueError("not a Tool release")
            input_value = json.loads(payload.input_json)
            input_digest = digest_value(input_value)
            command_digest = sandbox_command_digest(payload.version_id, payload.artifact_digest,
                payload.environment_digest, input_value, payload.timeout_seconds)
            old = self.receipts.accept(str(payload.execution_id), payload.artifact_digest, input_digest, command_digest)
            if old is not None:
                if old[3] == "succeeded":
                    reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                          artifact_digest=payload.artifact_digest, outcome="succeeded",
                                          output_json=old[4], stopped=True)
                elif old[3] == "failed":
                    reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                          artifact_digest=payload.artifact_digest, outcome="failed",
                                          error_code=old[5] or "SANDBOX_FAILED", stopped=True)
                else:
                    reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                          artifact_digest=payload.artifact_digest, outcome="unknown",
                                          error_code=old[5] or "SANDBOX_EVIDENCE_UNKNOWN", stopped=False)
                await self._send(ws, "sandbox.result", reply, correlation)
                return
        except sqlite3.Error:
            self._available = False
            self._reason = "Receipt storage failed; execution disabled"
            raise
        except (ValueError, KeyError, TypeError):
            await self._send(ws, "sandbox.result", SandboxResult(
                session_id=session_id, execution_id=payload.execution_id,
                artifact_digest=payload.artifact_digest, outcome="failed",
                error_code="SANDBOX_INPUT_INVALID", stopped=True), correlation)
            return
        self._active = payload.execution_id
        with self.receipts.connection:
            self.receipts.connection.execute("UPDATE sandbox_receipts SET status='running' WHERE execution_id=?", (str(payload.execution_id),))
        try:
            result = await asyncio.to_thread(self.runtime.execute, payload.release_json, input_value,
                                             min(payload.timeout_seconds, self.config.execution_timeout_seconds),
                                             execution_id=payload.execution_id.hex, cancel_event=cancel_event)
            if result.get("outcome") == "succeeded":
                output_json = canonical(result["output"])
                if len(output_json.encode("utf-8")) > self._result_limit:
                    raise ValueError("sandbox output exceeds local result budget")
                self.receipts.finish(str(payload.execution_id), "succeeded", output_json, None)
                reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                      artifact_digest=payload.artifact_digest, outcome="succeeded",
                                      output_json=output_json, stopped=True)
            else:
                status = "unknown" if result.get("outcome") == "unknown" else "failed"
                code = result.get("error_code", "SANDBOX_FAILED")
                self.receipts.finish(str(payload.execution_id), status, None, code)
                if status == "unknown":
                    self._available = False
                    self._reason = "Previous execution has no confirmed stop; reconciliation required"
                reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                      artifact_digest=payload.artifact_digest, outcome=status,
                                      error_code=code, stopped=bool(result.get("stopped")))
        except ValueError:
            self.receipts.finish(str(payload.execution_id), "failed", None, "SANDBOX_OUTPUT_INVALID")
            reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                  artifact_digest=payload.artifact_digest, outcome="failed",
                                  error_code="SANDBOX_OUTPUT_INVALID", stopped=True)
        except Exception:
            self._available = False
            self._reason = "Execution evidence unknown; reconciliation required"
            self.receipts.finish(str(payload.execution_id), "unknown", None, "SANDBOX_EVIDENCE_UNKNOWN")
            reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                  artifact_digest=payload.artifact_digest, outcome="unknown",
                                  error_code="SANDBOX_EVIDENCE_UNKNOWN", stopped=False)
        finally:
            self._active = None
        await self._send(ws, "sandbox.result", reply, correlation)

    async def _run_execution(self, ws, session, payload, correlation, cancellation):
        try:
            await self._execute(ws, session, payload, correlation, cancellation)
        finally:
            self._active = None

    async def _connected(self, ws) -> None:
        await self._send(ws, "sandbox.hello", self.hello())
        async with asyncio.timeout(5):
            message = parse_sandbox_message(await ws.recv())
        if message.type != "sandbox.welcome":
            raise ValueError("sandbox welcome required")
        welcome = message.payload
        self._result_limit = min(self.config.max_result_bytes, welcome.max_result_bytes)
        session = welcome.session_id
        execution = None
        cancellation = None
        resends = {}

        def result_from(row):
            return SandboxResult(session_id=session, execution_id=UUID(row[0]), artifact_digest=row[1],
                outcome=row[3], output_json=row[4], error_code=row[5], stopped=row[3] != "unknown")

        async def heartbeat():
            while True:
                await self._send(ws, "sandbox.heartbeat", SandboxHeartbeat(session_id=session, active_execution_id=self._active))
                for row in self.receipts.unacknowledged():
                    if resends.get(row[0], 0) < 3:
                        resends[row[0]] = resends.get(row[0], 0) + 1
                        await self._send(ws, "sandbox.result", result_from(row))
                await asyncio.sleep(welcome.heartbeat_interval_seconds)

        async def receive():
            nonlocal execution, cancellation
            while True:
                raw = await ws.recv()
                if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > welcome.max_message_bytes:
                    raise ValueError("v2 frame too large")
                incoming = parse_sandbox_message(raw)
                if getattr(incoming.payload, "session_id", None) != session:
                    raise ValueError("v2 session mismatch")
                if incoming.type == "sandbox.execute":
                    if execution is not None and not execution.done():
                        await self._send(ws, "sandbox.result", SandboxResult(session_id=session,
                            execution_id=incoming.payload.execution_id, artifact_digest=incoming.payload.artifact_digest,
                            outcome="failed", error_code="SANDBOX_BUSY", stopped=True), incoming.message_id)
                        continue
                    if execution is not None:
                        execution.result()
                    self._active = incoming.payload.execution_id
                    cancellation = threading.Event()
                    execution = asyncio.create_task(self._run_execution(ws, session, incoming.payload, incoming.message_id, cancellation))
                elif incoming.type == "sandbox.cancel":
                    if self._active == incoming.payload.execution_id and cancellation is not None:
                        cancellation.set()
                    await self._send(ws, "sandbox.cancelled", SandboxCancelled(
                        session_id=session, execution_id=incoming.payload.execution_id,
                        stopped=self._active != incoming.payload.execution_id and self.receipts.stopped(incoming.payload.execution_id)), incoming.message_id)
                elif incoming.type == "sandbox.result_ack":
                    self.receipts.acknowledge(incoming.payload.execution_id, incoming.payload.artifact_digest)
                elif incoming.type == "sandbox.status":
                    row = self.receipts.get(incoming.payload.execution_id)
                    recovered = False
                    if row is not None and row[3] == "unknown" and self._active is None:
                        if await asyncio.to_thread(self.runtime.reconcile, incoming.payload.execution_id.hex):
                            self.receipts.finish(row[0], "failed", None, "EXECUTION_RESULT_UNKNOWN")
                            row = self.receipts.get(incoming.payload.execution_id)
                            if not self.receipts.unsettled():
                                recovered = not self._available
                                self._available, self._reason = True, None
                                self.runtime._blocked = False
                    status = "not_found" if row is None else "settled" if row[3] in {"succeeded", "failed"} else row[3]
                    await self._send(ws, "sandbox.status_result", SandboxStatusResult(
                        session_id=session, execution_id=incoming.payload.execution_id, status=status,
                        command_digest=row[8] if row is not None else None,
                        result=result_from(row) if status == "settled" else None), incoming.message_id)
                    if recovered:
                        return  # Reconnect with the newly confirmed availability.
                else:
                    raise ValueError("v2 message is not valid in this direction")

        tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for item in done:
                item.result()
        finally:
            for item in tasks:
                item.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if execution is not None:
                if cancellation is not None:
                    cancellation.set()
                # Keep the execution slot until the owned runtime has really returned.
                await asyncio.gather(execution, return_exceptions=True)
                self._active = None

    async def run(self, stop: asyncio.Event) -> None:
        context = ssl.create_default_context() if self.config.core_url.startswith("wss:") else None
        delay = 1.0
        try:
            while not stop.is_set():
                try:
                    connector = connect(
                        self.config.core_url,
                        additional_headers={"Authorization": "Bearer " + self._token.get_secret_value()},
                        ssl=context, proxy=None, compression=None, ping_interval=None,
                        open_timeout=5, close_timeout=2, max_size=192 * 1024, max_queue=8,
                        write_limit=65536, logger=LOGGER,
                    )
                    connector.process_redirect = lambda exc: exc
                    async with connector as ws:
                        connected = asyncio.create_task(self._connected(ws))
                        stopping = asyncio.create_task(stop.wait())
                        try:
                            done, _ = await asyncio.wait([connected, stopping], return_when=asyncio.FIRST_COMPLETED)
                            if connected in done:
                                connected.result()
                        finally:
                            connected.cancel(); stopping.cancel()
                            await asyncio.gather(connected, stopping, return_exceptions=True)
                        delay = 1.0
                except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake, ValueError):
                    pass
                if not stop.is_set():
                    try:
                        async with asyncio.timeout(random.uniform(delay, min(30.0, delay * 1.25))):
                            await stop.wait()
                    except TimeoutError:
                        pass
                    delay = min(30.0, delay * 2)
        finally:
            self.receipts.close()
            self.lock.close()


def token_from_environment() -> SecretStr:
    token = os.environ.get("NEXUS_NODE_TOKEN", "")
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("valid Sandbox Node credential required")
    return SecretStr(token)


def main() -> None:
    from argparse import ArgumentParser
    from nexus.service_config import load_config

    parser = ArgumentParser(description="NEXUS Sandbox Node")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        config = load_config(Path(args.config), SandboxConfig, {
            "NEXUS_NODE_DATA_DIR": "data_dir", "NEXUS_CORE_URL": "core_url",
        })
        node = SandboxNode(config, token_from_environment())
        stop = asyncio.Event()
        try:
            asyncio.run(node.run(stop))
        except KeyboardInterrupt:
            stop.set()
    except (OSError, ValueError):
        raise SystemExit("Sandbox configuration or startup failed") from None


if __name__ == "__main__":
    main()
