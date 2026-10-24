# ADR 0031：参考 DSH 的聊天输出配置及预算收紧

日期：2026-10-23。状态：已采纳，用于本次后端修改；本地验证、部署与用户验收统一记 [ROADMAP](../ROADMAP.md)。

后续明确授权：[ADR 0032](0032-owner-cost-limits.md) 按用户要求关闭生产金额限额并部署，替代本决定中 USD 金额限制和“本轮不部署”的范围；其他边界继续保留。

后续参数处理：[ADR 0033](0033-tool-argument-feedback.md) 将完整工具信封中已知的参数拒绝作为未执行证据，在原交换上限内允许修正；截断和未知结果的停止语义保持。

用户在两次网页生成停于 2048 token 后，明确要求核对 DSH 上限并参照修改。线上只读记录确认两次生成恰好用满 2048；失败响应没有保留原始工具参数，因此不把具体参数截断位置当成已知事实。本地合成 `finish_reason=length` 的工具批次可复现 `MODEL_RESPONSE_INVALID` 误报。

## 参考证据

核对此前固定的 DSH 官方提交 [`687f282e8625c6b8d11ff58211a90524be2e4317`](https://github.com/deepseek-ai/deepseek-harness/commit/687f282e8625c6b8d11ff58211a90524be2e4317)：[defaults.ts](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-deepseek/src/defaults.ts) 的 `DEFAULT_MAX_TOKENS = 256_000`；[config.ts](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-deepseek/src/config.ts) 允许配置及模型/请求覆盖。[agent.ts](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/agent.ts) 在 `max-tokens` 结束时返回，不继续执行该批工具。

本机 `D:/DeepSeekHarness/resources/app.asar` 中 `dsh/node_modules/@deepseek-ai/dsh-llm-deepseek/lib/index.js` 同样包含 `DEFAULT_MAX_TOKENS = 256e3`。这是安装版 Provider 默认值，不代表所有用户会话都没有覆盖，也不同于 Codex → DSH 协作桥的 4096 token 限额。本次未启动 DSH 模型任务。

## 决定

1. 移除聊天的 2048 硬上限；`chat_max_output_tokens` 默认和可配置最大值为 256000。M2 的 512 与扩展生成的独立限额保持各自语义。显式旧配置继续生效，不自动重写生产配置。
2. 配置上限与实际请求额度分开。每个请求使用 `min(配置上限, floor((剩余费用 × 1000000 − 32768 × 输入单价) / 输出单价))`，价格单位沿用 microunits/百万 token。保守输入上界不变，剩余输出少于 32 时拒绝新调用。默认价格/USD 0.05 对应首次 33474 token，压缩额外收紧至 1024。整数量值保证向上取整结算也不超过预留。
3. 普通回复、结果总结、工作循环、压缩和插件目标续跑复用同一预算规则；最终插件根预算检查后才冻结实际 `max_tokens`。请求日志与真正发送的正文来自同一序列化路径。未知费用继续占用预算，不自动重试。
4. Provider 收到完整 `length` 结束与有效用量后，保留文字、标题、终止原因及用量，返回不完整回复；不解析或创建其中任何工具建议。Core 沿用 `incomplete / MODEL_OUTPUT_LIMIT`，清理未开始的工作，不继续付费调用；其他非法终止、参数和未知结果仍拒绝。
5. 正文在 Provider、增量落盘及终态统一限制为 1 MiB；SSE 字节/事件边界按实际输出额度有界调整，单事件行仍限 16 KiB。日志为可能的 JSON 转义及独立 partial_text 留容量，保留总存储限制。单文件 12000 UTF-8 字节、工具参数/批次、32 KiB 请求和 Node 协议不扩大；工具说明提示较大网页拆文件。

## 边界

本次不改变 USD 0.05/轮、USD 1/日、100 次/日、4 动作/5 次交换、30 秒模型总时限、授权与文件路径保护。不增加依赖或迁移，不调整前端或 Windows 桥，不自动部署生产，也不重试用户失败的网页任务。提升 token 上限不保证长任务能在时间、文件和请求容量限制内完成。
