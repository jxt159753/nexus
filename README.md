# NEXUS

**One Mind, Many Bodies — 一个大脑，多个身体。**

NEXUS 是一个面向个人、长期运行的自主 AI 系统。它希望逐步具备统一认知、长期记忆、目标管理、任务调度与可靠执行能力，并通过 Windows、Android 及未来设备感知环境和执行操作。

项目长期愿景是接近 JARVIS 的个人助手能力。开发从一个可验收的只读执行闭环开始，不将远期愿景当作当前已有功能。

## 当前状态

当前已有可安装的 Core、独立 Windows Node 和同源 Web 控制台。浏览器支持提交只读任务、审批、结果与历史、取消和全局暂停/恢复，并能在断线后同步持久事件。Core 保留权限、持久派发、工具验证、有界恢复及真实取消；个人记忆必须明确同意，支持项目关联、信息类型、时效与撤销。路由原因和零模型调用可查询。浏览器操作见 [S6 说明](docs/s6-browser-records.md)，实测及限制见 [S6 验证](docs/S6-VERIFICATION.md)，身份与启动配置见 [S3 Link](docs/s3-link.md)。后端任务及恢复细节保留在 [S4](docs/s4-harness.md)和 [S5](docs/s5-recovery.md)。

首次配置、可信 TLS、停机备份和恢复流程见 [部署与恢复](docs/s7-deployment.md)，本机及实际设备证据见 [S7 记录](docs/S7-VERIFICATION.md)。实际部署已按用户批准改为自有 Ubuntu 云端唯一 Core、IP 直连 HTTPS/WSS 和独立 Windows Node；证书自动续期及单活迁移的结果见 ROADMAP。此前工程与契约证据见 [P01](docs/P01-VERIFICATION.md)、[P02](docs/P02-VERIFICATION.md)，存储与权限见 [接口](docs/storage-security.md)及 [S2 验证](docs/S2-VERIFICATION.md)，本地工具见 [Node Runtime](docs/node-runtime.md)及 [P05 验证](docs/P05-VERIFICATION.md)。

阶段授权、进度、用户验收状态及后续批准要求统一见 [ROADMAP.md](docs/ROADMAP.md)；本 README 不授予后续阶段实施权限。

早期实验文件已清理。当前仅执行两个固定只读动作，不接受任意工具或自报任务终态；未知执行必须有旧执行已停止的证据才能在预算内重试。M1 技术交付及原 S7 真机检查已完成，最终用户验收状态见 ROADMAP；新部署的跨主机和真机证据单独记录。

## 启动入口

先按 [部署与恢复说明](docs/s7-deployment.md)准备独立数据目录、登记 owner/Node 身份并安全注入环境凭证，再在两个终端分别运行（示例配置也须先确认资源范围）：

```powershell
& .\.venv\Scripts\python.exe -B -m nexus.core --config config/core.example.toml
& .\.venv\Scripts\python.exe -B -m nexus.node --config config/windows-node.example.toml
```

示例仅启用显式 loopback HTTP/WS 开发模式。远程运行必须配置可信 TLS；不关闭证书校验或自动修改系统设置。

默认配置启动后，在浏览器打开 `http://127.0.0.1:8000/`，用离线登记的 owner 凭证登录。无需前端构建或模型密钥；工具授权及 workspace 根仍须按配置明确登记。

## 文档入口

| 文档 | 用途 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 每次开发必读的协作规范、范围约束、质量要求和交付规则 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 愿景、架构边界、MVP 技术方案、接口、状态机、协议及预期目录 |
| [ROADMAP.md](docs/ROADMAP.md) | 唯一的开发阶段、任务编号、批准状态、依赖和验收标准入口 |
| [P01-VERIFICATION.md](docs/P01-VERIFICATION.md) | 工程安装与配置的复现命令、实测结果和限制 |
| [P02-VERIFICATION.md](docs/P02-VERIFICATION.md) | 契约与规则路由的复核结果、问题及复现方式 |
| [S2-VERIFICATION.md](docs/S2-VERIFICATION.md) | 持久化、身份、会话、权限、审批和独立安装的实测证据 |
| [storage-security.md](docs/storage-security.md) | 仓储与权限接口、离线管理、事务边界及后续接入要求 |
| [node-runtime.md](docs/node-runtime.md) | Windows 本地策略、固定工具、命令摘要、回执与进程管理接口 |
| [P05-VERIFICATION.md](docs/P05-VERIFICATION.md) | 真实 Windows 路径、junction、进程清理、去重及重启恢复证据 |
| [s3-link.md](docs/s3-link.md) | Core/Node 配置、独立启动、认证通信及后续派发边界 |
| [S3-VERIFICATION.md](docs/S3-VERIFICATION.md) | 真实 WebSocket、独立进程重连、凭证轮换和全新离线安装证据 |
| [s4-harness.md](docs/s4-harness.md) | 任务提交、审批、持久派发、证据验证与历史接口 |
| [S4-VERIFICATION.md](docs/S4-VERIFICATION.md) | S4 权威事务、真实工具闭环和独立进程重启证据 |
| [s5-recovery.md](docs/s5-recovery.md) | 有界恢复、取消、暂停与控制接口、升级及未知状态限制 |
| [S5-VERIFICATION.md](docs/S5-VERIFICATION.md) | S5 故障注入、真实进程停止和恢复验证证据 |
| [s6-browser-records.md](docs/s6-browser-records.md) | 同源页面、客户端 WS、事件恢复、显式记忆、项目及路由接口 |
| [S6-VERIFICATION.md](docs/S6-VERIFICATION.md) | 独立安装包、浏览器与手机尺寸、真实进程及零模型调用证据 |
| [s7-deployment.md](docs/s7-deployment.md) | 首次启动、直接 HTTPS/WSS、Android 验收步骤及停机一致备份恢复 |
| [S7-VERIFICATION.md](docs/S7-VERIFICATION.md) | 最终本机回归、真实 TLS、恢复演练、故障矩阵及未验收项 |
| [protocol-v1.md](docs/protocol-v1.md) | 已实现的消息字段、编解码入口、错误分类和字节限额 |

