# NEXUS 文档导航

第一次了解项目，先读 [项目首页](../README.md)；想知道功能做到哪一步，直接看 [路线图](ROADMAP.md)。

这里按阅读目的分类，不另维护功能状态或开发计划。

## 日常使用

| 文档 | 能解决什么问题 |
| --- | --- |
| [向设备发送文件](file-transfers.md) | 文件上传、选择 Node、收件箱、送达校验、权限和恢复 |
| [聊天使用说明](m3-chat.md) | 多轮聊天、停止回复、工具确认、结果回传、历史和费用 |
| [聊天中的多步流程](m5-chat-skills.md) | 可用流程目录、参数与计划确认、步骤审批/取消及结果解读 |
| [项目、可控记忆与基础工具](m6-projects-tools.md) | 项目会话与待办、记忆保存/逐轮外发、受限文件读写/查找和固定命令 |
| [自动长期记忆与 DSH 接入](automatic-memory.md) | 持续保存/使用同意、自动记忆范围、控制接口及前端接入要求 |
| [新文件工具、会话交互与配置目标](dsh-tools-usage.md) | 分页/内容搜索/精确修改、纠正/排队/日志/历史引用、Edge/Chrome 与明确配置程序 |
| [DSH 图片与查询工具](dsh-builtin-tools.md) | 图片读取配置、时间和任务查询、Skill 说明及有界结果 |
| [聊天附件](chat-attachments.md) | 任意文件上传、图片/文本预览、原件下载和逐轮外发同意 |
| [原生插件与权限](plugins.md) | 普通/完全模式、会话覆盖、宿主终端、自写插件测试/启用与接口 |
| [目标验收与持续运行](autonomy.md) | 依赖计划、证据、事件订阅、独立业务测试、归档健康接口及 DSH 工作单 |
| [Skill Shell 与受控 MCP](skill-mcp.md) | 原生步骤、MCP 登记/发现/重载/调用、权限与兼容限制 |
| [聊天工作与网页工具](chat-work.md) | 工作区默认读写、有限工具循环、DeepSeek 搜索/抓取、来源及费用 |
| [任务、记忆与项目页面](s6-browser-records.md) | 提交只读任务、审批、暂停、保存或撤销记忆、关联项目 |
| [单条自然语言任务](m2-language.md) | 把一句操作请求解释为已有工具，确认后执行 |
| [自定义工具与流程](m4-usage.md) | 创建、测试、批准、启用、使用和停用能力；使用前先核对路线图中的限制 |
| [部署、远程连接与恢复](s7-deployment.md) | 首次配置、可信加密连接、升级、备份及恢复 |
| [Android App / Node 接入](../apps/android/README.md) | 构建、身份与资源配置、通知范围、Core 实时语音及联调方式 |

旧使用文档保留 S6、S7 等文件名，避免破坏已有链接。S 表示当时的开发子阶段，不是软件版本号；其中 M1 的零模型、单步、两个只读工具和逐次目录审批只适用于相应历史入口。当前工作区默认权限和有界工具循环先看[聊天工作](chat-work.md)，各能力是否上线仍查路线图。

## 功能状态与后续规划

