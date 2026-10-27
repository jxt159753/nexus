"""Virtual-phone scope, observed targets, durable calls and image projection."""
import base64
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.plugins.android_emulator import AndroidEmulator, AVD
from nexus.plugins.catalog import RELEASES, definition
from nexus.plugins.config import AndroidEmulatorConfig, PluginNodeConfig
from nexus.plugins.schema import checked


def test_emulator_contract_cannot_select_real_device_or_host_command(tmp_path):
    release = RELEASES['builtin.android_emulator']
    for tool in release.manifest.tools:
        with pytest.raises(ValueError):
            checked(tool.input_schema, {'serial':'physical-phone', 'command':'anything'})
    assert PluginNodeConfig(enabled=True).features({})['builtin.android_emulator'] == []
    policy = AndroidEmulatorConfig(sdk_dir=tmp_path, system_image=tmp_path/'image',
                                   emulator_version='36.6.11', image_revision='6')
    assert 'observe' in PluginNodeConfig(enabled=True,android_emulator=policy).features({})['builtin.android_emulator']
    with pytest.raises(ValidationError):
        AndroidEmulatorConfig(**{**policy.model_dump(), 'port':5555})
    with pytest.raises(ValidationError):
        AndroidEmulatorConfig(**{**policy.model_dump(), 'adb_port':policy.port})


@pytest.fixture
def phone(monkeypatch):
    Image = pytest.importorskip('PIL.Image')
    value = object.__new__(AndroidEmulator)
    picture = Image.new('RGB', (720,1280), 'white')
    output = io.BytesIO(); picture.save(output,format='PNG')
    current = {'pixels':picture, 'package':'com.example.app'}
    calls = []
    value.frames = {}; value.lock = threading.RLock()
    monkeypatch.setattr(value, 'screenshot', lambda *args:(output.getvalue(),current['pixels'],current['package']))
    monkeypatch.setattr(value, 'command', lambda args,*a,**kw:calls.append(args) or b'Broadcast completed: result=1')
    monkeypatch.setattr(value, 'ensure_input', lambda *args:None)
    return value, current, calls


def observe(phone, context='a'):
    return phone.observe(context,time.monotonic()+10,threading.Event())


def act(phone, frame, action='tap', **extra):
    return phone.act('act',dict(observation_digest=frame['observation_digest'],action=action,**extra),
                     'a',time.monotonic()+10,threading.Event())


def test_only_observed_conversation_and_fresh_frame_can_act(phone):
    device,current,calls = phone
    frame=observe(device,'other')
    with pytest.raises(ValueError,match='过期'):act(device,frame,x=10,y=10)
    frame=observe(device)
    device.frames['a']['time']-=121
    with pytest.raises(ValueError,match='过期'):act(device,frame,x=10,y=10)
    assert not calls


