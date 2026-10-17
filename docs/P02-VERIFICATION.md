# P02 契约与规则路由验证记录

复核日期：2026-10-17。任务：S2 / P02。最新结论：**修复后技术复核通过，完成待用户验收**。批准范围、任务状态与用户验收统一见 [ROADMAP.md](ROADMAP.md)，设计依据为 [ARCHITECTURE.md](ARCHITECTURE.md) 第 4、5、8、9 节，具体字段见 [protocol-v1.md](protocol-v1.md)。

下文为 P02 修复轮次的历史证据，“未实现/未批准”仅指当时。用户随后明确要求“完成S2”，P03/P04 及整体回归见 [S2 验证记录](S2-VERIFICATION.md)，当前批准与交付状态以 ROADMAP 为准。

## 本轮修复与再次验收

用户在首轮报告后明确要求“解决你提出的问题，并再次验收”。本轮据此修复 P02，不推定整个 S2 或 P03/P04 已获批准，没有启动服务、打开原数据库、安装依赖、提交或推送。

- Windows，64 位 CPython 3.13.9，Pydantic 2.14.1；使用现有 `.venv`。
- 修复 `contracts`：共用严格类型、25 种 v1 消息与 payload、工具判别联合、结果/错误互斥、会话与回执字段、消息编解码和有限错误分类；现有规则路由继续复用，没有新增模型或执行逻辑。
- 修复两项 UUID 干扰的边界测试，改走真实 JSON 入口并检查具体错误位置/类型，补上合法对照；删除一项重复版本测试，将非法版本集中参数化。
- 新增协议回归测试，覆盖消息、参数、证据、会话字段、UTC、大小及编码边界；同步 README、架构当前事实、路线状态及协议字段文档。
- 本轮临时目录：`E:\NEXUS\.p02-recheck-1792213120869`，只用于测试和隔离导入，复核结束清理。
- 未重复独立环境安装；安装证据和 Python 3.12/其他系统未测限制沿用 [P01 记录](P01-VERIFICATION.md)。

## 再次验收的实际验证

从仓库根目录执行：

```powershell
$reviewRoot = Join-Path (Get-Location).Path '.p02-recheck-1792213120869'
New-Item -ItemType Directory -Path $reviewRoot -ErrorAction Stop | Out-Null
& .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=short -W error --basetemp "$reviewRoot\pytest-strict"
& .\.venv\Scripts\python.exe -m pip check
```

复现时应将目录名换成未使用的新名字，避免覆盖已有临时资源。

| 检查 | 实际观察 |
| --- | --- |
| 全套测试 | **199 passed**，含原 P01 配置测试；启用 `-W error`，没有警告或跳过 |
| 依赖兼容 | `No broken requirements found` |
| 契约覆盖 | 独立脚本从架构 8.2 节提取消息类型，对比判别联合 JSON Schema；**25 种完全匹配**。每种均有编解码往返、信封及 payload 多余字段拒绝测试 |
| 首轮版本缺陷 | 独立最小复现确认 true、1.0 现均拒绝；1 合法；其他非法版本、工具版本由严格整数类型拒绝 |
| 首轮测试缺陷 | 两项负向测试改为 JSON 路径；只产生预期的 limit 越界或 extra_forbidden 错误；合法 1/100/200 和无多余字段对照通过 |
| 路由与错误分类 | 两个已知意图得到单步 rule 建议；未知 run_shell 返回有限 `UNSUPPORTED_TASK`；坏 JSON、未知 type、未知字段及超限消息返回 `INVALID_MESSAGE`，非法版本为 `PROTOCOL_UNSUPPORTED` |
| 证据与会话形状 | 错工具/参数/结果、错误与结果同时出现、缺少运行期 session_id、回执内外 attempt/session/摘要不一致、未知执行结果冒充成功等均被拒绝 |
| 原始消息字节边界 | 65536 字节的合法帧（含空白）接受，65537 字节拒绝；字符串与 bytes 均测，字符数未超限而 UTF-8 字节超限的中文消息也拒绝 |
| 结果字节边界 | 规范化结果 JSON 为 32768 字节接受、32769 字节拒绝；首轮 **159056 字节**中文目录结果的独立复现现已拒绝 |
| 隔离导入与网络保护 | 在本轮临时目录通过 `python.exe -I -` 使用已安装包；fresh process 的 Python audit hook 拒绝 socket.connect/getaddrinfo/urllib.Request，已知与未知意图均按预期处理，没有网络请求 |
| 语法与复杂度 | Python AST 解析通过；协议模型复用共用信封、参数、结果及验证原语；没有新增依赖、服务、状态机或模型适配器 |
| 文档与范围 | 7 个 Markdown、44 个本地链接、2 个与对应契约匹配的 JSON 示例、3 个 PowerShell 示例语法、代码围栏及空白检查通过；12 项任务保留，仅 P01/P02 勾选；`git diff --check` 通过 |

