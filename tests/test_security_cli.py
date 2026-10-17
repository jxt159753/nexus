import asyncio
import os
import secrets
import subprocess
import sys
from uuid import uuid4

from pydantic import SecretStr

from nexus.core.security.service import SecurityService
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import Database


def test_offline_management_in_separate_processes(tmp_path):
    data_dir = tmp_path / "data"
    environment = {**os.environ, "NEXUS_DATA_DIR": str(data_dir), "NEXUS_HOST": "127.0.0.1"}
    owner_id, node_id = uuid4(), uuid4()
    owner_token, node_token, new_token = (SecretStr(secrets.token_urlsafe(32)) for _ in range(3))

    def command(*args, token=None, success=True):
        env = {**environment, "NEXUS_ENROLL_TOKEN": token.get_secret_value() if token else ""}
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "nexus.core.security", *map(str, args)],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
        )
        assert (result.returncode == 0) is success
        assert all(secret.get_secret_value() not in result.stdout + result.stderr for secret in (owner_token, node_token, new_token))
        return result

    command("register-owner", owner_id, token=owner_token)
    command("register-node", node_id, token=node_token)
    command("grant", owner_id, "--tool", "system.info")
    command("grant", node_id, "--tool", "system.info")
    command("rotate", node_id, token=new_token)

    async def check():
        security = SecurityService(Database(data_dir / "core.sqlite3"))
        assert (await security.authenticate_token(new_token, "node")).identity_id == node_id
        try:
            await security.authenticate_token(node_token, "node")
        except AccessDenied:
            pass
        else:
            raise AssertionError("old credential still accepted")
    asyncio.run(check())
    command("revoke", node_id)
    command("register-node", uuid4(), success=False)


def test_task_history_survives_fresh_process(world, tmp_path):
    from nexus.contracts.task import SystemInfoArguments, SystemInfoRequest

    task = asyncio.run(world.tasks.create(world.owner_id, SystemInfoRequest(
        request_id=uuid4(), intent="get_system_info", arguments=SystemInfoArguments(),
    ), world.node_id))
    code = """
import asyncio, os
from pathlib import Path
from uuid import UUID
from nexus.core.storage.database import Database
from nexus.core.storage.tasks import TaskRepository
async def main():
    db = Database(Path(os.environ['S2_DB']))
    await db.initialize()
    repo = TaskRepository(db)
    task = await repo.get(UUID(os.environ['S2_TASK']), UUID(os.environ['S2_OWNER']))
    assert task.status == 'queued' and task.revision == 0
    assert len(await repo.history(task.owner_id)) == 1
    print('fresh process history: passed')
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], cwd=tmp_path,
        env={**os.environ, "S2_DB": str(world.db.path), "S2_TASK": str(task.task_id), "S2_OWNER": str(world.owner_id)},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "fresh process history: passed" in result.stdout
