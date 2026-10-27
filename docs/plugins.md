# NEXUS 原生插件

full 模式可在文件工具参数或 `plugin_execute` 外层提供 `access_root`，指定目标设备任意本地绝对目录；`path` 相对此目录，省略仍访问 workspace。例如 `{"plugin_id":"builtin.files","tool":"read","access_root":"C:/Users/name/Desktop","input":{"path":"note.txt"}}`。文件、图片、交付物、LSP 及组合调用共用本次根；Shell/终端支持原有宿主命令及 `workdir`。normal 不接受 `access_root`，工作区及原宿主确认语义保持；full 不提升操作系统账户权限。切换模式后须重新发起工作，不复用旧授权。设计见 [ADR 0046](decisions/0046-full-mode-host-roots.md)，实际上线版本查 [P91](ROADMAP.md#p91-full-host)。

设计与批准边界见 [ADR 0028](decisions/0028-native-plugins-permission-modes.md)，实际验证和部署状态以 [ROADMAP](ROADMAP.md) 为准。这里说明接口与运行方式，不把注册表条目等同于已配置、已验证或已上线。

原生 Skill 的 Shell 步骤，以及 MCP 配置登记、工具发现、重载与调用，见 [Skill 与受控 MCP](skill-mcp.md)。它们共用原 Harness、会话权限、Node 回执和取消协议。

网页/Windows 的手势可视化插件使用独立的浏览器隔离运行契约，开发、宿主验证、自动升级和失败回退见[可视化插件](ui-extensions.md)。下文 Python 插件的执行权限和具体版本批准规则继续保留。

## 权限与执行

全局 normal/full 决定无覆盖会话的权限；会话可设置 normal、full 或 null（继承）。只有认证 owner 的 HTTP 请求可以修改。模式变化使旧授权失效，不能使用旧确认派发新执行；已派发操作保留真实结果，降权不回滚已发生的效果。

normal 在当前任务和有效范围内工作，额外宿主能力需要任务授权，生成插件需批准具体测试版本。full 免执行确认，测试通过后可自动安装、升级与启用；身份、节点静态授权、范围摘要、费用、取消、审计、执行期限和结果核对仍生效。

Shell、run_code 和已启用 Python 插件运行于 Node 当前 Windows 用户。工作目录不是文件隔离。执行进程由 Windows Job 管理，环境不继承 NEXUS、DeepSeek 等令牌变量；这不等于阻止当前用户代码访问本机其他数据。生成代码测试在独立 runsc 环境完成，Core 不导入插件源码。

`plugin.call@1` 复用原 task/attempt、Node 回执、结果验证与恢复协议。版本摘要、节点插件配置摘要、输入摘要、会话和步骤标识绑定执行。重复回执不重复操作；未知执行不能当成未开始重跑。后台进程属于会话，输出环形缓冲 32 KiB、单次读取 8 KiB，最多 8 个运行进程，默认 600 秒回收。真实 PTY 支持 shell 状态与 Ctrl+C；进程重启后未决终端保留 unknown，不假装恢复同一个进程。

## 插件包与 SDK

本地包是一个 UTF-8 JSON，包含 `manifest`、`source` 和 `tests`。参考 [加法示例](examples/add-numbers.plugin.json)。整个包最多 24 KiB；source 最多 20 KiB；每个工具至少一个契约测试，总计最多 12 个。版本使用数字 `major.minor.patch`，同一用户插件的既有版本不能覆盖。

manifest 包含 `schema_version:1`、`plugin_id:user.<name>`、`version`、`name`、`description`、`license`、`runtime:python`、工具列表和依赖。工具声明输入、输出的有界 JSON Schema；支持 object/array/string/integer/number/boolean/null、anyOf、enum/const、大小与数值范围，不支持远程引用或正则 schema。

插件实现 `handle(tool, input, context) -> dict`。`from nexus_plugin import PluginContext` 可获得 `context.workspace`、`context.testing` 和 `context.call(plugin_id, tool, input)`。宿主 SDK 只能桥接本次 Core 已冻结的节点插件版本；不能调用 Core 权限开关或生成第二个规划循环。程序化调用按顺序执行，默认至多 8 次，且在派发前占用本任务剩余操作额度；失败或未知不返还额度。

测试在无网络、无宿主目录的固定独立镜像中执行。`tests` 的 `files` 提供临时 UTF-8 文件夹具，`expected` 与真实输出严格比较。该镜像当前只有标准库，不自动 pip 安装声明依赖，也不连接真实宿主工具；有第三方依赖或测试需要真实宿主桥接时不能伪装成通过。通过表示契约样例匹配，不证明全部业务正确或代码安全。启用记录绑定包摘要与测试环境；停用、取消、归档、撤销身份或权限变化可阻止测试后的自动启用。

模型通过 `plugin_inspect` 按需查询实际契约，通过 `plugin_install` 提交自己编写的插件，结果返回候选、测试和启用状态。full 自动启用的版本在当前轮次目录中按摘要加入；普通模式通过测试后仍等待 owner 批准。模型输出一段代码或自述完成不代表插件已安装。

会话创建按 [ADR 0037](decisions/0037-chat-capability-creation.md) 支持用户直接要求与当前任务缺口判断：优先复用已有能力，组合流程使用 `skill_create`。有效 standard 范围内的新插件候选保存及隔离测试不再额外询问一次确认，normal 的版本批准保留，旧待确认步骤不自动执行。创建前、测试开始和自动启用前重查范围及计划模式；操作额度在候选事务中登记。沙箱不可用、取消或撤销不降级到宿主测试。Skill 创建与使用说明见[聊天工作](chat-work.md#在会话中创建插件或-skill)。

## 接口

所有 owner 写接口要求现有登录 Cookie、Origin 和 CSRF；请求和返回均为 JSON，下载除外。

| 方法与路径 | 输入/用途 |
| --- | --- |
| GET/PUT `/api/v1/permissions/default` | GET 返回 mode/revision；PUT 为 request_id、expected_revision、mode |
| GET/PUT `/api/v1/chat/conversations/{id}/permission` | 会话覆盖；PUT mode 可为 null；返回 effective_mode、source、fingerprint 等 |
| GET/POST `/api/v1/plugins` | 版本列表 / `{request_id,conversation_id,release}` 注册本地或生成包 |
| POST `/api/v1/plugins/github` | `{request_id,conversation_id,repository,commit,path,sha256}`，commit 必须完整 40 位固定提交，sha256 绑定原始 JSON 字节 |
| GET `/api/v1/plugins/versions/{id}` | 包、来源与真实测试记录 |
| POST `/api/v1/plugins/versions/{id}/test` | `{conversation_id,digest}` 启动独立测试；离线返回 unavailable |
| POST `/api/v1/plugins/versions/{id}/decision` | `{conversation_id,digest,action}`；action 为 approve/enable/disable，回退选择旧固定版本 enable |
| GET `/api/v1/plugins/conversations/{id}` | 待办、目标、提醒与触发事件、计划模式与 revision |
| PUT `/api/v1/plugins/conversations/{id}/plan` | `{expected_revision,enabled}`，启用只读计划模式 |
| PATCH/DELETE `/api/v1/plugins/conversations/{id}/schedules/{schedule_id}` | `{request_id,changes}` 修改/暂停/恢复或删除；changes 采用 schedule_update 字段，不重复 schedule_id |
| PATCH `/api/v1/plugins/conversations/{id}/goals/{goal_id}` | `{request_id,changes:{status:"paused"或"active"}}`，恢复仍检查原范围、工具版本、期限及剩余预算 |
| GET `/api/v1/plugins/deliverables/{id}` | 已核对任务的认证附件下载；摘要响应头，保留原文件名 |

GitHub 安装只读取 raw.githubusercontent.com 的固定提交 JSON，不跟随重定向；不执行安装脚本，不建立市场。安装包注册不等于批准执行。管理工具与接口均保留不可变历史，以停用代替破坏性清除。

`present` 把实际文件通过认证 Node 传输通道交给 Core，最多每文件 4 MiB、每次 8 MiB，Core 交付物总容量 64 MiB。只有当前步骤、节点和真实运行尝试匹配才接收；字节摘要核对后才能下载。插件提供的 HTML 或链接不会自动执行。

目标有轮次和到期上限，定时任务支持延时、绝对时间、固定间隔、每日/每周和五字段 cron。默认提醒只写会话事件、不调用模型；`execute:true` 才唤醒原会话的有界任务。目标续跑与定时执行共用原根请求费用归属；启用金额限制时共用该根额度和全局日预算，`models.cost_limits_enabled=false` 时仍保留调用次数、费用/未知记录与其他边界。权限、工具目录或作用范围改变会暂停。重启不自动恢复付费续跑。目标完成标记是状态声明，逐任务证据另查执行记录。

合法 cron 在31天的有限搜索范围内没有后续触发时返回结束；无效字段仍拒绝。达到次数/有效期上限时不再计算下一次触发，当前到期事件先落盘。未消费事件超过300秒会标为 `missed`，继续选择有效事件；已有 wakeup 的事件不补发，准备唤醒前再次检查时效。

用户明确恢复暂停的目标时，允许上一轮保留为 `stopped / PLUGIN_AUTONOMY_PAUSED`，在原目标下创建新轮次；原轮次、执行记录、根费用归属和已消耗次数保留。恢复须确认旧执行已停止且结果可判定、调用费用已知，并重新检查范围/目录/权限；其他失败、恢复未决、未知费用或未知执行不获此例外。模型收到旧轮次标识及核对任务证据的要求，不自动重发旧模型请求或工具执行。次数耗尽或到期仍拒绝恢复。

## 与 DSH 的映射

参考 [固定 DSH 工具目录](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/tool-catalog.zh.md)。本项目独立实现原生等价能力，保留 Core/Node 分工；不是 Cordis 包格式或 DSH TypeScript 插件的二进制兼容层。

| DSH 能力组 | NEXUS 入口 |
| --- | --- |
| 插件管理、Cordis 检查 | builtin.manager；NEXUS 运行注册表，不检查不存在的 Cordis 服务 |
| MCP 资源与工具 | builtin.mcp；Core 版本化登记、发现/重载/调用；stdio 或 Streamable HTTP JSON/SSE |
| 浏览器 Stagehand 工具 | builtin.browser；专用 Edge/Chrome CDP，真实 DOM、控件动作、提取及截图，由主模型规划；不另外启动 Stagehand 模型 |
| 一次性/持久 bash、pwsh、终端、jobs | builtin.shell、terminal、jobs；真实当前用户进程和 Windows ConPTY |
| fs、搜索、str_replace_editor | builtin.files；受控工作区、摘要绑定、预览与读回；正则搜索在可停止的工作进程执行 |
| run_code、workflow | builtin.workflow；Python SDK 与有限节点工具序列；依赖子代理的 DSH workflow DSL 不在本轮 |
| working_directory、worktree、LSP、依赖查询 | builtin.workspace；本地固定提交 Git 与配置语言服务器 |
| Skill | builtin.skill 读取已批准固定 Skill；run_skill 支持显式新 profile 的固定 Shell/MCP 步骤，旧版本不自动获得宿主权限 |
| 会话查询、待办、提问、退出计划 | builtin.session、todo、interaction；按 owner/项目隔离，私有引用保留原外发边界 |
| 目标、调度、文件交付、Web、时间 | builtin.goal、schedule、present、web、clock |
| 子代理、Agent Team、Ralph、插件市场 | 用户明确暂缓，不注册假工具 |

配置示例见 [Core](../config/plugins.core.example.toml)、[Windows Node](../config/plugins.node.example.toml)。终端依赖用 `requirements-plugins.lock`，LSP 用 `config/plugin-runtime/package-lock.json` 固定版本；未配置服务器、浏览器或 Shell 时明确不可用。前端接入见 [DSH 工作单](PLUGINS-FRONTEND.md)。
