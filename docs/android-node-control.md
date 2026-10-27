# 手机操作与文件

Android Node 使用已有 Core 任务与会话工具通道执行手机操作。当前交付／部署／验收状态只看 [P104](ROADMAP.md#p104-android-control)。

## 开启

1. 在新版 Android App 登录并注册本机节点。
2. 在“设备 → 手机操作与文件”打开需要的本机开关；点击“打开系统无障碍设置”，手动开启 NEXUS 手机操作服务。
3. 文件功能点击“选择授权目录”，使用系统文件选择器选择目录。可以添加多个目录，也可以逐个撤销；只读提供方不会获得写权限。
4. 配套 schema044 Core 会自动接收本机开关的明确选择，页面显示“本机权限已同步到 Core”后无需再发开启 Core 权限的消息。开启设备执行，等待实际在线。离线时本机关闭立即生效，恢复连接或回到 App 前台后重试；同步失败不会阻塞聊天。系统服务与目录授权仍由你操作。

随后可请求“读取手机当前界面并点击某按钮”“在手机刚授权的目录读取 note.txt”“把这段文字保存到手机的 note.txt”。Core 按设备名称、有效权限和实际能力路由；手机需要解锁、屏幕点亮。无障碍服务不在后台持续采集界面。

界面读取返回可见文本、控件、支持动作及摘要；密码控件不返回文字或可执行动作。点击、长按、输入、滚动和返回必须引用仍与当前界面一致的摘要，控件动作只能选择该节点返回的 `actions`。0.1.13 起，主页和最近任务接受最近一次、两分钟内的观察摘要，只要求前台应用与窗口仍匹配；同一窗口里的聊天流式文字或按钮状态刷新不会阻止这两项导航。未知摘要、过期观察或切换窗口仍需重新观察。

0.1.14 起可在控件缺失时使用下方的屏幕视觉操作；不提供应用私有数据访问。NEXUS 自身及受保护的系统设置／授权界面中的控件需人工操作；允许使用主页、最近任务离开这些界面。打开微信可先读取界面、回到桌面，再读取桌面并点击实际返回的微信控件；系统动作后应重新观察，确认应用确实打开。

## 屏幕视觉操作

用于微信等界面缺少可读无障碍控件的情况；是否存在控件混淆不影响采用截图定位。需要 Android 11 或更新系统、配套 schema043 Core 和已开启的无障碍服务。

1. 在“设备 → 手机操作与文件”开启“允许截图与视觉操作”。该开关说明按需截图会发送给现有 DeepSeek；默认关闭。
2. 等待本机权限自动同步到 Core；不需要再发送授权消息。视觉与无障碍仍是独立开关，未选择视觉不会开启；旧版本 Core 需要升级后才能自动同步。
3. 可请求“用屏幕画面打开微信并查看当前页面”。Core 优先使用可读控件，缺失时按需截图、识别目标、提出坐标动作，再检查动作后的新截图。

截图通过原加密连接送到 Core，再作为图片输入交给现有视觉模型。手机只在工具调用时截取，不持续录屏；内存中仅缓存最近原图两分钟。已识别的密码控件区域涂黑，系统禁止截屏的界面保持拒绝；普通页面可能包含个人信息，外发范围就是当次截图或选取的区域。任务与回执保留有界截图证据，关闭权限阻止后续调用，不删除已有历史。

单张 JPEG 最长边不超过1600像素、最多16 KiB；复杂全屏可能进一步缩小，细字不清时应裁剪局部再读。动作坐标使用**返回图片的像素**，由 Node 映射到真实屏幕；裁剪参数 `region` 使用真实屏幕坐标。支持点击、长按和直线滑动。每次动作绑定最近截图摘要、两分钟有效期、同一应用／窗口／方向及目标区域像素；目标外的时钟或文字刷新不会拒绝动作，目标区域变化则重新观察。动作后返回新的全屏截图，不自动重发已提交而结果未知的手势。

NEXUS 自身及系统授权页面的坐标操作仍需用户完成；可用原主页／最近任务操作离开。截图按钮被识别或系统手势回调完成，不等于微信业务目标完成，仍需观察结果。

## 文件范围与结果

文件工具只访问系统选择器授予的目录。目录 URI 留在手机，Core 使用不透明 `tree_id` 和相对路径；`workspace` 仍表示原有 App 私有收件箱，不代表整个手机。Android 对其他应用私有目录、Android/data 等范围的限制照常生效，full 模式不能绕过系统沙箱。

- 列目录最多100项／页并受响应字节限制；目录超过2000项时需选择更小的子目录。
- 读取 UTF-8 文本最多1 MiB，每页最多8192 UTF-8字节，返回文件摘要与后续字符偏移。二进制文件不伪装成文本；现有原件传输仍走[收件箱](file-transfers.md)。
- 新建／覆盖的内容及待覆盖旧文件均最多12000 UTF-8字节，父目录须存在。Core 先取真实旧内容预览，绑定用户、设备、项目、目录、路径、新旧摘要和预览任务；普通模式沿用该会话已有文件授权，否则显示具体预览确认。
- 写入先创建同目录 `.nexus-stage-…` 暂存文件并读回；旧文件改名为 `.nexus-backup-…` 后再替换目标并读回。成功结果包含实际字节数、摘要及备份相对路径，备份由用户保留或清理。

SAF 提供方需要支持创建和重命名。多次提供方调用不是事务：断电、取消、授权撤销或提供方故障可能只留下暂存／备份文件。此时回执保留结果未知，先检查目标及备份，不自动重发写入，也不自动删除恢复资料。没有批量删除、目录移动或任意路径写入入口。

## Core 接口

工具版本均为1，使用原 `/api/v1/tasks`、审批及 `/ws/v1/node` 协议；聊天公开六个操作入口，预览由 Core 自动插入。

| 会话 intent | Node 工具 | 参数与结果 |
| --- | --- | --- |
| `android_ui_observe` | `android.ui_observe` | 空参数；返回 `state`、`observation_digest`、`ui_observed` |
| `android_ui_act` | `android.ui_act` | `observation_digest`、`action`、非全局动作的 `node`，仅输入动作有 `text`；返回动作后状态和 `action_returned` |
| `android_screen_observe` | `android.screen_observe` | 可选真实屏幕矩形 `region=[l,t,r,b]`；返回 `state`、有界 `image`、`observation_digest`、`screen_observed` |
| `android_screen_act` | `android.screen_act` | 截图摘要、`action=tap/long_press/swipe`、图片像素 `x/y`，滑动另有 `end_x/end_y`；返回动作后全屏图与 `gesture_completed` |
| `android_files` | `android.files` | `operation=roots/list/read`；后两项有 `tree_id`、`path`，分页 `offset`／`limit`；返回条目或文本、SHA-256、`next_offset` |
| 内部 `android_file_preview` | `android.file_preview` | `tree_id`、`path`、`content`；返回旧内容／摘要、新摘要和预览摘要 |
| `android_file_write` | `android.file_write` | 会话仅提交 `tree_id/path/content`；Core 补 `expected_sha256`、`preview_task_id`、`preview_digest`；返回摘要、字节数、备份路径 |

UI 动作在 normal 下使用具体 Harness 审批，full 下沿用有效工作授权；计划模式拒绝 UI 动作和文件写入。系统服务和目录、本机开关、设备与 owner 授权始终重新核对，旧请求或旧摘要不能用于改变后的操作。迁移042至044均按原数据库机制备份，不创建授权；新版客户端会在 owner 登录和设备绑定校验后同步已有的明确本机选择。未选择的手机操作开关保持关闭。同步协议见[设备管理](device-enrollment.md)。

回执成功说明 Node 证据通过契约核验。`action_returned` 仅说明 Android 接受动作且随后取得界面，异步页面业务仍需后续观察；文件成功要求实际内容读回匹配。停止或断线后保留已发生的效果与回执，不能把 unknown 当成未执行。

## 本地验证

`tests/test_android_screen.py` 验证截图摘要、裁剪、图片输入、手势回执及独立权限；原生测试另用没有语义控件的画布验证真实触摸效果。`tests/test_android_control.py` 覆盖严格参数、摘要／结果绑定、WS派发与幂等、Core授权撤销、普通／完全／计划模式、真实预览衔接及041→042保留授权。`:protocol:test` 用 Python／Kotlin 共用命令向量验证签名一致。

`apps/android/scripts/serve-phone-fixture.py <仓库var中的新目录>` 创建端口18810的隔离 Core，凭据仅写入本地夹具。专用模拟器通过 `adb reverse tcp:18810 tcp:18810` 连接；将夹具复制到目标 App 的 `files/phone-fixture.json`，运行 `PhoneNodeIntegrationTest`。测试 APK 包含独立测试页面和 DocumentsProvider，实际通过系统 URI 授权与文件描述符执行，不进入正式 APK。

每套新身份使用新的模拟器数据；原回执绑定不得在正常升级中清除。测试会开启并关闭该专用模拟器的测试无障碍服务，不能拿真实手机运行清数据或自动授权步骤。运行记录与失败修正保存在 `var/android-node/`；当前通过数量及限制以 ROADMAP 为准。

`phonePermissionControlsFitScrollablePage` 复用真实设备页检查滚动后目录选择入口可见；[截图](assets/android-node-control-permissions.png)与[本地验证摘要](assets/android-node-control-evidence.json)可供核对。已发布APK沿用原签名、提高版本号并配套迁移043的Core，下载链接与最终验证见[P104](ROADMAP.md#p104-android-control)。覆盖安装保留App数据，不需要先卸载旧版；旧的本地0.1.10候选仅为历史测试产物。

平台依据：[AccessibilityService](https://developer.android.com/guide/topics/ui/accessibility/service)、[Storage Access Framework](https://developer.android.com/training/data-storage/shared/documents-files)、[DocumentsContract](https://developer.android.com/reference/android/provider/DocumentsContract)。
