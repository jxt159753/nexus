"""Phone tools use the existing authenticated task, preview and receipt paths."""
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from nexus.contracts.android_control import (
    AndroidUIActArguments, AndroidFilesArguments, AndroidFilePreviewArguments,
    AndroidUIResult, AndroidFilesCall, AndroidFilesResult, digest, preview_digest,
)
from nexus.contracts.commands import command_digest, command_identity
from nexus.contracts.messages import CommandResult, NodeHello
from nexus.contracts.protocol import parse_message
from nexus.contracts.tools import validate_result
from nexus.core.app import create_app
from nexus.core.config import TrustedNode
from nexus.core.security.service import has_grant
from nexus.core.storage.database import one
from nexus.link import wire_message
from test_device_enrollment import binding, say
from test_harness_http import login
from test_node_link import core_config, headers

ROOTS = {"android.ui_observe":"android:ui", "android.ui_act":"android:ui",
         "android.files":"android:files", "android.file_preview":"android:files", "android.file_write":"android:files"}
TREE = "a"*64
STATE = {"package_name":"test.phone.app","window_id":7,"nodes":[],"truncated":False}


def ui(action=None):
    return {"state":STATE,"observation_digest":digest(STATE),"action":action,
            "previous_digest":digest(STATE) if action else None,"verification":"action_returned" if action else "ui_observed"}


def setup(world):
    async def seed():
        for identity in (world.owner_id,world.node_id):
            for tool,root in ROOTS.items():
                await world.security.set_grant(identity,tool,root_id=root)
    asyncio.run(seed())
    return create_app(core_config(world,heartbeat_interval_seconds=10,heartbeat_timeout_seconds=30,nodes={world.node_id:TrustedNode(tools=frozenset(t+'@1' for t in ROOTS),roots=frozenset(ROOTS.values()))}))


@pytest.mark.parametrize("arguments", [
    {"observation_digest":TREE,"action":"click"},
    {"observation_digest":TREE,"action":"home","node":"r.0"},
    {"observation_digest":TREE,"action":"click","node":"r.0","text":"extra"},
    {"observation_digest":TREE,"action":"set_text","node":"r.0"},
    {"observation_digest":TREE,"action":"tap","x":10,"y":20},
])
def test_reject_unbound_or_unsupported_ui_actions(arguments):
    with pytest.raises(ValidationError): AndroidUIActArguments.model_validate(arguments)


@pytest.mark.parametrize("path", ["/sdcard/test", "../test", "x/../test", "x\\test", "x//test", "x/", "x\x00test"])
def test_document_paths_stay_relative(path):
    with pytest.raises(ValidationError): AndroidFilesArguments(operation="read",tree_id=TREE,path=path)


def test_evidence_rejects_tampering_passwords_and_bad_continuation():
    value=ui();value['observation_digest']='b'*64
    with pytest.raises(ValidationError): AndroidUIResult.model_validate(value)
    secret={"node":"r","text":"secret","description":"","class_name":"EditText","bounds":[0,0,1,1],"actions":[],"password":True}
    state={**STATE,"nodes":[secret]}
    with pytest.raises(ValidationError): AndroidUIResult.model_validate({**ui(),"state":state,"observation_digest":digest(state)})
    call=AndroidFilesCall(tool="android.files",tool_version=1,arguments={"operation":"read","tree_id":TREE,"path":"a.txt"})
    output=dict(operation="read",tree_id=TREE,path="a.txt",offset=0,entries=[],text="你好🙂",sha256=hashlib.sha256("你好🙂".encode()).hexdigest(),size_bytes=10,next_offset=None,truncated=False)
    validate_result(call,AndroidFilesResult(**output),32768)
    for patch in ({"next_offset":2,"truncated":True},{"sha256":"b"*64},{"tree_id":"c"*64}):
        with pytest.raises(ValueError): validate_result(call,AndroidFilesResult(**(output|patch)),32768)
    with pytest.raises(ValidationError): AndroidFilePreviewArguments(tree_id=TREE,path="a.txt",content="中"*4001)
    assert AndroidFilesArguments(operation='read',tree_id=TREE,path='a.txt',offset=1_040_000).offset==1_040_000
    with pytest.raises(ValidationError): AndroidFilesArguments(operation='read',tree_id=TREE,path='a.txt',offset=1_048_577)


