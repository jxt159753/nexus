# NEXUS

**One Mind, Many Bodies — 一个大脑，多个身体。**

NEXUS 是一个面向个人、长期运行的自主 AI 系统。它希望逐步具备统一认知、长期记忆、目标管理、任务调度与可靠执行能力，并通过 Windows、Android 及未来设备感知环境和执行操作。

项目长期愿景是接近 JARVIS 的个人助手能力。开发从一个可验收的只读执行闭环开始，不将远期愿景当作当前已有功能。

## 当前状态

当前已有可安装的 Python 工程、环境变量配置校验、依赖锁定和基础测试；安装与配置验证步骤见 [P01 工程验证记录](docs/P01-VERIFICATION.md)。正式 Core、Node、Web 客户端及执行闭环尚未交付。

当前复核范围为 S1/P01 工程准备。阶段授权、进度、用户验收状态及后续批准要求统一见 [ROADMAP.md](docs/ROADMAP.md)；本 README 不授予后续阶段实施权限。

仓库中保留了早期实验 API、`start_nexus.bat` 和本地 `nexus.db`。它们未纳入正式 Core、持久化或安全验收，实验启动入口也未接入新的配置校验，不作为正式启动方式。

## 文档入口

| 文档 | 用途 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 每次开发必读的协作规范、范围约束、质量要求和交付规则 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 愿景、架构边界、MVP 技术方案、接口、状态机、协议及预期目录 |
| [ROADMAP.md](docs/ROADMAP.md) | 唯一的开发阶段、任务编号、批准状态、依赖和验收标准入口 |
| [P01-VERIFICATION.md](docs/P01-VERIFICATION.md) | 工程安装与配置的复现命令、实测结果和限制 |

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

技术方案为 Python 3.12+、FastAPI、Pydantic、SQLite、WebSocket 和原生 HTML/CSS/JavaScript；Windows Node 使用 Python，Android 原生 Node 后续采用 Kotlin。工程依赖已在 Windows / Python 3.13.9 上按 `requirements.lock` 完成独立环境安装与兼容性检查；Python 3.12 和其他系统未实测，构建工具尚未完全锁定。

远程连接采用 HTTPS/WSS、应用身份认证及逐操作授权，Tailscale 由用户配置。默认本机开发模式不开放远程访问。

## 安全与可靠性边界

- 任务有步骤、执行次数、期限、费用和输出大小上限。
- 派发前持久化，重复请求及重复命令分别去重。
- 断线后先对账；无法确认执行结果时记录 unknown，不伪报成功或盲目重试。
- 全局暂停阻止新派发；取消需要真实执行停止确认。
- 个人长期记忆需要明确授权，并区分事实、推测、计划及过期信息。
- 删除重要文件、对外发送消息、支付或修改安全设置等能力尚未实现，不能通过模型或普通审批自行启用。

## 开发与验收

先读取必读文档，确认当前批准阶段，再执行其中的任务。每个任务必须交付可检查的改动、实际验证证据、未验证项和已知限制。阶段完成后先汇报，不自动进入未获批准的下一阶段。

完整目录设计见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。可执行任务清单、逐项验收标准和故障测试矩阵见 [ROADMAP.md](docs/ROADMAP.md)。工程安装与测试命令见 [P01 工程验证记录](docs/P01-VERIFICATION.md)；当前不提供正式服务启动命令。

第一版暂不实现 Android 无障碍控制、全天候语音、真实 Jev/LLM、复杂自主规划、任意命令执行或副作用工具。
