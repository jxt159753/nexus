# 聊天过程展示与最终总结：前端适配

本说明记录共享展示契约，可供用户直接指挥 DSH 适配前端。2026-10-26 用户明确授权本次由 Codex 修改三端并部署，见 [P102](ROADMAP.md#p102-chat-collapse)。批准、开发、上线与验收状态只记录在 [ROADMAP](ROADMAP.md)；总体分工沿用[前端工作说明](FRONTEND-DSH-WORKORDER.md)。本次选择展示公开的分析摘要、进度和工具步骤，不接入模型原始推理文本。

## 体验与职责

执行时展开“处理过程”：显示简短分析摘要、当前状态、真实工具步骤及正在生成的文本。最终反馈完整落盘且执行已结束后，将回答单独显示在过程之前，并自动折叠过程为“查看处理过程 · N 步”。执行中手动展开不阻止完成时收起；结束后重新展开会跨轮询保留，历史初次加载默认收起。用户仍可展开查看；折叠不删除历史或工具证据。普通聊天没有过程记录时直接显示回答，不造一个空面板。

Core 从原聊天、逐次交换、任务、执行尝试和沙箱记录生成只读展示投影。前端负责布局、展开/折叠和交互状态。原 Harness、工具验证、预算、权限、停止和恢复机制继续负责执行，不引入第二套任务状态机。

不需要新增“完成任务”Tool：最后一次正常回复承载总结，Core 根据已有记录判定本轮是否结束。新增 Tool 也不能在任务完成后倒推生成实际进度，不能替代完成验证或保证模型一定调用。以后如确需固定格式的报告，可单独评估报告提交接口；本批没有增加此工具。

## 现有接口新增字段

复用已有认证、HTTP 轮询和请求，不新增 WebSocket 或 SSE 客户端协议：

- `GET /api/v1/chat/conversations/{conversation_id}/turns`：每个 `turns[]` 增加 `presentation`。
- `GET /api/v1/chat/turns/{turn_id}`：`turn.presentation` 相同。
- 原提交、停止及其他返回轮次详情的接口也通过同一投影返回；旧字段保留。

示例为工具执行及回答均已结束的快照，`process[].id` 实际为交换调用 ID：

```json
{
  "version": 1,
  "phase": "completed",
  "label": "回复已完成",
  "process": [
    {"id": "exchange-id", "text": "先查询设备连接状态，再汇总实际结果。", "truncated": false}
  ],
  "process_truncated": false,
  "live_text": "",
  "final_text": "设备当前离线，未执行设备操作。",
  "collapse_process": true
}
```

| 字段 | 前端使用方式 |
| --- | --- |
| `version` | 当前为 `1`；不支持的版本回退既有轮次展示，不能自己推定完成 |
| `phase` / `label` | 当前展示阶段与公开状态文案；阶段不等同于工具业务结果 |
| `process` | 按时间顺序的已有公开回复摘要，至多 8 条、每条 1200 字符；按 `id` 替换，禁止在轮询时重复追加 |
| `truncated` / `process_truncated` | 对应摘要/条目被裁剪时提示；完整内容仍可从原日志查看 |
| `live_text` | 当前交换的真实增量全文快照，及停止/失败后的部分回复；按快照替换，不用计时器伪造生成 |
| `final_text` | 本轮最终回答或自然语言错误反馈；失败时可能是确定性兜底，不保证来自 LLM，也不表示工具成功。`null` 表示尚无最终反馈；不能把旧 `response_text` 自行补成总结 |
| `collapse_process` | 首次历史加载或该值从否变是时收起一次；此后轮询保留手动选择，不代表删除或工具成功 |

`process` 不包含请求体、system prompt、函数参数、工具输出或供应商推理文本。工具步骤继续读取原 `turn.work_steps`，具体执行证据与审批仍用原任务/Skill 卡；`process` 没有文本时也可展示真实步骤。过程摘要裁剪不影响原记录、后续上下文或工具证据。

阶段值：

| `phase` | 行为 |
| --- | --- |
| `queued` / `responding` | 等待或生成回复；有实际步骤后的下一次回复可显示“正在整理总结” |
| `working` | 等待或正在执行工具；此时底层 `turn.status` 可能暂为 `completed`，不能据此折叠 |
| `awaiting_input` | 展示原审批、权限答复或工具确认，操作入口保持可见 |
| `recovering` / `unknown` | 明示需核对/结果未知，不自动续发请求或假称成功 |
| `stopping` | 正在停止回复或真实子执行；不等于执行已停止 |
| `completed` | 当前回复流程已结束；工具具体成功与否仍看原步骤证据 |
| `stopped` / `incomplete` / `interrupted` / `failed` / `expired` | 保留最终反馈和简短原因；执行已结束且有最终反馈时可收起过程，不表示工具成功 |

已知失败的工具可以有解释性最终回答；有最终反馈且无未停止执行时 `collapse_process=true`，失败状态保留，过程仍可展开检查。停止后若真实子执行尚未结束，展示阶段继续为 `stopping`；未知结果不成为最终成功。旧版手动结果回传未获同意时，不额外调用模型，也不把原工具建议当作执行后的总结。

2026-10-23 错误反馈扩展：最终回答/本轮反馈放在处理过程之前，失败时也保持可见；只有 `responding` 的 `live_text` 标记“正在生成”，停止或失败片段标记“已保留的回复片段”。状态栏使用自然语言，错误码和原始 JSON 放入闭合的技术详情。用户本次明确授权 Codex 直接修改 Web、Windows 随包 UI 和旧控制台；下方 DSH 工作单保留为此前接入说明，不是本次委派要求。模型解释与兜底边界见 [ADR 0036](decisions/0036-natural-failure-feedback.md)，实际验证和部署状态见 ROADMAP 的 P76。

## DSH 需要完成的界面适配

可在 DSH 中发送：

```text
请在你当前负责的 NEXUS 前端批次中适配 docs/CHAT-PROGRESS-FRONTEND.md。
先读取工作区实际接口、contracts/chat.py 和 core/cognitive/chat_presentation.py。
Codex 本批只实现了 Core 展示投影和模型的公开进度/最终总结指导，未修改你的 UI 文件。

聊天轮次使用 presentation v1：处理过程中展开公开摘要、状态和原 work_steps；
live_text 按快照替换；final_text 出现后在独立的最终回答区域显示，
按 collapse_process 的默认建议折叠过程，保留“查看处理过程”和手动展开。
折叠只在完成转换或初次加载历史时设置默认；后续轮询不得覆盖用户手动选择。
普通聊天没有过程时不生成空面板；失败、未知、审批和停止中的提示/按钮保持可见。
不要依据 turn.status=completed 或文本中的“完成”自行判定结束。
子执行状态可以在 turn.revision 不变时变化，不能仅凭相同 revision 跳过投影更新。
旧 Core 没有 presentation 时保持现有展示，不能模拟新后端能力。

改动你当前拥有的前端组件，并核对 src/nexus/web/app 与 apps/windows/ui 的实际打包/同步关系；
如旧控制台 src/nexus/web/chat.js 仍在交付范围，也适配对应轮次展示。
不要改 Core 状态机、认证、数据库或 Node；不要调用模型来控制折叠。
在隔离测试环境覆盖：多工具、最终回答流式中、完成后折叠/手动展开、
刷新/重连恢复、停止真实执行、已知失败、未知、审批、旧 Core 回退、长文本和手机布局。
提供实际检查结果和未验证项。未经另行授权不变更生产或自动发送付费请求。
```

`presentation` 是整个快照的一部分。前端缓存、刷新和重连时以新快照替换；子执行仍在停止时继续同步，不能因为聊天的活动轮次标识清空而永久停止查询。恢复或展开历史只读记录，不重新发起任务、模型总结或 Tool 调用。

## 后端验证入口与限制

新增用例见 `tests/test_chat_presentation.py`；相关回归见聊天、工作、日志、失败及旧结果续接测试。测试使用合成 Provider 和临时 SQLite/执行记录，不产生真实供应商调用。实际检查结果以路线图本次记录为准。

以下为 P76 时点记录；当前三端版本和验证见 [P102](ROADMAP.md#p102-chat-collapse)。原过程投影批次未包含 DSH 界面、Windows 安装包或 Android；P76 后续直接修改 Web、Windows 随包 UI 和旧控制台，Core/托管 Web/旧控制台已于 2026-10-23 部署。Windows 随包界面仅完成本地源码，未构建新安装包。浏览器验证见 `tests/test_chat_feedback_browser.py`，发布核对见 [ROADMAP 的 P76](ROADMAP.md#p76错误自然语言反馈)；真实模型表述质量仍需在授权范围内实测。展示投影不调用模型，错误解释按 ADR 0036 使用原轮次剩余交换额度。