def test_real_link_all_phone_tools_preview_binding_and_idempotency(world):
    app=setup(world)
    with TestClient(app,base_url="http://127.0.0.1:8000") as client:
        auth=login(client,world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node",headers=headers(world)) as ws:
            ws.send_text(wire_message("node.hello",NodeHello(node_id=world.node_id,node_version="phone-test",boot_id=uuid4(),tools=[{"tool":t,"tool_version":1} for t in ROOTS],roots=list(set(ROOTS.values())))))
            session=parse_message(ws.receive_text()).payload.session_id
            catalog=client.get("/api/v1/chat/work-catalog",params={"node_id":str(world.node_id)}).json()['catalog']
            assert {"android_ui_observe","android_ui_act","android_files","android_file_write"} <= {e['name'] for e in catalog}
            assert not {"android_file_preview"} & {e['name'] for e in catalog}
            def run(tool, arguments, result):
                request={"node_id":str(world.node_id),"request":{"request_id":str(uuid4()),"intent":tool.replace('.','_'),"arguments":arguments}}
                response=client.post('/api/v1/tasks',json=request,headers=auth);response.raise_for_status();task=response.json()['task']
                detail=client.get('/api/v1/tasks/'+task['task_id']).json()
                assert task['status']=='awaiting_approval' and not detail['attempts']
                client.post('/api/v1/approvals/'+detail['approvals'][0]['approval_id'],json={"decision":"approve"},headers=auth).raise_for_status()
                command=parse_message(ws.receive_text()).payload
                assert command_digest(command,world.node_id)==command.command_digest
                payload=TypeAdapter(CommandResult).validate_json(json.dumps(dict(session_id=str(session),task_id=str(command.task_id),attempt_id=str(command.attempt_id),tool=tool,tool_version=1,command_digest=command.command_digest,observed_at=datetime.now(UTC).isoformat(),outcome='succeeded',result=result)))
                ws.send_text(wire_message('command.result',payload));assert parse_message(ws.receive_text()).type=='result.ack'
                detail=client.get('/api/v1/tasks/'+task['task_id']).json()
                assert detail['task']['status']=='succeeded',detail
                assert client.post('/api/v1/tasks',json=request,headers=auth).json()['task']['task_id']==task['task_id']
                return task['task_id']
            run('android.ui_observe',{},ui())
            run('android.ui_act',dict(action='home',observation_digest=digest(STATE)),ui('home'))
            run('android.files',dict(operation='roots'),dict(operation='roots',tree_id=None,path='',offset=0,entries=[],text=None,sha256=None,size_bytes=None,next_offset=None,truncated=False))
            content='手机读写🙂';sha=hashlib.sha256(content.encode()).hexdigest();pd=preview_digest(TREE,'note.txt',None,sha)
            args=dict(tree_id=TREE,path='note.txt',content=content)
            preview=run('android.file_preview',args,dict(tree_id=TREE,path='note.txt',previous_sha256=None,previous_text=None,content_sha256=sha,preview_digest=pd,bytes_written=len(content.encode())))
            write={**args,'expected_sha256':None,'preview_task_id':preview,'preview_digest':pd}
            bad=client.post('/api/v1/tasks',json={'node_id':str(world.node_id),'request':{'request_id':str(uuid4()),'intent':'android_file_write','arguments':write|{'tree_id':'b'*64}}},headers=auth)
            assert bad.status_code==409
            run('android.file_write',write,dict(tree_id=TREE,path='note.txt',sha256=sha,bytes_written=len(content.encode()),backup_path=None))


def test_new_permissions_are_explicit_scoped_and_revocable(world):
    app=create_app(core_config(world))
    async def check():
        chat=app.state.chat;device=binding();await chat.devices.prepare(world.owner,device)
        await say(chat,world,'将此设备注册为节点',device)
        status=await chat.devices.status(world.owner,device.client_id,device.proof.get_secret_value())
        assert not status['permissions']['accessibility'] and not status['permissions']['files']
        await say(chat,world,'允许此设备的无障碍操作和手机文件权限',device)
        async with world.db.transaction(write=False) as conn:
            for tool,root in ROOTS.items():
                assert await has_grant(conn,world.owner_id,device.node_id,tool,1,root)
                assert not await has_grant(conn,world.owner_id,world.node_id,tool,1,root)
        await say(chat,world,'关闭此设备的无障碍操作和手机文件权限',device)
        async with world.db.transaction(write=False) as conn:
            assert not await one(conn,'SELECT 1 FROM grants WHERE identity_id=? AND tool IN (?,?,?,?,?)',(str(device.node_id),*ROOTS))
            assert not await one(conn,'SELECT 1 FROM model_calls')
    asyncio.run(check())


def test_phone_python_kotlin_vectors():
    vectors=json.loads(Path('apps/android/protocol/src/test/resources/phone-commands.json').read_text(encoding='utf-8'))
    for vector in vectors:
        command=parse_message(vector['wire']).payload
        assert command_digest(command,UUID(vector['node_id']))==command.command_digest
        assert command_identity(command,UUID(vector['node_id']))==vector['identity']


@pytest.mark.parametrize('mode', ['normal','full','plan'])
def test_phone_chat_write_binds_preview_and_respects_mode(world,monkeypatch,mode):
    from test_chat_skills import app_for
    from test_chat_work import setup as work_setup
    from nexus.contracts.work import WorkPermissionReply
    from nexus.core.security.modes import ModeChange, PermissionHTTP
    from nexus.core.storage.execution import ExecutionRepository, RESULT
    from nexus.core.storage.database import utc_now
    app=app_for(world,monkeypatch)
    async def run():
        for identity in (world.owner_id,world.node_id):
            for tool,root in ROOTS.items(): await world.security.set_grant(identity,tool,root_id=root)
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset(t+'@1' for t in ROOTS),roots=frozenset(ROOTS.values()))
        if mode=='full': await PermissionHTTP(app.state.chat,None).change(world.owner,ModeChange(request_id=uuid4(),expected_revision=1,mode='full'))
        content='手机新内容';sha=hashlib.sha256(content.encode()).hexdigest()
        args=dict(tree_id=TREE,path='note.txt',content=content)
        chat=app.state.chat
        conv,_,turn=await work_setup(app,world,[{'intent':'android_file_write','arguments':args}])
        if mode=='plan':
            async with world.db.transaction() as conn: await conn.execute('UPDATE conversations SET plan_mode=1 WHERE conversation_id=?',(conv['conversation_id'],))
        async def finish(identifier,output):
            task=await world.tasks.get(UUID(identifier),world.owner_id)
            assert task.status=='ready'
            repo=ExecutionRepository(world.db,world.security,32768)
            command=await repo.dispatch(task.task_id,world.node,app.state.harness.scope(world.node_id),uuid4(),uuid4(),10)
            result=RESULT.validate_json(json.dumps({**command.model_dump(mode='json',include={'session_id','task_id','attempt_id','tool','tool_version','command_digest'}),'observed_at':utc_now().isoformat(),'outcome':'succeeded','result':output}))
            await repo.store_result(world.node,result);await repo.verify(command.attempt_id)
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn['turn_id'])
        if mode=='plan':
            assert not current['work_steps'] or not current['work_steps'][0]['task_id']
            async with world.db.transaction(write=False) as conn: assert not await one(conn,"SELECT 1 FROM task_attempts")
            return
        step=current['work_steps'][0]
        await finish(step['preview_task_id'],dict(tree_id=TREE,path='note.txt',previous_sha256=None,previous_text=None,content_sha256=sha,preview_digest=preview_digest(TREE,'note.txt',None,sha),bytes_written=len(content.encode())))
        await chat.process_one()
        current=await chat.get(world.owner,turn['turn_id']);step=current['work_steps'][0]
        if mode=='normal':
            assert current['work_status']=='permission' and step['task_id'] is None
            assert '手机授权目录/note.txt' in step['request_text'] and '+'+content in step['result']['preview']['diff']
            await chat.work.answer(world.owner,UUID(turn['turn_id']),WorkPermissionReply(request_id=uuid4(),step_id=UUID(step['step_id']),expected_revision=current['revision'],text='同意'))
            await chat.process_one();current=await chat.get(world.owner,turn['turn_id']);step=current['work_steps'][0]
        await finish(step['task_id'],dict(tree_id=TREE,path='note.txt',sha256=sha,bytes_written=len(content.encode()),backup_path=None))
        await chat.process_one()
        assert (await chat.get(world.owner,turn['turn_id']))['status']=='completed'
    asyncio.run(run())