| 阅读入口 | 内容 |
| --- | --- |
| [功能状态](ROADMAP.md#功能状态) | 已实现、部分实现和未实现的功能，及证据范围 |
| [里程碑总览](ROADMAP.md#里程碑总览) | M1–M9 的目标和阶段状态 |
| [M4 待完成项](ROADMAP.md#m4-待完成项) | 当前收尾工作、环境条件和验收要求 |
| [后续规划](ROADMAP.md#后续规划) | M5/M6 的演进记录与 M7/M8 等后续方向 |
| [高自由度自迭代](ROADMAP.md#m9-self-iteration) | 插件、Skill 与 Core/Node 自迭代的任务、批准范围和验收要求 |
| [前端与 App 开发](ROADMAP.md#前端与-app-开发) | 网页体验、页面与交互任务、Android App 首版及手机执行端的区别 |
| [版本与部署记录](ROADMAP.md#版本与部署记录) | 线上版本、本地候选版本、验证与部署的区别 |

以上状态和计划均回到 ROADMAP 查询；本次 M6 与基础工具已有用户实施授权，后续阶段仍需明确批准。

## 开发与技术参考

| 文档 | 内容 |
| --- | --- |
| [开发规范](../AGENTS.md) | 必读材料、授权范围、质量和交付要求 |
| [架构设计](ARCHITECTURE.md) | 中心服务与设备的分工、任务状态、权限、恢复、模型与扩展设计 |
| [自迭代架构方案](ARCHITECTURE.md#self-iteration-design) | 范围预授权、可信隔离、独立评测、固定版本发布与数据兼容回退 |
| [实时语音](realtime-voice.md) | Windows 按钮/唤醒、流式播报、打断/停止、Core 接口与独立主动权限 |
| [实时语音验收标准](realtime-voice-acceptance.md) | 真实麦克风证据、唤醒误触发、打断/首段延迟、资源占用与模拟结果标注 |
| [Web 与 App 设计边界](ARCHITECTURE.md#17-web-前端与-app-设计边界) | 页面职责、共享数据、App 接入、离线行为和客户端权限 |
| [Windows / Android 前端 DSH 工作说明](FRONTEND-DSH-WORKORDER.md) | 用户直接指挥 DSH 的首轮提示、方案决策、开发边界与交付验收方式 |
| [聊天过程展示与最终总结](CHAT-PROGRESS-FRONTEND.md) | 公开摘要、真实工具进度、最终回答与前端折叠契约，及 DSH 适配说明 |
| [DSH 图片工具前端接入要求](DSH-BUILTIN-TOOLS-FRONTEND.md) | 交给 DSH 的图片展示、外发提示、查询结果及验收要求 |
| [图片与文件上传前端要求](CHAT-ATTACHMENTS-FRONTEND.md) | 交给 DSH 的附件选择/拖放/粘贴、Windows 大文件传输、预览和验收要求 |
| [原生插件与权限前端要求](PLUGINS-FRONTEND.md) | 权限继承、插件生命周期、终端、交付物及 Windows Node 配置桥 |
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
| M6 的 DSH 图片与查询工具 | [真实解码、权限、协议、前端接入及本地验证](DSH-BUILTIN-TOOLS-VERIFICATION.md) |
| M6 的会话附件 | [任意文件、外发同意、迁移和独立 Core 重启验证](CHAT-ATTACHMENTS-VERIFICATION.md) |
| Windows-MCP 会话链路 P83 | [真实模型安装、登记、调用、批准Skill复用与发布回退](WINDOWS-MCP-VERIFICATION.md)、[脱敏记录](assets/windows-mcp-evidence.json) |
| 原生插件 P57–P61 | [固定包、真实宿主与 runsc、生产部署及费用证据](PLUGINS-VERIFICATION.md) |
| 会话创建插件与 Skill P77 | [直接请求、缺口判断、真实模型/runsc/Windows 执行、批准与停用](CHAT-CREATION-VERIFICATION.md) |
| 性格与自动记忆 P78/P79 | [实际模型调用、记忆隔离/纠正/撤销、部署与费用证据](assets/automatic-memory-evidence.json) |
| Skill Shell 与受控 MCP P65 | [真实 Shell、stdio/HTTP/SSE、权限、独立 Core/Node 与重启证据](SKILL-MCP-VERIFICATION.md) |
| 原生插件前端/权限/Windows 桥（本地） | [界面、会话覆盖、插件生命周期、工具卡、390px/键盘与安装版验证](PLUGINS-FRONTEND-VERIFICATION.md) |
| P63 本地语音唤醒 | [手动开启、待机隐私、安装更新及延期真人验证](assets/voice-wake-evidence.json) |
| P63 Windows语音补齐 | [部署、安装包、真人确认、测试层级与未验矩阵](assets/voice-completion-evidence.json) |
| P63 实时语音合成音冒烟（非验收） | [百炼 TTS/ASR/Core 媒体代理时延与限制](assets/voice-bailian-smoke.json) |
| P63 真实麦克风 ASR 冒烟（录音回放，非真人实时） | [Realtek 麦克风阵列采集 + Fun-ASR 时延](assets/voice-mic-human-smoke.json) |
| P63 Windows 运行时端到端冒烟（录音回放，fake Chat） | [真实麦克风 → Core → Fun-ASR → CosyVoice → 播放](assets/voice-runtime-e2e-smoke.json) |
| P63 待机资源实测（真实麦克风） | [KWS 待机 10 s：CPU 2.5%（范围 1.7–2.5%）/ RSS 96.7 MiB](assets/voice-standby-resource.json) |
| P63 待机 60 s 环境声（真实麦克风，非误唤醒率） | [0 次命中、CPU 2.14%、RSS 97.3 MiB](assets/voice-standby-60s.json) |
| M6 的会话附件前端与 Windows 桥 | [界面、旧控制台、Base64 传输、安装版大文件与恢复验证](CHAT-ATTACHMENTS-FRONTEND-VERIFICATION.md) |
| M6 的 DSH 参考优化 | [本地验证、源码基线与限制](DSH-REFERENCE-VERIFICATION.md) |
| M6 跨轮代码与保存衔接 | [DSH 对照、回归与未验证项](CHAT-CONTINUITY-VERIFICATION.md) |
| M6 回执与执行队列恢复 | [时钟、停止证据与实际上线验证](RECOVERY-EVIDENCE-VERIFICATION.md) |
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
- [Windows App 与随包 Node](decisions/0023-windows-app-bundled-node.md)：Tauri 2、Core 原生桥、Node 进程与安装包边界。
- [Three.js 三维过程视图](decisions/0024-threejs-3d-visualization.md)：Core/Node/任务场景、输入桥与 WebGL 回退。
- [工具轮次上下文复用](decisions/0025-tool-turn-context-reuse.md)：已完成工具轮次进入后续上下文，记忆/引用边界保留。
- [实时语音扩展](decisions/0029-realtime-voice.md)：独立音频运行时、文本控制通道、分离的主动权限与本地唤醒边界（历史提案，现实现由ADR0038替代）。
- [百炼实时语音与 Core 媒体代理](decisions/0030-bailian-voice-core-media.md)：Fun-ASR/CosyVoice 与公网媒体代理的选型记录。
- [Windows 语音闭环](decisions/0038-windows-voice-loop.md)：设备实际播放/停止证据、流式 PCM、代次打断、owner 激活与独立主动权限。
- [聊天输出上限](decisions/0031-dsh-chat-output-limits.md)、[owner 金额限额开关](decisions/0032-owner-cost-limits.md)与[工具参数拒绝反馈](decisions/0033-tool-argument-feedback.md)：输出、费用策略、未执行证据与有界修正。
- [范围预授权自迭代](decisions/0035-scoped-self-iteration.md)：最少人工确认、可演进组件与可信控制部分、单活发布和回退的设计取舍。
- [会话创建插件与 Skill](decisions/0037-chat-capability-creation.md)：复用优先、候选创建与验证、版本批准、幂等和停止边界。
- [自动长期记忆](decisions/0039-automatic-memory.md)：持续保存与模型外发同意、当前原文提取、个人/项目范围及撤销传播。
- [有界原文回取与停滞检测](decisions/0043-bounded-context-retrieval.md)：短引用、记忆筛选与 Token 成本；状态见路线图 P88。
- [累计调用与金额额度可关闭](decisions/0044-optional-usage-quotas.md)：统一关闭次数、轮数和金额额度，保留审计、权限及停止；状态见路线图 P89。
- [失败后的自然语言反馈](decisions/0036-natural-failure-feedback.md)：网页失败继续、保留费用预留、仅解释的模型总结和不可调用时的自然语言兜底。

- [DSH 上下文压缩验证](CONTEXT-COMPACTION-VERIFICATION.md)：固定版本、测试、上线与限制。

- [Windows 常驻语音唤醒验证](assets/voice-resident-evidence.json)
