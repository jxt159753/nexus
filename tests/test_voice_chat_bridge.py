import asyncio
from uuid import uuid4

import pytest

from nexus.core.voice.chat_bridge import ChatVoiceBridge
from nexus.core.voice.service import VoiceSession


class FakeChat:
    def __init__(self, database):
        self.database = database
        self.submitted = []

    async def create(self, principal, value):
        return {"conversation_id": str(uuid4()), "revision": 1}

    async def history(self, principal, conversation_id):
        return {"conversation": {"revision": 7}}

    async def submit(self, principal, conversation_id, request):
        self.submitted.append((conversation_id, request))
        return {"turn_id": str(uuid4())}

    async def get(self, principal, turn_id):
        return {"status": "completed", "response_text": "这是真实轮次的回答。", "error_code": None}


def test_chat_voice_bridge_uses_existing_chat(world):
    chat = FakeChat(world.db)
    bridge = ChatVoiceBridge(chat, world.owner_id)
    session = VoiceSession(
        voice_session_id=uuid4(), request_id=uuid4(), owner_id=world.owner_id,
        node_id=world.node_id, trigger="button", state="listening",
        utterance_id=uuid4(),
    )

    async def run():
        conversation = await bridge.create_conversation(session)
        session.conversation_id = conversation
        return await bridge.respond(session, "你好")

    answer = asyncio.run(run())
    assert answer == "这是真实轮次的回答。"
    assert len(chat.submitted) == 1
    conversation_id, request = chat.submitted[0]
    assert conversation_id == session.conversation_id
    assert request.allow_model is True
    assert request.allow_tools is False
    assert request.expected_revision == 7


@pytest.mark.parametrize("phase", ["completed", "failed", "unknown", "recovering"])
def test_voice_reads_existing_natural_failure_explanation(world, phase):
    class FailedChat(FakeChat):
        async def get(self, principal, turn_id):
            return {"status": phase, "error_code": "TOOL_FAILED", "presentation": {
                "phase": phase, "final_text": "文件尚未写入，设备断线后我已经停止执行。"}}

    bridge = ChatVoiceBridge(FailedChat(world.db), world.owner_id)
    session = VoiceSession(voice_session_id=uuid4(), request_id=uuid4(), owner_id=world.owner_id,
                           node_id=world.node_id, trigger="button", utterance_id=uuid4())
    assert asyncio.run(bridge.respond(session, "保存文件")) == "文件尚未写入，设备断线后我已经停止执行。"
    assert session.end_after_reply == (phase in {"unknown", "recovering"})


@pytest.mark.parametrize("scope", ["workspace", "explicit"])
def test_voice_tools_use_real_chat_work_and_verified_result(world, monkeypatch, scope):
    from test_chat import app_for
    from test_chat_work import WorkProvider, basic
    from nexus.contracts.work import WorkGrantCreate
    app = app_for(world, monkeypatch)
    chat = app.state.chat
    chat.provider = WorkProvider([{"intent": "get_node_status", "arguments": {}}])
    bridge = ChatVoiceBridge(chat, world.owner_id)
    session = VoiceSession(voice_session_id=uuid4(), request_id=uuid4(), owner_id=world.owner_id,
        node_id=world.node_id, trigger="button", utterance_id=uuid4(),
        consent={"allow_tools": True, "allow_work_results": True, "include_context": True})

    async def scenario():
        session.conversation_id = await bridge.create_conversation(session)
        if scope == "workspace":
            await basic(app, world)
        else:
            await chat.work.grant(world.owner, session.conversation_id,
                                 WorkGrantCreate(request_id=uuid4(), node_id=world.node_id, consent=True))
        task = asyncio.create_task(bridge.respond(session, "查看当前设备连接状态"))
        try:
            async with asyncio.timeout(5):
                while not task.done():
                    await asyncio.sleep(0.02)
                    await chat.process_one()
                assert await task
            turn = await chat.get(world.owner, session.chat_turn_id)
            assert turn["status"] == "completed", turn.get("error_code")
            assert turn["work_steps"][0]["result"]["verification"] == "core_connection_observation"
            assert turn["work_steps"][0]["result"]["data"]["online"] is False
            assert turn["model_call_count"] == 2
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
