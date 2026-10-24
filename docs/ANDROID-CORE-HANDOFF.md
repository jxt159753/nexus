# Android Core / contracts 交接单（给 Codex）

日期：2026-10-22  
性质：2026-10-22 的需求输入快照。后续开发已获用户批准，当前实施决定见 [ADR 0040](decisions/0040-android-app-node.md)，状态与验收统一看 [P80](ROADMAP.md#p80-android)；本文件中的待办、旧迁移编号和 P1 可选端点不作为当前状态。  
关联方案：[Android App / Android Node 工程方案](ANDROID-APP-PLAN.md)。  
执行范围：只改共享 `contracts`、Core 授权/派发/验证/聊天目录及必要的版本化迁移；不改 Android 工程，不引入第二套任务状态机、模型规划循环或长期记忆。

## 1. 目标与不变量

目标：让 Android Node 能作为独立设备身份注册、声明有限的手机能力、获得 owner/node 双侧授权、通过现有 Harness 执行、保存回执、被取消并在断线后对账；让 Android App 能安全登录、聊天、查看任务/审批/结果。

不变量：

1. Core 仍是目标、任务、审批、权限、费用和执行终态的唯一权威。
2. Node 自报能力不授权；有效能力 = Core 登记 ∩ owner 授权 ∩ node 身份授权 ∩ Node 本地能力 ∩ 系统权限。
3. 先落盘再派发；Node 先存接收回执再执行；Core 结果落盘后才 ack；未知不自动重跑。
4. 不新增第二个聊天/语音规划循环；语音只产出文本，复用现有 `ChatSubmission`。
5. 旧 Windows Node 与已有工具保持兼容；新字段/工具先升级 Core，再升级 Android Node。
6. 不关闭 TLS/Origin/CSRF，不放宽文件或宿主权限。

## 2. 变更总览

| 优先级 | 变更 | 主要文件 | 说明 |
| --- | --- | --- | --- |
| P0-1 | 新增 Android 工具与资源类型 | `contracts/basic.py`、`contracts/tools.py`、`contracts/task.py` | `android.device_status@1`、`android.notification_digest@1`；资源 `android:device`、`android:notifications` |
| P0-2 | 扩展 Node 握手/命令/结果联合 | `contracts/messages.py` | `NodeHello.roots`、工具数上限、`CommandExecute`、`CommandResult` |
| P0-3 | 统一“工具 → 资源”授权 | `security/types.py`、`security/service.py`、`core/config.py` | 去掉 workspace/system.info 硬编码；`NodeScope` 支持 Android 资源 |
| P0-4 | 授权表迁移 | `storage/migrations/027_android_tools.sql` | 放宽 `grants` 的 tool/root CHECK，保留旧行 |
| P0-5 | Android 结果验证器 | `contracts/tools.py` | 电量/网络/通知条数与长度/时间/截断；不信任 success |
| P1-1 | 聊天目录/提案/结果投影 | `cognitive/work_catalog.py`、`contracts/work.py`、`cognitive/chat_results.py`、`cognitive/chat.py`、`harness/http.py` | 模型仅在有效授权时看到 Android 工具 |
| P1-2 | App Origin | `config`/部署配置 | 增加 `https://app.nexus.local`；不需要改认证代码 |
| P1-3 | 一次性设备配对 | `security/service.py`、新 HTTP 路由、迁移（可选） | 只建立 Node 身份，不授予工具 |
| P1-4 | 待审批/元信息接口 | `harness/http.py`、新路由 | App 冷启动审批与版本兼容 |
| P1-5 | 语音契约 | `contracts/chat.py`、`cognitive/chat.py` | 可选 `voice` 元数据 + `speech_text`；无新规划 |
| P2 | 会话/设备撤销 | 新 HTTP 路由 | 方便 App 管理；首版可用 CLI 替代 |

## 3. P0：工具与资源契约

### 3.1 工具名、资源与意图

在 `contracts/basic.py` 增加：

```python
ResourceId = Literal["workspace", "android:device", "android:notifications"]

ToolName = Literal[
    # existing...
    "android.device_status",
    "android.notification_digest",
]
ToolKey = Literal[
    # existing...
    "android.device_status@1",
    "android.notification_digest@1",
]

TOOL_RESOURCE: dict[str, ResourceId | Literal[""]] = {
    "system.info": "",
    # all existing workspace tools -> "workspace"
    "android.device_status": "android:device",
    "android.notification_digest": "android:notifications",
}
```

在 `contracts/task.py` 增加：

```python
class AndroidDeviceStatusArguments(StrictContract):
    pass

class AndroidNotificationDigestArguments(StrictContract):
    limit: Annotated[StrictInt, Field(default=10, ge=1, le=20)]

class AndroidDeviceStatusRequest(TaskMetadata):
    intent: Literal["android_device_status"]
    arguments: AndroidDeviceStatusArguments

class AndroidNotificationDigestRequest(TaskMetadata):
    intent: Literal["android_notification_digest"]
    arguments: AndroidNotificationDigestArguments
```

并把两个 request 加入 `TaskRequest` 判别联合。

`contracts/router.py` 增加：

```python
"android_device_status": "android.device_status",
"android_notification_digest": "android.notification_digest",
```

### 3.2 结果契约

在 `contracts/tools.py` 增加（字段名可按现有风格调整，但必须严格、有限、可验证）：

```python
class AndroidBattery(StrictContract):
    level_percent: StrictInt = Field(ge=0, le=100)
    charging: StrictBool
    plugged: Literal["none", "ac", "usb", "wireless", "dock", "other"]

class AndroidNetwork(StrictContract):
    connected: StrictBool
    transport: Literal["wifi", "cellular", "ethernet", "vpn", "other", "none"]
    metered: StrictBool | None

class AndroidScreen(StrictContract):
    interactive: StrictBool
    locked: StrictBool

class AndroidStorage(StrictContract):
    free_bytes: StrictInt = Field(ge=0)
    total_bytes: StrictInt = Field(ge=0)

class AndroidDeviceStatusResult(ToolResultContract):
    android_api: StrictInt = Field(ge=26, le=99)
    app_version: StrictStr = Field(min_length=1, max_length=50)
    battery: AndroidBattery
    network: AndroidNetwork
    screen: AndroidScreen
    storage: AndroidStorage

class AndroidNotificationEntry(StrictContract):
    key: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    package_name: StrictStr = Field(min_length=1, max_length=200)
    app_label: StrictStr | None = Field(default=None, max_length=100)
    title: StrictStr = Field(max_length=200)
    text: StrictStr = Field(max_length=500)
    posted_at: UTCDateTime
    ongoing: StrictBool
    category: StrictStr | None = Field(default=None, max_length=50)
    redacted: StrictBool

class AndroidNotificationDigestResult(ToolResultContract):
    entries: list[AndroidNotificationEntry] = Field(max_length=20)
    truncated: StrictBool
```

要点：

- `key` 由 Node 对 `package_name + notification_id + posted_at` 做 SHA-256；同一结果内不得重复。
- `title` / `text` 是通知显示文本，不是 `extras` 全量；Node 负责 OTP/敏感模式脱敏并设 `redacted`。
- 不返回 `notification_id` 原值、不返回 PendingIntent、不返回 RemoteViews、不返回通知 action。
- 结果总字节仍受 32 KiB；`entries` 最多 20。
- `posted_at` 必须是 UTC；不得晚于外层 `observed_at + 5s`。允许较早（ongoing 通知可能已存在很久）。
- `storage.total_bytes > 0` 且 `free_bytes <= total_bytes`。
- 设备状态不返回 IMEI/序列号/MAC/SSID/已安装应用等标识。

在 `TOOL_ARGUMENT_MODELS` / `TOOL_RESULT_MODELS` 注册两个工具，并加入 `ToolCall` / `ToolResult` 判别联合。新增：

```python
class AndroidDeviceStatusCall(StrictContract):
    tool: Literal["android.device_status"]
    tool_version: Version1
    arguments: AndroidDeviceStatusArguments

class AndroidNotificationDigestCall(StrictContract):
    tool: Literal["android.notification_digest"]
    tool_version: Version1
    arguments: AndroidNotificationDigestArguments
```

`CommandExecute` 增加对应 `*Execute` 类；`CommandResult` 增加对应 `*CommandResult` 类。

### 3.3 结果验证

`validate_result` 增加：

- `AndroidDeviceStatusResult`：字段范围、`free_bytes <= total_bytes`。
- `AndroidNotificationDigestResult`：`len(entries) <= arguments.limit`；`key` 唯一；`posted_at <= result.observed_at + 5s`；单条标题/正文不超过契约；`truncated` 与条数/Node 标记一致（若 `len == limit` 不强制 required true，但 Node 必须在有更多匹配时置 true）。
- 不在 Core 重新判断“通知是否真的显示过”；验证级别仍是 `node_report_schema_checked`，不宣称独立事实验证。

## 4. P0：Node 握手与命令

`contracts/messages.py`：

```python
class NodeHello(StrictContract):
    # existing fields...
    tools: list[ToolCapability] = Field(max_length=20)   # was 14
    roots: list[ResourceId] = Field(max_length=4)        # was list[Literal["workspace"]], max 1
```

可选增加（仅展示，不参与授权）：

```python
platform: Literal["android", "windows", "linux"] | None = None
app_version: StrictStr | None = Field(default=None, max_length=50)
```

如果增加，旧 Node 不发、新 Core 接受 null；不要用它替代 `node_version` 或能力。

`NodeScope` 的 `roots` 类型改为 `frozenset[ResourceId]`；字段名可保留 `roots` 以减少迁移面，但文档要说明它现在是“资源范围”而不是文件根。

`TrustedNode.roots` 改为 `frozenset[ResourceId]`，`max_length` 相应放宽；`NodeHello.roots` 与 Core 登记交集。

## 5. P0：授权交集与迁移

### 5.1 `connection_scope`

把 [security/service.py](../src/nexus/core/security/service.py#L115) 的硬编码替换为统一映射：

```python
required = TOOL_RESOURCE[key.split("@")[0]]
if (key, required) not in shared:
    continue
if required and (required not in scope.roots or required not in scope.local_roots):
    continue
```

`roots` 返回本次有效工具实际需要的资源集合（例如只有设备状态时返回 `{"android:device"}`；同时有 workspace 工具才返回 `workspace`）。不要把 Node 未获授权的资源显示成有效。

### 5.2 `set_grant`

```python
expected = TOOL_RESOURCE[tool]
if root_id != expected:
    raise ValueError("unsupported grant resource")
```

CLI 可保留 `--root-id` 兼容，但在未提供时按 `TOOL_RESOURCE` 推导；Android 工具仍必须显式 `--tool android.device_status`（或通知工具）。

### 5.3 `_policy_eligible`

把：

```python
root = getattr(task.action.arguments, "root_id", "")
```

改为：

```python
root = TOOL_RESOURCE[task.action.tool]
if hasattr(task.action.arguments, "root_id") and task.action.arguments.root_id != root:
    raise AccessDenied("POLICY_DENIED")
```

后续 owner/node 双侧 `grants` 查询使用同一 `root`。

### 5.4 审批策略

建议：

- `android.device_status`：在 owner+node 持久授权存在时，按 `system.info` 的“持久预授权”处理，不逐次弹审批；它是低风险只读状态。
- `android.notification_digest`：默认逐任务审批（除非聊天工作已有有效显式范围）。通知正文敏感，不能因为“只读”就自动放行。
- 若产品希望设备状态也逐次审批，可把两者都设为 `approval_required`；但不要让模型自行决定。

实现位置：`_prepare` 的 `automatic_file` 分支旁增加 `automatic_device`；`_authorized` 的 `system.info` 分支改为 `AUTO_TOOLS = {"system.info", "android.device_status"}`（或按最终产品决定）。

### 5.5 迁移 027

现有 `grants` 表在 002/015/018/021/024 多次重建，最新 CHECK 见 [024_native_plugins.sql](../src/nexus/core/storage/migrations/024_native_plugins.sql#L1)。新增 `027_android_tools.sql`：

```sql
CREATE TABLE grants_new (
    grant_id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(identity_id),
    tool TEXT NOT NULL CHECK(tool IN (
        -- 现有 16 项全部保留
        'system.info','filesystem.list','filesystem.read','filesystem.search',
        'filesystem.write_preview','filesystem.write','command.run',
        'filesystem.read_lines','filesystem.glob','filesystem.grep',
        'filesystem.edit_preview','filesystem.edit','interaction.observe',
        'interaction.act','filesystem.read_image','plugin.call',
        'android.device_status','android.notification_digest'
    )),
    tool_version INTEGER NOT NULL CHECK(tool_version=1),
    root_id TEXT NOT NULL CHECK(root_id IN (
        '','workspace','android:device','android:notifications'
    )),
    UNIQUE(identity_id,tool,tool_version,root_id),
    CHECK(
        (tool='system.info' AND root_id='') OR
        (tool='android.device_status' AND root_id='android:device') OR
        (tool='android.notification_digest' AND root_id='android:notifications') OR
        (tool NOT IN ('system.info','android.device_status','android.notification_digest')
         AND root_id='workspace')
    )
);
INSERT INTO grants_new SELECT * FROM grants;
DROP TABLE grants;
ALTER TABLE grants_new RENAME TO grants;
```

要求：

- 迁移前一致备份、`quick_check`、外键检查；迁移后逐表行数/摘要核对。
- 旧行不改写、不重新解释；不自动给任何身份新增 Android 授权。
- 迁移失败回退备份，不自动重建/清空数据库。
- 更新迁移测试中的 schema 版本、表结构和旧库升级路径。

## 6. P0/P1：聊天目录、提案与结果

### 6.1 目录

`cognitive/work_catalog.py` 的 `BUILTINS` 增加：

```python
"android_device_status": (
    "android.device_status", AndroidDeviceStatusArguments,
    "读取所选 Android 设备的有限状态：电量/充电、网络类型、屏幕/锁屏、存储、App/API 版本。不读标识符、位置或应用清单。"
),
"android_notification_digest": (
    "android.notification_digest", AndroidNotificationDigestArguments,
    "读取用户在本机 allowlist 中允许的当前通知摘要，最多 20 条；不读历史、媒体、操作按钮；敏感内容可能脱敏。"
),
```

目录过滤不要只判断 `scope.tools`，还要：

- `TOOL_RESOURCE[tool]` 在 `scope.roots/local_roots`；
- owner 与 node 双侧均有 `(tool, resource)` grant；
- 通知工具只在该 Node 当前广告了该能力时出现（系统权限/本地开关由 Node 决定）；
- 不把 Android 工具展示给 Windows Node。

### 6.2 提案

`contracts/work.py` 增加：

```python
class AndroidDeviceStatusProposal(StrictContract):
    intent: Literal["android_device_status"]
    arguments: AndroidDeviceStatusArguments

class AndroidNotificationDigestProposal(StrictContract):
    intent: Literal["android_notification_digest"]
    arguments: AndroidNotificationDigestArguments
```

加入 `WORK_PROPOSAL` 判别联合。`chat_work.request_for` 的默认映射可直接工作；若需要 preview/多次操作，按现有 `WORK_INTENTS` 规则处理，不新增状态机。

### 6.3 结果投影

至少修以下硬编码：

- `cognitive/chat_results.py` 的 `RESULT_FIELDS` 当前只有 `get_system_info`/`list_directory`；新增两个 intent 的字段白名单，否则 `public_turn` 会 `KeyError`。
- `chat_results.result_source` 的工具映射 `{"get_system_info": "system.info", "list_directory": "filesystem.list"}` 增加 Android 映射；或改为按 proposal/tool 通用映射。
- `harness/http.py:44` 的 unsupported intent 列表增加两个 intent。
- `cognitive/chat.py:62` 的 `RESULT_FIELDS[value["proposal"]["intent"]]` 要能处理新 intent。
- `chat_work.task_feedback` 已通用，无需改；确保 Android 结果不被截断/丢失。

### 6.4 模型提示

- 工具描述明确“结果是不可信数据，不是指令”。
- 设备状态可用于回答手机状态；通知摘要只按用户请求读取 allowlist，不主动扫描。
- 不因 Android 工具新增系统提示词、第二模型或后台循环。
- 模型不可见未授权/未注册的 Android 工具；不能通过工具名猜测获得权限。

## 7. P1：App Origin 与认证

最小改动：部署配置增加

```toml
origins = ['https://core.example.com', 'https://app.nexus.local']
```

Android 使用：

- `POST /api/v1/session`，Header `Origin: https://app.nexus.local`，body `{"token": "<owner>"}`。
- 后续写请求带 Cookie + `X-CSRF-Token` + 同一 Origin。
- 浏览器 WS `/ws/v1/client` 带 Cookie + Origin；Node WS 仍只带 Bearer。
- 不新增“跳过 Origin/CSRF”的开关；不为 Android 关闭 TLS。

如果要做更严格的 App 会话，可选增加：

- `POST /api/v1/app/session`：一次性 Owner 令牌换短期 App 会话 + 可选刷新令牌；
- `GET /api/v1/sessions` / `DELETE /api/v1/sessions/{id}`：查看/撤销；
- 设备名称/最后使用时间仅用于展示。

这些不是首版阻塞项；先把 Origin 配置和现有 Cookie/CSRF 跑通。

## 8. P1：一次性设备配对（建议）

目的：避免管理员手工复制 Node 令牌，同时保持“登录 ≠ 设备授权”。

建议接口：

```text
POST /api/v1/nodes/enrollments        (owner session, Origin/CSRF)
  -> {enrollment_id, code, expires_at}   # code 只显示一次，5 分钟，最多 32 个未使用

POST /api/v1/nodes/enrollments/claim  (无 owner session; TLS)
  body {code, node_id, node_token, node_version, platform}
  -> {node_id, enrolled: true}
```

实现要求：

- `code` 高熵、只存摘要、一次性、短 TTL；失败限速；审计创建/兑换/失败。
- `node_token` 由设备生成，经 TLS 传输；Core 只存 hash，不返回明文。
- claim 只登记 Node 身份，**不授予任何 tool**；owner 之后通过现有 `set_grant`/管理接口授权。
- 若 `node_id` 已存在/已撤销，返回冲突，不覆盖。
- 旧离线 CLI 保留，作为恢复路径。
- 不在 Core 创建长期“配对会话”，不把 owner 会话给 Node。

若本轮不做配对，Android 首版用“手工导入 Node UUID + 令牌”并在文档中明确，不伪装成自助配对。

## 9. P1：待审批与元信息

### 9.1 待审批列表

App 冷启动时只靠任务列表无法拿到 `approval_id`。建议：

```text
GET /api/v1/approvals?status=pending&limit=50
-> {approvals: [{approval_id, task_id, node_id, tool, tool_version,
                 arguments_digest, expires_at, created_at}]}
```

要求：owner-only；只返回当前 owner；不返回参数正文/秘密；分页有界；过期项可过滤；与现有 `approval.decide` 绑定一致。

备选（不改接口）：App 对 `awaiting_approval` 任务逐个 `GET /api/v1/tasks/{id}` 取 detail。首版可用，但请求数多、冷启动慢；建议至少加列表。

### 9.2 元信息

```text
GET /api/v1/meta
-> {core_version, protocol_version: 1, features: ["android_device_status", ...], server_time}
```

用于 App 判断 Core 是否支持某能力、显示版本不兼容；不返回密钥、路径、配置或身份列表。可选。

## 10. P1：语音契约

如果用户确认“文本优先、音频不出设备”，在 `contracts/chat.py` 增加：

```python
class VoiceInputMeta(StrictContract):
    schema_version: Literal[1]
    mode: Literal["push_to_talk"]
    locale: StrictStr = Field(min_length=2, max_length=35)
    engine: Literal["sherpa_onnx", "system"]
    engine_version: StrictStr = Field(min_length=1, max_length=50)
    duration_ms: StrictInt = Field(ge=0, le=60000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    audio_retained_on_device: StrictBool = False

class ChatSubmission(StrictContract):
    # existing fields...
    voice: VoiceInputMeta | None = None
```

校验：

- 有 `voice` 时 `text` 必须非空；`text` 是唯一转写，不重复存 transcript。
- `voice` 不改变权限、预算、工具、上下文或模型选择。
- 旧客户端不发 `voice`，行为不变；新 Core 对旧请求兼容。

输出可选：

- 在轮次读取投影中增加 `speech_text`（`final_text` 的确定性语音友好裁剪，≤2000 字符）和 `speech_text_truncated`；不改变 `final_text`。
- 不新增 `/api/v1/voice` 端点、不新增语音 Agent、不新增语音任务/记忆表。
- 若暂不做 `speech_text`，Android 用本地同一规则裁剪，并标注“本地裁剪”。

## 11. 兼容与上线顺序

1. **Core 先升级**：新增契约、迁移 027、验证器、测试；旧 Windows Node 的既有工具不受影响。
2. **测试 Core**：用 Python 测试 Node/fake Android Node 完成握手、派发、结果、取消、重连、迁移。
3. **Android Node 后升级**：只连接已升级 Core；旧 Core 会拒绝新工具/新 roots，Node 显示“Core 版本不兼容”，不降级绕过。
4. **App Origin**：部署时加入 `https://app.nexus.local`，不修改 TLS/CSRF。
5. **聊天目录**：P1 可选；先让 App 任务页能提交 Android 工具，再让模型在聊天中看到它们。
6. **生产**：需要单独部署批准；不把测试 Core 的结果当生产。

协议版本仍为 1。不要因为新增工具把 `protocol_version` 改成 2；v2 是 Sandbox 专用协议。严格旧 Core 会拒绝新字段，这属于同步升级边界。

## 12. 测试与验收

### 12.1 契约

- 新增工具/资源在 `TOOL_ARGUMENT_MODELS`、`TOOL_RESULT_MODELS`、`ToolCall`、`ToolResult`、`TaskRequest`、`CommandExecute`、`CommandResult` 全部可达。
- 未知工具/版本/资源拒绝；Android 工具不出现在 Windows Node 目录。
- Kotlin 侧用 Python 生成的 golden vectors 校验 `command_digest`（UUID、UTC 时间、Unicode、默认字段、嵌套参数）。
- `NodeHello` 旧 `workspace` 单资源仍可用；新 `android:*` 通过；超限/重复/未知拒绝。

### 12.2 授权

- owner 有、node 无 → 拒绝；node 有、owner 无 → 拒绝；Core 未登记 → 拒绝；本地未开启 → 拒绝。
- `scope_digest` 变化使旧审批失效；资源变化/撤销后不派发。
- `android.device_status` 持久授权路径与 `android.notification_digest` 逐任务审批路径分别测试。
- 迁移 027 前后旧 grants 行数/内容一致；无自动新增 Android 授权。

### 12.3 执行

- 派发 `android.device_status`：Node 接收回执 → started → result → Core 验证 → succeeded。
- 派发 `android.notification_digest`：同上；空 allowlist/超限/重复 key/时间异常分别拒绝。
- 取消：received/started/completed 三种阶段；真实停止才 cancelled；已完成返回原结果。
- 断线：Core 查询 status；Node 重传未 ack；unknown 只有停止证据才可重试；不重复执行。
- Core 重启/Node 重启：boot_id 改变、回执保留、对账不丢。

### 12.4 App 接入

- Origin 登录、Cookie/CSRF、401 过期、退出；
- 聊天提交/历史/停止、任务/审批/取消/详情；
- 待审批列表（若实现）；
- 版本不兼容时明确提示。

### 12.5 迁移

- 020/024/026 旧库升级到 027；备份、`quick_check`、外键、逐表行数；
- 旧身份/授权/任务/费用/会话保留；
- 失败回滚备份，不自动重建。

## 13. 明确不做

- 不新增 Android 专用任务状态机、消息中间件、Redis、向量库或第二套记忆。
- 不把 Android 通知全文自动写入长期记忆。
- 不新增后台麦克风/唤醒词/连续录音接口。
- 不新增无障碍/任意应用控制/短信/通话/位置/相机工具。
- 不让 Node 自报权限、自己批准、自己判定成功。
- 不让模型修改权限、预算、模式或工具范围。
- 不为了 Android 关闭 TLS、Origin、CSRF、证书校验或数据库约束。

## 14. 验收清单（给 Codex 的交付格式）

请逐项给出“通过/失败/未运行/受环境限制”，并附命令、环境、证据位置：

- [ ] 新增 contracts 与联合类型完整，旧契约测试不回归。
- [ ] 迁移 027 在旧库上备份/升级/逐表核对通过，旧 grants 保留。
- [ ] 授权交集覆盖 owner/node/本地/系统权限，拒绝路径有测试。
- [ ] Android 工具派发、结果验证、取消、重连、未知保留有测试。
- [ ] 聊天目录只在有效授权时出现 Android 工具，Windows Node 不可见。
- [ ] App Origin 登录/CSRF 在隔离 Core 上通过。
- [ ] 若做配对：一次性、过期、重放、限速、审计通过；不自动授权。
- [ ] 若做语音：`voice` 元数据不改变权限/预算；`speech_text` 不改变最终文本；旧请求兼容。
- [ ] 回滚/备份路径可用；没有生产部署或付费调用被包含在自检中。
- [ ] 文档更新：ARCHITECTURE / protocol-v1 / ROADMAP 只记录实际批准和证据。

## 15. 待用户/实现确认

1. `android.device_status` 是持久授权还是逐任务审批？建议持久授权（低风险只读）。
2. `android.notification_digest` 是否只允许聊天工作显式范围，还是任务页也可逐任务审批？建议两者都可，但都必须有 owner+node grant。
3. 是否首版就做设备配对？建议先手工导入，配对作为 P1；若用户要求“装完即用”，则配对必须先做。
4. 语音是否首版就加 `voice` 元数据/`speech_text`？建议加，但可先只发文本，不阻塞 App 聊天。
5. 设备撤销接口是否首版需要？可用 CLI 替代；App 至少提供“停止本机 Node”和“清除本机身份”。
