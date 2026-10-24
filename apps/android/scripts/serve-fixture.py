"""Isolated loopback integration Core. No real model or user database is used."""
import asyncio
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import uvicorn
from pydantic import SecretStr

from nexus.core.app import create_app
from nexus.core.config import CoreConfig, TrustedNode, VoiceConfig, VoiceNodePolicy
from nexus.core.models.config import ModelConfig
from nexus.core.models.providers import ModelReply
from nexus.core.security.service import SecurityService
from nexus.core.storage.database import Database


class LocalProvider:
    async def invoke(self, messages, on_delta, **options):
        text = "安卓联调成功：此回复来自本地测试替身，未调用付费模型。"
        await on_delta(text[:6])
        await asyncio.sleep(0.4)
        if messages[-1]["content"] == "等待":
            await asyncio.sleep(20)
        await on_delta(text[6:])
        return ModelReply(text, 100, 20, True)


async def seed(directory):
    directory.mkdir(parents=True, exist_ok=False)
    database = Database(directory / "core.sqlite3")
    await database.initialize()
    service = SecurityService(database)
    owner, node = uuid4(), uuid4()
    tokens = [secrets.token_urlsafe(32) for _ in range(2)]
    for identifier, role, token in zip((owner, node), ("owner", "node"), tokens):
        await service.register_identity(identifier, role, SecretStr(token))
        for tool, root in (("android.device_status", "android:device"), ("android.notification_digest", "android:notifications"), ("filesystem.receive", "workspace")):
            await service.set_grant(identifier, tool, root_id=root)
    config = CoreConfig(data_dir=directory, owner_id=owner, port=18780, local_http=True,
        origins=frozenset({"https://app.nexus.local"}), heartbeat_interval_seconds=1, heartbeat_timeout_seconds=4,
        nodes={node: TrustedNode(tools=frozenset({"android.device_status@1", "android.notification_digest@1", "filesystem.receive@1"}), roots=frozenset({"android:device", "android:notifications", "workspace"}),voice=VoiceNodePolicy(enabled=True,features=frozenset({"capture","playback","streaming_asr","streaming_tts"})))},
        voice=VoiceConfig(enabled=True,provider="fake",heartbeat_interval_seconds=1,heartbeat_timeout_seconds=4,max_session_seconds=60),
        models=ModelConfig(enabled=True, chat_enabled=True, chat_max_output_tokens=512))
    (directory / "fixture.json").write_text(json.dumps(dict(url="http://127.0.0.1:18780", owner_token=tokens[0], node_id=str(node), token=tokens[1], packages=["ai.nexus.android"])), encoding="utf-8")
    return config


if __name__ == "__main__":
    import sys
    destination = Path(sys.argv[1]).resolve()
    if "var" not in destination.parts:
        raise SystemExit("Use a fresh directory inside the repository var directory")
    config = asyncio.run(seed(destination))
    os.environ["DEEPSEEK_API_KEY"] = "local-fixture-no-network"
    app = create_app(config)
    app.state.chat.provider = LocalProvider()
    uvicorn.run(app, host="127.0.0.1", port=config.port, access_log=False, log_level="warning")
