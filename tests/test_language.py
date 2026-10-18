"""Cost, privacy, authority and recovery checks for M2 interpretation."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from nexus.contracts.language import LanguageSubmission
from nexus.core.app import create_app
from nexus.core.config import TrustedNode
from nexus.core.models.config import ModelConfig
from nexus.core.models.deepseek import DeepSeekProvider, ModelReply, ProviderFailure
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_harness_http import login
from test_node_link import ORIGIN, core_config, grant


class FakeProvider:
    def __init__(self, content=None, *, failure=False, block=False):
        self.calls = 0
        self.content = content or '{"intent":"get_system_info","arguments":{}}'
        self.failure, self.block = failure, block
        self.stopped = False

    async def invoke(self, text):
        self.calls += 1
        if self.failure:
            raise ProviderFailure("network unknown")
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped = True
        return ModelReply(self.content, 100, 20, True)


def service(world, monkeypatch, provider=None, **limits):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-never-network")
    policy = TrustedNode(tools=frozenset({"system.info@1","filesystem.list@1"}), roots=frozenset({"workspace"}))
    app = create_app(core_config(world, models=ModelConfig(enabled=True, **limits), nodes={world.node_id:policy}))
    language = app.state.language
    language.provider = provider or FakeProvider()
    return language


def submitted(world, **changes):
    values = dict(request_id=uuid4(), node_id=world.node_id, text="请帮我看看这台电脑使用的操作系统", allow_model=True)
    values.update(changes)
    return LanguageSubmission(**values)


async def rows(world, table="model_calls"):
    async with world.db.connection() as conn:
        return [dict(row) for row in await conn.execute_fetchall(f"SELECT * FROM {table}")]


def test_model_proposal_needs_confirmation_and_existing_authorization(world, monkeypatch):
    grant(world)
    language = service(world, monkeypatch)
    async def scenario():
        value = submitted(world)
        item = await language.submit(world.owner, value)
        assert item["status"] == "queued" and await rows(world, "tasks") == []
        assert await language.submit(world.owner, value) == item
        await language.process_one()
        proposal = await language.get(world.owner, UUID(item["language_id"]))
        assert proposal["status"] == "proposed" and proposal["mode"] == "llm"
        assert await rows(world, "tasks") == []
        call = (await rows(world))[0]
        assert call["cost_status"] == "calculated" and call["cost_microunits"] == 54
        first = await language.confirm(world.owner, UUID(item["language_id"]))
        second = await language.confirm(world.owner, UUID(item["language_id"]))
        assert first == second and first["task"].status == "ready"
        assert (await rows(world))[0]["task_id"] == str(first["task"].task_id)
        assert language.provider.calls == 1
        assert await rows(world, "memory_entries") == []
    asyncio.run(scenario())


@pytest.mark.parametrize("content", [
    '{"intent":"shell","arguments":{"command":"whoami"}}',
    '{"intent":"list_directory","arguments":{"root_id":"other","limit":1}}',
    '{"intent":"list_directory","arguments":{"root_id":"workspace","limit":true}}',
    '{"intent":"get_system_info","arguments":{},"approved":true}',
    '{"intent":"get_system_info","arguments":{"path":"E:/"}}',
    '[]', '{', '{"intent":"unsupported","arguments":{}}',
])
def test_untrusted_model_output_never_creates_execution(world, monkeypatch, content):
    language = service(world, monkeypatch, FakeProvider(content))
    async def scenario():
        item = await language.submit(world.owner, submitted(world))
        await language.process_one()
        assert (await language.get(world.owner, UUID(item["language_id"])))["status"] in {"failed", "rejected"}
        with pytest.raises(AccessDenied):
            await language.confirm(world.owner, UUID(item["language_id"]))
        assert await rows(world, "tasks") == []
        assert (await rows(world))[0]["cost_microunits"] == 54
    asyncio.run(scenario())


def test_rule_and_no_consent_disable_network(world, monkeypatch):
    language = service(world, monkeypatch)
    async def scenario():
        rule = await language.submit(world.owner, submitted(world, text="读取系统信息", allow_model=False))
        await language.process_one()
        assert (await language.get(world.owner, UUID(rule["language_id"])))["mode"] == "rule"
        denied = await language.submit(world.owner, submitted(world, allow_model=False))
        await language.process_one()
        assert (await language.get(world.owner, UUID(denied["language_id"])))["error_code"] == "MODEL_CONSENT_REQUIRED"
        assert await rows(world) == [] and language.provider.calls == 0
    asyncio.run(scenario())


def test_paused_no_calls_unknown_reserves_daily_budget_and_never_retries(world, monkeypatch):
    language = service(world, monkeypatch, FakeProvider(failure=True), daily_cost_microunits=50000)
    async def scenario():
        await language.harness.control(world.owner, "pause", uuid4())
        first = await language.submit(world.owner, submitted(world))
        await language.process_one()
        assert language.provider.calls == 0
        await language.harness.control(world.owner, "resume", uuid4())
        await language.process_one()
        assert (await rows(world))[0]["cost_status"] == "unknown"
        assert (await rows(world))[0]["reserved_microunits"] == 50000
        second = await language.submit(world.owner, submitted(world))
        await language.process_one()
        assert (await language.get(world.owner, UUID(second["language_id"])))["error_code"] == "BUDGET_EXCEEDED"
        assert language.provider.calls == 1
        for _ in range(2):
            await language.process_one()
        assert language.provider.calls == 1
        assert (await language.get(world.owner, UUID(first["language_id"])))["status"] == "failed"
    asyncio.run(scenario())


def test_unknown_prior_day_cost_survives_restart_and_day_rollover(world, monkeypatch):
    language = service(world, monkeypatch, FakeProvider(block=True), daily_cost_microunits=50000)
    async def scenario():
        item = await language.submit(world.owner, submitted(world))
        claimed = await language.claim()
        _, _, task = claimed
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE model_calls SET created_at=?", (timestamp(utc_now()-timedelta(days=2)),))
        language.active = None  # Discard the old process's coroutine ownership.
        await language.recover()
        assert (await language.get(world.owner, UUID(item["language_id"])))["error_code"] == "MODEL_OUTCOME_UNKNOWN"
        other = await language.submit(world.owner, submitted(world))
        await language.process_one()
        assert (await language.get(world.owner, UUID(other["language_id"])))["error_code"] == "BUDGET_EXCEEDED"
        assert len(await rows(world)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("global_cancel", [True, False])
def test_cancellation_closes_actual_provider_and_blocks_confirmation(world, monkeypatch, global_cancel):
    provider = FakeProvider(block=True)
    language = service(world, monkeypatch, provider)
    async def scenario():
        item = await language.submit(world.owner, submitted(world))
        running = asyncio.create_task(language.process_one())
        for _ in range(200):
            if provider.calls:
                break
            await asyncio.sleep(.01)
        assert provider.calls == 1
        if global_cancel:
            await language.harness.control(world.owner, "cancel_all", uuid4())
        else:
            await language.cancel(world.owner, UUID(item["language_id"]))
        await asyncio.wait_for(running, 3)
        assert provider.stopped
        assert (await language.get(world.owner, UUID(item["language_id"])))["status"] == "cancelled"
        assert (await rows(world))[0]["cost_status"] == "unknown"
    asyncio.run(scenario())


def test_worker_shutdown_closes_owned_provider_and_preserves_unknown_cost(world, monkeypatch):
    provider = FakeProvider(block=True)
    language = service(world, monkeypatch, provider)
    async def scenario():
        await language.submit(world.owner, submitted(world))
        worker = asyncio.create_task(language.process_one())
        for _ in range(200):
            if provider.calls:
                break
            await asyncio.sleep(.01)
        assert provider.calls == 1
        worker.cancel()
        await asyncio.gather(worker,return_exceptions=True)
        assert provider.stopped and language.active is None
        call = (await rows(world))[0]
        assert call["cost_status"] == "unknown" and call["reserved_microunits"] == 50000
    asyncio.run(scenario())


def test_bound_directory_still_requires_harness_approval(world, monkeypatch):
    grant(world, "filesystem.list")
    provider = FakeProvider('{"intent":"list_directory","arguments":{"root_id":"workspace","limit":2}}')
    language = service(world, monkeypatch, provider)
    async def scenario():
        item = await language.submit(world.owner, submitted(world))
        await language.process_one()
        task = (await language.confirm(world.owner, UUID(item["language_id"])))["task"]
        assert task.status == "awaiting_approval"
        assert await rows(world, "task_attempts") == []
        detail = await language.harness.executions.detail(task.task_id, world.owner_id)
        assert len(detail["approvals"]) == 1
    asyncio.run(scenario())


def test_language_identity_idempotency_expiry_and_budget_count(world, monkeypatch):
    language = service(world, monkeypatch, daily_calls=1)
    async def scenario():
        value = submitted(world)
        item = await language.submit(world.owner, value)
        with pytest.raises(AccessDenied):
            await language.get(world.node, UUID(item["language_id"]))
        with pytest.raises(Conflict):
            await language.submit(world.owner, value.model_copy(update={"text":"changed"}))
        await language.process_one()
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE language_requests SET expires_at=? WHERE language_id=?", (timestamp(utc_now()-timedelta(seconds=1)), item["language_id"]))
        with pytest.raises(AccessDenied):
            await language.confirm(world.owner, UUID(item["language_id"]))
        next_item = await language.submit(world.owner, submitted(world))
        await language.process_one()
        assert (await language.get(world.owner, UUID(next_item["language_id"])))["error_code"] == "BUDGET_EXCEEDED"
    asyncio.run(scenario())


def test_http_auth_bounds_worker_and_confirm(world, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-never-network")
    grant(world)
    app = create_app(core_config(world, models=ModelConfig(enabled=True)))
    app.state.language.provider = FakeProvider()
    with TestClient(app, base_url=ORIGIN) as client:
        value = submitted(world).model_dump(mode="json")
        assert client.get("/api/v1/language").status_code == 401
        auth = login(client, world)
        assert client.post("/api/v1/language", json=value).status_code == 403
        assert client.post("/api/v1/language", json={**value,"text":" "}, headers=auth).status_code == 400
        assert client.post("/api/v1/language", json={**value,"allow_model":"yes"}, headers=auth).status_code == 400
        item = client.post("/api/v1/language", json=value, headers=auth).json()["request"]
        import time
        for _ in range(100):
            item = client.get("/api/v1/language/"+item["language_id"]).json()["request"]
            if item["status"] == "proposed":
                break
            time.sleep(.02)
        assert item["status"] == "proposed"
        path = "/api/v1/language/"+item["language_id"]+"/confirm"
        assert client.post(path, json={}).status_code == 403
        result = client.post(path, json={}, headers=auth)
        assert result.status_code == 200 and result.json()["task"]["status"] == "ready"
        assert client.get("/api/v1/models/usage").json()["enabled"] is True


def test_deepseek_https_request_minimizes_context_and_validates_usage():
    captured = []
    async def handle(request):
        captured.append(request)
        value = json.loads(request.content)
        assert value["thinking"] == {"type":"disabled"}
        assert value["max_tokens"] == 512 and not value["stream"]
        assert set(value) == {"model","messages","thinking","response_format","max_tokens","stream"}
        return httpx2.Response(200, json={"model":"deepseek-flash","choices":[{"message":{"content":'{"intent":"unsupported","arguments":{}}'},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":20}})
    provider = DeepSeekProvider(SecretStr("test-key"), ModelConfig(), transport=httpx2.MockTransport(handle))
    reply = asyncio.run(provider.invoke("test input"))
    assert reply.input_tokens == 100
    assert str(captured[0].url) == "https://api.deepseek.com/chat/completions"


@pytest.mark.parametrize("response", [
    httpx2.Response(302, headers={"location":"https://other.test"}),
    httpx2.Response(200, content=b"x"*32769),
    httpx2.Response(200, json={"error":"remote-secret"}),
    httpx2.Response(401, json={"message":"remote-secret"}),
])
def test_provider_fails_closed_without_remote_error_or_redirect(response):
    provider = DeepSeekProvider(SecretStr("test-key"), ModelConfig(), transport=httpx2.MockTransport(lambda request: response))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider.invoke("test input"))
    assert "remote-secret" not in str(caught.value) and "test-key" not in str(caught.value)


def test_model_config_and_missing_secret_fail_closed(world, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="configured securely"):
        create_app(core_config(world, models=ModelConfig(enabled=True)))
    for changes in ({"daily_cost_microunits":1000001},{"max_output_tokens":513},{"daily_calls":True},{"timeout_seconds":31},{"api_key":"secret"}):
        with pytest.raises(ValidationError):
            ModelConfig(**changes)


def test_budget_claims_are_serial_and_expired_price_makes_no_call(world, monkeypatch):
    language = service(world, monkeypatch)
    async def scenario():
        for _ in range(2):
            await language.submit(world.owner, submitted(world))
        first, second = await asyncio.gather(language.claim(), language.claim())
        assert (first is None) != (second is None)
        item = first or second
        row, call_id, task = item
        await language.finish(row, call_id, await task)
        language.active = None
        language.config = language.config.model_copy(update={"price_valid_until":utc_now().date()-timedelta(days=1)})
        await language.process_one()
        assert len(await rows(world)) == 1
        assert any(r["error_code"] == "MODEL_PRICING_EXPIRED" for r in await rows(world,"language_requests"))
    asyncio.run(scenario())


def test_audit_write_failure_rolls_back_reservation_before_any_network(world, monkeypatch):
    language = service(world, monkeypatch)
    async def scenario():
        item = await language.submit(world.owner, submitted(world))
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER fail_model_audit BEFORE INSERT ON audit_events WHEN NEW.kind='model.reserved' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            await language.process_one()
        assert language.provider.calls == 0 and await rows(world) == []
        assert (await language.get(world.owner, UUID(item["language_id"])))["status"] == "queued"
    asyncio.run(scenario())


def test_commit_failure_cannot_start_provider(world, monkeypatch):
    language = service(world, monkeypatch)
    async def scenario():
        import aiosqlite, sqlite3
        await language.submit(world.owner, submitted(world))
        original = aiosqlite.Connection.commit
        async def failed_commit(conn):
            if (await one(conn,"SELECT count(*) FROM model_calls WHERE outcome='started'"))[0]:
                raise sqlite3.OperationalError("injected commit failure")
            await original(conn)
        monkeypatch.setattr(aiosqlite.Connection,"commit",failed_commit)
        with pytest.raises(sqlite3.OperationalError):
            await language.process_one()
        await asyncio.sleep(.02)
        assert language.provider.calls == 0
        assert await rows(world) == []
    asyncio.run(scenario())


def test_confirmation_and_language_link_rollback_together(world, monkeypatch):
    language = service(world, monkeypatch)
    async def scenario():
        item = await language.submit(world.owner, submitted(world, text="读取系统信息"))
        await language.process_one()
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER fail_link BEFORE UPDATE OF task_id ON language_requests BEGIN SELECT RAISE(ABORT,'test failure'); END")
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            await language.confirm(world.owner, UUID(item["language_id"]))
        assert await rows(world,"tasks") == []
        assert (await language.get(world.owner,UUID(item["language_id"])))["status"] == "proposed"
    asyncio.run(scenario())


def test_v5_upgrade_preserves_nonempty_call_ledger_and_verified_backup(world, tmp_path):
    import hashlib
    import sqlite3
    from contextlib import closing
    from importlib.resources import files
    from nexus.core.storage.database import Database
    from test_storage import request
    task = asyncio.run(world.tasks.create(world.owner_id, request(), world.node_id))
    path = tmp_path / "v5.sqlite3"
    with closing(sqlite3.connect(path)) as target, closing(sqlite3.connect(world.db.path)) as source, target:
        target.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(),key=lambda p:p.name):
            if not resource.name.endswith(".sql") or int(resource.name[:3]) > 5:
                continue
            script = resource.read_text(encoding="utf-8")
            target.executescript(script)
            target.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(resource.name[:3]),hashlib.sha256(script.encode()).hexdigest(),timestamp(utc_now())))
        tables = [r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in {"schema_migrations","sqlite_sequence"}]
        for table in tables:
            columns = ','.join(r[1] for r in target.execute(f'PRAGMA table_info({table})'))
            records = source.execute(f'SELECT {columns} FROM {table}').fetchall()
            target.execute(f'DELETE FROM {table}')
            target.executemany(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in columns.split(","))})',records)
        legacy = (str(uuid4()),str(task.task_id),"legacy","legacy-model",123,"reported",timestamp(utc_now()))
        target.execute("INSERT INTO model_calls VALUES(?,?,?,?,?,?,?)",legacy)
    asyncio.run(Database(path).initialize())
    backup = list(tmp_path.glob('v5.sqlite3.before-v6-*.sqlite3'))
    assert len(backup) == 1
    with closing(sqlite3.connect(path)) as target, closing(sqlite3.connect(backup[0])) as previous:
        assert target.execute("SELECT call_id,task_id,provider,model,cost_microunits,cost_status,created_at FROM model_calls").fetchone() == legacy
        assert previous.execute("SELECT * FROM model_calls").fetchone() == legacy
        assert previous.execute("PRAGMA quick_check").fetchone()[0] == 'ok'
        assert target.execute("PRAGMA foreign_key_check").fetchall() == []
