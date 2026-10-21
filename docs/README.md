# NEXUS 文档导航

第一次了解项目，先读 [项目首页](../README.md)；想知道功能做到哪一步，直接看 [路线图](ROADMAP.md)。

这里按阅读目的分类，不另维护功能状态或开发计划。

## 日常使用

| 文档 | 能解决什么问题 |
| --- | --- |
| [聊天使用说明](m3-chat.md) | 多轮聊天、停止回复、工具确认、结果回传、历史和费用 |
| [聊天中的多步流程](m5-chat-skills.md) | 可用流程目录、参数与计划确认、步骤审批/取消及结果解读 |
| [项目、可控记忆与基础工具](m6-projects-tools.md) | 项目会话与待办、记忆保存/逐轮外发、受限文件读写/查找和固定命令 |
| [新文件工具、会话交互与配置目标](dsh-tools-usage.md) | 分页/内容搜索/精确修改、纠正/排队/日志/历史引用、Edge/Chrome 与明确配置程序 |
| [聊天工作与网页工具](chat-work.md) | 工作区默认读写、有限工具循环、DeepSeek 搜索/抓取、来源及费用 |
| [任务、记忆与项目页面](s6-browser-records.md) | 提交只读任务、审批、暂停、保存或撤销记忆、关联项目 |
| [单条自然语言任务](m2-language.md) | 把一句操作请求解释为已有工具，确认后执行 |
| [自定义工具与流程](m4-usage.md) | 创建、测试、批准、启用、使用和停用能力；使用前先核对路线图中的限制 |
| [部署、远程连接与恢复](s7-deployment.md) | 首次配置、可信加密连接、升级、备份及恢复 |

旧使用文档保留 S6、S7 等文件名，避免破坏已有链接。S 表示当时的开发子阶段，不是软件版本号；其中 M1 的零模型、单步、两个只读工具和逐次目录审批只适用于相应历史入口。当前工作区默认权限和有界工具循环先看[聊天工作](chat-work.md)，各能力是否上线仍查路线图。

## 功能状态与后续规划

