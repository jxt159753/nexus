"""Tool availability must not erase ordinary chat or generated artifacts."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest

from nexus.contracts.work import WorkGrantCreate
from nexus.core.cognitive import work_catalog
from test_chat import FakeChat, app_for, conversation, rows, send
from test_chat_compaction import CompactProvider
from test_chat_work import WorkProvider, basic, file_task


PROGRAM = 'import random\n\nprint(random.choice(["石头", "剪刀", "布"]))\n'
ANSWER = "```python\n" + PROGRAM + "```"


async def work_input(chat, world, conversation_id, text, *, compact=False, grant_id=None, **changes):
    async with world.db.transaction(write=False) as conn:
        directory = await work_catalog.catalog(chat, conn, world.owner, world.node_id)
    scope = {"work_grant_id": UUID(grant_id)} if grant_id else {"workspace_work": True}
    return await send(chat, world, conversation_id, text=text, allow_tools=True,
                      node_id=world.node_id, work_digest=directory["work_digest"],
                      allow_work_results=True, allow_compaction=compact, **scope, **changes)


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("explicit_grant", [False, True])
def test_plain_work_reply_is_sent_verbatim_on_followup(world, monkeypatch, compact, explicit_grant):
    provider = FakeChat(text=ANSWER)
    app = app_for(world, monkeypatch, provider)
    chat = app.state.chat

    async def run():
        await basic(app, world)
        conv = await conversation(chat, world)
        grant_id = None
        if explicit_grant:
            grant = await chat.work.grant(world.owner, conv["conversation_id"], WorkGrantCreate(
                request_id=uuid4(), node_id=world.node_id, consent=True))
            grant_id = grant["grant_id"]
        first, _ = await work_input(chat, world, conv["conversation_id"], "写个猜拳python程序",
                                    compact=compact, grant_id=grant_id)
        await chat.process_one()
        assert (await chat.get(world.owner, first["turn_id"]))["status"] == "completed"
        second, _ = await work_input(chat, world, conv["conversation_id"], "保存到本地",
                                     compact=compact, grant_id=grant_id)
        assert second["context_count"] == 1
        await chat.process_one()
        assert provider.requests[1] == [
            {"role": "user", "content": "写个猜拳python程序"},
            {"role": "assistant", "content": ANSWER},
            {"role": "user", "content": "保存到本地"},
        ]
        journal = await chat.journal.detail(world.owner, second["turn_id"])
        request = json.loads(journal["exchanges"][0]["request_body"])
        assert {"role": "assistant", "content": ANSWER} in request["messages"]
        assert not await rows(world, "chat_work_steps")
        assert not await rows(world, "tasks")
        assert not await rows(world, "memory_entries")
    asyncio.run(run())


@pytest.mark.parametrize("compact", [False, True])
def test_save_followup_writes_previous_code_through_preview_and_readback(world, monkeypatch, tmp_path, compact):
    app = app_for(world, monkeypatch, FakeChat(text=ANSWER))
    chat = app.state.chat
    folder = tmp_path / "workspace"
    folder.mkdir()

    class SaveProgram(WorkProvider):
        async def invoke(self, messages, receive, **options):
            assert any(message.get("role") == "assistant" and message.get("content") == ANSWER
                       for message in messages)
            return await super().invoke(messages, receive, **options)

    async def run():
        await basic(app, world)
        conv = await conversation(chat, world)
        await work_input(chat, world, conv["conversation_id"], "写个猜拳python程序", compact=compact)
        await chat.process_one()
        chat.provider = SaveProgram([{"intent": "write_file", "arguments": {
            "root_id": "workspace", "path": "rock_paper_scissors.py", "content": PROGRAM}}])
        turn, _ = await work_input(chat, world, conv["conversation_id"], "保存到本地", compact=compact)
        await chat.process_one()
        await chat.process_one()
        current = await chat.get(world.owner, turn["turn_id"])
        await file_task(app, world, current["work_steps"][0]["preview_task_id"], folder)
        await chat.process_one()
        current = await chat.get(world.owner, turn["turn_id"])
        await file_task(app, world, current["work_steps"][0]["task_id"], folder)
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        assert done["status"] == "completed"
        assert done["work_steps"][0]["status"] == "succeeded"
        assert (folder / "rock_paper_scissors.py").read_text(encoding="utf-8") == PROGRAM
        assert len(await rows(world, "tasks")) == 2
        assert not await rows(world, "chat_work_answers")
    asyncio.run(run())


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("boundary", ["tool_result", "memory", "references"])
def test_work_history_keeps_private_data_boundary(world, monkeypatch, compact, boundary):
    app = app_for(world, monkeypatch, FakeChat(text=ANSWER))
    chat = app.state.chat

    async def run():
        await basic(app, world)
        conv = await conversation(chat, world)
        if boundary == "tool_result":
            chat.provider = WorkProvider([{"intent": "get_node_status", "arguments": {}}])
        first, _ = await work_input(chat, world, conv["conversation_id"], "旧轮次", compact=compact)
        await chat.process_one()
        if boundary == "tool_result":
            await chat.process_one()
            assert await rows(world, "chat_work_steps")
        else:
            column = "memory_refs_json" if boundary == "memory" else "references_json"
            async with world.db.transaction() as conn:
                await conn.execute(f"UPDATE chat_turns SET {column}='[{{}}]' WHERE turn_id=?", (first["turn_id"],))
        follow, _ = await work_input(chat, world, conv["conversation_id"], "继续", compact=compact)
        assert follow["context_count"] == 0
        assert (await chat.get(world.owner, follow["turn_id"]))["context_messages"] == [
            {"role": "user", "content": "继续"}]
        chat.provider = FakeChat(text="新的普通回答")
        await chat.process_one()
        next_turn, _ = await work_input(chat, world, conv["conversation_id"], "再继续", compact=compact)
        assert next_turn["context_count"] == 1
        assert (await chat.get(world.owner, next_turn["turn_id"]))["context_messages"] == [
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "新的普通回答"},
            {"role": "user", "content": "再继续"},
        ]
    asyncio.run(run())


@pytest.mark.parametrize("compact", [False, True])
def test_work_history_obeys_context_opt_out_and_conversation_isolation(world, monkeypatch, compact):
    app = app_for(world, monkeypatch, FakeChat(text=ANSWER))
    chat = app.state.chat

    async def run():
        await basic(app, world)
        conv = await conversation(chat, world)
        await work_input(chat, world, conv["conversation_id"], "写个猜拳python程序", compact=compact)
        await chat.process_one()
        opted_out, _ = await work_input(chat, world, conv["conversation_id"], "保存到本地",
                                        compact=compact, include_context=False)
        assert opted_out["context_count"] == 0
        assert (await chat.get(world.owner, opted_out["turn_id"]))["context_messages"] == [
            {"role": "user", "content": "保存到本地"}]
        other = await conversation(chat, world)
        isolated, _ = await work_input(chat, world, other["conversation_id"], "保存到本地", compact=compact)
        assert isolated["context_count"] == 0
        assert (await chat.get(world.owner, isolated["turn_id"]))["context_messages"] == [
            {"role": "user", "content": "保存到本地"}]
    asyncio.run(run())


def test_manual_compaction_can_summarize_plain_work_replies(world, monkeypatch):
    provider = CompactProvider(text=ANSWER + "\n# 注释" * 400)
    app = app_for(world, monkeypatch, provider)
    chat = app.state.chat

    async def run():
        await basic(app, world)
        conv = await conversation(chat, world)
        source_ids = []
        for text in ("写个猜拳python程序", "说明游戏规则"):
            turn, _ = await work_input(chat, world, conv["conversation_id"], text, compact=True)
            await chat.process_one()
            source_ids.append(turn["turn_id"])
        compact, _ = await send(chat, world, conv["conversation_id"], text="/compact",
                                 allow_compaction=True, compact_only=True)
        await chat.process_one()
        done = await chat.get(world.owner, compact["turn_id"])
        assert done["status"] == "completed"
        journal = await chat.journal.detail(world.owner, compact["turn_id"])
        assert journal["compactions"][0]["outcome"] == "completed"
        assert all(identifier in journal["compactions"][0]["source_ids_json"] for identifier in source_ids)
        assert not await rows(world, "chat_work_steps")
    asyncio.run(run())
