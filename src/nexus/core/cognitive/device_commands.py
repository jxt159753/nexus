"""Explicit owner utterances, never model output or retrieved conversation text.

This small control grammar deliberately consumes the whole sentence. Unsupported
or ambiguous changes are explained without making a partial permission change.
"""
import re
from dataclasses import dataclass, field

from nexus.core.cognitive.node_routing import NodeName

LABELS = {"vision": "屏幕视觉操作","status": "设备状态", "receive_files": "接收文件", "voice": "语音", "notifications": "通知摘要", "accessibility": "无障碍操作", "files": "手机文件"}
ALIASES = {"屏幕视觉操作": "vision", "视觉操作": "vision", "截图操作": "vision","设备状态": "status", "查看设备状态": "status", "查询设备状态": "status",
           "接收文件": "receive_files", "文件接收": "receive_files", "语音": "voice", "语音对话": "voice",
           "通知摘要": "notifications", "读取通知": "notifications", "通知读取": "notifications", "无障碍操作": "accessibility", "无障碍": "accessibility", "操作手机": "accessibility", "手机文件": "files", "读写手机文件": "files", "文件读写": "files"}
ALIASES.update({"收文件": "receive_files", "接收文件到收件箱": "receive_files", "语音聊天": "voice",
                "通知": "notifications", "读取通知摘要": "notifications", "截图与视觉操作": "vision",
                "截图和视觉操作": "vision", "读写文件": "files", "读写所选目录": "files"})
CURRENT = {"此设备", "这台设备", "当前设备", "这个设备", "本机", "本设备", "这台平板", "这个平板", "这台手机", "这部手机", "我的手机"}
CURRENT_PATTERN = "|".join(sorted(CURRENT, key=len, reverse=True))
ENABLE = {"允许", "开启", "启用", "打开"}
VERBS = "开启|启用|允许|打开|关闭|禁用|撤销|关掉"
HELP = '可以说：“注册这台手机”“这台手机改名叫红魔”“打开语音和接收文件”“关闭书房平板的接收文件权限”。也可直接在设备页开启功能，本机选择会自动同步。未明确指定权限的新节点仅开启设备状态。'


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
    text = re.sub(r"(?:的)?(?:权限|功能)$", "", text.strip())
    if text in ALIASES:
        return {ALIASES[text]: enabled}
    parts = [re.sub(r"(?:的)?(?:权限|功能)$", "", p.strip()) for p in re.split(r"、|和|与|及|,|，", text)]
    if not parts or any(p.strip() not in ALIASES for p in parts):
        raise ValueError("请明确指定设备状态、接收文件、语音、通知摘要、无障碍操作、屏幕视觉操作或手机文件；不会自动授予全部权限。")
    return {ALIASES[p.strip()]: enabled for p in parts}


def parse(text: str, *, permission_reply: bool = False) -> DeviceCommand | None:
    text = text.strip().rstrip("。！!").strip()
    # A question, quotation, instruction to explain, or negative request is not
    # a control command. Anchoring also excludes embedded/quoted instructions.
    if re.search(r"[？?\n]|不要|不需要|暂不|别把|先别|如何|怎么|能否|可否|能不能|可以吗", text):
        return None
    text = re.sub(r"^(?:麻烦帮我|请帮我|帮我|麻烦|请)", "", text).strip()
    command = None
    registration = re.fullmatch(rf"(?:将|把)?({CURRENT_PATTERN})(?:注册|登记|添加|接入)(?:一下)?(?:成为|为|成|到)?(?:一个)?(?:节点|设备节点)(.*)", text)
    if not registration:
        registration = re.fullmatch(rf"(?:注册|登记|添加)(?:一下)?({CURRENT_PATTERN})(?:(?:为|成)(?:设备)?节点)?(.*)", text)
    short_registration = re.fullmatch(r"(?:注册|登记|添加)(?:一下|一个)?(?:设备)?节点(.*)", text)
    rename = re.fullmatch(r"(?:将|把)?(.+?)(?:的?(?:名称|名字))?(?:重命名为|改名为|改名叫|改名成|改为|改成)(.+)", text)
    description = re.fullmatch(r"(?:将|把)(.+?)的(?:备注|描述)(?:设为|改为|设置为)(.*)", text)
    permission = re.fullmatch(rf"({VERBS})(.+?)的(.+)", text)
    local_permission = re.fullmatch(rf"(?:给|为)?({CURRENT_PATTERN})[，,\s]*(?:把)?({VERBS})(.+)", text)
    simple_permission = re.fullmatch(rf"({VERBS})(.+)", text)
    try:
        if registration or short_registration:
            command = DeviceCommand(register=True, target=registration[1] if registration else "此设备")
            tail = (registration[2] if registration else short_registration[1]).strip().lstrip("，,；;")
            for clause in re.split(r"[，,；;]|\s+(?=命名|名称|叫|备注|描述|并|开启|允许|启用|打开|关闭|禁用)", tail) if tail else []:
                clause = clause.strip()
                named = re.fullmatch(r"(?:并)?(?:命名为|名称为|名字叫|叫作|叫)(.+)", clause)
                described = re.fullmatch(r"(?:备注为|描述为)(.*)", clause)
                permitted = re.fullmatch(rf"(?:并)?({VERBS})(.+)", clause)
                if named and command.name is None:
                    command.name = name(named[1])
                elif described and command.description is None:
                    command.description = described[1].strip()
                elif permitted:
                    patch = permissions(permitted[2], permitted[1] in ENABLE)
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
            command.permissions = permissions(permission[3], permission[1] in ENABLE)
        elif local_permission:
            command = DeviceCommand(target=local_permission[1])
            command.permissions = permissions(local_permission[3], local_permission[2] in ENABLE)
        elif simple_permission:
            # Do not consume ordinary actions such as opening an app or a file.
            if not any(alias in simple_permission[2] for alias in ALIASES):
                return None
            command = DeviceCommand()
            command.permissions = permissions(simple_permission[2], simple_permission[1] in ENABLE)
        elif permission_reply:
            # Bare choices only answer the immediately preceding registration question.
            try:
                selected = permissions(re.sub(r"^(?:我想要|我需要|我要|我想用|只要|就要)", "", text), True)
            except ValueError:
                return None
            command = DeviceCommand(permissions=selected)
        if command and command.description is not None and (len(command.description) > 200 or any(ord(c) < 32 for c in command.description)):
            raise ValueError("备注最多 200 字，且不能包含控制字符。")
    except ValueError as exc:
        if command is None:
            return None
        command.error = str(exc) if not hasattr(exc, 'errors') else "设备名称需要 1–40 字，不能包含控制字符。"
    return command
