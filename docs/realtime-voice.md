# NEXUS Windows 实时语音

本文件说明当前实现和使用方法。批准、部署、验证及未完成项只在 [ROADMAP 的 P63](ROADMAP.md#p63实时语音扩展选型确认待配置实施) 更新；开发自检与用户验收分别记录。早期离线 ASR/TTS、MP3 和自动重连设想已由 [ADR 0030](decisions/0030-bailian-voice-core-media.md) 和 [ADR 0038](decisions/0038-windows-voice-loop.md) 替代，不能据旧提案配置当前程序。

## 1. 使用

使用更新后的 Windows App，登录原 Core，保存本机 Node 配置。在聊天输入区点“语音”，核对输入/输出设备、音频外发提示、上下文及工具选项，再开始。设备枚举不会打开麦克风。

点“语音”可直接进入收听；点“语音唤醒”并确认后，麦克风持续开启，在本机识别“你好奈克斯”或“Hey NEXUS”。命中前不上传音频、不启动云端ASR或聊天模型；命中后听到短提示音再说话，提示音表示Core已经准备好收听。待机缓存与唤醒词不作为预卷发送，只保留命中后的音频。回答结束会继续收听，按钮会话静音30秒结束，唤醒会话静音后回本地待机。最长30分钟、30轮，界面显示自动关闭时间，单次收听最多60秒。App最小化或收起到托盘后，独立音频进程继续待机；托盘“退出”会关麦。每次启动App仍需手动开启，不自动恢复。

“打断”立即清空当前播报并取消原回答；AEC可用时也可直接插话。AEC初始化失败明确显示半双工，使用打断按钮再说话。点“结束语音”、退出登录、切换会话或关闭 App 会停止真实采集/播放。断线和默认音频设备变化会结束本次会话，需重新开始，不自动开麦。

音色使用经试听选定的原创轻透、空灵、成熟克制女声，运行于 CosyVoice 3.5 Flash 专用 voice_id，语速与音高均为1.0，不叠加电子回声效果。早期C参考及机械效果样本已被替代；用户确认当前方向接近，并确认三轮声音和续聊正常。部署配置记录 voice_id，失败不会静默换声；长期听感仍可继续微调。

## 2. 数据与权限

麦克风音频经 Core 送往阿里云百炼；final转写、选定的近期上下文与获准工具目录/结果按原聊天链路送往现有模型。原始录音不落盘，partial不写入持久聊天；final与回答可在同一会话查阅。供应商处理范围依各自服务条款。

语音使用原文字聊天的 ChatService、工具目录、normal/full、审批、费用和取消规则。唤醒词不授予工具权限，语音回答“同意”不替代界面审批。记忆、图片/附件和压缩同意保持独立；只有另行有效的逐轮或持续同意才适用，不因开始语音自动授权。

主动播报与主动收听分别勾选，默认关闭；仅在本次唤醒会话、原设备及期限内有效。主动播报处理本会话开始后生成的新提醒，免打扰为北京时间22:00–08:00，每日最多10次。没有独立主动收听许可时，播报后不启动云端ASR；获得许可后最多8秒无语音回本地待机。待机麦克风仍用于本地KWS，这与音频上传权限分别显示。

## 3. 架构和延迟

Windows App 原生桥从 Windows 凭据管理器读取既有 Node 身份，启动独立 nexus-voice.exe，经有界 stdin/stdout 传配置与状态；令牌不进入 JavaScript。Node工具进程与音频进程并列，Core是唯一任务/权限权威。

链路为：48 kHz采集 → AEC3/降噪 → 16 kHz PCM16，默认40 ms一包 → Core Fun-ASR → 原 ChatService增量 → 短句流入同一 CosyVoice双向任务 → 22.05 kHz PCM16 → Windows重采样到48 kHz播放。无本地ASR/TTS或GPU依赖。

首段在模型整段完成前启动，长文本最多朗读1000字符/180秒，代码和详细资料留在聊天界面。TTS接收和文本发送并行，必须取得供应商结束事件，不能以短暂静音推定完成。

下行NXV2帧含generation和结束标记；打断增加代次，设备丢弃旧帧。播放缓冲顺序且有界，AEC远端参考来自实际输出回调。设备真实播放结束并等待输出延迟后回ack，Core才恢复收听。插话先本地静音，再取消网络工作，有界预卷减轻重新开始ASR时丢首音。目标值和声学测量要求见 [验收标准](realtime-voice-acceptance.md)。

## 4. Core 配置和接口

[Core示例](../config/core.example.toml)中启用voice并登记节点voice许可，密钥只从Core环境变量DASHSCOPE_API_KEY读取。[百炼配置片段](../config/voice-bailian.example.toml)只包含合法Core字段；不要交给Windows运行时读取。

| 接口 | 用途 |
| --- | --- |
| GET /api/v1/voice | owner读取启用状态、许可设备、活跃会话、用量和披露 |
| POST /api/v1/voice/sessions | Cookie/Origin/CSRF绑定本次外发同意、会话、节点、request_id |
| POST /api/v1/voice/sessions/{id}/interrupt | 打断本轮，不自动重试旧工作 |
| POST /api/v1/voice/sessions/{id}/stop | 结束并取得真实设备停止证据，未收到不伪造 |
| POST /api/v1/voice/sessions/{id}/announce | 在独立许可/DND/配额内播报有界文本 |
| /ws/v1/voice、/ws/v1/voice/audio | Node认证的独立控制/PCM通道；不能伪造识别final |

真实百炼连接必须先有owner准备的激活请求；Node握手绑定原request_id，再由按钮或唤醒激活。未连接的准备会话30秒释放，重试不重复创建付费调用。027迁移保存会话、final、播放/停止证据、独立音频费用估计和未知；无新增数据库或消息服务。

## 5. 有界费用和恢复

ASR按音频秒、TTS按文本字符预留，分别记录估计及unknown，不伪装为供应商精确账单。默认每日1800秒ASR、20000字符TTS；LLM仍进入原model_calls。关闭金额限额不取消次数、音频配额、权限、超时及审计。失败和未知请求不自动重复外发。

Core启动将旧活动会话标记interrupted、未决音频预留保留unknown。全局暂停/取消、owner会话失效、Node撤销、期限耗尽均停止语音。当前聊天自然语言错误说明沿用文字链路，未知执行解释后结束语音，先对账再执行。

## 6. Windows 构建和配置

安装包0.1.1同时包含冻结Node、语音EXE及KWS模型；构建方法见 [Windows说明](../apps/windows/README.md)。常规用户在App选择设备与权限；[独立运行时配置](../config/voice-runtime.example.toml)仅供调试。真实服务独立启动时也须先由owner准备会话，再填写同一个request_id/conversation_id，令牌从环境读取。

默认使用系统音频端点，变化后停止并要求重新开始。明确指定设备名称时失败不自动改用未知设备。只有实际设备验证能证明具体耳机、扬声器、驱动与房间下的回声和延迟表现。

<a id="11-依赖与模型清单"></a>

## 7. 依赖与模型

Core复用已有websockets和原模型/存储依赖，无音频DSP依赖。Windows可选依赖固定在 [requirements-voice.lock](../requirements-voice.lock)：sounddevice、samplerate、numpy、pywebrtc-audio、sherpa-onnx及传递依赖。KWS使用带前缀的完整短语，移除容易误触的裸“NEXUS”。每次回到待机创建新识别流，避免拼接旧对话音频。KWS固定模型文件与许可证见 [模型锁](../config/voice-models.lock.json)，安装资源逐文件核对SHA-256，失败拒绝加载。

模型锁中的本地MeloTTS/ASR资源只供历史离线研究，不随当前Windows包分发。早期调研参考sherpa-onnx与Pipecat的来源记录保留在 [ADR 0029](decisions/0029-realtime-voice.md)，当前实现未引入Pipecat框架。

<a id="12-参考项目与不照搬边界"></a>
<a id="18-百炼-core-媒体代理"></a>

## 8. 验证边界

协议/权限/代次/停止/费用使用本地真实数据库与受控进程验证；浏览器替身不代表麦克风通过。真实百炼试听只证明选定音色可用。真人实时多轮、语音插话、扬声器回声、设备竞争、声学延迟及长时间误唤醒需分别记录。Android只复用契约，不含本轮客户端实现。实际测试结果和未验项只记ROADMAP及脱敏证据。
