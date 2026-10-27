"""Host plugin execution: owned processes, bounded output and durable journals.

This is current-user host execution. A working directory and a Windows Job
are not a security sandbox. Core authentication/authorization precedes entry.
"""

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from nexus.contracts.extensions import canonical
from nexus.node.process import ExecutionStopped
from nexus.node.windows import WindowsJob, job_stopped
from nexus.plugins.catalog import RELEASES, definition
from nexus.plugins.contracts import PluginNodeResult, PluginRelease
from nexus.plugins.schema import checked, bounded


class HostProcess:
    def __init__(self,argv,cwd,job_name,*,pty=False,environment=None,buffer_limit=32768):
        self.identifier=uuid4().hex
        self.job_name=job_name
        self.job=WindowsJob(job_name) if os.name=='nt' else None
        self.process=None
        self.buffer=bytearray()
        self.buffer_limit=buffer_limit
        self.offset=0
        self.base=0
        self.truncated=False
        self.lock=threading.Lock()
        self.started=time.monotonic()
        self.closed=False
        self.pty=pty
        try:
            launcher=([sys.executable,'--plugin-process'] if getattr(sys,'frozen',False) else [sys.executable,'-B','-m','nexus.plugins.process_worker'])
            environment={**{key:value for key,value in os.environ.items() if not key.upper().startswith(('NEXUS_','DEEPSEEK_','OPENAI_','ANTHROPIC_'))},**(environment or {})}
            self.process=subprocess.Popen(launcher,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                env=environment,shell=False,close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),start_new_session=os.name!='nt',bufsize=0)
            if self.job: self.job.assign(self.process)
            self.reader=threading.Thread(target=self._read,daemon=True)
            self.reader.start()
            self._send(canonical({'argv':argv,'cwd':str(cwd),'pty':pty,'job_name':job_name})+'\n')
        except BaseException:
            self.stop()
            raise

    def _read(self):
        try:
            while data:=self.process.stdout.read(1024):
                with self.lock:
                    self.buffer.extend(data)
                    if len(self.buffer)>self.buffer_limit:
                        removed=len(self.buffer)-self.buffer_limit
                        del self.buffer[:removed]
                        self.base+=removed
                        self.truncated=True
        except (OSError,ValueError): pass

    def _send(self,text):
        if not self.process or self.process.poll() is not None: raise ValueError('process is not running')
        encoded=text.encode('utf-8')
        if len(encoded)>65536: raise ValueError('process input limit exceeded')
        self.process.stdin.write(encoded); self.process.stdin.flush()

    def send(self,text):
        self._send(canonical({'op':'write','text':text})+'\n' if self.pty else text)

    def interrupt(self):
        if not self.pty: raise ValueError('interrupt requires an interactive terminal')
        self._send(canonical({'op':'interrupt'})+'\n')

    def take_bytes(self,limit=8192,consume=True):
        code=self.process.poll()
        if code is not None: self.reader.join(timeout=.1)
        with self.lock:
            start=max(0,self.offset-self.base)
            data=bytes(self.buffer[start:start+limit])
            if consume: self.offset=self.base+start+len(data)
            return {'bytes':data,'truncated':self.truncated or start+len(data)<len(self.buffer),'dropped':self.truncated,
                    'exit_code':code,'status':'running' if code is None else 'succeeded' if code==0 else 'failed',
                    'execution_stopped':code is not None and (not self.job or job_stopped(self.job_name))}

    def output(self,limit=8192,consume=True):
        value=self.take_bytes(limit,consume)
        value['output']=value.pop('bytes').decode('utf-8','replace')
        return value

    def stop(self):
        if self.closed: return
        self.closed=True
        if self.job: self.job.close()
        if self.process is not None:
            if self.process.poll() is None:
                if os.name=='nt': self.process.kill()
                else:
                    import signal
                    try: os.killpg(self.process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
            self.process.wait(timeout=3)
            if hasattr(self,'reader'): self.reader.join(timeout=2)
            self.process.stdin.close(); self.process.stdout.close()
            if self.job and not job_stopped(self.job_name): raise OSError('plugin process stop unconfirmed')


class PluginRuntime:
    def __init__(self,config):
        self.config=config
        self.policy=config.plugins
        self.workspace_root=config.roots.get('workspace')
        self.lock=threading.RLock()
        self.processes={}
        self.closed=False
        self.invocation=threading.local()
        self.uploader=None
        self.emulator=None
        self.game_mode_error=''
        self.db_path=config.data_dir/'plugins.sqlite3'
        with self.db() as conn:
            conn.executescript('CREATE TABLE IF NOT EXISTS processes(id TEXT PRIMARY KEY,context_id TEXT NOT NULL,kind TEXT NOT NULL,job_name TEXT NOT NULL,state TEXT NOT NULL,result TEXT NOT NULL);'
                               'CREATE TABLE IF NOT EXISTS directories(context_id TEXT PRIMARY KEY,path TEXT NOT NULL);'
                               'CREATE TABLE IF NOT EXISTS calls(invocation_id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT);'
                               'CREATE TABLE IF NOT EXISTS subcalls(call_id TEXT PRIMARY KEY,context_id TEXT NOT NULL,plugin_id TEXT NOT NULL,tool TEXT NOT NULL,input_json TEXT NOT NULL,result_json TEXT);')
            for row in conn.execute("SELECT id,job_name FROM processes WHERE state='running'").fetchall():
                stopped=job_stopped(row[1]) if os.name=='nt' else False
                conn.execute("UPDATE processes SET state='unknown',result=? WHERE id=?",(canonical({'status':'unknown','execution_stopped':stopped,'error':'node_restarted'}),row[0]))
        self.reaper=threading.Thread(target=self._reap,daemon=True)
        self.reaper.start()

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.db_path,timeout=3)
        connection.execute('PRAGMA synchronous=FULL')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @property
    def root(self):
        return getattr(self.invocation, "access_root", None) or self.workspace_root

    def cwd(self,context):
        with self.db() as conn:
            row=conn.execute('SELECT path FROM directories WHERE context_id=?',(context,)).fetchone()
        path=Path(row[0]) if row else self.root
        from nexus.node.config import local_directory
        return local_directory(path)

    def _reap(self):
        next_game_check = 0.0
        while not self.closed:
            try:
                if time.monotonic() >= next_game_check:
                    next_game_check = time.monotonic() + 1
                    try:
                        self._apply_game_mode()
                        self.game_mode_error=''
                    except (OSError, ValueError, ExecutionStopped) as error:
                        self.game_mode_error=str(error)
                with self.lock:
                    for identifier,(context,kind,process) in list(self.processes.items()):
                        if process.process.poll() is not None or time.monotonic()-process.started>self.policy.max_job_seconds:
                            result=process.output(8192,consume=False)
                            expired=process.process.poll() is None
                            process.stop()
                            if expired: result.update(status='timed_out',execution_stopped=True)
                            with self.db() as conn:
                                conn.execute('UPDATE processes SET state=?,result=? WHERE id=?',(result['status'],canonical(result),identifier))
                            del self.processes[identifier]
            except (OSError,sqlite3.Error):
                self.closed=True
                for _,_,process in self.processes.values(): process.stop()
                return
            time.sleep(.1)

    def _apply_game_mode(self):
        from nexus.plugins.game_mode import blocked
        if self.emulator is None:
            return
        # Only the dedicated NEXUS phone is affected. Keep its owned processes
        # until normal shutdown confirms completion; never terminate other apps.
        if blocked(self.config.data_dir):
            with self.emulator.lock:
                if self.emulator.managed:
                    deadline = time.monotonic() + 10
                    if not self.emulator.stopping:
                        self.emulator.stop(deadline, threading.Event())
                    else:
                        self.emulator.status(deadline, threading.Event())

    def close(self):
        self.closed=True
        if self.emulator is not None: self.emulator.close()
        with self.lock:
            for _,_,process in self.processes.values(): process.stop()
            self.processes.clear()
        self.reaper.join(timeout=3)

    def _shell(self,name):
        path=getattr(self.policy,name,None)
        if path is None or not path.is_absolute() or not path.is_file(): raise ValueError('shell is not configured: '+name)
        return str(path)

    def _argv(self,name,command=None):
        executable=self._shell(name)
        if name=='pwsh' and command is not None:
            command='[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; '+command
        if name=='pwsh': return [executable,'-NoLogo','-NoProfile',*(['-NonInteractive','-Command',command] if command is not None else ['-NoExit'])]
        return [executable,'--noprofile','--norc',*(['-c',command] if command is not None else [])]

    def start(self,context,kind,argv,cwd):
        if len(self.processes)>=self.policy.max_jobs: raise ValueError('host job capacity exceeded')
        with self.db() as conn:
            if conn.execute('SELECT count(*) FROM processes').fetchone()[0]>=10000: raise ValueError('host job journal is full')
        process=HostProcess(argv,cwd,'Local\\NEXUS-Plugin-'+uuid4().hex,pty=kind=='terminal' or kind.startswith('persistent:'))
        try:
            with self.db() as conn:
                conn.execute('INSERT INTO processes VALUES(?,?,?,?,?,?)',(process.identifier,context,kind,process.job_name,'running','{}'))
            self.processes[process.identifier]=(context,kind,process)
        except BaseException:
            process.stop(); raise
        return process

    def _owned(self,context,identifier):
        with self.db() as conn:
            row=conn.execute('SELECT kind,state,result FROM processes WHERE id=? AND context_id=?',(identifier,context)).fetchone()
        if row is None: raise PermissionError('job is not owned by this conversation')
        active=self.processes.get(identifier)
        return row,active[2] if active else None

    def execute(self,args,deadline,cancel,job_name):
        if not self.policy.enabled or self.closed: raise PermissionError('plugin runtime disabled')
        release=RELEASES.get(args.plugin_id)
        if release is None:
            release=args.release
            if not self.policy.allow_host_python or release is None or release.manifest.plugin_id!=args.plugin_id:
                raise PermissionError('host Python plugin is not configured')
        if release.digest()!=args.release_digest: raise PermissionError('plugin release digest mismatch')
        contract=next((item for item in release.manifest.tools if item.name==args.tool),None)
        if contract is None: raise PermissionError('unknown plugin tool')
        checked(contract.input_schema,args.input)
        identity=hashlib.sha256(canonical(args.model_dump(mode='json')).encode()).hexdigest()
        with self.lock, self.db() as conn:
            old=conn.execute('SELECT digest,result FROM calls WHERE invocation_id=?',(str(args.invocation_id),)).fetchone()
            if old:
                if old[0]!=identity: raise PermissionError('plugin invocation identity conflict')
                if old[1] is None: raise OSError('previous plugin invocation outcome unknown')
                return PluginNodeResult.model_validate_json(old[1])
            if conn.execute('SELECT count(*) FROM calls').fetchone()[0]>=10000: raise ValueError('plugin call journal full')
            conn.execute('INSERT INTO calls VALUES(?,?,NULL)',(str(args.invocation_id),identity))
        from nexus.node.config import local_directory
        self.invocation.access_root = local_directory(Path(args.access_root)) if args.access_root is not None else None
        self.invocation.capabilities=args.capability_digests
        self.invocation.max_subcalls=args.max_subcalls
        self.invocation.identifier=str(args.invocation_id)
        self.invocation.mcp_binding=args.mcp_binding
        try:
            data=self.call(args.plugin_id,args.tool,args.input,str(args.context_id),deadline,cancel,release)
        finally:
            self.invocation.access_root=None
            self.invocation.capabilities={}
            self.invocation.identifier=None
            self.invocation.mcp_binding=None
        checked(contract.output_schema,data)
        result=PluginNodeResult(release_digest=args.release_digest,tool=args.tool,data=data)
        with self.db() as conn: conn.execute('UPDATE calls SET result=? WHERE invocation_id=?',(result.model_dump_json(),str(args.invocation_id)))
        return result

    def call(self,plugin_id,name,value,context,deadline,cancel,release=None):
        if cancel.is_set(): raise ExecutionStopped('cancelled')
        if time.monotonic()>=deadline: raise ExecutionStopped('timeout')
        if plugin_id=='builtin.game_mode':
            from nexus.plugins.game_mode import execute
            return {**execute(self.config.data_dir,name,value,deadline,cancel), 'emulator_error':self.game_mode_error}
        if plugin_id=='builtin.android_emulator':
            from nexus.plugins.android_emulator import AndroidEmulator
            from nexus.plugins.config import MuMuConfig
            from nexus.plugins.mumu import MuMu
            from nexus.plugins.game_mode import blocked
            if name not in {'status','stop'} and blocked(self.config.data_dir):
                raise ValueError('游戏模式已开启，虚拟手机已暂停；请先关闭游戏模式后再操作。')
            with self.lock:
                if self.emulator is None:
                    self.emulator=(MuMu if isinstance(self.config.plugins.android_emulator,MuMuConfig) else AndroidEmulator)(self.config)
            return self.emulator.execute(name,value,context,deadline,cancel)
        if plugin_id=='builtin.shell':
            return self.shell(name,value,context,deadline,cancel)
        if plugin_id in {'builtin.terminal','builtin.jobs'}:
            return self.control(name,value,context,deadline,cancel)
        if plugin_id=='builtin.workspace':
            from nexus.plugins.workspace import execute
            return execute(self,name,value,context,deadline,cancel)
        if plugin_id=='builtin.files':
            from nexus.plugins.files import execute
            return execute(self,name,value,context,deadline,cancel)
        if plugin_id=='builtin.browser':
            from nexus.plugins.browser import execute
            return execute(self,name,value,context,deadline,cancel)
        if plugin_id=='builtin.mcp':
            from nexus.plugins.protocols import mcp
            return mcp(self,value,name,context,deadline,cancel)
        if plugin_id=='builtin.present': return self.present(value,deadline,cancel)
        if plugin_id=='builtin.workflow':
            from nexus.plugins.workflow import execute
            return execute(self,name,value,context,deadline,cancel)
        if plugin_id.startswith('user.'):
            from nexus.plugins.user import execute
            return execute(self,release,name,value,context,deadline,cancel)
        raise ValueError('node plugin has no executor')

    def shell(self,name,value,context,deadline,cancel):
        if value.get('persistent'):
            with self.lock:
                existing=next((process for ctx,kind,process in self.processes.values() if ctx==context and kind=='persistent:'+name),None)
                process=existing or self.start(context,'persistent:'+name,self._argv(name),self.cwd(context))
                process.send(value['command']+'\n')
            time.sleep(min(.1,max(0,deadline-time.monotonic())))
            return {'job_id':process.identifier,**process.output()}
        cwd=self.cwd(context)
        if value.get('workdir'):
            from nexus.node.config import local_directory
            cwd=local_directory((cwd/value['workdir']).absolute())
        with self.lock: process=self.start(context,'job',self._argv(name,value['command']),cwd)
        if value.get('run_in_background'): return {'job_id':process.identifier,'status':'running','execution_stopped':False,'cwd':str(cwd)}
        try:
            wait_until=min(deadline,time.monotonic()+value.get('timeout_ms',8000)/1000)
            while process.process.poll() is None:
                if cancel.is_set(): raise ExecutionStopped('cancelled')
                if time.monotonic()>=wait_until: raise ExecutionStopped('timeout')
                time.sleep(.01)
        finally:
            with self.lock:
                process.stop()
                self.processes.pop(process.identifier,None)
                with self.db() as conn:
                    result=process.output(8192,consume=False)
                    conn.execute('UPDATE processes SET state=?,result=? WHERE id=?',(result['status'],canonical(result),process.identifier))
        # Capture foreground evidence after tree cleanup and output draining;
        # an earlier snapshot can report exit=0 while the Job is still active.
        return {'job_id':process.identifier,'cwd':str(cwd),**result}

    def json_process(self,argv,payload,context,deadline,cancel):
        with self.lock: process=self.start(context,'worker',argv,self.cwd(context))
        diagnostic={}
        try:
            process.send(canonical(payload)+'\n')
            process.process.stdin.close()
            while process.process.poll() is None:
                if cancel.is_set(): raise ExecutionStopped('cancelled')
                if time.monotonic()>=deadline: raise ExecutionStopped('timeout')
                time.sleep(.01)
            output=process.output(32768,consume=False)
            if output['exit_code'] or output['truncated']: raise ValueError('plugin worker failed or exceeded output budget')
            data=json.loads(output['output'])
            if not isinstance(data,dict): raise ValueError('plugin worker must return an object')
            if data.get('worker_error'): raise ValueError(data['worker_error'])
            return bounded(data,24576)
        except (OSError,ValueError) as exc:
            diagnostic={'status':'unknown','error':str(exc)[:500]}
            raise
        finally:
            with self.lock:
                process.stop();self.processes.pop(process.identifier,None)
                with self.db() as conn:
                    conn.execute("UPDATE processes SET state='completed',result=? WHERE id=?",(canonical(diagnostic),process.identifier))

    def control(self,name,value,context,deadline,cancel):
        with self.lock:
            if name=='terminal_open':
                cwd=self.cwd(context)
                if value.get('workdir'):
                    from nexus.node.config import local_directory
                    cwd=local_directory(Path(os.path.abspath(cwd/value['workdir'])))
                process=self.start(context,'terminal',self._argv(value.get('shell','pwsh')),cwd)
                return {'session_id':process.identifier,'status':'running','cwd':str(cwd)}
            if name in {'terminal_list','job_list'}:
                with self.db() as conn:
                    rows=conn.execute('SELECT id,kind,state,result FROM processes WHERE context_id=? ORDER BY rowid DESC LIMIT ?',(context,value.get('limit',20))).fetchall()
                return {'jobs':[{'id':row[0],'kind':row[1],'status':row[2]} for row in rows if name!='terminal_list' or row[1] in {'terminal','persistent:pwsh','persistent:bash'}]}
            identifier=value.get('session_id',value.get('job_id'))
            row,process=self._owned(context,identifier)
            if name=='terminal_signal' and value.get('signal','interrupt')=='interrupt':
                if process is None: raise ValueError('terminal is no longer running')
                process.interrupt()
                return {'id':identifier,'status':'running','signal':'interrupt','execution_stopped':False}
            if name in {'job_kill','terminal_signal','terminal_close'}:
                if process:
                    result=process.output(8192,consume=False); process.stop(); self.processes.pop(identifier,None)
                    result.update(status='cancelled',execution_stopped=True)
                    with self.db() as conn: conn.execute('UPDATE processes SET state=?,result=? WHERE id=?',('cancelled',canonical(result),identifier))
                else: result=json.loads(row[2])
                return {'id':identifier,**result}
            if name=='terminal_send':
                if process is None: raise ValueError('terminal is no longer running')
                process.send(value['text'])
            if process is None: return {'id':identifier,**json.loads(row[2])}
            wait=min(value.get('wait_ms',0)/1000,max(0,deadline-time.monotonic()))
        if wait: cancel.wait(wait)
        if cancel.is_set(): raise ExecutionStopped('cancelled')
        return {'id':identifier,**process.output(value.get('max_bytes',8192))}

    def present(self,value,deadline,cancel):
        from nexus.node.tools.paths import guarded_path,read_handle
        output=[]; total=0
        for index,item in enumerate(value['files']):
            if cancel.is_set(): raise ExecutionStopped('cancelled')
            if time.monotonic()>=deadline: raise ExecutionStopped('timeout')
            from nexus.contracts.basic import relative_path
            relative_path(item['path'])
            with guarded_path(self.root,item['path']) as path,read_handle(path) as stream:
                data=stream.read(4*1024*1024+1)
            total+=len(data)
            if len(data)>4*1024*1024 or total>8*1024*1024: raise ValueError('present transfer limit is 4 MiB per file / 8 MiB per call')
            if self.uploader:
                if not getattr(self.invocation,'identifier',None): raise ValueError('present upload requires a top-level invocation')
                output.append(self.uploader(self.invocation.identifier,index,data))
                continue
            if total>16384: raise ValueError('authenticated artifact transfer is not configured')
            output.append({'name':Path(item['path']).name,'description':item.get('description',''),
                           'sha256':hashlib.sha256(data).hexdigest(),'content_base64':base64.b64encode(data).decode(),'size_bytes':len(data)})
        return {'files':output,'verification':'file_bytes_read'}
