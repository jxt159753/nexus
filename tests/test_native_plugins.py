"""Plugin identity, Core ownership and real managed Windows shell evidence."""

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.plugins.catalog import RELEASES
from nexus.plugins.contracts import PluginRelease,PluginNodeArguments
from nexus.plugins.schema import checked
from nexus.core.security.modes import PermissionHTTP,ModeChange
from nexus.core.storage.database import one,utc_now
from nexus.core.plugins.schedule import manage,tick
from test_chat_skills import app_for
from test_chat_work import setup


def test_plugin_contracts_reserve_authority_and_reject_undeclared_fields():
    for release in RELEASES.values():
        assert PluginRelease.model_validate_json(release.model_dump_json()).digest()==release.digest()
    original=RELEASES['builtin.shell'].model_dump(mode='json')
    original['manifest']['plugin_id']='user.bad_plugin'
    with pytest.raises(ValidationError): PluginRelease.model_validate(original)
    with pytest.raises(ValueError): checked(RELEASES['builtin.shell'].manifest.tools[0].input_schema,{'command':'ok','permission_mode':'full'})
    with pytest.raises(ValueError): checked({'type':'integer'},True)


def test_node_plugin_policy_binds_workspace_and_browser_origins(tmp_path):
    from nexus.node.config import NodeConfig
    from nexus.plugins.config import PluginNodeConfig
    from nexus.node.tools.interaction_config import BrowserTarget
    first=NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True))
    other=tmp_path/'other';other.mkdir()
    second=first.model_copy(update={'roots':{'workspace':other}})
    assert first.plugin_digest()!=second.plugin_digest()
    third=first.model_copy(update={'interaction_targets':{'browser':BrowserTarget(kind='edge',endpoint='http://127.0.0.1:9222',allowed_origins=['https://example.com'])}})
    assert first.plugin_digest()!=third.plugin_digest()


def test_plugins_use_native_pairing_and_full_mode_core_operations(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        chat.plugins.config.enabled=True
        await chat.plugins.seed()
        await PermissionHTTP(chat,None).change(world.owner,ModeChange(request_id=uuid4(),expected_revision=1,mode='full'))
        _,_,turn=await setup(app,world,[{'intent':'plugin_execute','arguments':{'plugin_id':'builtin.todo','tool':'todo_write','input':{'todos':[{'id':'a','text':'check evidence','status':'in_progress'}]}}}])
        for _ in range(4): await chat.process_one()
        current=await chat.get(world.owner,turn['turn_id'])
        result=current['work_steps'][0]['result']
        assert result['status']=='succeeded' and result['data']['verification']=='planning_status_only'
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,'SELECT count(*) FROM plugin_todos'))[0]==1
            assert (await one(conn,'SELECT count(*) FROM tasks'))[0]==0
            assert (await one(conn,'SELECT count(*) FROM chat_work_operations'))[0]==1
    asyncio.run(run())


def test_reminders_survive_repeated_ticks_without_model_or_duplicate_events(world,monkeypatch):
    from datetime import timedelta
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        conversation,_,_=await setup(app,world,[])
        now=utc_now()
        async with world.db.transaction() as conn:
            result=await manage(conn,world.owner,conversation['conversation_id'],'schedule_create',{'prompt':'检查待办','after_seconds':1},now)
        await tick(chat.plugins,now+timedelta(seconds=2))
        await tick(chat.plugins,now+timedelta(seconds=3))
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,'SELECT count(*) FROM plugin_schedule_events'))[0]==1
            row=await one(conn,'SELECT runs,paused FROM plugin_schedules WHERE schedule_id=?',(result['schedule_id'],))
            assert tuple(row)==(1,1)
            assert (await one(conn,'SELECT count(*) FROM model_calls'))[0]==0
    asyncio.run(run())


