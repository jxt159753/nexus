"""Session working directories, actual dependencies and fixed argv developer tools."""

import importlib.util
import os
import sys
import time
from pathlib import Path

from nexus.node.config import local_directory
from nexus.node.process import ExecutionStopped
from nexus.contracts.extensions import canonical


def capture(runtime,argv,context,deadline,cancel,cwd=None):
    with runtime.lock: process=runtime.start(context,'command',argv,cwd or runtime.cwd(context))
    try:
        while process.process.poll() is None:
            if cancel.is_set(): raise ExecutionStopped('cancelled')
            if time.monotonic()>=deadline: raise ExecutionStopped('timeout')
            time.sleep(.01)
        return process.output(8192,consume=False)
    finally:
        with runtime.lock:
            process.stop();runtime.processes.pop(process.identifier,None)
            with runtime.db() as conn:
                result=process.output(8192,consume=False)
                conn.execute('UPDATE processes SET state=?,result=? WHERE id=?',(result['status'],canonical(result),process.identifier))


def execute(runtime,name,value,context,deadline,cancel):
    current=runtime.cwd(context)
    if name=='working_directory':
        if value.get('cd'):
            target=local_directory(Path(os.path.abspath(current/value['cd'])))
            with runtime.db() as conn:
                conn.execute('INSERT INTO directories VALUES(?,?) ON CONFLICT(context_id) DO UPDATE SET path=excluded.path',(context,str(target)))
            current=target
        return {'cwd':str(current),'permission_root':str(runtime.root),'host_execution':True}
    if name=='load_workspace_dependencies':
        libraries={}
        for package in ('numpy','pandas','docx','pptx','openpyxl','PIL','lxml','xlsxwriter'):
            spec=importlib.util.find_spec(package)
            libraries[package]={'available':spec is not None,'path':spec.origin if spec else None}
        return {'python':sys.executable,'python_version':sys.version.split()[0],'bundled_executable':bool(getattr(sys,'frozen',False)),'libraries':libraries,
                'shells':{name:str(getattr(runtime.policy,name)) if getattr(runtime.policy,name) else None for name in ('pwsh','bash')}}
    if name=='create_worktree':
        executable=runtime.policy.git
        if executable is None or not executable.is_absolute() or not executable.is_file(): raise ValueError('Git executable is not configured')
        if any(not value[key] or value[key].startswith('-') or any(ord(c)<32 for c in value[key]) for key in ('branch','ref','path')):
            raise ValueError('invalid worktree ref or path')
        target=Path(os.path.abspath(current/value['path']))
        local_directory(target.parent)
        if target.exists(): raise ValueError('worktree destination already exists')
        branch_check=capture(runtime,[str(executable),'check-ref-format','--branch',value['branch']],context,deadline,cancel,current)
        if branch_check['exit_code']: raise ValueError('invalid Git branch name')
        revision=capture(runtime,[str(executable),'rev-parse','--verify',value['ref']+'^{commit}'],context,deadline,cancel,current)
        commit=revision['output'].strip()
        if revision['exit_code'] or len(commit) not in (40,64) or any(c not in '0123456789abcdef' for c in commit): raise ValueError('fixed local commit required')
        result=capture(runtime,[str(executable),'worktree','add','-b',value['branch'],'--',str(target),commit],context,deadline,cancel,current)
        if result['exit_code']==0:
            local_directory(target)
            with runtime.db() as conn: conn.execute('INSERT INTO directories VALUES(?,?) ON CONFLICT(context_id) DO UPDATE SET path=excluded.path',(context,str(target)))
        return {**result,'path':str(target),'branch':value['branch'],'commit':commit,'verification':'git_exit_and_checkout' if result['exit_code']==0 else 'git_returned_error'}
    if name=='lsp':
        from nexus.plugins.protocols import lsp
        return lsp(runtime,value,context,deadline,cancel)
    raise ValueError('unknown workspace tool')