`ARCHITECTURE.md` 和 `ROADMAP.md` 的规范路径位于 docs/，根目录不维护同名副本。每次开发开始前，完整读取 AGENTS.md 和这两份文档。

## 三个核心目标

- **一个大脑**：只有 JARVIS Core 权威维护目标、任务、记忆、设备状态和授权；Body Node 不形成独立决策中心。
- **低成本运行**：简单事件先用规则；模型接口可替换，按能力与预算选择模型；心跳、重连和状态查询不调用大模型。
- **可靠自主执行**：依靠持久状态、工具证据、验证、审批、预算、取消和恢复推进任务，不依靠模型自述判断成功。

## 最小架构

第一阶段采用一个模块化 Core 服务、一个独立 Windows Node 进程和一个由 Core 托管的静态 Web 客户端。

浏览器通过 JARVIS Link 向 Core 提交结构化任务；Core 中的 Cognitive Kernel 进行规则路由，Harness 执行授权与生命周期管理，然后向 Node 派发具体命令。Node 执行受限工具并回传结果；Core 验证、持久化并推送任务状态。

Core SQLite 是权威存储。Node 的本地 SQLite 仅保存执行回执，用于去重和断线恢复，不保存长期目标、个人画像或独立规划。

| 组件 | 主要职责 |
| --- | --- |
| JARVIS Core | 任务入口、身份与授权、设备管理、长期记忆和权威状态 |
| Cognitive Kernel | 规则判断、推理需求判断接口和模型路由建议 |
| Agent Harness | 计划、审批、预算、派发、重试、取消、恢复和结果验证 |
| JARVIS Link | 认证后的 JSON / WebSocket 通信、能力注册、心跳与重连 |
| Body Node | 本地权限限制、固定工具执行和回执反馈 |

## 第一阶段 MVP

产品里程碑 M1 包含 ROADMAP 中的 S1–S7；其中 S1 仅指工程准备子阶段，不等于整个 MVP。

验收目标：Windows 上分别运行 Core 和 Node；Android 浏览器通过 WebSocket 提交任务；Core 规则路由并授权；Node 执行安全只读操作；Core 验证结果并推送；任务与结果保存到 SQLite，服务重启后仍可查询。

第一版任务：

- 获取白名单系统信息，使用用户明确配置的只读预授权。
- 列出配置目录根的直接条目，通过资源别名访问，每项任务需要审批；不递归、不读取正文、不跟随链接。

第一版模型调用数和模型费用均为零，预留统一决策模型与 Provider 接口。未知意图直接返回明确错误，不自动升级模型。

技术方案为 Python 3.12+、FastAPI、Pydantic、SQLite、WebSocket 和原生 HTML/CSS/JavaScript；Windows Node 使用 Python，Android 原生 Node 后续采用 Kotlin。依赖已在 Windows / Python 3.13.9 和 Ubuntu 24.04 / Python 3.12.3 的独立环境按锁文件安装；Linux 的 409 项 Core/协议/权限检查及实际云端 Core/Windows Node 闭环通过，其他组合和长期运行未据此宣称已验证。构建工具尚未完全锁定。

远程连接采用 HTTPS/WSS、应用身份认证及逐操作授权。当前自有云服务器使用可信公网 IP 证书直接连接，不依赖 Tailscale；公开 CA 负责签发和续期。默认本机开发模式不开放远程访问。

## 安全与可靠性边界

- 任务有步骤、执行次数、期限、费用和输出大小上限。
- 派发前持久化，重复请求及重复命令分别去重。
- 断线后先对账；无法确认执行结果时记录 unknown，不伪报成功或盲目重试。
- 全局暂停阻止新派发；取消需要真实执行停止确认。
- 个人长期记忆需要明确授权，并区分事实、推测、计划及过期信息。
- 删除重要文件、对外发送消息、支付或修改安全设置等能力尚未实现，不能通过模型或普通审批自行启用。

## 开发与验收

先读取必读文档，确认当前批准阶段，再执行其中的任务。每个任务必须交付可检查的改动、实际验证证据、未验证项和已知限制。阶段完成后先汇报，不自动进入未获批准的下一阶段。

完整目录设计见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。可执行任务清单、逐项验收标准和故障测试矩阵见 [ROADMAP.md](docs/ROADMAP.md)。工程历史见 [P01 工程验证记录](docs/P01-VERIFICATION.md)，当前安装及真实进程验证命令见 [S3 验证记录](docs/S3-VERIFICATION.md)。

第一版暂不实现 Android 无障碍控制、全天候语音、真实 Jev/LLM、复杂自主规划、任意命令执行或副作用工具。
