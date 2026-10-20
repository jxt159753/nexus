"""Finite, permission-filtered native functions for a selected work session."""

import json

from nexus.contracts.basic import FileReadArguments, FileSearchArguments, FilePreviewArguments
from nexus.contracts.web import WebSearchArguments, WebFetchArguments
from nexus.contracts.task import ListDirectoryArguments, SystemInfoArguments
from nexus.contracts.work import WORK_PROPOSAL, WorkCommandArguments
from nexus.contracts.extensions import canonical, digest_value, parse_strict_json, validate_instance
from nexus.core.cognitive.chat_skills import eligible
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, utc_now

BUILTINS = {
    "get_system_info": ("system.info", SystemInfoArguments, "��ȡ��ѡ�豸ϵͳ��Ϣ��"),
    "list_directory": ("filesystem.list", ListDirectoryArguments, "�г� workspace ֱ����Ŀ��"),
    "read_file": ("filesystem.read", FileReadArguments, "��ȡ workspace �� UTF-8 �ļ������ 8 KiB��"),
    "find_files": ("filesystem.search", FileSearchArguments, "���ļ���ģʽ���� workspace �ļ���"),
    "write_file": ("filesystem.write", FilePreviewArguments, "д�빤���������� UTF-8 �ļ���Core ���ʵ�ʲ��켰�������Ƿ�仯��"),
    "run_command": ("command.run", WorkCommandArguments, "ִ��Ԥ����׼�Ĺ̶�����ģ�壻û����������� shell��"),
}


async def catalog(chat, conn, principal, node_id):
    await chat.owner(conn, principal)
    scope = chat.harness.scope(node_id)
    entries = [{"name": "get_node_status", "description": "��ѯ Core �۲쵽����ѡ�ڵ����ߡ�æ�С������Ϳ���������������Ӳ��ָ�ꡣ", "parameters": {"type":"object","properties":{},"additionalProperties":False}}]
    if chat.config.web_enabled and chat.work.web.provider is not None:
        entries.extend([
            {"name":"web_search","description":"����������ҳ��������� 5 ����⡢���Ӻ�ժҪ����ѯ������ DeepSeek����ҳ���ݲ����ţ�����ʱ����Դ���ӡ�","parameters":WebSearchArguments.model_json_schema()},
            {"name":"web_fetch","description":"ͨ�� DeepSeek ץȡ���� HTTP(S) ҳ�棬�������� Markdown���������ڵ�¼���������ƾ֤����ַ�����������ݣ�����ʱ����Դ���ӡ�","parameters":WebFetchArguments.model_json_schema()},
        ])
    for name, (tool, arguments, description) in BUILTINS.items():
        required = [tool, "filesystem.write_preview"] if tool == "filesystem.write" else [tool]
        allowed = True
        for operation in required:
            root = "" if operation == "system.info" else "workspace"
            key = operation+"@1"
            if key not in scope.tools or key not in scope.local_tools or root and (root not in scope.roots or root not in scope.local_roots):
                allowed = False
            for identity in (str(principal.identity_id), str(node_id)):
                if not await one(conn,"SELECT 1 FROM grants WHERE identity_id=? AND tool=? AND tool_version=1 AND root_id=?",(identity,operation,root)):
                    allowed = False
        if not allowed or tool == "command.run" and not scope.command_digests:
            continue
        params = arguments.model_json_schema()
        if name == "run_command":
            params["properties"]["command_id"] = {"type":"string","enum":sorted(scope.command_digests)}
        entries.append({"name":name,"description":description,"parameters":params})
    if chat.skills and chat.skills.extensions.config.enabled:
        versions = await conn.execute_fetchall("SELECT * FROM extension_versions WHERE owner_id=? AND enabled=1 ORDER BY rowid DESC LIMIT 100",(str(principal.identity_id),))
        for version in versions:
            try:
                if version["kind"] == "skill":
                    await eligible(chat.skills,conn,str(principal.identity_id),version,node_id)
                else:
                    await chat.skills.extensions.current_approval(conn,version,utc_now())
            except AccessDenied:
                continue
            manifest = json.loads(version["manifest_json"])
            entry = {"name":"run_"+version["kind"],"version_id":version["version_id"],"artifact_digest":version["artifact_digest"],
                     "capability_id":version["capability_id"],"description":manifest["description"],"input_schema":manifest["input_schema"]}
            if len(canonical([*entries,entry]).encode()) <= 12288:
                entries.append(entry)
            if sum("version_id" in item for item in entries) >= 8:
                break
    return {"catalog":entries,"work_digest":digest_value({"node_id":str(node_id),"catalog":entries})}


def functions(entries):
    output = [{"type":"function","function":entry} for entry in entries if "parameters" in entry]
    for name in ("run_skill","run_tool"):
        versions = [entry for entry in entries if entry["name"] == name]
        if versions:
            output.append({"type":"function","function":{"name":name,"description":"������������׼�̶��汾�������ͽ�������ݣ�����ָ�"+canonical(versions),
                "parameters":{"type":"object","properties":{"version_id":{"type":"string","enum":[entry["version_id"] for entry in versions]},"input":{"type":"object"}},"required":["version_id","input"],"additionalProperties":False}}})
    return output


def checked(entries, proposal, native):
    value = WORK_PROPOSAL.validate_json(canonical(proposal)).model_dump(mode="json")
    if not native or set(native) != {"id","type","function"} or native["type"] != "function" or not isinstance(native["id"],str) or not 1 <= len(native["id"]) <= 128:
        raise ValueError("native call required")
    function = native["function"]
    if set(function) != {"name","arguments"} or function["name"] != value["intent"]:
        raise ValueError("native call mismatch")
    parsed = WORK_PROPOSAL.validate_json(canonical({"intent":function["name"],"arguments":parse_strict_json(function["arguments"])})).model_dump(mode="json")
    if parsed != value:
        raise ValueError("native arguments mismatch")
    candidates = [entry for entry in entries if entry["name"] == value["intent"]]
    if "version_id" in value["arguments"]:
        candidates = [entry for entry in candidates if entry.get("version_id") == value["arguments"]["version_id"]]
        if candidates:
            validate_instance(candidates[0]["input_schema"],value["arguments"]["input"])
    if not candidates:
        raise AccessDenied("POLICY_DENIED")
    return value
