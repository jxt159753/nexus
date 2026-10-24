# ADR 0029：实时语音扩展（常驻音频运行时与分离的主动权限）

日期：2026-10-22。状态：**提议（Proposed）**，仅方案与接口需求，不含实施授权。完整设计、Core 接口、依赖模型和验收见 [实时语音扩展](../realtime-voice.md)及[验收标准](../realtime-voice-acceptance.md)。完成状态只记 [ROADMAP](../ROADMAP.md)。

## 背景

用户要求为 NEXUS 增加可启停、可配置的实时语音扩展，第一目标 Windows，接口供 Android 复用。需要按钮/本地唤醒/Core 主动发起、连续多轮、流式识别、分段播报、插话打断、回声/自唤醒/麦克风竞争/设备切换处理、待机本地唤醒且不触发模型、默认不保存原始录音、主动播报与主动收听分别授权，以及停止/断线时真正停止收音和播放、重连不重放过期语音。

现有系统是文本聊天 + 离散工具执行：Core 是唯一权限/任务/模型/记忆权威；Node 是单执行槽、10 秒工具时限、32 KiB 结果和持久回执的设备执行端；插件 `plugin.call@1` 是请求/响应工具调用。实时音频的连续、双向、低延迟和过期即弃语义与工具结果不兼容。

## 决定

1. **独立常驻进程**：新增 `nexus-voice` Windows 运行时，作为与 `nexus-node` 并列的设备进程；不把音频线程塞进 Node，不把语音注册为插件工具。Node 继续只执行离散工具。
2. **本地优先**：第一版 ASR/TTS/KWS/VAD 全部在设备本地运行；Core 只通过独立 `/ws/v1/voice` 接收 partial/final 文本、发送待播报文本，不接收/转发音频。音频不进 `ToolResult`、`command.result` 或 `plugin.call@1`。
3. **独立控制协议**：新增严格 `VoiceMessage` 联合和 `parse_voice_message()`，不扩展现有 `parse_message()` 的 Node/客户端联合。消息带 `voice_session_id + session_epoch`，播报带 `generation`；旧值拒绝。
4. **本地唤醒**：待机只运行本地 KWS + VAD；命中只发 `voice.activate`，不发送音频、不创建任务、不授予工具权限、不调用模型。首版唤醒词为“Hey NEXUS”+“你好奈克斯”，不承诺单独“NEXUS”。
5. **语音轮次复用 Chat**：final 文本通过 `ChatService` 创建普通 Chat 轮次，复用预算、工具、记忆、审计、取消和恢复；新增增量观察者与 `voice_session_id/voice_utterance_id/input_modality/initiated_by` 元数据。语音默认 `allow_tools=false`；唤醒/语音不能绕过工具或写入授权。
6. **主动权限分离**：`proactive_speak` 与 `proactive_listen` 是两个独立 grant，默认关闭；绑定设备范围、来源、有效期、DND、日配额和是否允许模型生成。DND 默认建议 22:00–08:00 Asia/Shanghai 但不默认启用；用户按钮/唤醒默认不受 DND 影响。full 模式不隐含主动语音权限。
7. **停止/断线 fail-closed**：运行时在 WS 关闭、按钮停止、Core stop、全局 pause/cancel_all、设备丢失时立即关麦/停播/清 TTS 与 ASR 队列；Core 取消语音 Chat 轮次并保留费用未知。重连只重建控制连接，旧 epoch 永久失效，不重放 TTS/播报/音频/旧 final。
8. **模型与许可固定**：KWS/ASR 用 Apache-2.0 的 sherpa-onnx 模型，VAD 用 MIT Silero，TTS 用 MIT MeloTTS fp32；模型逐文件 SHA-256，Core 通过 `TrustedNode.voice.model_digests` 绑定，不自动升级。参考 sherpa-onnx `51ec64f7...`（Apache-2.0）与 Pipecat `3c9da9d8...`（BSD-2-Clause）的具体文件，但不复制其 Agent/管线框架。
9. **Windows 第一版**：WASAPI 共享模式；PyWebRTC AEC3 + NS；AEC 不可用降级半双工并明确显示；设备用稳定名称 + index，活动会话设备丢失即停止，不静默切换。Android 只共享 JSON 协议/模型清单/权限语义，Kotlin + sherpa-onnx AAR 另立阶段。

## 后果

- 新增 Core 模块、迁移 027、`/ws/v1/voice`、`VoiceConfig`/`TrustedNode.voice`、语音运行时常驻进程和 Windows App 子进程管理；不改变现有 Node 工具协议。
- 新增运行依赖 `sherpa-onnx`、`pywebrtc-audio`、`sounddevice`、`samplerate`、`numpy`，独立 lock，不并入 Core；模型下载约 403 MiB、安装后约 377 MiB，不随安装包默认分发。
- 需要真实麦克风验收；合成音/文件回放只能作模拟回归。性能目标、设备环境和未验项按验收文档记录。
- 主动语音权限变更会使相关会话失效；已发生的播放/收音不能回滚，只能停止后续。转写仍作为 Chat 记录保存，原始音频不保存。

## 参考（固定）

| 项目 | 提交 | 许可证 |
| --- | --- | --- |
| k2-fsa/sherpa-onnx | `51ec64f7c401997bcbf76a50d96d5aa7fc1dae58` | Apache-2.0 |
| pipecat-ai/pipecat | `3c9da9d83d038ef82c272bf234856d9543f68888` | BSD-2-Clause |

具体文件、借鉴点和“不照搬”的边界见[实时语音扩展第 12 节](../realtime-voice.md#12-参考实现sherpa-onnx-与-pipecat)。
