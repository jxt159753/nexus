"""Explicit owner utterances, never model output or retrieved conversation text.

This small control grammar deliberately consumes the whole sentence. Unsupported
or ambiguous changes are explained without making a partial permission change.
"""
import re
from dataclasses import dataclass, field

from nexus.core.cognitive.node_routing import NodeName

LABELS = {"status": "设备状态", "receive_files": "接收文件", "voice": "语音", "notifications": "通知摘要"}
ALIASES = {"设备状态": "status", "查看设备状态": "status", "查询设备状态": "status",
           "接收文件": "receive_files", "文件接收": "receive_files", "语音": "voice", "语音对话": "voice",
           "通知摘要": "notifications", "读取通知": "notifications", "通知读取": "notifications"}
CURRENT = {"此设备", "这台设备", "当前设备", "本机", "这台平板", "这个平板", "这台手机"}
HELP = '可以说：“将此设备注册为节点，命名为客厅平板，允许设备状态、接收文件和语音”；或“将客厅平板改名为书房平板”“关闭书房平板的接收文件权限”“将书房平板的备注设为放在书桌上”。未明确指定权限的新节点仅开启设备状态。'


@dataclass
class DeviceCommand:
    target: str = "此设备"
    register: bool = False
    name: str | None = None
    description: str | None = None
    permissions: dict[str, bool] = field(default_factory=dict)
    error: str = ""


def name(value: str) -> str:
    return NodeName(name=value.strip(' \t“”「」\"')).name


def permissions(text: str, enabled: bool) -> dict[str, bool]:
    text = re.sub(r"(?:的)?权限$", "", text.strip())
    parts = re.split(r"、|和|与|及|,|，", text)
    if not parts or any(p.strip() not in ALIASES for p in parts):
        raise ValueError("请明确指定设备状态、接收文件、语音或通知摘要；不会自动授予全部权限。")
    return {ALIASES[p.strip()]: enabled for p in parts}


def parse(text: str) -> DeviceCommand | None:
    text = text.strip().rstrip("。！!").strip()
    # A question, quotation, instruction to explain, or negative request is not
    # a control command. Anchoring also excludes embedded/quoted instructions.
    if re.search(r"[？?\n]|不要|别把|如何|怎么|能否|可否|能不能|可以吗", text):
        return None
    text = re.sub(r"^(?:请帮我|请|帮我)", "", text)
    command = None
    registration = re.fullmatch(r"(?:将|把)?(此设备|这台设备|当前设备|本机|这台平板|这个平板|这台手机)(?:注册|登记)(?:为|成|成为)(?:一个)?(?:节点|设备节点)(.*)", text)
    rename = re.fullmatch(r"(?:将|把)(.+?)(?:的名称)?(?:改名为|重命名为|改为)(.+)", text)
    description = re.fullmatch(r"(?:将|把)(.+?)的(?:备注|描述)(?:设为|改为|设置为)(.*)", text)
    permission = re.fullmatch(r"(开启|启用|允许|关闭|禁用|撤销)(.+?)的(.+)", text)
    try:
        if registration:
            command = DeviceCommand(register=True, target=registration[1])
            tail = registration[2].strip()
            if tail and not re.match(r"^[，,；;]", tail):
                raise ValueError("注册指令后请用逗号分开名称和权限。")
            for clause in re.split(r"[，,；;]", tail.lstrip("，,；;")) if tail else []:
                clause = clause.strip()
                named = re.fullmatch(r"(?:命名为|名称为|叫作|叫)(.+)", clause)
                described = re.fullmatch(r"(?:备注为|描述为)(.*)", clause)
                permitted = re.fullmatch(r"(?:并)?(允许|开启|启用|关闭|禁用)(.+)", clause)
                if named and command.name is None:
                    command.name = name(named[1])
                elif described and command.description is None:
                    command.description = described[1].strip()
                elif permitted:
                    patch = permissions(permitted[2], permitted[1] in {"允许", "开启", "启用"})
                    if command.permissions.keys() & patch.keys():
                        raise ValueError("同一权限出现多次，请保留一次明确设置。")
                    command.permissions.update(patch)
                else:
                    raise ValueError("未能完整理解节点配置，本次没有修改。" )
        elif description:
            command = DeviceCommand(target=name(description[1]), description=description[2].strip(' “\"”'))
        elif rename:
            command = DeviceCommand(target=rename[1].strip())
            command.name = name(rename[2])
        elif permission:
            if not permission[3].endswith("权限") and not any(alias in permission[3] for alias in ALIASES):
                return None
            command = DeviceCommand(target=name(permission[2]))
            command.permissions = permissions(permission[3], permission[1] in {"开启", "启用", "允许"})
        if command and command.description is not None and (len(command.description) > 200 or any(ord(c) < 32 for c in command.description)):
            raise ValueError("备注最多 200 字，且不能包含控制字符。")
    except ValueError as exc:
        if command is None:
            return None
        command.error = str(exc) if not hasattr(exc, 'errors') else "设备名称需要 1–40 字，不能包含控制字符。"
    return command
