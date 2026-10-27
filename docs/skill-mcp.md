# Skill Shell 与受控 MCP

本文件说明后端接口。最初实现查 [P65](ROADMAP.md#p65skill-shell-与受控-mcp)，实际 Windows-MCP 接通和当前发布查 [P83](ROADMAP.md#p83-windows-mcp)；设计见 [ADR 0034](decisions/0034-skill-managed-mcp.md)、[ADR 0042](decisions/0042-conversation-capability-readiness.md)。其他前端需求仍由用户控制 DSH 按 [工作单](PLUGINS-FRONTEND.md#skillmcp-追加接入要求)实施。

## 使用条件

MCP 的冷启动、初始化、目录核对和本次工具调用共享一个执行时限：新版 Core／Node 的 `mcp_execution_timeout_seconds` 默认和最大为60秒，双方取较小值，旧 Node 未声明时保持10秒。授权到期和用户取消仍立即生效，其他工具时限保持10秒。明确的超时失败保留原始原因并禁止修改类自动重试；`USER_CONTROL` 表示键鼠仍由用户使用，不能绕过让权保护。

Core 启用 plugins 与 extensions，双方登记 `plugin.call@1`、workspace 及一致的 plugin_digest。Node 启用 plugins、配置实际 Shell 路径；托管 MCP 另须在本地策略显式设置 `allow_managed_mcp=true`，修改后同步 Core 信任摘要。配置示例见 [Node](../config/plugins.node.example.toml)。模型和 Skill 不能修改这些开关或权限模式。

Core 信任的是完整 `NodeConfig.plugin_digest()`，包括 Node 侧相关资源配置，不是单独 `PluginNodeConfig.digest()`。用节点接口报告的实际摘要核对；`plugin_inspect` 同时报告执行可用状态及阻塞原因。已有注册项、full 模式或 Node 在线均不能代替这些检查。

手机、Windows 和网页均经同一个 Core 选择设备。工作目录中各设备的 `plugin_inspect` 描述附带最多 20 项托管 MCP 登记摘要及截断标记，仅含名称、修订和状态，不外发配置路径／参数。`plugin_inspect('builtin.mcp')` 的 `data.registrations` 返回相同设备的登记列表；列表 `revision` 表示当前 active（若无则最新）版本，`latest_revision` 表示最新登记修订，失败／等待中的更新不掩盖仍 active 的旧版。列表和摘要均不等于实际执行成功，空列表不覆盖设备的静态配置。

用户问“能否调用 Windows-MCP”时，先只读检查电脑端插件及执行状态；若需了解具体工具，再用 `mcp_list(server)` 读取契约。不能因入口在手机、此会话没有安装记录或旧助手说过“不支持”而否定电脑能力；能力询问本身也不授权重新安装、登记或试运行桌面动作。实际发布与验证范围见 [P87](ROADMAP.md#p87-mobile-mcp)。

Shell 以 Node 当前用户权限运行，工作目录不构成文件隔离。原生 Skill 保留现有生成、测试、具体版本批准与启用流程；full 免运行步骤的执行确认，不代替旧 M4 Skill 的版本批准。普通模式每个原生步骤创建具体审批任务，审批绑定固定版本、参数、设备与策略。

## Skill 步骤

manifest 使用 `verification_profile: skill_native_steps_v1`；仍最多三步、五分钟，单步骤参数最多 4 KiB。生成器在插件启用时获得实际固定版本的 Shell/MCP 契约。新步骤形式为：

```json
{
  "id": "install",
  "tool": "plugin.call",
  "tool_version": 1,
  "plugin_id": "builtin.shell",
  "plugin_tool": "pwsh",
  "plugin_digest": "必须替换为当前插件发布的64位SHA256",
  "arguments": {"command": "Write-Output '安装检查'", "timeout_ms": 8000}
}
```

上例为字段说明，摘要占位符必须替换后才能通过严格契约。查询 `/api/v1/plugins` 和版本详情获取当前 digest，不接受 latest 或旧版本批准自动继承。步骤也可使用 `builtin.shell/bash`、`builtin.mcp` 的登记/发现/重载/调用/资源工具；Core 管理查询 `mcp_list` 与删除 `mcp_remove` 不作为 Node Skill 步骤。实际调用参数以发布目录为准。

原有直接 `$input: {field: ...}` 与 `$ref: {step: ..., field: ...}` 可传递已校验输入及此前结果白名单字段；没有表达式、递归或循环。原生步骤结果包装为 `root_id/release_digest/tool/data/verification`，MCP 工具正文在 data，完整 Skill 输出 schema 应相应声明。旧 profile 与旧参数/结果规则保持兼容。

`POST /api/v1/extensions/runs` 在原 `request_id/version_id/node_id/input` 外增加 `conversation_id`。独立入口运行原生 Skill 必须提供归属 owner 的未归档、非计划模式会话；聊天运行自动沿用原会话。运行保存有效模式指纹；切换模式、归档或进入计划模式阻止后续步骤。

测试只检查契约、引用、顺序和输出形状，返回 `native_execution: not_run` 与 `verification: contract_standins_only`，不执行安装命令。正式步骤复用 Task/Node 的真实证据。Shell 必须前台运行，不能设置 persistent/run_in_background；timeout_ms 最多 10000，实际还受当前 Node/Harness 剩余时间限制。长安装需拆成可完成的有界操作，当前 Skill 不会将“后台已启动”视为安装完成。非零退出、失败、未知或 MCP 未激活都会停止后续步骤。

## MCP 配置和版本

配置可为 `{"process":{"executable":"绝对程序路径","argv":["参数"]}}`，或 `{"url":"https://server.example/mcp"}`，两者只能选一。可执行文件在目标 Node 检查，不要求 Core 同路径存在；argv 直接传给程序，注册接口不调用 Shell。HTTP 只允许 HTTPS 或显式 loopback HTTP，不接受 URL 用户名/密码和 fragment，不跟随重定向。没有认证头、OAuth 或通用秘密注入字段，勿将密钥写入 argv、URL 或仓库。

登记范围为 owner + Node + server，名字为小写字母起始、后续字母/数字/下划线/短横线，最多 64 字符。配置与请求字段共最多 4 KiB。expected_revision 首次为 0，之后等于该名字的最新修订号，包括失败版本；冲突返回 409。request_id 是幂等键，重试必须沿用原内容和会话，不能换参数复用。

注册、发现和重载均产生真实发现任务。新版本先 pending，核验成功后 active，前版 inactive；失败变 failed，旧 active 保持。目录最多 64 工具/20 KiB，发现最多八页；不支持的 schema 保留 `supported:false/reason`，禁止调用。MCP 描述与 annotations 是服务器资料，不能改变 NEXUS 权限。

所有写接口要求 owner 登录 Cookie、Origin、CSRF；普通模式的注册/发现/重载任务返回 awaiting_approval，使用原任务审批 API，完全模式可 ready。返回的 `task.status=succeeded` 只代表任务证据已收到；查询登记 state=active 与 catalog_digest 才是可调用条件。Core 会核对权限/取消屏障，拒绝迟到激活。

| 方法与路径 | 输入与返回 |
| --- | --- |
| GET `/api/v1/mcp?node_id={id}` | 最近登记版本、配置、目录、摘要和 pending/active/inactive/failed/removed 状态；最多 256 行 |
| POST `/api/v1/mcp` | request_id、conversation_id、node_id、server、expected_revision、configuration；返回 operation_id、task、approval_required |
| GET `/api/v1/mcp/{server}/tools?node_id={id}` | 当前 active 配置、工具及 catalog_digest；无 active 时拒绝 |
| POST `/api/v1/mcp/{server}/discover` | request_id、conversation_id、node_id；重新发现当前配置，创建新修订 |
| POST `/api/v1/mcp/{server}/reload` | 同上，另须 expected_revision；重新初始化、发现并原子切换目录 |
| DELETE `/api/v1/mcp/{server}` | 同上，另须 expected_revision；停用登记、保留历史并取消关联未决任务 |

模型通过 builtin.mcp 的 `mcp_register/mcp_discover/mcp_reload/mcp_remove` 使用相同 Core 版本与任务边界；`mcp_list` 不执行设备操作，不指定 server 只返回最多 20 个摘要，指定 server 返回已发现完整工具契约。`mcp_call_tool` 输入 server、name、arguments、catalog_digest，Node 实际调用前重新 tools/list；摘要变化先拒绝调用并要求重载。普通模式的 MCP 管理/调用须当前具体动作授权，不复用同一插件此前其他动作的确认；full 仍受原范围约束。

登记不是下载器。NEXUS 可先用获准 Shell 或原生 Skill 安装到选定设备，再登记实际执行程序，发现后通过通用工具入口调用。真实 MCP 工具 `isError:true` 为失败；声明 outputSchema 时核对 structuredContent。注册/重载不会在 Core 导入服务器或生成代码。

指定 server 的模型 `mcp_list` 也会返回 `not_registered/pending/failed`、配置和最新修订，帮助恢复未完成登记；执行入口仍只接受 active。`plugin_inspect('builtin.mcp')` 提供精确登记 schema。跨 Linux Core / Windows Node 的可执行文件路径使用规范化正斜杠参与摘要。FastMCP `x-fastmcp-wrap-result` 布尔注解不影响字段校验，其他不支持的 schema 仍拒绝。

原生步骤收到业务失败时，Skill `error.tool_result` 保存已校验的完整底层结果，`step_id/task_id` 指向来源，步骤标为 failed、后续不执行；聊天在原结果外发授权内反馈这条证据。`NATIVE_STEP_FAILED` 本身不能证明目录/插件摘要不符，任务传输成功也不能证明 MCP 成功。

## Windows-MCP 实际接入

已验证包版本为0.8.8，实际要求 Python>=3.14；使用独立环境及 stdio，不替换系统 Python、不设置开机启动、不打开端口。安装日志以后台 job 的真实退出码和停止证据为准；`builtin.jobs/job_output` 使用 Node job_id，Core 独立 `job_output` 使用 task_id，不能混用。长安装用当前会话的有界后台 Shell，不能装进有10秒上限的原生 Skill 步骤后将 started 当完成。

关闭该 MCP 子进程遥测的启动方式是独立 Python 的 argv：`-c`、`import os;os.environ['ANONYMIZED_TELEMETRY']='false';from windows_mcp.__main__ import main;main()`、`serve --transport stdio --tools ...` 的分项参数。不新增通用环境/秘密注入权限。上游额外提供 `ControlStatus`；`USER_CONTROL` 表示检测到用户操作后暂时让权，保留保护，通过状态和明确结果判断是否可继续，不关闭保护或重复未知操作。

当前检查 Skill 固定调用 Process list（name=nexus-node、limit=2）。上游名称匹配可能带出相似进程，不能把所有返回项称为 Nexus 子进程。它证明进程存在及一次资源采样，不能证明全部业务健康。批准、启用与有效期限以版本接口为准；保持启用不等于无限期批准，策略/依赖变化仍检查。版本及两次真实调用见[验证记录](WINDOWS-MCP-VERIFICATION.md)。

## 恢复与兼容

Core 保存配置、目录、operation 与 Task，重启恢复查询及原回执对账，不重新安装或调用模型。中断在创建 Task 之前的 pending 可用相同 request_id 恢复提交，或显式移除；未知执行不能新建尝试重跑。移除使新调用失效，取消真实未决 Task，并防止回落到同名静态配置。

stdio 每次操作单独初始化并清理受控进程；重载表示新初始化/目录切换，不承诺跨调用保留内存状态。持久数据依赖服务器文件或远程服务。HTTP 每次操作独立初始化并在本次操作内使用 MCP session/protocol 头，接收 JSON 或 SSE；客户端不支持 sampling、永久订阅和后台服务端规划。

旧 Node TOML 静态 MCP 资源配置仍可用；未开启托管标志时仅声明原三项资源能力。新增 binding 只随托管命令发送，无 binding 的旧命令规范化 JSON 不增添 null 字段，保留旧回执摘要。升级需 Core 与 Node 配套、备份数据库并同步信任摘要；本地 migration 028 依赖已存在的 027 版本，部署时须统筹其他获准迁移，不能自动发布并行语音内容。
