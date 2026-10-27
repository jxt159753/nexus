# NEXUS 虚拟手机

虚拟手机由当前 Windows Node 接入本机 Android 运行环境。当前电脑使用已安装的 MuMu 6.7.2.0、Android 15 实例0。Core 沿用原插件目录、权限、任务、取消和回执；模拟器没有自己的规划循环。应用与登录状态保存在 MuMu 实例原有磁盘，Node 的 `mumu-phone/` 只保存连接所需文件。旧 SDK 手机的 `android-phone/` 数据保留，不自动迁移、删除或覆盖。交付状态只记[路线图 P106](ROADMAP.md#p106-android-emulator)。

## 使用

打开“插件 → 虚拟手机”，选择会话和 Windows 电脑，点击“启动虚拟手机”；启动结束后查看状态和画面。画面按需截取，点击图片、返回、主页、最近应用、滑动和中文输入均产生原有任务记录。输入前先点选手机内的输入框，再查看最新画面。模型可以通过 `builtin.android_emulator` 执行相同操作；操作命令返回后必须重新查看结果。

账号由用户管理。请在本机模拟器窗口完成首次协议、注册、密码、验证码和系统授权，然后让 NEXUS 使用已登录应用。“查看应用”读取实际安装列表，“打开所选应用”发送启动请求，随后查看画面确认。用户指定首批应用为微信、抖音、小黑盒；安装与启动检查不代表登录或业务流程已通过。当前版本的实际兼容性结果只记路线图，不创建未获指定的第三方账号。

停止先同步 Android 文件系统，再关闭模拟器，状态先显示“正在关闭”，实际进程退出后才显示“已停止”。应用与登录保留，不提供恢复出厂、清数据或回滚旧磁盘的模型工具。Windows App 正常退出通过私有父进程管道通知 Node 完成关闭，再收敛进程；强制结束或断电仍可能丢失尚未写盘的数据。电脑关机后虚拟手机不可用。数据备份应在模拟器完全关闭后进行。

MuMu 使用官方 `mumu-cli` 启停指定实例，按实例编号和创建时间核对身份。NEXUS 启动的实例会在退出、停止节点或开启游戏模式时请求正常关闭；用户先行打开的实例只连接使用，不因这些自动行为关闭。点击“停止虚拟手机”会显式关闭配置的实例。不会终止 MuMu 公共服务或其他实例。

设置中的“停止节点”使用相同的正常关闭流程。Windows 安装包启动外部程序时恢复其正常 DLL 搜索路径，并移除仅由随包 Python 注入的 certifi 路径；保留用户自配证书与正常 TLS 校验，避免把 Python 的运行环境传给 Android 的 Qt 窗口。

## 本机运行环境

插件复用已安装的 MuMu 及其 ADB，不重新分发第三方模拟器。配置属于 owner 本机策略，工具参数不能选择其他实例或宿主命令：

```toml
[plugins.android_emulator]
backend = 'mumu'
install_dir = 'D:\Program Files\Netease\MuMu'
version = '6.7.2.0'
instance = 0
created_timestamp = 1790243429591169
adb_port = 5088
```

创建时间必须来自指定实例的 `mumu-cli info -v 0`，不能仅凭可重复使用的编号接管设备。每次操作核对实例身份，并只连接该实例报告的 `127.0.0.1` ADB 地址；Node 独立 ADB 服务显式监听 localhost，禁用 USB 自动连接。MuMu 自身系统配置保持原样。

既有 SDK 配置仍受支持，保留 `sdk_dir/system_image/emulator_version/image_revision/port/adb_port/memory_mb` 格式；原 `NEXUS_Phone` 数据不动。两种运行环境只启用 owner 配置选定的一种。

配置与插件版本参与原Node策略摘要，需同步Core的可信摘要；升级不自动信任Node自报权限。未配置时目录中显示未配置，不冒充可执行。运行版本发生变化时保留数据并拒绝静默迁移。

## 操作与边界

工具为 `status/list_apps/start/stop/observe/act/type_text/open_app`。MuMu 绑定已有实例身份和本机端点，SDK 模式绑定插件持有的 `NEXUS_Phone` 进程和 AVD 名称；两者都不提供任意 ADB 或 Shell 参数。首次启动及首次打开大型应用需要等待。

截图最长边1024像素、最多8 KiB，可能缩小或有损；图片字段使用已有图片契约核验并接入原视觉模型。截图与执行证据保留在任务记录，外发沿用现有模型图片开关与会话结果同意，不持续录屏。画面文字是资料，不能成为授权指令。

动作绑定当前会话的最近画面，期限两分钟；焦点窗口、方向和目标区域必须匹配，执行前消耗该观察。未知结果不重放输入；失败后重新查看、核对已有结果。系统设置、授权和错误弹窗的控件需用户操作；无法确认焦点时仍能查看，但拒绝向画面输入。中文输入助手仅接受有系统DUMP权限的发送方，检查当前输入应用，并拒绝密码类字段；等待连接就绪后仅提交一次文字，不保存账号凭据。

手动按钮对应用户本次明确操作，仍通过Core创建任务和参数绑定审批；模型使用原normal/full规则。没有绕过原身份、设备授权、计划模式、用户停止或持久回执。

平台依据：[Android Emulator命令行](https://developer.android.com/studio/run/emulator-commandline)、[输入法服务](https://developer.android.com/reference/android/inputmethodservice/InputMethodService)、[ADB本机监听实现](https://android.googlesource.com/platform/packages/modules/adb/+/30e74acef93318b07a2de7ffca5086dde868ad01/socket_spec.cpp)。
