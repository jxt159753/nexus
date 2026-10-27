"""Speech uses final replies and completed milestones, never speculative call prose."""
import asyncio
import copy
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from nexus.core.models.chat_format import ChatText
from nexus.core.cognitive.chat_feedback import fallback
from nexus.core.voice.chat_bridge import ChatVoiceBridge
from nexus.core.voice.service import VoiceSession
from test_voice_chat_bridge import FakeChat


@pytest.mark.parametrize("size", [1, 4, 1000])
@pytest.mark.parametrize("metadata", ["", '{"title":"报告"}\n', 'NEXUS_META {"title":"报告","memories":[]}\n'])
def test_progress_marker_is_stream_safe_and_never_public(size, metadata):
    parts = []
    async def run():
        async def receive(value):
            parts.append(value)
        text = ChatText(bool(metadata), receive, auto_memory=metadata.startswith("NEXUS_META"), allow_progress=True)
        value = metadata + "NEXUS_PROGRESS: 已找到导致失败的配置，正在验证修复。"
        for offset in range(0, len(value), size):
            await text.feed(value[offset:offset + size])
        await text.feed("", final=True)
        assert text.public_progress
        assert "".join(parts) == "已找到导致失败的配置，正在验证修复。"
        assert text.title == ("报告" if metadata else None)
    asyncio.run(run())


def test_ordinary_answer_still_streams_without_a_marker():
    async def run():
        parts = []
        async def receive(value):
            parts.append(value)
        text = ChatText(False, receive, allow_progress=True)
        await text.feed("你好。")
        assert parts == ["你好。"]
        assert not text.public_progress
    asyncio.run(run())


@pytest.mark.parametrize("substantive", [False, True])
def test_tool_commentary_projection_preserves_evidence_and_journal(world, monkeypatch, substantive):
    from test_chat_skills import app_for
    from test_chat_work import setup
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{"intent": "get_node_status", "arguments": {}}])
        provider = chat.provider
        original = provider.invoke
        commentary = "已确认目标设备离线，将核对已有记录。" if substantive else "正在执行第一个工具，请稍等。"
        async def invoke(*args, **kwargs):
            reply = await original(*args, **kwargs)
            if reply.native_tool_call:
                return replace(reply, content=commentary, public_progress=substantive,
                               work_calls=((reply.proposal, reply.native_tool_call),))
            return reply
        monkeypatch.setattr(provider, "invoke", invoke)
        await chat.process_one()
        presentation = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert presentation["process"][0]["substantive"] is substantive
        await chat.process_one()
        messages = provider.calls[-1][0]
        call = next(item for item in messages if item.get("tool_calls"))
        assert call["content"] == (commentary if substantive else None)
        assert next(item for item in messages if item["role"] == "tool")["tool_call_id"] == call["tool_calls"][0]["id"]
        if not substantive:
            old = copy.deepcopy(messages)
            next(item for item in old if item.get("tool_calls"))["content"] = commentary
            assert len(json.dumps(messages, ensure_ascii=False).encode()) < len(json.dumps(old, ensure_ascii=False).encode())
        journal = await chat.journal.detail(world.owner, turn["turn_id"])
        assert json.loads(journal["exchanges"][0]["response_json"])["content"] == commentary
        assert len(provider.calls) == 2
    asyncio.run(run())


def voice_sequence(world, monkeypatch, states, *, tools=True):
    import nexus.core.voice.chat_bridge as module
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    async def directory(*args):
        return {"nodes": [{}], "work_digest": "a" * 64}
    monkeypatch.setattr("nexus.core.cognitive.node_routing.directory", directory)
    class Sequence(FakeChat):
        async def get(self, principal, turn_id):
            at, state = states.pop(0)
            clock[0] = at
            return state
    chat = Sequence(world.db)
    session = VoiceSession(voice_session_id=uuid4(), request_id=uuid4(), owner_id=world.owner_id,
        node_id=world.node_id, trigger="button", utterance_id=uuid4(),
        consent={"allow_tools": tools, "allow_work_results": tools})
    async def run():
        return [part async for part in ChatVoiceBridge(chat, world.owner_id).stream(session, "完成工作")]
    return asyncio.run(run()), chat, session


def state(phase, text="", *, progress=(), steps=(), model_phase="reply", work_round=0):
    return {"status": "generating" if phase == "responding" else phase, "phase": model_phase,
            "work_round": work_round, "response_text": text, "work_steps": list(steps),
            "presentation": {"phase": phase, "live_text": text, "process": list(progress),
                             "final_text": text if phase in {"completed", "failed", "unknown"} else None}}


def test_voice_ignores_tool_preambles_and_counting_even_during_long_work(world, monkeypatch):
    states = [
        (0, state("responding", "正在执行8个工具。")),
        (60, state("working", progress=[{"id":"1", "text":"还有2项工具尚未完成。", "substantive":False}],
                   steps=[{"status":"succeeded"}])),
        (70, state("responding", "我接下来再调用一个工具。", work_round=1)),
        (80, state("completed", "报告已保存。", work_round=1)),
    ]
    parts, chat, _ = voice_sequence(world, monkeypatch, states)
    assert parts == ["报告已保存。"]
    assert len(chat.submitted) == 1


