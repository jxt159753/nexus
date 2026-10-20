"""Real local file guards, pinned commands, write preview and effect boundaries."""

import asyncio
import hashlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from nexus.contracts.basic import FileReadArguments, FileSearchArguments, FilePreviewArguments, FileWriteArguments, CommandRunArguments
from nexus.contracts.messages import CommandExecute
from nexus.contracts.task import FileWriteRequest
from nexus.core.storage.tasks import Conflict
from nexus.core.storage.database import timestamp, utc_now
from nexus.node.config import CommandTemplate
from nexus.node.tools.files import read_file, find_files, preview_write, write_file
from nexus.node.tools.commands import run_command


@pytest.mark.parametrize("path",["../a","/a","a/../b","a\\b","C:/a","a:stream","a//b","CON.txt","aux","a.","a ","a/*"])
def test_path_traversal_devices_and_streams_rejected(path):
    with pytest.raises(ValidationError):
        FileReadArguments(root_id="workspace",path=path)


def test_file_read_search_write_hash_staleness_and_utf8(tmp_path):
    root=tmp_path/"workspace";root.mkdir();(root/"src").mkdir()
    (root/"src"/"hello.txt").write_bytes("你好\n旧内容".encode())
    (root/"binary.dat").write_bytes(b"\xff")
    read=read_file(FileReadArguments(root_id="workspace",path="src/hello.txt",max_bytes=4),root)
    assert read.text=="你" and read.truncated and read.size_bytes==len("你好\n旧内容".encode())
    with pytest.raises(UnicodeDecodeError):
        read_file(FileReadArguments(root_id="workspace",path="binary.dat"),root)
    results=find_files(FileSearchArguments(root_id="workspace",pattern="*.txt"),root)
    assert results.paths==["src/hello.txt"]
    assert find_files(FileSearchArguments(root_id="workspace",pattern="*.txt",max_depth=0),root).truncated
    args=FilePreviewArguments(root_id="workspace",path="src/hello.txt",content="新的内容\n")
    preview=preview_write(args,root)
    assert "旧内容" in preview.diff and "新的内容" in preview.diff
    write=FileWriteArguments(**args.model_dump(),expected_sha256=preview.previous_sha256,preview_task_id=uuid4(),preview_digest=preview.preview_digest)
    result=write_file(write,root)
    assert result.sha256==hashlib.sha256(args.content.encode()).hexdigest()
    assert (root/"src"/"hello.txt").read_bytes()==args.content.encode()
    with pytest.raises(PermissionError):
        write_file(write,root)
    args=FilePreviewArguments(root_id="workspace",path="new.txt",content="created")
    preview=preview_write(args,root)
    write_file(FileWriteArguments(**args.model_dump(),expected_sha256=None,preview_task_id=uuid4(),preview_digest=preview.preview_digest),root)
    assert (root/"new.txt").read_text()=="created"
    assert not list(root.glob(".nexus-write-*"))


def test_hardlinks_and_windows_junction_are_not_followed(tmp_path):
    root=tmp_path/"workspace";root.mkdir()
    outside=tmp_path/"outside";outside.mkdir();(outside/"secret.txt").write_text("secret")
    os.link(outside/"secret.txt",root/"hard.txt")
    with pytest.raises(PermissionError):
        read_file(FileReadArguments(root_id="workspace",path="hard.txt"),root)
    if os.name=="nt":
        import subprocess
        made=subprocess.run(["cmd","/c","mklink","/J",str(root/"jump"),str(outside)],capture_output=True)
        assert made.returncode==0
    else:
        (root/"jump").symlink_to(outside,target_is_directory=True)
    try:
        with pytest.raises((PermissionError,ValueError,OSError)):
            read_file(FileReadArguments(root_id="workspace",path="jump/secret.txt"),root)
        assert find_files(FileSearchArguments(root_id="workspace",pattern="*"),root).paths==[]
    finally:
        # Remove this test's link only, never traverse or recursively delete it.
        if os.name=="nt":
            os.rmdir(root/"jump")
        else:
            (root/"jump").unlink()


def template(argv):
    executable=Path(sys.executable).resolve()
    return CommandTemplate(executable=executable,executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),argv=argv)


def test_commands_are_pinned_fixed_argv_and_do_not_inherit_secrets(tmp_path,monkeypatch):
    monkeypatch.setenv("NEXUS_NODE_TOKEN","do-not-inherit")
    command=template(["-I","-c","import os; print(os.environ.get('NEXUS_NODE_TOKEN','absent'))"])
    args=CommandRunArguments(root_id="workspace",command_id="env-check",template_digest=command.digest())
    result=run_command(args,tmp_path,{"env-check":command})
    assert result.exit_code==0 and result.stdout.strip()=="absent"
    with pytest.raises(PermissionError):
        run_command(args.model_copy(update={"template_digest":"a"*64}),tmp_path,{"env-check":command})
    bad=command.model_copy(update={"executable_sha256":"a"*64})
    with pytest.raises(PermissionError):
        run_command(args.model_copy(update={"template_digest":bad.digest()}),tmp_path,{"env-check":bad})


def test_command_output_overflow_stops_owned_process(tmp_path):
    command=template(["-I","-c","print('x'*100000)"])
    with pytest.raises(ValueError):
        run_command(CommandRunArguments(root_id="workspace",command_id="overflow",template_digest=command.digest()),tmp_path,{"overflow":command})


def test_write_task_requires_matching_successful_preview(world):
    async def scenario():
        request=FileWriteRequest(request_id=uuid4(),intent="write_file",arguments=FileWriteArguments(root_id="workspace",path="a.txt",content="ok",expected_sha256=None,preview_task_id=uuid4(),preview_digest="a"*64))
        with pytest.raises(Conflict,match="WRITE_PREVIEW_REQUIRED"):
            await world.tasks.create(world.owner_id,request,world.node_id)
        async with world.db.transaction(write=False) as conn:
            assert (await conn.execute_fetchall("SELECT * FROM tasks"))==[]
    asyncio.run(scenario())