本轮编写中一次合法消息探针发现 UTC BeforeValidator 返回字符串后被 strict datetime 拒绝，已改为在限定 RFC 3339 文本后解析为 aware datetime。中间测试为 166 passed、1 个序列化警告；编码器随后改为序列化错误转有限协议错误，补充边界测试后以 `-W error` 完整运行得到上述 199 passed。没有关闭警告来获得通过。

## 首轮问题的处置

| 首轮发现 | 当前处置 |
| --- | --- |
| 只有 task.submit，v1 契约不全 | 已补齐架构列出的 25 种消息，包括 Node、命令、结果、审批、控制、恢复查询和 error；没有提前实现这些消息对应的 I/O |
| Literal[1] 按相等性接受 true/1.0 | 改为 StrictInt + 值限制，JSON 入口和直接模型入口均有回归覆盖 |
| 两项测试可能被 UUID 类型错误误导 | 改走 JSON 并断言目标错误，增加合法边界；不再使用非目标异常作为通过依据 |
| 未知意图仅抛内部 ValidationError | 统一入口将未知意图映射为 `UNSUPPORTED_TASK`，不回显输入 |
| 字符数与条目数不能保证 32/64 KiB | 工具结果模型实施规范化 UTF-8 总字节限制；统一消息入口实施完整原始 UTF-8 帧限额，编码时再次验证 |

首轮历史结果保留：当时 29 项测试及 pip check 通过，但独立检查复现上述缺陷，未判通过。首轮第一次 pytest 为 24 passed、5 errors，原因是临时父目录未创建；准备目录后 29 passed，未修改测试以跳过错误。首轮临时目录 `E:\NEXUS\.p02-acceptance-1792212605782` 已清理。

## 版本问题的当前复现

```powershell
@'
import json
from uuid import uuid4
from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.protocol import parse_message
message = {
    "protocol_version": 1, "type": "task.submit", "message_id": str(uuid4()),
    "correlation_id": None, "sent_at": "2026-10-17T08:00:00Z",
    "payload": {"request_id": str(uuid4()), "intent": "get_system_info", "arguments": {}},
}
for version in (True, 1.0):
    message["protocol_version"] = version
    try:
        parse_message(json.dumps(message))
    except ProtocolError as exc:
        assert exc.error.code == ErrorCode.PROTOCOL_UNSUPPORTED
        print(repr(version), "REJECTED")
    else:
        raise AssertionError("invalid version accepted")
'@ | & .\.venv\Scripts\python.exe -
```

实际结果：`True REJECTED`、`1.0 REJECTED`。

## 未验证边界及阶段结论

- P02 只验证纯数据契约、解析/编码和零模型规则路由。实际身份授权、当前认证会话、摘要计算与动作绑定、期限判定、持久化、状态机及工具真实性验证未实现，不由 schema 通过证明。
- P05 仍须实际按数量/字节截断；P06 必须接入统一 codec，并在 WebSocket 层限制帧大小及按身份/方向/当前会话检查；P07 仍须核对当前有效 attempt 与工具证据。测试中的 session 字段检查不代表真实会话认证已经完成。
- 未启动正式或实验 Core/Node，未做 WS、TLS、进程、Android 或恢复验收；未使用或改动原数据库。
- 零模型证据限于当前已执行路径及规则路由代码；没有配置模型密钥或真实模型请求。Provider 接口及禁用适配器仍在 P10 范围。
- Python 3.12、其他系统及本轮干净安装未重测，未增加依赖；构建工具锁定限制见 P01。

P02 的修复与技术复核已完成，可以勾选交付清单；按项目规范保持“完成待验收”，由用户确认后再记录为“已验收”。P03/P04 及 S2 整体仍需明确批准。