def test_coordinate_mapping_and_observation_is_consumed(phone):
    device,current,calls=phone
    frame=observe(device)
    act(device,frame,x=100,y=200)
    assert calls[0]==['shell','input','tap',str(100*720//frame['image']['width']),str(200*1280//frame['image']['height'])]
    with pytest.raises(ValueError,match='过期'):act(device,frame,x=100,y=200)
    assert len(calls)==1


def test_changed_target_or_foreground_is_rejected_but_unrelated_pixels_allowed(phone):
    device,current,calls=phone
    frame=observe(device)
    current['pixels']=current['pixels'].copy();current['pixels'].putpixel((0,0),(0,0,0))
    act(device,frame,x=100,y=200)
    frame=observe(device)
    current['package']='com.other.app'
    with pytest.raises(ValueError,match='前台'):act(device,frame,x=100,y=200)
    current['package']='com.example.app'
    current['pixels']=current['pixels'].copy()
    current['pixels'].putpixel((100*720//frame['image']['width'],200*1280//frame['image']['height']),(0,0,0))
    with pytest.raises(ValueError,match='区域'):act(device,frame,x=100,y=200)
    assert len(calls)==1


def test_protected_settings_and_out_of_image_coordinates_are_rejected(phone):
    device,current,calls=phone
    frame=observe(device)
    with pytest.raises(ValueError,match='坐标'):act(device,frame,x=frame['image']['width'],y=1)
    current['package']='com.android.settings';frame=observe(device)
    with pytest.raises(ValueError,match='手动'):act(device,frame,x=1,y=1)
    act(device,frame,'home')
    assert calls==[['shell','input','keyevent','3']]


def test_system_overlay_cannot_receive_app_input(phone):
    device,current,calls=phone
    current['package']='android.system_overlay';frame=observe(device)
    with pytest.raises(ValueError,match='手动'):act(device,frame,x=1,y=1)
    assert not calls


def test_unicode_input_is_encoded_and_scoped_to_observed_app(phone):
    device,current,calls=phone
    frame=observe(device)
    text='中文输入 🙂 ; $(anything)'
    device.act('type_text',{'observation_digest':frame['observation_digest'],'text':text},'a',time.monotonic()+10,threading.Event())
    assert calls[0][4].endswith('.READY')
    assert base64.b64encode(text.encode()).decode() in calls[1]
    assert text not in calls[1]
    assert calls[1][-1]=='com.example.app'


def test_input_helper_first_install_and_existing_digest(monkeypatch):
    import json
    from nexus.plugins.android_emulator import INPUT_PACKAGE,INPUT_SERVICE
    assets=Path(__import__('nexus.plugins.android_emulator',fromlist=['x']).__file__).with_name('emulator_assets')
    digest=json.loads((assets/'input.json').read_text(encoding='utf-8'))['sha256']
    for installed in (False,True):
        device=object.__new__(AndroidEmulator);device.input_ready=False;calls=[]
        def command(args,*a,**kw):
            calls.append(args)
            if args[:4]==['shell','pm','list','packages']:
                return ('package:/data/app/~~abc==/'+INPUT_PACKAGE+'-xyz==/base.apk='+INPUT_PACKAGE).encode() if installed else b''
            if args[:2]==['shell','sha256sum']:return (digest+'  base.apk').encode()
            if args[0]=='install':return b'Success'
            return INPUT_SERVICE.encode()
        monkeypatch.setattr(device,'command',command)
        device.ensure_input(time.monotonic()+10,threading.Event())
        assert any(args[0]=='install' for args in calls)==(not installed)
        assert device.input_ready


def test_image_projection_removes_base64_from_text_context(phone):
    from nexus.core.cognitive.work_images import image_proposal,model_result
    frame=observe(phone[0]);data={'status':'succeeded','data':frame}
    assert image_proposal({'intent':'plugin_execute','arguments':{'plugin_id':'builtin.android_emulator','tool':'observe'}})
    projected=model_result(data,is_image=True)
    assert 'data_base64' not in projected['data']['image']
    assert 'data_base64' in data['data']['image']


def test_durable_invocation_replay_never_repeats_emulator_action(tmp_path,monkeypatch):
    from nexus.node.config import NodeConfig
    from nexus.plugins.node_runtime import PluginRuntime
    from nexus.plugins.contracts import PluginNodeArguments
    runtime=PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True)))
    calls=[]
    runtime.emulator=SimpleNamespace(execute=lambda *args:calls.append(args) or {'status':'starting'},close=lambda:None)
    arguments=PluginNodeArguments(plugin_id='builtin.android_emulator',tool='start',release_digest=RELEASES['builtin.android_emulator'].digest(),context_id=uuid4(),invocation_id=uuid4())
    try:
        first=runtime.execute(arguments,time.monotonic()+10,threading.Event(),'test')
        assert runtime.execute(arguments,time.monotonic()+10,threading.Event(),'test')==first
        assert len(calls)==1
    finally:runtime.close()


def test_node_parent_pipe_shutdown_releases_runtime(tmp_path):
    import json,os,subprocess,sys,sqlite3
    config=tmp_path/'node.toml'
    config.write_text('node_id = '+json.dumps(str(uuid4()))+'\ndata_dir = '+json.dumps(str(tmp_path))+ '\ncore_url = "ws://127.0.0.1:9/ws/v1/node"\nlocal_http = true\n',encoding='utf-8')
    child=subprocess.Popen([sys.executable,'-B','-m','nexus.node','--config',str(config),'--parent-stdin'],
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           env={**os.environ,'NEXUS_NODE_TOKEN':'synthetic-test-token-'+'a'*32})
    try:
        until=time.monotonic()+10
        while not (tmp_path/'receipts.sqlite3').exists() and child.poll() is None and time.monotonic()<until:time.sleep(.05)
        assert (tmp_path/'receipts.sqlite3').exists()
        out,error=child.communicate(b'shutdown\n',timeout=12)
        assert child.returncode==0,(out,error)
        with sqlite3.connect(tmp_path/'receipts.sqlite3') as connection:assert connection.execute('PRAGMA quick_check').fetchone()==('ok',)
    finally:
        if child.poll() is None:child.kill();child.wait()
