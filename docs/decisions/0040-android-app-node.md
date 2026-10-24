# ADR 0040：原生 Android App、独立 Node 与 Core 实时语音

日期：2026-10-23。用户要求开发 Android App（含 Node），明确本轮 Codex 可实现界面，之后可由用户安排 DSH 接手；随后明确语音沿用 Windows 的 Core 实时语音。交付及验收状态只在 [P80](../ROADMAP.md#p80-android) 记录。

## 决定

- Kotlin / Compose 单 APK，`:app` 与纯 JVM `:protocol` 两模块；App 内按 HTTP、UI、Node、凭证、语音分包。暂不创建草案中的六个模块或 Room 缓存层，避免少量首版功能产生空抽象。Node 回执用 SQLiteOpenHelper，UI 仅内存投影。
- 执行服务和通知监听使用独立 `:node` 进程。单执行槽、接收先落盘、结果先落盘、确认后保留幂等墓碑；重启的 received/started 变 unknown，不重新执行。每个原生只读调用有超时；Android Binder/存储卡住时 watchdog 只结束本 App 的专用 Node 进程，回执留待 Core 对账。
- 复用协议 v1，新增 `android.device_status@1` / `android.notification_digest@1` 与精确资源 `android:device` / `android:notifications`。静态配置、owner/node 持久授权、运行时广告和本机策略取交集。不把 workspace 授权扩展到手机资源。
- 设备状态沿用持久只读预授权。通知默认允许名单为空，只处理启用后新回调，不查询 activeNotifications；隐藏 secret 通知、验证码/密码内容和 4–8 位独立数字，最多缓存 100 条、返回 20 条/32 KiB。摘要 key 对系统通知键及时间做 SHA-256，以区分同包同 ID 不同 tag；不返回原键或通知 actions。
- 当前通知内容需要具体任务审批，包括 full 模式。自动审批审查拒绝将该新工具改为 full 免确认，认为这会扩大敏感通知权限；本轮保留较严格实现，未绕过此限制。
- Android Keystore AES-GCM 分开保存 owner Cookie/CSRF、Node 凭证、草稿及待核对请求；禁用备份及设备转移。普通网络失败不重发写请求，明确点击才用原 ID/原内容重试可幂等操作。HTTP 操作延迟、消息、结果、队列、回执容量均有界。
- 语音替代旧草案的本地 ASR/TTS：复用现有 Core VoiceConsent、控制/音频 WebSocket 和费用/停止机制，上行 PCM16 16 kHz、下行 NXV2 PCM16 22.05 kHz。手机只提供 AudioRecord/AudioTrack，不新增模型或长期记忆。
- Android 首版语音为前台半双工和按钮打断；不广告未经验证的 AEC、KWS 或后台录音。切后台、锁屏、焦点/路由丢失、停止或断线即关闭音频，不自动恢复。generation 隔离旧播放，实际排空后向 Core 确认。工具与结果外发需该语音会话明确同意。

旧 [App 草案](../ANDROID-APP-PLAN.md) 和 [Core 交接草案](../ANDROID-CORE-HANDOFF.md) 保留作为设计输入；模块数、Room、本地语音、P1 可选端点和结果可选字段不表示已承诺全部实现。当前契约以代码、本文及接入说明为准。

## 真机反馈后的共同设备入口

用户进一步要求无需手选节点、以自然语言识别设备、自动刷新和 Windows 一致风格。核对仓库及已有 DSH 交付后，没有找到另一套可复用的 Android 界面实现，因此在现有 Compose 中采用 Windows 的黑白颜色、N 标识与消息布局，不另建客户端框架。

`GET /api/v1/chat/work-catalog` 不传 `node_id` 时返回有界共同目录和名称；`all_nodes=true` 的 workspace 轮次绑定这份目录摘要。每个可用节点沿用已有工作授权，子授权绑定同一根授权；步骤持久化 `routed_grant_id` 后才执行，预算按根授权计费，撤销与停止继续阻止执行。工具调用函数显式绑定目录中的节点，不能由模型任意换节点。`client_node_id` 仅提示“这台设备”的含义，语音媒体节点与工具执行节点分离。旧显式节点 API 保持兼容。

owner 通过 `PATCH /api/v1/nodes/{node_id}/name` 保存 1–40 字符的唯一名称，需原会话认证与 CSRF；名称不改变身份或授权。迁移 033 保存名称、授权父子关系和步骤节点，沿用已有 032 结构。目录保留插件固定版本绑定；Core 共用的时间、网页和插件候选提交只提供一份，设备相关执行分别绑定。前台自动同步仅发送读取请求，不重放写入或触发 LLM。

## 各客户端统一默认与可发送状态

2026-10-24 追加决定：共同设备目录同时用于 Windows、网页和后续客户端。新工作请求在没有显式设备/旧授权且未指定 `all_nodes` 时，由 Core 默认进入共同目录，仍必须提交目录摘要和结果外发同意。兼容请求不改写既有明确范围。Windows/网页的设备在线状态每 2 秒独立读取，避免被聊天历史请求阻塞；失败标记状态待同步，回到前台立即更新，不用旧数据宣称在线。

Android 输入文字即可点击发送；需要外发同意时就地展示确认，确认后按需创建会话，不用提前新建或展开隐藏权限。有待核对操作、活动回复、语音或归档限制时显示具体原因。空闲连接失效允许只读 GET 在原超时内恢复，写请求仍禁止自动重发。

## 官方依据

[AGP 9.2 兼容矩阵](https://developer.android.com/build/releases/agp-9-2-0-release-notes)、[前台服务类型](https://developer.android.com/develop/background-work/services/fgs/service-types)、[AudioRecord](https://developer.android.com/reference/android/media/AudioRecord)、[AudioTrack](https://developer.android.com/reference/android/media/AudioTrack)、[音频焦点](https://developer.android.com/media/optimize/audio-focus)。平台行为仍需实际设备测试；模拟器不代替厂商后台策略和声学验收。
