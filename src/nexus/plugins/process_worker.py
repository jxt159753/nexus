"""Trusted stdin gate: no host command runs before its parent assigns a Job."""

import json
import os
import subprocess
import sys
import threading


def terminal(request, environment):
    from winpty import PtyProcess
    from nexus.node.windows import require_current_job
    require_current_job(request['job_name'])
    process=PtyProcess.spawn(request['argv'],cwd=request['cwd'],env=environment,dimensions=(30,120))
    # ConPTY descendants must inherit the carrier's lifecycle boundary.
    import ctypes
    from ctypes import wintypes
    from nexus.node.windows import kernel
    api=kernel()
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    api.OpenProcess.restype=wintypes.HANDLE
    handle=api.OpenProcess(0x1000,False,process.pid)
    job=api.OpenJobObjectW(4,False,request['job_name'])
    try:
        member=wintypes.BOOL()
        if not handle or not job or not api.IsProcessInJob(handle,job,ctypes.byref(member)) or not member.value:
            process.terminate(force=True)
            raise OSError('terminal did not inherit its owned job')
    finally:
        if handle: api.CloseHandle(handle)
        if job: api.CloseHandle(job)
    def controls():
        try:
            for line in sys.stdin.buffer:
                command=json.loads(line)
                if command['op']=='interrupt': process.sendintr()
                elif command['op']=='write': process.write(command['text'])
        except (OSError,ValueError,EOFError): pass
    threading.Thread(target=controls,daemon=True).start()
    try:
        while True:
            data=process.read(1024)
            if data:
                sys.stdout.buffer.write(data.encode('utf-8'));sys.stdout.buffer.flush()
            elif not process.isalive(): break
    except EOFError: pass
    os._exit((process.exitstatus or 0)&255)


def main():
    raw=sys.stdin.buffer.readline(32769)
    if not raw or len(raw)>32768: return 2
    request=json.loads(raw)
    argv=request['argv']
    if not isinstance(argv,list) or not argv or not all(isinstance(v,str) and '\x00' not in v for v in argv): return 2
    environment={key:value for key,value in os.environ.items() if not key.upper().startswith(('NEXUS_','DEEPSEEK_','OPENAI_','ANTHROPIC_'))}
    environment.update(PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',TERM='dumb',NO_COLOR='1',GIT_TERMINAL_PROMPT='0')
    if request.get('pty'): return terminal(request,environment)
    process=subprocess.Popen(argv,cwd=request['cwd'],env=environment,stdin=subprocess.PIPE,
                             stdout=sys.stdout.buffer,stderr=sys.stdout.buffer,shell=False,close_fds=True,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    def relay():
        try:
            while data:=sys.stdin.buffer.read1(4096):
                process.stdin.write(data); process.stdin.flush()
        except (OSError,ValueError): pass
        finally:
            try: process.stdin.close()
            except (OSError,ValueError): pass
    threading.Thread(target=relay,daemon=True).start()
    code=process.wait()
    # The relay can still be blocked in a buffered stdin read. This carrier has
    # no buffered output of its own; avoid interpreter finalization waiting on
    # that daemon's stdin lock after the owned child has exited.
    os._exit(code & 255)


if __name__=='__main__': sys.exit(main())
