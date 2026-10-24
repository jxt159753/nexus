# ADR 0030：百炼实时语音与 Core 媒体代理

日期：2026-10-22。状态：**选型已确认；V1 Core 骨架、Windows 运行时骨架、本地 KWS 唤醒、真实百炼 TTS/ASR 合成音、真实麦克风录音回放和 Windows 运行时端到端冒烟（fake Chat）已通过；真人实时麦克风/AEC/主动权限/部署未验收**。用户确认：Core 部署在青岛，设备经公网连接，Core 是小服务器（2 vCPU/200 Mbps）；ASR 用 `fun-asr-realtime`，TTS 用 `cosyvoice-v3.5-flash`，语音输入输出均经过 Core；已在阿里云百炼控制台用声音设计创建音色。

本文件只记录架构选择；依赖、接口、验收的完整细节见 [实时语音扩展](../realtime-voice.md)及[验收标准](../realtime-voice-acceptance.md)。本文替代该方案中“本地 ASR/TTS”的主路径；本地模型仅保留为可选离线备选。

## 决定

1. **Core 是语音媒体代理**：设备只负责采集、AEC、VAD、本地唤醒、播放；麦克风音频上行到 Core，Core 调 Fun-ASR；Core 调 CosyVoice 合成，音频下行到设备播放。设备不直连百炼，不持有百炼 API Key。
2. **ASR/TTS 模型**：`fun-asr-realtime-2026-02-28`（可固定快照）与 `cosyvoice-v3.5-flash`。Core 在青岛，使用百炼**华北2（北京）**工作空间端点；Workspace ID 必填，API Key 只从 Core 环境变量 `DASHSCOPE_API_KEY` 读取。
3. **音频通道**：控制仍走 `/ws/v1/voice` JSON；新增独立 `/ws/v1/voice/audio` 二进制通道。第一版上行 16 kHz mono PCM16、100 ms 一包（约 256 kbps），下行 CosyVoice MP3 64 kbps；设备解码播放并以其作为 AEC far-end。Core 不做转码，只做有界转发；后续带宽紧张再评估 Opus。
4. **小服务器/公网**：Core 为 2 vCPU、200 Mbps 公网的小服务器；默认 **1 个并发语音会话**，每会话约 320 kbps，带宽不是瓶颈，CPU/事件循环和稳定性是主要约束。200 ms 设备播放缓冲；有界队列，溢出即停止会话，不静默丢帧。公网断线不重放音频，只重建控制连接。
5. **本地只保留 KWS/VAD/AEC**：移除本地 MeloTTS/ASR 主路径（约 377 MiB 模型不再默认下载）；待机唤醒仍本地，唤醒后才开始上行。断网/百炼不可用时没有本地 ASR，回退文字。
6. **语音设计**：使用用户在百炼控制台创建的 `voice_id`；确认该音色在 `cosyvoice-v3.5-flash` 下可用。设备端可加轻板混响/合唱（可关闭），但默认不重处理，保持清晰。
7. **权限与费用**：ASR 按秒计费（¥0.00033/秒，约 ¥1.19/小时），TTS 按字符（¥0.8/万字符）；新增提供商费用账本与字符/时长预算；沿用 owner 权限、主动 grant、DND、停止/断连语义。唤醒词不授予操作权限。
8. **停止/打断**：设备本地 VAD 立即停播；Core 关闭当前 CosyVoice task、取消 Chat 生成；Fun-ASR 会话按新 utterance 继续。已生成/已发送的百炼调用可能计费，不承诺退款。
9. **隐私**：原始麦克风音频经 Core 到阿里云，Core 不落盘；final 转写仍作为聊天记录保存；供应商的数据保留按百炼条款处理，需用户明确同意。不再宣称“音频完全不出设备”。
10. **失败处理**：百炼 401/403/429/超时/断流 → 停止语音、记录原因、回退文字；不自动重试已扣费音频/TTS，不重放旧内容。

## 影响

- 设备端模型从约 377 MiB 降至 KWS/VAD 约 6 MiB，CPU/RAM 降低；Core 增加公网音频转发、百炼连接和费用账本，CPU/带宽/稳定性成为新瓶颈。
- Android 复用同一 Core 音频通道和协议，不需要本地 ASR/TTS；仍需本地 AEC/KWS/VAD 和 Opus/MP3 解码能力。
- 现有 P63 文档中的本地 ASR/TTS 章节降级为离线备选；验收新增公网时延、带宽、百炼故障、费用和 Core 资源项。

## 待配置

- 百炼 Workspace ID（北京工作空间）。
- 声音设计生成的 `voice_id`，并确认模型为 `cosyvoice-v3.5-flash`。
- Core 环境变量 `DASHSCOPE_API_KEY`（不要写入仓库/聊天）。
- 小服务器为 2 vCPU/200 Mbps；默认 1 并发，带宽余量充足，优先观察 CPU、事件循环和百炼 RTT。
- 用户确认：待机唤醒仍本地、断网回退文字、默认预算（30 分钟 ASR/天、2 万字符 TTS/天、每轮 1000 字符）。
