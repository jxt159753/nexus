# ADR 0011：首轮命名与聊天内受约束工具调用

日期：2026-10-18。用户明确要求按首条输入命名、支持工具、完成后上传 GitHub。状态见 [ROADMAP](../ROADMAP.md#11-m3-补充首轮命名与聊天工具)。

首轮沿用一次 SSE 请求，要求先返回一行有限的 title JSON 元数据，再返回普通正文或提出原生函数建议。解析器剥离有效标题后继续显示真实增量；无合格标题时保留首条输入的短名称。标题最多 40 字，用户在生成期间手动改名也不会被覆盖。不为命名追加模型调用。

函数仅有 get_system_info、list_directory，映射原 system.info@1、filesystem.list@1。浏览器逐轮明确允许建议并选择节点，API 默认不开启。模型不能选节点、访问任意路径或创建新工具。格式依据：[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)、[Chat Completions](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)。Core 聚合至多一个函数及有限 JSON 参数，完整终止和最终用量通过后才保存建议，继续本地严格校验。

动作摘要绑定轮次、节点、工具版本、严格参数、权限版本、资源范围摘要和期限。归档、过期、取消、摘要或策略变化阻止新确认。显式确认后复用 Harness；任务、轮次和模型账本关联同事务提交，稳定 request_id 避免重复创建。目录继续要求原任务审批。会话和任务页复用同一结果卡，成功仍需真实工具证据。结果展示不追加模型总结或自动外发，不向后续 messages 塞入悬空的 tool_call。

迁移 008 仅新增标题标记及建议/任务关联字段，旧会话默认保留名称。沿用唯一 Core、备份、未知费用保留和控制语义；每天 USD 1、每轮 USD 0.05、每日最多 100 次、每轮一次模型交换及 2048 token/30 秒限额不变。自扩展仍未批准；本轮 GitHub 上传不切换生产版本。
