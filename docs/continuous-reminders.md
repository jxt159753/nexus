# 定时工作、长期任务与跨设备提醒

状态与验证记录见 [P97 路线图](ROADMAP.md#p97-continuous-reminders)。设计决定见 [ADR 0048](decisions/0048-continuous-reminders.md)。

当前发布：Windows0.1.21、Android0.1.9（versionCode10），配套Core schema040。[下载Android APK](https://core.example.com/static/downloads/NEXUS-Android-0.1.9-debug.apk)。APK与上一版使用同一debug签名，可覆盖安装；真实红魔后台／锁屏行为仍需在用户设备验证。

## 使用

先在 Windows 的顶部通知按钮，或 Android 底部“提醒”页，给当前设备命名并开启系统提醒。网页可以查看任务、提醒内容和各设备接收状态。设备接收开关与 Node 执行权限独立：接收提醒不需要授予文件、终端、通知读取或模型执行权限。

在聊天中说明内容、时间和接收设备，例如：

- “三分钟后提醒我喝水。”
- “一分钟后在 NX789J 提醒我学习。”
- “查询电脑系统信息，完成后在 NX789J 提醒我查询结果。”
- “每天早上 9 点提醒我查看今天的安排，持续到我停止，发给红魔。”
- “每隔一小时检查这个项目的工作进度，把结果发到所有已启用提醒的设备，直到我停止。”
- “持续完成这份报告，按计划推进，读回核对后告诉我；不要设置结束日期。”

直接提醒只记录并投递内容，到点不调用模型。周期检查通过原 Core/Harness 执行已授权工作，保存本轮实际结果并通知。长期目标保留计划、检查点和验收证据；目标完成即结束，重复周期工作使用定时任务。需要审批、来源授权失效、执行未知或无进展时仍会暂停并说明原因，不越权空转。

聊天通过原生 `plugin_inspect` 查询 `builtin.schedule` 契约，再由 Core 共用执行入口创建提醒或通知，无需手机具备电脑的插件执行器。指定设备时先查已启用接收的客户端，使用其 `client_id`，不是 Node ID；同名或“手机”对应多台设备时确认目标，不擅自广播。完成通知在实际工具结果核验后创建，后台作业刚启动不算完成；已保存、客户端已提交系统通知、用户已读分别记录。

重复任务没有隐含的 7 天／10 次截止；可显式指定期限或次数。旧任务保持原期限与恢复设置。新长期目标与持续周期工作默认允许 Core 重启后恢复已结算的轮次，用户可关闭；不会重发未知执行。离线错过多个周期时合并为一次，不追赶重放全部历史。

“提醒与持续任务”可查看所有会话的计划，暂停、恢复、停止周期任务，标记已读或打开来源会话。关闭某台设备接收不停止 Core 工作；停止任务也不删除既有执行与通知记录。已读状态跨端同步。投递对象在产生提醒时固定，新启用设备不会收到此前没有分配给它的旧通知；提醒历史仍可在中心查看。

## 红魔与无 Google 服务手机

本实现不需要 FCM、Google Play 或厂商推送 SDK。用户开启后，App 以可见的前台提醒服务维持连接，使用独立接收凭据和系统通知。离开 App 或锁屏后无需保持界面打开；会增加耗电。操作步骤：

1. 在“提醒”页开启后台提醒，授予系统通知权限。
2. 点击“设置锁屏后台运行”，允许本 App 不受电池优化限制；在红魔系统设置中允许后台运行／自启动。
3. 点击“系统通知与锁屏显示”，按需要允许通知、声音和锁屏内容。敏感内容是否展示仍由系统锁屏设置决定。

通知栏的“停止”或 App 的停止按钮可停止本机接收。断网时提醒留在 Core，联网后补收；回执丢失不会重复创建同一通知。强行停止 App、关闭通知、厂商强制清理或设备关机时无法保证即时显示；重启手机后需打开 App 并重新连接后台提醒。Android 标准模拟器通过不代表红魔厂商省电策略已经验收。

Windows 关闭窗口到托盘后继续接收，退出程序或电脑休眠期间保留 Core 待投递记录，程序重新启动后恢复已开启的提醒。系统勿扰／锁屏设置决定显示方式。普通浏览器页面只提供提醒中心，不宣称关闭浏览器后仍能推送。

Android 按用户要求允许系统截屏和录屏，不再设置窗口 `FLAG_SECURE`。

## 接口与持久化

迁移 040 增加 `plugin_schedules/plugin_goals` 的 `run_until_stopped`、`resume_after_restart`，周期任务的 `targets_json`，以及 `reminder_clients/reminders/reminder_deliveries`。迁移前自动备份，原数据与有限任务语义保留。持续任务的公共快照将 `expires_at/max_runs/max_rounds` 投影为 `null`，不是填入极大的伪上限。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/plugins/automation` | 本人各会话的定时／长期任务摘要 |
| `GET/POST /api/v1/reminders/clients` | 列出客户端／登记或轮换该客户端接收令牌 |
| `DELETE /api/v1/reminders/clients/{id}` | 撤销接收授权 |
| `GET/POST /api/v1/reminders` | 提醒历史／提交本人提醒；POST 绑定稳定 request_id 与内容、目标 |
| `POST /api/v1/reminders/{id}/read` | 本人跨端已读 |
| `GET /api/v1/reminders/delivery` | 专用 Bearer 令牌获取本客户端待投递内容 |
| `POST /api/v1/reminders/delivery/{id}` | 本客户端报告已提交系统通知；重复确认幂等 |

除最后两项外均需 owner 会话，写入必须有 Origin 与 CSRF。接收令牌只读本设备收件箱并提交回执，不能调用聊天、创建任务或授权设备；Core 只存令牌哈希，Windows 使用系统凭据库，Android 使用原加密 Vault。客户端不规划工作、调用模型或保存第二套任务权威。

模型复用 `builtin.schedule.schedule_create`，`execute=true` 为到点执行；默认只提醒。`notification_targets` 查询已启用的设备名称，`notify` 提交本人通知，`targets` 省略表示当时所有已启用设备；指定设备停用后不会改发给其他设备。重复提交不更改原内容与目标。接收回执仅标记 `client_reported_notification_posted`，不等同于用户看到或完成验收。

系统后台行为依据 [Android Doze 文档](https://developer.android.com/training/monitoring-device-state/doze-standby)、[前台服务类型](https://developer.android.com/develop/background-work/services/fgs/service-types) 与 [Windows ToastNotifier](https://learn.microsoft.com/en-us/uwp/api/windows.ui.notifications.toastnotifier)。最终显示仍受用户的系统通知设置影响。
