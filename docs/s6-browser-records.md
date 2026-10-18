# S6 浏览器与记录接口

S6/P09–P10 的实现说明。阶段批准和验收状态以 [ROADMAP](ROADMAP.md) 为准，技术证据见 [S6 验证](S6-VERIFICATION.md)。只支持既有两个只读工具；Core 仍是唯一权威。

## 启动与浏览器操作

1. 按 [S3 Link](s3-link.md)准备独立 Core/Node 数据目录、owner/Node 身份、双方授权及 workspace 资源。
2. 按 README 的两个独立启动命令运行 Core 和 Windows Node。默认显式 loopback 示例是 `http://127.0.0.1:8000`，Origin 必须与配置一致。
3. 浏览器打开 Core 根路径 `/`，输入离线登记的 owner 凭证。页面换取会话后清空输入，不保存长期令牌到 URL、LocalStorage 或 SessionStorage。
4. 在任务页选择配置节点和可选项目，提交系统信息或目录任务。目录审批卡显示工具、参数、节点及到期时间；批准只作用于这次任务。
5. 状态、结构化结果、截断标志、验证级别和执行历史由 Core 返回。执行未知、取消请求与确认取消分别显示。暂停仅停止新派发；取消所有请求实际停止活动执行。
6. 记忆与项目页可创建项目、明确保存记忆、查看当前或历史信息并撤销。路由页显示每个已接受任务的路由原因、固定工具、路由策略版本和最终验证状态。

远程模式仍要求用户显式配置可信 HTTPS/WSS。手机尺寸的浏览器模拟不等于 Android 真机验收；当前部署与实测边界见 [S7 部署](s7-deployment.md)和 [S7 验证](S7-VERIFICATION.md)。

原生 HTML/CSS/JavaScript 随 Python 包安装，由 Core 同源提供；无前端构建步骤、CDN、外部字体、第三方脚本或新增依赖。页面工具输出和用户内容均通过文本节点显示。HTTP 统一 `no-store`、CSP、禁止嵌入与 MIME 嗅探。

## 会话与客户端 WS

- `POST /api/v1/session`：既有凭证登录，返回 CSRF 令牌并设置 HttpOnly / SameSite=Strict 会话 Cookie；可信远程模式额外 Secure。
- `GET /api/v1/session`：以 Cookie 认证，返回当前会话期限、owner ID 和该会话稳定的 CSRF 令牌。CSRF 由高熵会话令牌单向派生，数据库仍只保存摘要；浏览器仅在内存持有 CSRF。无 CORS 放行。
- `DELETE /api/v1/session`：Origin、Cookie、CSRF 检查后撤销会话；旧 WS 也会关闭。S6 前创建的会话不能恢复其随机 CSRF，需要重新登录；持有旧 CSRF 的既有 API 客户端仍可按原接口退出。旧会话继续受原 8 小时期限及 10 会话上限约束。
- `WS /ws/v1/client?node_id=<UUID>`：Cookie + 允许 Origin 认证。查询参数只选择可信配置节点，不携带凭证。单配置节点时允许省略；多节点省略只能同步，不能提交任务。审批、取消按任务的持久归属检查，不受当前选择改变。
- 连接首条消息必须在 5 秒内为 `client.sync`。随后使用 [v1 协议](protocol-v1.md)的 submit、cancel、approval、control 消息，复用 Harness 和 SecurityService。不存在浏览器自报终态或直接控制 Node 的入口。
- 每条接收消息和每次发送均复核会话；空闲接收最长 1 秒复查，事件轮询也会检查。最多 10 个浏览器连接，握手 10 秒 60 次；每连接入站 10 秒 60 条；原始帧仍受配置和 64 KiB 上限限制。Node 的身份通道不能替代 owner Cookie。

提交的 `request_id` 在连接丢失后保持不变，最多保留 10 条内存待确认请求，并绑定最初节点。错误响应关联原消息 ID；未知意图仍返回 UNSUPPORTED_TASK，不进入模型。审批和控制重传仍由既有绑定及幂等规则处理。

## 事件和快照恢复

Core 迁移 005 增加 `client_events`，由任务事件及控制事件的 SQLite 触发器在原事务内追加，统一单调递增浏览器游标。旧事件升级时按记录时间合并补记。它是可恢复通知索引，不替代任务或控制状态机。

`client.synced` 的 `event_id` 是回放起点：有效游标返回原起点、`full_sync_required=false`，之后按序发送所有大于起点的可见事件。null、超出当前库边界或不存在的游标返回当前高水位、`full_sync_required=true`。页面先取得快照，再处理高水位之后排队的事件；不会在快照与后续订阅间留出空窗。

历史任务事件携带该任务**当前已提交快照**及相应 revision，不伪造旧时刻的结果；客户端按 event_id 去重，并通过任务详情重取审批和动作信息。单个事件游标只在页面成功处理后推进；处理失败或超过 64 条页面待处理消息时关闭连接并请求完整同步。服务端每页读 16 条，每 250 ms 检查持久记录，不等待浏览器 ACK 后才允许调度。