def test_migration_keeps_existing_device_grants_and_verified_backup(tmp_path):
    import sqlite3
    from contextlib import closing
    from importlib.resources import files
    from nexus.core.storage.database import Database, timestamp, utc_now
    path=tmp_path/'before-phone.sqlite3';now=timestamp(utc_now())
    owner,node,grant=(str(uuid4()) for _ in range(3))
    with closing(sqlite3.connect(path)) as conn:
        for migration in sorted(files('nexus.core.storage').joinpath('migrations').iterdir(),key=lambda p:p.name):
            if not migration.name.endswith('.sql') or int(migration.name[:3])>41: continue
            source=migration.read_text(encoding='utf-8');conn.executescript(source)
            conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)')
            conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(int(migration.name[:3]),hashlib.sha256(source.encode()).hexdigest(),now));conn.commit()
        for identifier,role in ((owner,'owner'),(node,'node')):
            conn.execute('INSERT INTO identities VALUES(?,?,?,?,?)',(identifier,role,'test-hash-'+identifier,None,now))
        conn.execute("INSERT INTO grants VALUES(?,?, 'android.device_status',1,'android:device')",(grant,node))
        conn.execute("INSERT INTO device_owner_grants VALUES(?,?,'android.device_status',1,'android:device')",(owner,node))
        conn.commit()
        old={table:conn.execute('SELECT * FROM '+table).fetchall() for table in ('identities','grants','device_owner_grants')}
    asyncio.run(Database(path).initialize())
    backups=list(tmp_path.glob('*.before-v42-*.sqlite3'));assert len(backups)==1
    for database in (path,backups[0]):
        with closing(sqlite3.connect(database)) as conn:
            assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
            for table,rows in old.items(): assert conn.execute('SELECT * FROM '+table).fetchall()==rows


