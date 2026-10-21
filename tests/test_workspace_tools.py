"""DSH-style file operations preserve local scope and actual write evidence."""

import asyncio
import hashlib
import os
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from nexus.contracts.basic import FileLinesArguments, FileGlobArguments, FileGrepArguments, FileEditPreviewArguments, FileEditArguments
from nexus.contracts.tools import ToolCall, validate_result
from nexus.node.tools.workspace import read_lines, glob_files, grep_files, preview_edit, edit_file


def test_lines_glob_grep_are_bounded_and_skip_links(tmp_path):
    (tmp_path/"src").mkdir()
    (tmp_path/"src"/"a.py").write_bytes("你好\nneedle\nthird\n".encode())
    (tmp_path/"empty").write_bytes(b"")
    (tmp_path/"binary").write_bytes(b"\x00needle")
    os.link(tmp_path/"src"/"a.py",tmp_path/"hard")
    args=FileLinesArguments(root_id="workspace",path="src/a.py",offset=2,limit=1)
    with pytest.raises(PermissionError):
        read_lines(args,tmp_path)
    (tmp_path/"hard").unlink()
    result=read_lines(args,tmp_path)
    assert result.lines[0].text=="needle" and result.lines[0].number==2 and result.next_offset==3
    assert result.sha256==hashlib.sha256((tmp_path/"src"/"a.py").read_bytes()).hexdigest()
    call=TypeAdapter(ToolCall).validate_python({"tool":"filesystem.read_lines","tool_version":1,"arguments":args})
    validate_result(call,result,32768)
    with pytest.raises(ValueError):
        validate_result(call,result.model_copy(update={"next_offset":None}),32768)
    assert read_lines(FileLinesArguments(root_id="workspace",path="empty"),tmp_path).total_lines==0
    with pytest.raises(ValueError):
        read_lines(args.model_copy(update={"offset":9}),tmp_path)
    assert glob_files(FileGlobArguments(root_id="workspace",pattern="**/*.py"),tmp_path).paths==["src/a.py"]
    assert glob_files(FileGlobArguments(root_id="workspace",path="src",pattern="*.py"),tmp_path).paths==["src/a.py"]
    grep=grep_files(FileGrepArguments(root_id="workspace",pattern="NEEDLE",ignore_case=True),tmp_path)
    assert [(m.path,m.number) for m in grep.matches]==[("src/a.py",2)]
    assert grep.skipped==1 and grep.truncated
    assert grep_files(FileGrepArguments(root_id="workspace",pattern="n.*e"),tmp_path).matches==[]


def test_exact_edit_requires_read_hash_unique_match_preview_and_readback(tmp_path):
    path=tmp_path/"note";path.write_bytes(b"before\nbefore\n")
    original=path.read_bytes()
    args=FileEditPreviewArguments(root_id="workspace",path="note",old_string="before",new_string="after",expected_sha256=hashlib.sha256(original).hexdigest())
    with pytest.raises(ValueError):
        preview_edit(args,tmp_path)
    args=args.model_copy(update={"replace_all":True})
    preview=preview_edit(args,tmp_path)
    assert path.read_bytes()==original and preview.replacements==2 and "+after" in preview.diff
    write=FileEditArguments(**args.model_dump(),preview_task_id=uuid4(),preview_digest=preview.preview_digest,content_sha256=preview.content_sha256,bytes_written=preview.bytes_written)
    result=edit_file(write,tmp_path)
    assert path.read_bytes()==b"after\nafter\n" and result.replacements==2
    with pytest.raises(PermissionError):
        edit_file(write,tmp_path)
    assert not list(tmp_path.glob(".nexus-write-*"))


@pytest.mark.parametrize("pattern",["../*","/a","a\\*","C:/*","a//b","!*.py"])
def test_glob_escape_rejected(pattern):
    with pytest.raises(ValidationError):
        FileGlobArguments(root_id="workspace",pattern=pattern)


def test_read_and_edit_use_existing_work_tasks_and_evidence(world,monkeypatch,tmp_path):
    from test_chat_skills import app_for
    from test_chat_work import setup, file_task
    from nexus.core.config import TrustedNode
    from nexus.core.storage.execution import ExecutionRepository, RESULT
    from nexus.core.storage.database import utc_now
    import json
    path=tmp_path/"note";path.write_bytes(b"before")
    app=app_for(world,monkeypatch)
    async def run():
        tools={"filesystem.read_lines@1","filesystem.edit_preview@1","filesystem.edit@1"}
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset(tools),roots=frozenset({"workspace"}))
        for key in tools:
            for identity in (world.owner_id,world.node_id):
                await world.security.set_grant(identity,key[:-2],root_id="workspace")
        _,_,turn=await setup(app,world,[{"intent":"read","arguments":{"root_id":"workspace","path":"note"}},{"intent":"edit","arguments":{"root_id":"workspace","path":"note","old_string":"before","new_string":"after","expected_sha256":hashlib.sha256(b"before").hexdigest()}}])
        chat=app.state.chat
        for _ in range(12):
            await chat.process_one()
            current=await chat.get(world.owner,turn["turn_id"])
            for step in current["work_steps"]:
                for field in ("preview_task_id","task_id"):
                    if not step[field]:
                        continue
                    task=await world.tasks.get(UUID(step[field]),world.owner_id)
                    if task.status!="ready":
                        continue
                    repo=ExecutionRepository(world.db,world.security,32768)
                    command=await repo.dispatch(task.task_id,world.node,app.state.harness.scope(world.node_id),uuid4(),uuid4(),10)
                    output={"filesystem.read_lines":read_lines,"filesystem.edit_preview":preview_edit,"filesystem.edit":edit_file}[command.tool](command.arguments,tmp_path)
                    result=RESULT.validate_json(json.dumps({**command.model_dump(mode="json",include={"session_id","task_id","attempt_id","tool","tool_version","command_digest"}),"observed_at":utc_now().isoformat(),"outcome":"succeeded","result":output.model_dump(mode="json")}))
                    await repo.store_result(world.node,result);await repo.verify(command.attempt_id)
            if current["work_status"]=="permission":
                from nexus.contracts.work import WorkPermissionReply
                await chat.work.answer(world.owner,UUID(turn["turn_id"]),WorkPermissionReply(request_id=uuid4(),step_id=UUID(current["work_steps"][-1]["step_id"]),expected_revision=current["revision"],text="同意"))
            if current["status"]=="completed" and not current["work_status"]:
                break
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and not done["work_status"]
        assert path.read_bytes()==b"after"
        assert done["work_steps"][-1]["result"]["data"]["sha256"]==hashlib.sha256(b"after").hexdigest()
    asyncio.run(run())