每连接最多 32 条发送消息；入队满或单次发送超过 2 秒，关闭为 1013，客户端通过持久游标重连。浏览器的断开不取消任务；慢发送不会持有 Harness 调度锁。服务端数据库故障会停止新派发。

浏览器游标只能来自 `/ws/v1/client`，不要混用任务详情的 task_events ID 或原 `/api/v1/control` 的 control_events ID；这两个历史接口保留原各自命名空间。

`GET /api/v1/client/snapshot?before=<rowid>` 返回最近或更早 20 条任务、`next_before`、`has_more` 及全局控制 revision。单页固定有界；页面最多保留 100 张卡片，继续加载可滚动查看更早历史。重载页面总是完整同步；断线重连使用内存游标。原有 tasks HTTP 分页接口继续可用。

## 显式记忆、项目及路由

所有接口以 owner 会话认证；写入还须 Origin 和 CSRF。普通 JSON 写入最多 4096 字节，记忆写入最多 16384 字节，读入期限 2 秒。

| 接口 | 请求或查询 | 返回及约束 |
| --- | --- | --- |
| `POST /api/v1/projects` | name（1–100 字符）、description（最多 500） | project；最多 1000 个，不接受空白名称 |
| `GET /api/v1/records/projects` | after、limit | projects、next_after |
| `POST /api/v1/memories` | 下方严格对象 | memory；同一 owner/request_id 相同内容返回原条目，内容不同 409 |
| `GET /api/v1/memories` 或 `/api/v1/records/memories` | after、limit、history、project_id | 默认仅当前有效条目；history=true 含过期、撤销、被替代记录 |
| `POST /api/v1/memories/{id}/retract` | 空对象 | 幂等撤销，不删除原内容、同意或历史 |
| `GET /api/v1/records/routes` | after、limit、project_id | routes、next_after，关联当前任务结果与验证 |
| `GET /api/v1/models/usage` | 无 | enabled=false、调用数、费用、未知费用数；模型成功率 null（不适用） |

分页 after 是十进制 rowid 字符串，limit 为 1–100，默认 20；空页保持原游标。任务提交可选 `project_id`，项目必须归当前 owner；不会改变 Node 动作或权限摘要。项目只能显式创建，不扫描磁盘。

记忆请求示例：

```json
{
  "request_id": "331f1be4-dc44-4c77-a3b0-81d4625652bc",
  "consent": true,
  "content": "本周继续核对浏览器恢复行为。",
  "kind": "plan",
  "verification": "unverified",
  "source": "用户明确输入",
  "evidence_ref": null,
  "verified_at": null,
  "valid_until": "2026-10-25T00:00:00Z",
  "project_id": null,
  "supersedes_id": null
}
```

consent 必须是显式布尔 true，数字 1、字符串、false 或缺省均拒绝。内容最多 2000 字符，来源/依据最多 256 字符。kind 为 fact / inference / plan；verification 为 unverified / verified，verified 必须附非空依据和非未来 UTC 验证时间。这是用户填写的验证声明，不表示 Core 独立证明内容真实。

每次成功保存，将完整有限请求、owner、请求 ID、同意时间和独立 `consent_event_id` 与记忆一起提交。UI 失败重试相同内容沿用请求 ID。工具输出、任务历史、路由或聊天均不会自动转成个人记忆；本轮测试只保存隔离库的合成内容。

`supersedes_id` 显式替代旧条目，新的同意与旧条目标记在同一事务内完成；不能替代已经撤销或被替代的条目。valid_until、superseded_at、retracted_at 分别保留；默认查询排除失效信息。最多 10000 条，满后拒绝新增而不清理旧记录。撤销是停止作为当前信息使用，不是物理擦除；删除/保留期政策另行设计。

每个新任务的规则路由记录与任务创建、事件、审计同一事务写入，幂等重提不新增记录。S6 前的任务根据原持久意图补记为 origin=migrated，耗时 null；新路由 origin=live，记录实际耗时。路由 policy_version 是规则表版本，与动作权限策略版本分别管理。未被契约接受的未知请求不创建任务，直接以有限错误返回。

`DisabledProvider` 和 `DisabledDecisionModel` 是应用装配的唯一模型实现；estimate、invoke、decide 全部返回 MODEL_DISABLED，连费用估算也不访问网络。未提供模型启用开关、密钥配置或真实 Provider。model_calls 表没有伪造的零费用调用行，调用和费用统计来自真实表计数；模型成功率不适用。

## 升级

停旧 Core、取得一致备份后更新包；按既有启动流程应用 005。迁移器还会自动生成升级前一致备份并校验，失败回滚，不删除或重建用户数据库。迁移保留身份、授权、任务、尝试、审批、控制和事件，新加可选项目列、同意/记忆、路由、模型调用及客户端事件表。Node 沿用 S5 回执模式和协议；不向 Node 引入记忆、模型或规划逻辑。

页面与 Core 同包升级且 no-store，避免新任务字段发给旧 Core。历史文档是当时交付快照，不作为当前模式中 model_calls 表缺失的断言。