@pytest.mark.parametrize('mode', ['normal','full','plan'])
def test_phone_actions_require_concrete_normal_approval_and_never_run_in_plan(world,monkeypatch,mode):
    from test_chat_skills import app_for
    from test_chat_work import setup as work_setup
    from nexus.core.security.modes import ModeChange, PermissionHTTP
    app=app_for(world,monkeypatch)
    async def run():
        tool='android.ui_act'
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({tool+'@1'}),roots=frozenset({'android:ui'}))
        for identity in (world.owner_id,world.node_id): await world.security.set_grant(identity,tool,root_id='android:ui')
        if mode=='full': await PermissionHTTP(app.state.chat,None).change(world.owner,ModeChange(request_id=uuid4(),expected_revision=1,mode='full'))
        conv,_,turn=await work_setup(app,world,[dict(intent='android_ui_act',arguments=dict(action='home',observation_digest=digest(STATE)))])
        if mode=='plan':
            async with world.db.transaction() as conn: await conn.execute('UPDATE conversations SET plan_mode=1 WHERE conversation_id=?',(conv['conversation_id'],))
        await app.state.chat.process_one();await app.state.chat.process_one()
        async with world.db.transaction(write=False) as conn:
            tasks=await conn.execute_fetchall('SELECT status FROM tasks')
            if mode=='plan': assert not tasks or all(row[0]=='failed' for row in tasks)
            else: assert [row[0] for row in tasks]==['ready' if mode=='full' else 'awaiting_approval']
            assert not await one(conn,'SELECT 1 FROM task_attempts')
    asyncio.run(run())


def test_hello_supports_all_android_permission_roots():
    roots=['workspace','android:device','android:notifications','android:ui','android:files']
    hello=NodeHello(node_id=uuid4(),node_version='phone-test',boot_id=uuid4(),tools=[],roots=roots)
    assert hello.roots==roots