| 阅读入口 | 内容 |
| --- | --- |
| [功能状态](ROADMAP.md#功能状态) | 已实现、部分实现和未实现的功能，及证据范围 |
| [里程碑总览](ROADMAP.md#里程碑总览) | M1–M8 的目标和阶段状态 |
| [M4 待完成项](ROADMAP.md#m4-待完成项) | 当前收尾工作、环境条件和验收要求 |
| [后续规划](ROADMAP.md#后续规划) | M5/M6 的演进记录与 M7/M8 等后续方向 |
| [前端与 App 开发](ROADMAP.md#前端与-app-开发) | 网页体验、页面与交互任务、Android App 首版及手机执行端的区别 |
| [版本与部署记录](ROADMAP.md#版本与部署记录) | 线上版本、本地候选版本、验证与部署的区别 |

以上状态和计划均回到 ROADMAP 查询；本次 M6 与基础工具已有用户实施授权，后续阶段仍需明确批准。

## 开发与技术参考

| 文档 | 内容 |
| --- | --- |
| [开发规范](../AGENTS.md) | 必读材料、授权范围、质量和交付要求 |
| [架构设计](ARCHITECTURE.md) | 中心服务与设备的分工、任务状态、权限、恢复、模型与扩展设计 |
| [Web 与 App 设计边界](ARCHITECTURE.md#17-web-前端与-app-设计边界) | 页面职责、共享数据、App 接入、离线行为和客户端权限 |
| [Windows / Android 前端 DSH 工作说明](FRONTEND-DSH-WORKORDER.md) | 用户直接指挥 DSH 的首轮提示、方案决策、开发边界与交付验收方式 |
| [存储与权限](storage-security.md) | 数据保存、身份、会话、审批及相关接口 |
| [Windows 执行端](node-runtime.md) | 目录范围、本地工具、执行记录和进程管理 |
| [设备连接](s3-link.md) | Core/Node 配置、身份认证、连接与重连 |
| [任务执行](s4-harness.md) | 提交、派发、验证结果和历史查询 |
| [恢复与全局控制](s5-recovery.md) | 超时、取消、暂停和执行结果未知的处理 |
| [设备协议 v1](protocol-v1.md) | 消息信封、原两工具基线及 M6 基础工具扩展说明 |
| [扩展与隔离执行协议 v2](m4-extension-protocol.md) | 生成工具的版本、测试、隔离环境与通信要求 |
| [WSL2 沙箱部署](m4-sandbox-wsl.md) | 固定 runsc、非特权用户、宿主资源限制、运行与恢复 |

Core、Node、Tool、Skill 等词的解释见 [架构文档的名词说明](ARCHITECTURE.md#常用名词)。

## 验证证据与历史

验证文档记录具体环境、操作、结果及未验证项。测试数量是技术证据的一部分，不能代替用户验收；手机尺寸的浏览器截图也不能代替 Android 真机操作。

| 范围 | 记录 |
| --- | --- |
| M1 工程与基础契约 | [P01](P01-VERIFICATION.md)、[P02](P02-VERIFICATION.md)、[S2 存储与权限](S2-VERIFICATION.md) |
| M1 设备与执行 | [P05 本地工具](P05-VERIFICATION.md)、[S3 连接](S3-VERIFICATION.md)、[S4 执行](S4-VERIFICATION.md) |
| M1 恢复、页面和实际设备 | [S5 恢复](S5-VERIFICATION.md)、[S6 页面](S6-VERIFICATION.md)、[S7 设备与部署](S7-VERIFICATION.md) |
| M2 模型与自然语言 | [脱敏验证汇总](assets/m2-evidence.json)；说明文字见路线图的 M2 记录 |
| M3 聊天及补充功能 | [聊天首版](assets/m3-evidence.json)、[会话工具](assets/chat-tools-evidence.json)、[结果回传](assets/chat-results-evidence.json)、[自动回传](assets/chat-auto-results-evidence.json) |
| M4 补充实现与部署 | [本轮验证](M4-COMPLETION.md)、[脱敏摘要](assets/m4-completion-evidence.json) |
| M4 首版与后续独立复核 | [原交付验证](M4-VERIFICATION.md)、[独立复核与修复](M4-CODEX-REVIEW.md)、[脱敏复核汇总](assets/m4-codex-evidence.json) |
| M5 实现与获批上线 | [验证与限制](M5-VERIFICATION.md)、[脱敏摘要](assets/m5-evidence.json) |
| M6 本地实现与基础工具 | [验证与限制](M6-VERIFICATION.md) |
| M6 会话工作与上线 | [验证与限制](M6-WORK-VERIFICATION.md)、[脱敏摘要](assets/m6-work-evidence.json) |
| M6 文件默认权限与网页工具 | [追加验证与限制](M6-WORK-FIX-VERIFICATION.md)、[脱敏摘要](assets/m6-work-fix-evidence.json) |
| M6 的 DSH 工具与会话机制 | [追加验证、固定包与限制](DSH-TOOLS-VERIFICATION.md) |
| M6 的 DSH 参考优化 | [本地验证、源码基线与限制](DSH-REFERENCE-VERIFICATION.md) |
| 历次交付、上线、批准与中间失败 | [2026 年 10 月历史记录](history/2026-10-delivery.md)，固定历史快照，不更新进度 |

## 设计决定与旧工作单

聊天自动工作与自然语言权限答复见 [使用说明](chat-work.md)及 [ADR 0018](decisions/0018-scoped-chat-work.md)。

工作区免重复审批、批量函数兼容与网页工具见 [ADR 0019](decisions/0019-workspace-defaults-web-tools.md)。

DSH GitHub 架构/Harness 对照、上下文投影、工具清单与按需能力详情见 [ADR 0020](decisions/0020-dsh-context-harness.md)。

重要技术决定保存在 [decisions 目录](decisions/)。主要包括 [单活云端部署](decisions/0008-self-hosted-core-ip-tls.md)、[模型接入](decisions/0009-m2-deepseek-language.md)、[聊天](decisions/0010-m3-chat.md)、[工具结果回传](decisions/0012-chat-result-feedback.md)、[自动回传同意](decisions/0013-chat-auto-result-consent.md)和 [自扩展](decisions/0014-m4-self-extension.md)。

[M4 原实施工作单](M4-DSH-WORKORDER.md)保留交付约定和验收清单，便于复核；当前负责人、剩余工作及批准范围仍以路线图为准。

## 文档维护约定

- 项目首页解释项目与使用入口；本页负责分类导航。
- ROADMAP 是功能状态、阶段批准、任务进度、验收与后续计划的唯一入口。
- ARCHITECTURE 解释技术设计，不重复维护阶段状态或另一套路线。
- 使用说明解释操作步骤；验证记录保存实际证据；history 中的历史快照不再追加当前进度。
- 新功能先同步相关设计和使用说明，再在 ROADMAP 记录实际完成情况。原有文件不因分类整理而批量改名。

新增文件工具、逐次请求记录与可控连续工作见 [ADR 0021](decisions/0021-dsh-tools-session-journal.md)；它与 ADR 0020 的参考分析相衔接，不替换旧交付记录。

- [上下文与压缩](context-compaction.md)：自动压缩、`/compact`、原始记录与费用边界。
- [DSH 压缩设计](decisions/0022-dsh-context-compaction.md)：参考来源、容量、记录与恢复。

- [DSH 上下文压缩验证](CONTEXT-COMPACTION-VERIFICATION.md)：固定版本、测试、上线与限制。
