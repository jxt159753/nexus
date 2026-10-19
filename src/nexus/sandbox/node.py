"""Sandbox Node process: authenticated v2 link, durable receipts, runsc only."""

import asyncio
import json
import logging
import os
import random
import sqlite3
import ssl
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
    encode_sandbox_message,
    parse_sandbox_message,
)
from nexus.sandbox.config import SandboxConfig
from nexus.sandbox.runtime import RunscRuntime, SandboxUnavailable

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
        self.connection.execute("UPDATE sandbox_receipts SET status='unknown',error_code='NODE_RESTARTED',updated_at=? WHERE status IN ('received','running')", (datetime.now(UTC).isoformat(),))
        self.connection.commit()

    def accept(self, execution_id: str, artifact_digest: str, input_digest: str) -> sqlite3.Row | None:
        row = self.connection.execute("SELECT * FROM sandbox_receipts WHERE execution_id=?", (execution_id,)).fetchone()
        if row is not None:
            if row[1] != artifact_digest or row[2] != input_digest:
                raise ValueError("execution identity conflict")
            return row
        count = self.connection.execute("SELECT count(*) FROM sandbox_receipts").fetchone()[0]
        if count >= self.limit:
            raise sqlite3.Error("sandbox receipt store is full")
        now = datetime.now(UTC).isoformat()
        self.connection.execute("INSERT INTO sandbox_receipts VALUES(?,?,?,'received',NULL,NULL,?,?)",
                                (execution_id, artifact_digest, input_digest, now, now))
        self.connection.commit()
        return None

    def finish(self, execution_id: str, status: str, output_json: str | None, error_code: str | None) -> None:
        self.connection.execute("UPDATE sandbox_receipts SET status=?,output_json=?,error_code=?,updated_at=? WHERE execution_id=?",
                                (status, output_json, error_code, datetime.now(UTC).isoformat(), execution_id))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


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
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.receipts = Receipts(config.data_dir / "receipts.sqlite3")
        try:
            self.runtime.preflight()
            self._available = True
            self._reason = None
            self._version = self.runtime.version
        except SandboxUnavailable as exc:
            self._reason = str(exc)[:300]
        except Exception:
            self._reason = "sandbox preflight failed"

    def hello(self) -> SandboxHello:
        return SandboxHello(
            node_id=self.config.node_id, node_version="0.1.0", boot_id=self.boot_id,
            runtime="runsc", runtime_version=self._version, image_digest=self.config.image_digest,
            available=self._available, reason=self._reason,
            verification_profiles=["directory_entry_stats_v1"],
        )

    async def _send(self, ws, kind: str, payload, correlation=None) -> None:
        text = encode_sandbox_message(kind, payload, correlation_id=correlation)
        async with asyncio.timeout(2):
            await ws.send(text)

    async def _execute(self, ws, session_id: UUID, payload: SandboxExecute, correlation) -> None:
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
            if release.get("kind") != "tool":
                raise ValueError("not a Tool release")
            input_value = json.loads(payload.input_json)
            input_digest = digest_value(input_value)
            old = self.receipts.accept(str(payload.execution_id), payload.artifact_digest, input_digest)
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
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            await self._send(ws, "sandbox.result", SandboxResult(
                session_id=session_id, execution_id=payload.execution_id,
                artifact_digest=payload.artifact_digest, outcome="failed",
                error_code="SANDBOX_INPUT_INVALID", stopped=True), correlation)
            return
        self._active = payload.execution_id
        try:
            result = await asyncio.to_thread(self.runtime.execute, payload.release_json, input_value, payload.timeout_seconds)
            if result.get("outcome") == "succeeded":
                output_json = canonical(result["output"])
                self.receipts.finish(str(payload.execution_id), "succeeded", output_json, None)
                reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                      artifact_digest=payload.artifact_digest, outcome="succeeded",
                                      output_json=output_json, stopped=True)
            else:
                status = "unknown" if result.get("outcome") == "unknown" else "failed"
                code = result.get("error_code", "SANDBOX_FAILED")
                self.receipts.finish(str(payload.execution_id), status, None, code)
                reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                      artifact_digest=payload.artifact_digest, outcome=status,
                                      error_code=code, stopped=bool(result.get("stopped")))
        except BaseException:
            self.receipts.finish(str(payload.execution_id), "unknown", None, "SANDBOX_EVIDENCE_UNKNOWN")
            reply = SandboxResult(session_id=session_id, execution_id=payload.execution_id,
                                  artifact_digest=payload.artifact_digest, outcome="unknown",
                                  error_code="SANDBOX_EVIDENCE_UNKNOWN", stopped=False)
        finally:
            self._active = None
        await self._send(ws, "sandbox.result", reply, correlation)

    async def _connected(self, ws) -> None:
        await self._send(ws, "sandbox.hello", self.hello())
        async with asyncio.timeout(5):
            message = parse_sandbox_message(await ws.recv())
        if message.type != "sandbox.welcome":
            raise ValueError("sandbox welcome required")
        welcome = message.payload
        session = welcome.session_id

        async def heartbeat():
            while True:
                await self._send(ws, "sandbox.heartbeat", SandboxHeartbeat(session_id=session, active_execution_id=self._active))
                await asyncio.sleep(welcome.heartbeat_interval_seconds)

        async def receive():
            while True:
                raw = await ws.recv()
                if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > welcome.max_message_bytes:
                    raise ValueError("v2 frame too large")
                incoming = parse_sandbox_message(raw)
                if getattr(incoming.payload, "session_id", None) != session:
                    raise ValueError("v2 session mismatch")
                if incoming.type == "sandbox.execute":
                    await self._execute(ws, session, incoming.payload, incoming.message_id)
                elif incoming.type == "sandbox.cancel":
                    await self._send(ws, "sandbox.cancelled", SandboxCancelled(
                        session_id=session, execution_id=incoming.payload.execution_id,
                        stopped=self._active != incoming.payload.execution_id), incoming.message_id)
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
                        done, _ = await asyncio.wait([connected, stopping], return_when=asyncio.FIRST_COMPLETED)
                        if connected in done:
                            connected.result()
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
