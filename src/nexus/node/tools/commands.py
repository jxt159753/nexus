"""Fixed administrator-authored argv, no shell, no client arguments or secrets."""

import hashlib
import os
import subprocess
import threading
import time

from nexus.contracts.tools import CommandRunResult
from nexus.node.tools.paths import guarded_path, read_handle


def run_command(args, root, templates):
    template = templates.get(args.command_id)
    if template is None or template.digest() != args.template_digest:
        raise PermissionError("unknown command template")
    # Pin the executable as well as its argv. No PATH lookup or inherited credentials.
    with guarded_path(template.executable.parent, template.executable.name) as executable, read_handle(executable) as stream:
        if os.fstat(stream.fileno()).st_size > 128 * 1024 * 1024:
            raise ValueError("executable exceeds hash limit")
        digest = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            digest.update(block)
        if digest.hexdigest() != template.executable_sha256:
            raise PermissionError("executable changed")
        with guarded_path(root, "__nexus_command_guard__"):
            environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT"}}
            environment.update(GIT_TERMINAL_PROMPT="0", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
            process = subprocess.Popen([str(executable), *template.argv], cwd=root, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       shell=False, close_fds=True, bufsize=0, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            buffers = [bytearray(), bytearray()]
            overflow = threading.Event()

            def read(pipe, buffer):
                while block := pipe.read(1024):
                    room = 8192-len(buffer)
                    buffer.extend(block[:room])
                    if len(block)>room:
                        overflow.set()
                        return

            readers = [threading.Thread(target=read,args=(pipe,buf),daemon=True) for pipe,buf in zip((process.stdout,process.stderr),buffers)]
            for reader in readers:
                reader.start()
            deadline = time.monotonic()+8
            try:
                while process.poll() is None:
                    if overflow.is_set() or time.monotonic()>=deadline:
                        raise ValueError("command output or runtime limit exceeded")
                    time.sleep(.01)
                for reader in readers:
                    reader.join(timeout=.2)
                if overflow.is_set() or any(reader.is_alive() for reader in readers):
                    raise ValueError("command output limit or descendant pipe still active")
                return CommandRunResult(root_id=args.root_id,command_id=args.command_id,template_digest=args.template_digest,
                                        exit_code=process.returncode,stdout=buffers[0].decode("utf-8",errors="replace"),stderr=buffers[1].decode("utf-8",errors="replace"),truncated=False)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                # In production the enclosing ManagedWorker Job owns descendants;
                # it closes after this worker returns, including on Node death.
                for pipe in (process.stdout,process.stderr):
                    pipe.close()
                for reader in readers:
                    reader.join(timeout=.2)
