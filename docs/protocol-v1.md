# JARVIS Link v1 已实现数据契约

本文件记录 P02 的具体字段和解析约定。架构权威入口仍为 [ARCHITECTURE.md](ARCHITECTURE.md)，授权与任务状态见 [ROADMAP.md](ROADMAP.md)，实际验证见 [P02-VERIFICATION.md](P02-VERIFICATION.md)。这些是共享数据契约，不实现身份认证、授权、WebSocket、持久化或工具执行。

## 信封和入口

所有消息包含以下字段；信封、payload 和嵌套对象都拒绝未知字段。

| 字段 | 约束 |
| --- | --- |
| `protocol_version` | 严格整数 1；布尔、浮点、字符串及其他版本均拒绝 |
| `type` | 下表列出的 25 种类型之一 |
| `message_id` | UUID 字符串 |
| `correlation_id` | 关联消息的 UUID 字符串或 null；字段必填 |
| `sent_at` | UTC RFC 3339 字符串，使用 T 分隔及 Z / +00:00，支持最多 6 位小数秒；不接受数字时间戳 |
| `payload` | 对应消息类型的严格模型 |

`nexus.contracts.protocol.parse_message(str_or_bytes)` 是接收 JSON 的统一入口，返回判别联合 `Message`。它在解码前检查完整 UTF-8 帧不超过 65536 字节，包含 JSON 空白和转义文本。`encode_message(message)` 重新验证再输出紧凑 UTF-8 JSON，拒绝构造后被错误修改的对象。单独使用模型的 `model_validate_json()` 只适合结构验证，不能替代统一入口的原始帧限额检查。

工具结果模型按规范化、紧凑、非 ASCII 转义的 UTF-8 JSON 限制为 32768 字节；32 KiB 本身允许，超过一个字节拒绝。目录条目最多 200 个，名称最多 255 个字符，另有结果总字节限制。工具实现仍须按数量与字节截断并设置 `truncated`，不能先构造超限结果再声称已验证。

`ProtocolError.error` 为有限 `ErrorPayload`：未知意图为 `UNSUPPORTED_TASK`，非法或不支持的协议版本为 `PROTOCOL_UNSUPPORTED`，其他 JSON/类型/参数/大小错误为 `INVALID_MESSAGE`；解析错误不可重试。公开错误不回显输入或 Pydantic 内部错误。error 信封由调用方设置消息 ID、关联 ID 和当前会话后发送，解析器本身不发送消息。

所有 ID 均为 UUID；事件游标 `event_id` 为 1–19 位非负十进制字符串，不带多余前导零；revision 为非负严格整数。`command_digest` 为 64 位小写十六进制。到期与观测时间采用相同的 UTC 时间类型。MVP 现有资源契约只接受 `workspace`，它不表示该资源已获配置或授权。

## 消息类型

`session_id` 在 Node welcome 及运行期 payload 中必填；error 在握手失败时可以没有会话，运行期由 Link 检查当前会话。浏览器身份来自会话而非 payload，Node 身份也不能由自报 ID 授权。

| 方向 | type | payload 字段 |
| --- | --- | --- |
| 浏览器 → Core | `client.sync` | event_id 或 null |
| 浏览器 → Core | `task.submit` | request_id、intent、arguments；可选 project_id=null |
| 浏览器 → Core | `task.cancel` | task_id |
| 浏览器 → Core | `approval.decide` | approval_id、decision=approve/reject |
| 浏览器 → Core | `control.pause / control.resume / control.cancel_all` | request_id |
| Core → 浏览器 | `client.synced` | event_id、full_sync_required |
| Core → 浏览器 | `task.accepted` | task_id、status |
| Core → 浏览器 | `task.updated` | event_id、task_id、revision、status、execution_outcome；可选 result/error/verification |
| Core → 浏览器 | `approval.required` | event_id、approval_id、task_id、node_id、action、expires_at |
| Core → 浏览器 | `control.updated` | event_id、paused、revision |
| Node → Core | `node.hello` | node_id、node_version、tools、roots、boot_id |
| Core → Node | `node.welcome` | session_id、heartbeat_interval_seconds、heartbeat_timeout_seconds、limits |
| Node → Core | `node.heartbeat` | session_id、active_attempt_id 或 null |
| Core → Node | `command.execute` | session_id、task_id、attempt_id、tool、tool_version、arguments、command_digest、grant_id、expires_at、execution_timeout_seconds |
| Node → Core | `command.accepted / command.started` | session_id、task_id、attempt_id、command_digest、boot_id |
| Core → Node | `command.status` | session_id、attempt_id |
| Node → Core | `command.status_result` | session_id、attempt_id、phase；按阶段携带 command_digest/result；可选 execution_stopped=false |
| Core → Node | `command.cancel` | session_id、attempt_id、reason |
| Node → Core | `command.cancelled` | session_id、attempt_id、stopped_phase=received/started |
| Node → Core | `command.result` | session_id、task_id、attempt_id、command_digest、tool、tool_version、observed_at、outcome；result 或 error |
| Core → Node | `result.ack` | session_id、attempt_id、status=stored/already_settled |
| 双向 | `error` | code、message、retryable；可选 session_id；correlation_id 位于信封 |

