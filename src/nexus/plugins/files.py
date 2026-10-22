"""File plugin adapter uses the original guarded path and atomic write code."""

import hashlib
import json
import re
import sys
from uuid import uuid4

from nexus.contracts.basic import FileLinesArguments,FileGlobArguments,FileGrepArguments,FilePreviewArguments,FileWriteArguments,FileEditPreviewArguments,FileEditArguments
from nexus.contracts.images import ImageReadArguments
from nexus.node.tools.workspace import read_lines,glob_files,grep_files,preview_edit,edit_file
from nexus.node.tools.files import preview_write,write_file
from nexus.node.tools.images import read_image
from nexus.node.tools.paths import guarded_path,read_handle


def operate(root,name,value):
    args={'root_id':'workspace',**value}
    if name=='read': return read_lines(FileLinesArguments(**args),root).model_dump(mode='json')
    if name=='glob': return glob_files(FileGlobArguments(**args),root).model_dump(mode='json')
    if name=='read_image': return read_image(ImageReadArguments(**args),root).model_dump(mode='json')
    if name=='grep':
        regular=args.pop('regex',False)
        if not regular: return grep_files(FileGrepArguments(**args),root).model_dump(mode='json')
        pattern=re.compile(args['pattern'],re.IGNORECASE if args.get('ignore_case') else 0)
        candidates=glob_files(FileGlobArguments(root_id='workspace',path=args.get('path','.'),pattern=args.get('include','*'),limit=100,max_depth=8),root)
        matches=[];skipped=0;scanned=0;limit=args.get('limit',50)
        for relative in candidates.paths:
            try:
                with guarded_path(root,relative) as path,read_handle(path) as stream: raw=stream.read(131073)
                if len(raw)>131072 or b'\0' in raw: raise ValueError('text limit')
                scanned+=len(raw)
                for number,line in enumerate(raw.decode('utf-8').splitlines(),1):
                    if pattern.search(line): matches.append({'path':relative,'number':number,'text':line[:1024],'truncated':len(line)>1024})
                    if len(matches)>=limit: break
            except (OSError,ValueError,UnicodeError): skipped+=1
            if len(matches)>=limit or scanned>=4194304: break
        return {'matches':matches,'skipped':skipped,'bytes_scanned':scanned,'truncated':candidates.truncated or len(matches)>=limit or skipped>0}
    if name=='str_replace_editor':
        command=args['command']
        if command=='view': return read_lines(FileLinesArguments(root_id='workspace',path=args['path'],limit=200),root).model_dump(mode='json')
        if command=='create': return operate(root,'write',{'path':args['path'],'content':args.get('file_text',''),'expected_sha256':None})
        if command=='str_replace': return operate(root,'edit',{'path':args['path'],'old_string':args['old_str'],'new_string':args.get('new_str',''),'expected_sha256':args['expected_sha256']})
        with guarded_path(root,args['path']) as path,read_handle(path) as stream: raw=stream.read(12001)
        if len(raw)>12000: raise ValueError('insert file exceeds write budget')
        if hashlib.sha256(raw).hexdigest()!=args.get('expected_sha256'): raise PermissionError('file changed since read')
        lines=raw.decode('utf-8').splitlines(keepends=True)
        if args['insert_line']>len(lines): raise ValueError('insert line exceeds file')
        lines.insert(args['insert_line'],args.get('new_str',''))
        return operate(root,'write',{'path':args['path'],'content':''.join(lines),'expected_sha256':args['expected_sha256']})
    if name=='write':
        expected=args.pop('expected_sha256')
        preview=preview_write(FilePreviewArguments(**args),root)
        if preview.previous_sha256!=expected: raise PermissionError('file changed since read')
        result=write_file(FileWriteArguments(**args,expected_sha256=expected,preview_task_id=uuid4(),preview_digest=preview.preview_digest),root)
        return {'write':result.model_dump(mode='json'),'preview':preview.model_dump(mode='json'),'verification':'preview_cas_readback'}
    if name=='edit':
        preview=preview_edit(FileEditPreviewArguments(**args),root)
        result=edit_file(FileEditArguments(**args,preview_task_id=uuid4(),preview_digest=preview.preview_digest,
            content_sha256=preview.content_sha256,bytes_written=preview.bytes_written),root)
        return {'edit':result.model_dump(mode='json'),'preview':preview.model_dump(mode='json'),'verification':'preview_cas_readback'}
    raise ValueError('unsupported file operation')


def execute(runtime,name,value,context,deadline,cancel):
    argv=([sys.executable,'--plugin-files'] if getattr(sys,'frozen',False) else [sys.executable,'-B','-m','nexus.plugins.files'])
    return runtime.json_process(argv,{'root':str(runtime.root),'name':name,'input':value},context,deadline,cancel)


def main():
    from pathlib import Path
    try:
        raw=sys.stdin.buffer.readline(65537)
        if not raw or len(raw)>65536: return 2
        value=json.loads(raw)
        result=operate(Path(value['root']),value['name'],value['input'])
    except (OSError,ValueError,KeyError) as exc:
        result={'worker_error':str(exc)[:500]}
    sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    return 0


if __name__=='__main__': raise SystemExit(main())
