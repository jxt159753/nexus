# ADR 0020：借鉴 DSH 的上下文投影、请求准入和能力发现

日期：2026-10-20。用户明确要求参考 DSH GitHub 源码，优化上下文、工具、整体架构与 Harness，取长补短。本文记录技术取舍；批准范围、交付与验证状态只更新 [ROADMAP](../ROADMAP.md)。

## 参考基线

官方仓库为 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。本次核对固定提交 [`687f282e8625c6b8d11ff58211a90524be2e4317`](https://github.com/deepseek-ai/deepseek-harness/commit/687f282e8625c6b8d11ff58211a90524be2e4317)，对应发布 `0.2.1-rc.2`。先前安装包只用于定位模块，决定以 GitHub 固定源码为依据。

直接检查的主要入口：

- [整体架构](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/architecture.md)：插件组成、能力接口、会话事件与模型循环。
- [Agent Loop](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/agent.ts)：输入准入、请求准备、轮次、步骤及取消后的收敛。
- [工具运行时](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/tools/src/index.ts)：目录、策略、执行和结果投影，取消与不可覆盖的拒绝。
- [调用配对边界](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/compaction/compaction/src/tool-pairing.ts)与[结果裁剪](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/compaction/compaction-tool-result-pruner/src/index.ts)：裁剪保持消息配对，原结果保留，投影可重放，不靠模型裁剪。
- [Skill 目录及加载工具](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/skill/tool-skill/src/index.ts)：先提供有限说明，按需加载具体内容。
- [文件读取](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs/src/read.ts)与[内容搜索](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs-search/src/grep.ts)：有界读取、结构化结果、固定参数向量，执行与显示分别处理。

本次按这些机制重新适配 Python 实现，没有复制 TypeScript 包、引入 Cordis 或启动 DSH 模型任务。

## 架构与 Harness 取舍

| 关注点 | DSH 的做法 | NEXUS 采用方式 |
| --- | --- | --- |
| 能力分层 | 接口、Provider、Consumer 可替换 | 沿用 contracts、Core Provider、Harness/Node；保持模块边界，不另建插件框架 |
| 模型循环 | turn 下有多个 step，请求准备和工具执行分开 | 沿用根聊天轮次、有限模型交换和工作步骤；不改为无界循环 |
| 权威记录 | 会话追加事件投影为上下文 | 保留原任务/attempt/回执和费用账本；聊天上下文只能引用执行证据，不修改事实 |
| 请求准入 | 准备实际请求后再进行模型交换 | 同一 `build_request` 服务于上下文测量与实际发送；大小校验在费用预留前完成 |
| 工具展示与执行 | 可见 schema、运行值和显示结果分层 | 完整契约保留供严格校验，模型先见简介；完整结果与模型摘录分别保存 |
| 权限 | 作用域限制与不可覆盖的拒绝 | 继续 Core/owner/Node/本地交集、摘要和期限；模型不可通过描述或结果重新授权 |
| 取消 | 传递取消信号并等待执行收敛 | 保留真实 Provider/网页连接关闭与 Node 停止/unknown；不能照搬本地取消来推断远端未执行 |
| 恢复 | 日志记录边界，失败步骤补齐结果 | 沿用当前事务及持久回执；已成功的结果即使无法进入模型上下文也不能丢失 |

DSH 的 JSONL 会话日志与 NEXUS 的任务权威表解决的问题不同。替换数据库或整个事件系统需要历史迁移、协议和生产恢复方案；本轮采用局部改进，不作这类替换。精确跨版本重建每一笔历史 HTTP 请求还需要不可变请求信封设计；本轮共享组装代码不等于已实现该能力。

## 上下文与结果投影

1. 请求大小包括 system、实际工具 schema、标题要求和消息，按真正发送的 UTF-8 JSON 字节测量，完整请求上限仍为 32 KiB。字节数不是精确 token 数；原 token、费用与调用次数保守上限继续保留。
2. 普通聊天选取近期整轮问答时也计算实际请求大小。已同意的记忆和当前输入不会被静默削短；无法容纳时在本地拒绝，尚未调用的请求不新增费用预留。
3. 工作结果增加压力时，先移除最旧的完整普通问答；不删除当前输入、所选记忆、assistant 原生调用或 tool 回复，不改变 `tool_call_id` 和调用参数。
4. 仍超限时，按原结果顺序将过大的 `data` 投影为有明确标记的 JSON 文本首尾摘录。结果 envelope 的 status、error、verification、费用保留，网页来源链接保留原值。摘录不是可重新解析的完整数据，不能用于全量统计。
5. `chat_work_steps.result_json` 保留完整有界结果，`work_messages_json` 保存实际模型投影，裁剪写入审计并由已有上下文提示告知用户。任务、Node 回执及原始上下文快照不改写。
6. 连最小上下文仍无法容纳时，保存已发生的真实工具结果后停止本轮；不会把成功工具改记为未执行，也不因此重跑。只更新模型投影，不自动写长期记忆或追加摘要模型调用。

## 工具适配与适用性

| DSH 工具/机制 | NEXUS 对应与取舍 |
| --- | --- |
| `skill` 按需加载 | 适配 `inspect_capability(version_id)`：只查看已入本轮目录的已批准固定 Tool/Skill 输入契约；执行前重查版本、启用及会话权限，不运行能力 |
| 原生工具 schema | 去除显示用 `title`，保留字段名、required、枚举、边界及拒绝未知字段；属性本身叫 `title` 时仍保留 |
| 目录和工具指导一致 | 提示只指导当前提供的网页/能力工具，并声明本轮剩余动作数；不提示调用未提供的网页工具 |
| `read` 分段带行号 | 值得适配。当前 Node 只返回有限 UTF-8 前缀；分页需要新契约、结果验证和 Core/Node 版本协商，不能仅在提示中假装支持 |
| `glob` / `grep` | 文件名搜索已存在；内容搜索仍有价值。DSH 的固定 `rg` 参数向量值得借鉴，但不能直接复制忽略文件扫描策略或绕过 Node 路径/链接保护 |
| `edit` / `str_replace_editor` | 局部精确替换可减少模型反复输出全文；需要唯一匹配、读取版本绑定、实际差异、旧内容检查及原子写入，不能只新增字符串替换入口 |
| `web_search` / `web_fetch` | 已有 Core 工具，继续固定端点、共享预算、来源链接和外部内容隔离；不再重复新增同名网页执行器 |
| `todo_write` / plan | 复用已有项目待办与证据要求；模型声明 completed 不能替代真实成功任务，不建立另一套完成状态 |
| `ask_user` | 现有聊天答复与权限确认可复用；业务澄清和审批必须分开，不能让普通自然语言答复授予新权限 |
| `session_search` 等历史查询 | 需按项目/owner、记忆撤销和逐轮外发同意设计；不自动搜索并外发其他会话 |
| shell / terminal / `run_code` | 任意程序执行与当前固定命令/隔离 JSON 工具边界不兼容；不作为普通工具直接迁入 |
| goal / schedule / jobs / subagents / workflow | 涉及后台生命周期、预算、多个规划者或 M7 范围；可参考设计，需独立确定行为及授权，不随本轮启用 |

`inspect_capability` 占一个工作动作和一次底层操作；后续模型交换仍计入原 5 次、每轮 USD 0.05 和每日共享上限。只有扩展目录存在合格版本时才提供该工具。调用详情属于契约数据，不能授予权限或替代版本批准。原 `run_tool` / `run_skill` 仍用完整输入 schema 严格校验，目录瘦身不放松执行检查。

候选工具的具体任务与后续顺序统一在 ROADMAP 维护；本表是兼容性评估，不构成实施或部署批准。
