"""Fixed Windows UI Automation bridge, limited to one pinned running application."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from nexus.contracts.interaction import InteractionObservation, InteractionResult
from nexus.node.tools.paths import guarded_path, read_handle


def app_operation(args, template):
    if os.name!="nt":
        raise PermissionError("Windows UI Automation required")
    if template.digest()!=args.template_digest:
        raise PermissionError("interaction policy changed")
    action_id=getattr(args,"action_id",None)
    action=template.actions.get(action_id) if action_id else None
    if action_id and (action is None or (action.kind=="set_value")!=(args.value is not None)):
        raise PermissionError("unconfigured application action or value")
    payload={"executable":str(template.executable),"executable_sha256":template.executable_sha256,
        "automation_ids":list(dict.fromkeys(item.automation_id for item in template.actions.values())),
        "action":action.model_dump() if action else None,"value":getattr(args,"value",None),
        "expected_state_sha256":getattr(args,"expected_state_sha256",None)}
    # Hold the binary against replacement for the complete observation/action.
    with guarded_path(template.executable.parent,template.executable.name) as executable,read_handle(executable) as stream:
        digest=hashlib.sha256()
        if os.fstat(stream.fileno()).st_size>512*1024*1024:
            raise ValueError("application binary too large")
        while block:=stream.read(1048576):
            digest.update(block)
        if digest.hexdigest()!=template.executable_sha256:
            raise PermissionError("application binary changed")
        system_root=Path(os.environ["SystemRoot"])
        powershell=system_root/"System32/WindowsPowerShell/v1.0/powershell.exe"
        script=Path(__file__).with_name("app_bridge.ps1")
        environment={key:value for key,value in os.environ.items() if key.upper() in {"SYSTEMROOT","WINDIR","TEMP","TMP"}}
        process=subprocess.Popen([str(powershell),"-NoLogo","-NoProfile","-NonInteractive","-File",str(script)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment,
            shell=False,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            output,error=process.communicate(json.dumps(payload,ensure_ascii=False).encode(),timeout=6)
            if process.returncode or len(output)>65536 or len(error)>8192:
                messages=("Exactly one configured running application required","Application binary changed","Unique configured control required","Sensitive or foreign control","Application state changed; observe again","Control unavailable","Unsupported configured action")
                detail=next((message for message in messages if message.encode() in error),"configured application operation failed")
                raise ValueError(detail)
            data=json.loads(output.decode("utf-8-sig"))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        after=data["after"]
        observation=InteractionObservation(kind="windows_app",state_json=after,state_sha256=hashlib.sha256(after.encode()).hexdigest(),
            actions=[{"action_id":key,"kind":item.kind,"description":item.description} for key,item in template.actions.items()])
        return InteractionResult(root_id=args.root_id,target_id=args.target_id,template_digest=template.digest(),observation=observation,
            action_id=action_id,previous_state_sha256=hashlib.sha256(data["before"].encode()).hexdigest() if action_id else None,
            verification="configured_action_returned" if action_id else "configured_target_observed")