def test_voice_long_progress_requires_completed_evidence_and_is_sparse(world, monkeypatch):
    update = "已找到配置错误，修复方案通过初步检查。"
    def progress(identifier, text=update):
        return [{"id":identifier, "text":text, "substantive":True}]
    states = [
        (1, state("working", progress=progress("early"), steps=[{"status":"succeeded"}])),
        (35, state("working", progress=progress("no-evidence"))),
        (40, state("working", progress=progress("valid"), steps=[{"status":"succeeded"}])),
        (50, state("working", progress=progress("frequent", "新版已构建。"), steps=[{"status":"succeeded"}])),
        (80, state("working", progress=progress("duplicate"), steps=[{"status":"succeeded"}])),
        (90, state("completed", "修复完成。")),
    ]
    parts, _, _ = voice_sequence(world, monkeypatch, states)
    assert parts == [update + "\n", "修复完成。"]


def test_voice_confirmation_reads_the_actual_question_without_call_preamble(world, monkeypatch):
    states = [(1, state("awaiting_input", "3个工具正在等待。",
        steps=[{"status":"permission", "request_text":"将覆盖 report.txt，请确认此更改。"}]))]
    parts, _, _ = voice_sequence(world, monkeypatch, states)
    assert parts == ["将覆盖 report.txt，请确认此更改。\n请在聊天界面核对并确认这一步。"]


def test_voice_important_error_is_kept_without_internal_counts(world, monkeypatch):
    message = fallback({"error_code":"NODE_OFFLINE", "work_steps":[{"status":"succeeded"}, {"status":"failed"}]}, "failed")
    assert "设备" in message and "连接" in message
    assert "项工具" not in message and "其余" not in message
    parts, _, _ = voice_sequence(world, monkeypatch, [(1, state("failed", message))])
    assert parts == [message]


@pytest.mark.parametrize("tools,model_phase", [(False, "reply"), (True, "summary")])
def test_tool_free_answers_and_final_summaries_keep_incremental_speech(world, monkeypatch, tools, model_phase):
    states = [(1, state("responding", "已经找到", model_phase=model_phase)),
              (2, state("completed", "已经找到原因。", model_phase=model_phase))]
    parts, _, _ = voice_sequence(world, monkeypatch, states, tools=tools)
    assert parts == ["已经找到", "原因。"]


@pytest.mark.parametrize("with_call", [False, True])
def test_actual_provider_marks_only_valid_completed_tool_progress(with_call):
    import httpx2
    from pydantic import SecretStr
    from nexus.contracts.basic import FilePreviewArguments
    from nexus.core.models.chat import DeepSeekChatProvider
    from nexus.core.models.config import ModelConfig
    from test_chat_argument_errors import native, response
    calls = []
    async def handle(request):
        calls.append(json.loads(request.content))
        return response([native("write_file", json.dumps({"root_id":"workspace", "path":"report.txt", "content":"ok"}))]
                        if with_call else None, text="NEXUS_PROGRESS: 已找到报告来源。")
    async def run():
        parts = []
        async def receive(value):
            parts.append(value)
        provider = DeepSeekChatProvider(SecretStr("synthetic"), ModelConfig(), transport=httpx2.MockTransport(handle))
        reply = await provider.invoke([{"role":"user", "content":"整理报告"}], receive, allow_tools=True,
            work_catalog=[{"name":"write_file", "description":"Write", "parameters":FilePreviewArguments.model_json_schema()}])
        assert reply.public_progress is with_call
        assert reply.content == "".join(parts) == "已找到报告来源。"
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("during_link", [False, True])
def test_silent_tool_wait_still_cancels_the_original_chat(world, monkeypatch, during_link):
    async def directory(*args):
        return {"nodes":[{}], "work_digest":"a"*64}
    monkeypatch.setattr("nexus.core.cognitive.node_routing.directory", directory)
    class Waiting(FakeChat):
        cancelled = False
        entered = False
        async def get(self, *args):
            self.entered = True
            return state("working", "8个工具执行中。")
        async def cancel(self, *args):
            self.cancelled = True
    chat = Waiting(world.db)
    session = VoiceSession(voice_session_id=uuid4(), request_id=uuid4(), owner_id=world.owner_id,
        node_id=world.node_id, trigger="button", utterance_id=uuid4(),
        consent={"allow_tools":True, "allow_work_results":True})
    async def run():
        task = asyncio.create_task(ChatVoiceBridge(chat, world.owner_id).respond(session, "整理资料"))
        while not session.chat_turn_id or not during_link and not chat.entered:
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert chat.cancelled
    asyncio.run(run())
