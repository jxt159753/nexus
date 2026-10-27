"""MuMu instance identity, lifecycle ownership and local-only transport."""
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nexus.plugins.config import MuMuConfig, PluginNodeConfig
from nexus.plugins.mumu import MuMu


@pytest.fixture
def phone(tmp_path, monkeypatch):
    device = object.__new__(MuMu)
    device.policy = MuMuConfig(backend='mumu', install_dir=tmp_path, version='6.7.2.0', instance=0, created_timestamp=123)
    device.cli = tmp_path/'mumu-cli.exe'; device.cli.touch()
    device.adb = tmp_path/'adb.exe'; device.adb.touch()
    device.owned=False; device.launch_until=0; device.stopping=False; device.server=None
    device.connection=None; device.frames={}; device.input_ready=False; device.lock=threading.RLock()
    state={'index':'0', 'created_timestamp':123, 'is_process_started':False, 'is_android_started':False}
    calls=[]
    def control(args,*unused):
        calls.append(args)
        return {'version':'6.7.2.0'} if args==['version'] else state.copy() if args[0]=='info' else {'errcode':0}
    monkeypatch.setattr(device,'control',control)
    return device,state,calls


def invoke(device,name):
    return getattr(device,name)(time.monotonic()+10,threading.Event())


def test_mumu_policy_is_explicit_and_changes_trusted_digest(tmp_path):
    p=MuMuConfig(backend='mumu',install_dir=tmp_path,version='6.7.2.0',instance=0,created_timestamp=123)
    config=PluginNodeConfig(enabled=True,android_emulator=p)
    assert isinstance(PluginNodeConfig.model_validate_json(config.model_dump_json()).android_emulator,MuMuConfig)
    assert config.digest()!=PluginNodeConfig(enabled=True,android_emulator=p.model_copy(update={'instance':1})).digest()
    for extra in ({'instance':-1},{'serial':'physical-phone'},{'install_dir':'relative'},{'created_timestamp':0}):
        with pytest.raises(ValidationError):MuMuConfig(**{**p.model_dump(),**extra})


@pytest.mark.parametrize('field,value',[('index','1'),('created_timestamp',456)])
def test_recreated_or_other_instance_is_rejected_before_action(phone,field,value):
    device,state,calls=phone;state[field]=value
    with pytest.raises(ValueError,match='身份'):invoke(device,'start')
    assert not any(c[0]=='control' for c in calls)


def test_start_is_not_repeated_while_launch_outcome_pending(phone):
    device,state,calls=phone
    assert invoke(device,'start')['status']=='starting'
    assert invoke(device,'start')['status']=='starting'
    assert calls.count(['control','-v','0','launch'])==1
    assert device.managed


def test_attach_does_not_own_shutdown_but_explicit_stop_can_close_selected_instance(phone):
    device,state,calls=phone;state['is_process_started']=True
    invoke(device,'start');device.close()
    assert not device.managed
    assert not any(c[0]=='control' for c in calls)
    invoke(device,'stop')
    assert calls[-1]==['control','-v','0','shutdown']
    assert invoke(device,'status')['status']=='stopping'
    state['is_process_started']=False
    assert invoke(device,'status')['status']=='stopped'


@pytest.mark.parametrize('host,port',[('0.0.0.0',16384),('192.168.1.2',16384),('127.0.0.1',5088),('127.0.0.1','16384')])
def test_invalid_endpoint_never_connects(phone,host,port):
    device,_,_=phone
    with pytest.raises(ValueError,match='本机'):
        device.connect({'adb_host_ip':host,'adb_port':port},time.monotonic()+5,threading.Event())
    assert device.server is None


def test_restart_invalidates_observations_before_connection(phone):
    device,_,_=phone;device.connection=('127.0.0.1:16384',10);device.frames={'conversation':{}}
    with pytest.raises(ValueError,match='重启'):
        device.connect({'adb_host_ip':'127.0.0.1','adb_port':16384,'pid':11},time.monotonic()+5,threading.Event())
    assert device.frames=={} and device.connection is None


def test_identity_snapshot_is_bounded_to_one_operation(phone):
    device,state,calls=phone
    device.execute('stop',{},'one',time.monotonic()+10,threading.Event())
    assert calls.count(['info','-v','0'])==1
    state['created_timestamp']=456
    with pytest.raises(ValueError,match='身份'):
        device.execute('status',{},'two',time.monotonic()+10,threading.Event())