@pytest.mark.skipif(os.name!='nt',reason='real Windows process lifecycle')
def test_real_host_shell_idempotency_and_conversation_job_ownership(tmp_path):
    from nexus.node.config import NodeConfig
    from nexus.plugins.config import PluginNodeConfig
    from nexus.plugins.node_runtime import PluginRuntime
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    config=NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True,pwsh=powershell))
    runtime=PluginRuntime(config)
    context=uuid4()
    try:
        args=PluginNodeArguments(plugin_id='builtin.shell',tool='pwsh',release_digest=RELEASES['builtin.shell'].digest(),context_id=context,invocation_id=uuid4(),input={'command':"[System.IO.File]::AppendAllText('once.txt', 'x'); Write-Output 'real-output 中文'"})
        result=runtime.execute(args,time.monotonic()+10,threading.Event(),'unused')
        assert 'real-output 中文' in result.data['output'] and result.data['exit_code']==0
        assert runtime.execute(args,time.monotonic()+10,threading.Event(),'unused')==result
        assert (tmp_path/'once.txt').read_text()=='x'
        background=runtime.call('builtin.shell','pwsh',{'command':'Start-Sleep -Seconds 20','run_in_background':True},str(context),time.monotonic()+10,threading.Event())
        with pytest.raises(PermissionError): runtime.control('job_output',{'job_id':background['job_id']},str(uuid4()),time.monotonic()+10,threading.Event())
        stopped=runtime.control('job_kill',{'job_id':background['job_id']},str(context),time.monotonic()+10,threading.Event())
        assert stopped['execution_stopped'] and stopped['status']=='cancelled'
    finally: runtime.close()


@pytest.mark.skipif(os.name!='nt',reason='real Windows managed workers')
def test_programmatic_calls_and_file_plugin_preserve_actual_content(tmp_path):
    from nexus.node.config import NodeConfig
    from nexus.plugins.config import PluginNodeConfig
    from nexus.plugins.node_runtime import PluginRuntime
    runtime=PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True)))
    context=uuid4()
    def invoke(plugin,tool,value,capabilities=None):
        args=PluginNodeArguments(plugin_id=plugin,tool=tool,input=value,release_digest=RELEASES[plugin].digest(),context_id=context,invocation_id=uuid4(),capability_digests=capabilities or {})
        return runtime.execute(args,time.monotonic()+10,threading.Event(),'unused').data
    try:
        write=invoke('builtin.files','write',{'path':'hello.txt','content':'中文与实际证据\n','expected_sha256':None})
        assert write['verification']=='preview_cas_readback'
        read=invoke('builtin.files','read',{'path':'hello.txt'})
        assert read['lines'][0]['text']=='中文与实际证据'
        with pytest.raises(ValueError): invoke('builtin.files','write',{'path':'hello.txt','content':'stale','expected_sha256':None})
        code="data=call('builtin.files','read',{'path':'hello.txt'}); result={'text':data['lines'][0]['text']}"
        result=invoke('builtin.workflow','run_code',{'code':code},{'builtin.files':RELEASES['builtin.files'].digest()})
        assert result['result']=={'text':'中文与实际证据'} and len(result['subcalls'])==1
        assert (tmp_path/'hello.txt').read_text(encoding='utf-8')=='中文与实际证据\n'
        with runtime.db() as conn:
            assert conn.execute('SELECT count(*) FROM subcalls WHERE result_json IS NOT NULL').fetchone()[0]==1
    finally: runtime.close()


@pytest.mark.skipif(os.name!='nt',reason='real Windows ConPTY')
def test_terminal_keeps_state_and_interrupt_preserves_shell(tmp_path):
    from nexus.node.config import NodeConfig
    from nexus.plugins.config import PluginNodeConfig
    from nexus.plugins.node_runtime import PluginRuntime
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    runtime=PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True,pwsh=powershell)))
    context=str(uuid4());cancel=threading.Event()
    def control(name,value): return runtime.control(name,value,context,time.monotonic()+10,cancel)
    try:
        opened=control('terminal_open',{'shell':'pwsh'})
        identifier=opened['session_id']
        control('terminal_send',{'session_id':identifier,'text':"$nexusValue=37; Write-Output ('state-'+$nexusValue)\r"})
        output=''; deadline=time.monotonic()+8
        while 'state-37' not in output and time.monotonic()<deadline:
            output+=control('terminal_read',{'session_id':identifier,'wait_ms':100})['output']
        assert 'state-37' in output,output
        control('terminal_send',{'session_id':identifier,'text':'Start-Sleep -Seconds 30\r'})
        time.sleep(.3)
        assert control('terminal_signal',{'session_id':identifier,'signal':'interrupt'})['status']=='running'
        # PowerShell flushes queued input when it finishes handling Ctrl+C.
        time.sleep(.3)
        control('terminal_send',{'session_id':identifier,'text':"Write-Output ('kept-'+$nexusValue)\r"})
        output=''; deadline=time.monotonic()+5
        while 'kept-37' not in output and time.monotonic()<deadline:
            output+=control('terminal_read',{'session_id':identifier,'wait_ms':100})['output']
        assert 'kept-37' in output,output
        assert control('terminal_close',{'session_id':identifier})['execution_stopped']
    finally: runtime.close()