S5 增加可选 `execution_stopped` 布尔字段，缺省 false；unknown 只有确认旧执行已停止才可能安全重试。not_found 不允许携带停止证据。新 Node 与 Core 应同步升级，严格旧 Core 不接受新字段；动作摘要及消息名称不变。实际恢复、取消和限额见 [S5 恢复](s5-recovery.md)。

S6 已实现浏览器端点 `/ws/v1/client?node_id=<配置节点 UUID>`，Cookie/Origin 认证，首条必须 client.sync。node_id 查询仅选择执行目标，不授予权限；单配置节点可省略，多节点省略不能提交任务。可选 project_id 由 Core 检查归属，不进入 Node 动作。升级前无该字段的历史请求通过规范化比较保持幂等。

S7 不改变信封、工具版本、摘要或消息字段。现有 Core 入口直接提供 HTTPS/WSS，Node 必须校验信任链与主机名；远程模式不信任代理头。停机恢复保留 attempt、审批绑定和事件游标，Node 在线状态只能通过新认证连接重建；恢复旧快照后的浏览器应重新登录并完整同步。部署和本机/Android 验收边界见 [S7 部署](s7-deployment.md)与 [S7 验证](S7-VERIFICATION.md)。

浏览器 event_id 来自统一持久 client_events，不能混用原任务详情或 HTTP 控制历史的各自 ID。client.synced 给出回放起点；有效游标回放其后所有可见通知，null/无效游标返回当前高水位并要求完整 HTTP 查询。回放的 task.updated 是该任务最新已提交快照，revision 与结果/状态一致；页面按游标去重后查询详情。队列满或发送超过 2 秒关闭 1013，断线不取消任务。接口、限额和边界见 [S6 说明](s6-browser-records.md)。

## 工具、证据和限额

- `get_system_info` 路由为 `system.info@1`，arguments 必须为空；结果只含 os_name、os_version、architecture、python_version。
- `list_directory` 路由为 `filesystem.list@1`，arguments 为 root_id=workspace 和严格整数 limit=1–200；结果为 root_id、entries、truncated，entry 只含 name 和 kind=file/directory/link/other。
- command 和 approval action 根据 tool 判别参数模型；command.result 根据 tool 判别结果模型。tool_version 也是严格整数 1。
- command.result 的 outcome=succeeded 必须有匹配工具类型的 result 且没有 error；outcome=failed 必须有 error 且没有 result。不存在以 success 布尔值代替证据的入口。
- command.status_result 的 phase 为 not_found/received/started/completed/cancelled/unknown。not_found 不带摘要或结果，其余阶段必须有摘要；只有 completed 必须携带结果。嵌套结果的 session_id、attempt_id 和摘要须与外层一致。旧回执重传时须使用当前会话，保留原 attempt 和动作身份。
- task.updated 的状态沿用架构状态集合。succeeded 必须有 result、execution_outcome=known 和 verification=node_report_schema_checked；result 和 error 不可同时存在。这只验证数据形状，不代替 P07 对真实归属、授权及证据的检查。
- node.hello 的 tools 最多包含两个不重复的固定工具版本，roots 最多包含 workspace；空列表不授予任何能力。node_version 最多 50 字符。
- node.welcome 的心跳间隔为 1–10 秒、超时为 1–30 秒且大于间隔。limits 包含 max_message_bytes≤65536、max_result_bytes≤32768、execution_timeout_seconds≤10、max_concurrent_attempts=1；所有数值为正严格整数，结果字节限额必须小于消息限额。运行期收紧限额仍由 P05/P06 接入并执行。

P02 只定义上述数据和无 I/O 的编解码。消息方向权限、当前认证会话、资源范围、动作摘要的计算与绑定、实际期限、状态转移、重试、持久化和工具验证分别由后续任务落实；模型解析通过不代表获得执行权限或任务已经成功。

P05 已实现 Node 本地执行，并在 `nexus.contracts.commands` 定义共享摘要：命令排除 session_id / command_digest，加入可信 node_id，按键排序后以紧凑 UTF-8 JSON 取 SHA-256；覆盖动作、task/attempt、grant 和期限。会话变化允许重传，动作/期限变化不得复用 attempt。P06 接入认证 Link，P07 接入权威派发与验证，P08 接入 [恢复与控制](s5-recovery.md)。消息名称和摘要算法不变，S5 增加上文停止证据字段。
