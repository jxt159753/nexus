"""Create-only receipt storage under the configured workspace, followed by readback."""

import hashlib
import os
import tempfile

from nexus.contracts.file_transfer import FileReceiveResult
from nexus.node.tools.paths import guarded_path, read_handle


def receive_file(command, config):
    args = command.arguments
    root = config.roots[args.root_id]
    parts = args.path.split('/')
    for relative in (parts[0],'/'.join(parts[:2])):
        with guarded_path(root,relative) as directory:
            directory.mkdir(exist_ok=True)
    stage = f'receive-{command.attempt_id}.tmp'
    with guarded_path(config.data_dir,stage) as source, read_handle(source) as incoming:
        with guarded_path(root,args.path) as target:
            descriptor, temporary = tempfile.mkstemp(prefix='.nexus-receive-',dir=target.parent)
            try:
                digest, total = hashlib.sha256(), 0
                with os.fdopen(descriptor,'wb') as outgoing:
                    while chunk := incoming.read(65536):
                        total += len(chunk)
                        if total > args.size_bytes:
                            raise ValueError('File size mismatch')
                        outgoing.write(chunk)
                        digest.update(chunk)
                    if total != args.size_bytes or digest.hexdigest() != args.sha256:
                        raise ValueError('File integrity mismatch')
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                os.link(temporary,target,follow_symlinks=False)
                os.unlink(temporary)
                with read_handle(target) as saved:
                    digest, total = hashlib.sha256(), 0
                    while chunk := saved.read(65536):
                        total += len(chunk)
                        if total > args.size_bytes:
                            raise ValueError('File changed during verification')
                        digest.update(chunk)
                if total != args.size_bytes or digest.hexdigest() != args.sha256:
                    raise ValueError('Saved file verification failed')
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    return FileReceiveResult(root_id=args.root_id,attachment_id=args.attachment_id,path=args.path,sha256=args.sha256,bytes_written=args.size_bytes)
