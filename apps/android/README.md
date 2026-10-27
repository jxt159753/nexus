# NEXUS Android

手机无障碍操作及用户选择目录的文本读写见[使用与接口说明](../../docs/android-node-control.md)。0.1.15 配合 schema044 将本机开关自动同步到 Core，无需在服务器或会话重复开启；离线保留待同步状态，系统无障碍、通知和目录授权仍需用户操作。注册可说“注册这台手机”，随后会主动询问功能，也可说“这台手机改名叫红魔”。视觉独立开关允许 Android 11+ 按需截屏交给现有 DeepSeek 识别，再通过坐标点击、长按或滑动。当前候选与实际验证见 [P107](../../docs/ROADMAP.md#p107-device-permissions)，手机操作见 [P104](../../docs/ROADMAP.md#p104-android-control)。

新设备可通过会话自动登记，无需手填 Node UUID 和凭据。登录后发送“将此设备注册为节点，命名为客厅平板，允许设备状态、接收文件和语音”；也支持会话改名、备注及权限开关。设备页提供例句和当前许可，聊天显示配置结果及真实连接状态。语音开麦、系统通知读取及本机允许名单仍由用户控制。详见[会话注册设备](../../docs/device-enrollment.md)和 [P95 状态](../../docs/ROADMAP.md#p95-device-enrollment)。下文离线登记为兼容旧部署的手动方式。

文件发送和本机收件箱见[向设备发送文件](../../docs/file-transfers.md)。通过聊天描述发送需求；文件卡片提供预览和打开，设置中的“收到的文件”保留本机收件入口。保存的 `receive_files` 本地选择自动同步到 Core；Android workspace 只映射 App 私有收件区。首次注册未选择该权限时仍由 Core 拒绝接收，不从 Node 默认能力推断授权。

底部保留对话、任务、设备和设置四个入口，提醒集中到顶部。新建、重命名和归档收在会话菜单，输入区的“＋”打开语音及上下文／设备选项，普通处理过程按需展开。设备页先显示本机状态与启停，手机操作权限和高级连接按需展开；设置按人格、隐私与本机文件分组。外发确认、审批、失败提示和停止入口保留。Android 与 Windows 共用随包 Three.js 场景，支持拖动、缩放和点选；窗口宽度达到 600dp 使用侧边导航，聊天区域达到 840dp 展示动态／聊天双栏。窄窗口切换“动态／对话”，动态铺满内容区域、输入框悬浮在底部，键盘出现时发送区上移。当前整理与交付状态见 [P109](../../docs/ROADMAP.md#p109-public-preparation)，跨端状态接入见 [P93](../../docs/ROADMAP.md#p93-mobile-sync)。

设备与活动每2秒分别读取，不等待聊天历史或用户操作；后台暂停，回前台立即同步，失败保留上次数据并标明等待同步。任务使用与电脑相同的最新快照和向前分页，不再从最早100条记录生成场景。`active_tasks` 补充较早仍未结束的任务，`activity.turns` 表示全局活动会话；当前打开的聊天不改变全局动态。完整效果需要配套 Core 和 Windows／网页更新，旧 Core 兼容显示近期任务与当前会话，不声称跨端全局活动已接通。

可视化只接收有界展示快照，不持有凭据、原生 JavaScript 桥或网络权限；隐藏或进入后台停止绘制，退出销毁 WebView。Gradle 的 `syncVisualization` 从共享网页复制固定场景及 Three.js 资源，不引入第二份渲染实现。`AdaptiveUiTest` 检查场景绘制／暂停、布局、键盘与旋转草稿；`MarkdownUiTest` / `FileCardsCoreTest` 检查安全渲染和真实 Core 原件接口。

原生 Kotlin / Compose 客户端与同 APK 独立 `:node` 执行进程。Core 保持唯一任务、记忆、授权和模型权威。当前任务状态、实测证据及待验收项统一见 [ROADMAP P80](../../docs/ROADMAP.md#p80-android)。设计决定见 [ADR 0040](../../docs/decisions/0040-android-app-node.md)。

## 构建和安装

使用 JDK 21、Android SDK 36.1、固定 Gradle 9.4.1 / AGP 9.2.1。依赖版本保存在各模块 `gradle.lockfile`，日常构建不要使用 `--write-locks`。

```powershell
cd E:\NEXUS\apps\android
# ANDROID_HOME 指向自己的 Android SDK；也可在不提交的 local.properties 设置 sdk.dir。
.\gradlew.bat :protocol:test :app:testDebugUnitTest :app:assembleDebug :app:lintDebug
.\gradlew.bat :app:assembleRelease
```

调试包：`app/build/outputs/apk/debug/app-debug.apk`，可侧载测试。发布候选：`app/build/outputs/apk/release/app-release-unsigned.apk`，未正式签名，不能直接作为升级安装包。不要提交私钥/签名密码。不配置应用商店或自动更新；Core 部署状态与下载验证见 [ROADMAP P80](../../docs/ROADMAP.md#p80-android)。

最小 Android 8 / API 26，目标 API 36。Release 只允许 HTTPS/WSS；Debug 仅额外允许 `127.0.0.1`、`localhost`、`10.0.2.2` 明文连接，仍不关闭证书校验。Core 本机模式校验 Host，模拟器联调应使用 `adb reverse` 后的 `http://127.0.0.1:<端口>`。

## 接入现有 Core

1. 备份现有数据库，在获准维护窗口升级 Core。Android 工具需要迁移 `031_android_tools.sql`，共同设备目录和名称需要 `033_node_routing.sql`；连续升级必须包含前置迁移，数据库初始化自动生成经检查的迁移前备份。旧插件唤醒逻辑须显式列名写入 `plugin_wakeups`，兼容 030 的新增列。不要单独改已执行的迁移或重建用户数据库；当前生产版本和实际保留的其他功能查 ROADMAP。
2. 显式在 Core `origins` 中追加 `https://app.nexus.local`，保留原有网页与 Windows 来源。App 使用 owner Cookie 和 CSRF，不把 Node 令牌用于 owner 请求。
3. 使用现有离线管理命令登记独立 Node UUID/令牌；在 `[nodes.'手机UUID']` 声明工具及资源。`config/android-core.example.toml` 是增量示例，不覆盖已有配置。
4. 分别给 owner 和手机 Node 授权；工具与资源必须匹配。示例（替换 UUID，在 Core 的原有配置环境运行）：

```powershell
python -m nexus.core.security grant <owner-uuid> --tool android.device_status --root-id android:device
python -m nexus.core.security grant <node-uuid> --tool android.device_status --root-id android:device
# 只有决定启用通知时才增加以下授权：
python -m nexus.core.security grant <owner-uuid> --tool android.notification_digest --root-id android:notifications
python -m nexus.core.security grant <node-uuid> --tool android.notification_digest --root-id android:notifications
```

5. 在 App 登录 owner，设备页展开“高级连接与本机权限”输入 Node 身份。保存不启动；点击“开启”才启动常驻前台服务。通知允许名单默认空；仅在系统通知读取授权、允许名单及 Core 双侧权限均通过时报告通知能力。
6. 退出登录、设备页和常驻通知提供停止入口。停止 Node 不等于取消 Core 上已提交任务，应另查任务状态。手机省电、Doze、强制停止可能使 Node 离线；不自动开机启动。

Owner 会话、Node 凭证、草稿和待核对请求使用不同文件与 Android Keystore AES-GCM 加密；数据库回执不含 owner 凭证。聊天历史只保留当前进程展示数据。系统备份与设备转移均排除 App 数据。Node 回执可能包含已经返回 Core 的有限通知摘要，保留用于核对，不用于记忆。

手机 Node ID 或 Core 地址与回执数据库绑定。已投入使用后不能直接换身份继续用原数据库；先在 Core 停止/核对该设备的任务、撤销旧身份，再明确清除这台 App 的数据并登记新身份。日常重新安装升级保留数据，不能用卸载掩盖未知执行。

## 实时语音

0.1.2 输入文字后直接点击“发送”，按弹窗确认本轮外发；没有打开会话时自动创建，不必先展开权限勾选。被活动回复、待核对操作、语音或归档阻塞时显示具体原因。GET 遇到空闲连接失效时在原超时内恢复，写请求仍不自动重试。

聊天不要求选择节点。开启工具并确认本轮结果外发后，Core 根据所有已授权设备的能力选择目标；例如在手机说“告诉我电脑系统”可以派发到电脑。设备页优先显示“我的电脑／我的手机”，可改为“办公电脑／家里电脑”等唯一名称；名称保存于 Core，UUID 仅留在连接设置。多台同类设备且目标不明确时，Core 应先澄清。每台设备仍须独立注册并在线执行，不因使用共同目录获得额外权限。

界面沿用 Windows 的黑白配色、N 标识和聊天气泡。会话列表、当前历史、设备和任务在前台自动同步；切回前台立即同步，后台暂停轮询，失败退避且不自动重发写请求。设备连接凭证与通知设置收在“高级连接与本机权限”。

用户选择复用 Windows 的 Core 实时语音：`/api/v1/voice/sessions` 创建经 owner 同意的会话；独立 Node 身份连接 `/ws/v1/voice` 及 `/ws/v1/voice/audio`。音频经 Core 到其 ASR/TTS 提供方，手机不保存模型密钥、不保存录音，也不直连提供方。上行 16 kHz 单声道 PCM16，下行 22.05 kHz PCM16 + NXV2 generation/end 帧。

Core 须显式启用该 Node 的 `voice` 策略及已配置的语音后端。聊天页“语音”展示服务端外发说明、上下文和工具结果选项，经用户点击“同意外发并开麦”及 Android 麦克风授权后开始。半双工：回答播放期间关闭采集，按钮打断立即停播，再由 Core 开始收听；收到音频结束标记且 AudioTrack 实际排空才确认播放完成。

语音只在前台运行；后台、锁屏、音频焦点丢失、路由变化、停止、断线、期限到达均关闭本地音频，不自动恢复、不重放。没有后台唤醒词或声学自动插话。语音任务沿用文字权限与审批，语音外发同意不能扩大工具授权。最终声学质量、真机麦克风/扬声器/蓝牙、真实服务商延迟需另做真人测试。

录音与播放保留在发起语音的设备，工具可由 Core 路由到其他已授权节点；手机语音不限定为手机工具。

## 开发验证

`protocol` 的 JVM 测试包含由 Python Core 生成并反向验证的固定命令摘要、通知脱敏和 NXV2 二进制边界。`VoiceOrderingTest` 以实际本地 WebSocket 验证双通道乱序和过期指令拒绝播放。`DeviceTest` 使用独立测试数据库验证加密、回执重复/冲突、完成确认、重启 unknown 和真实 Android 状态读取。

真实 HTTP/WS 联调使用 `scripts/serve-fixture.py <仓库var中的新目录>` 启动 `127.0.0.1:18780` 的隔离 Core；`fixture.json` 含仅该夹具的随机临时凭证，不输出、不提交。脚本使用本地 Chat/ASR/TTS 替身，无付费调用。把夹具放入测试 App 私有 `files/android-fixture.json`，为**专用模拟器**授予测试通知权限/监听并执行 instrumentation。不要在用户手机运行清数据脚本。

```powershell
.\gradlew.bat :app:assembleDebugAndroidTest
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb reverse tcp:18780 tcp:18780
adb shell am instrument -w ai.nexus.android.test/androidx.test.runner.AndroidJUnitRunner
```

`CoreIntegrationTest` 验证真实 Core 登录、聊天停止、独立 Node、审批及通知脱敏；`VoiceIntegrationTest` 使用内存静音音频替身验证实际 Core 控制/音频 WebSocket、播放确认、续轮、按钮打断和停止，不声称真人语音验收。无夹具时集成测试会跳过。`UiSnapshotTest` 只在测试 Activity 暂时解除截图保护，用于检查实际 Compose 页面。

## 后续接手

入口为 `MainActivity` / `ui/NexusApp.kt`；HTTP 与可恢复请求在 `client/`；独立设备执行在 `node/`；语音在 `voice/`；共享 JVM 协议在 `protocol/`。不要新增第二套 Node 任务/规划逻辑。后续由用户安排 DSH 接手时，读取根目录三个必读文档和 P80 状态，使用现有构建与夹具，不需要重建工程。
