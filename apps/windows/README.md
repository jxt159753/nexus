# NEXUS Windows App（B 方案首版）

0.1.16 与 Android 0.1.6 共用 Core 的全局活动快照，任务和节点独立刷新；当前聊天切换不再改变全局活动含义。发布与验证见 [P93](../../docs/ROADMAP.md#p93-mobile-sync)。

0.1.14 随包 Node 支持 full 模式下的宿主目录访问，normal 保持原文件和宿主确认边界。设备工具使用可选 `access_root` 指定本地绝对目录；不提升系统账户权限。当前交付见 [P91](../../docs/ROADMAP.md#p91-full-host)，接口见 [插件说明](../../docs/plugins.md)。

顶部“手势”入口提供当前会话的可视化插件授权和摄像头控制。NEXUS 可在授权后自行提交、测试和升级插件，运行失败回退；MediaPipe 在本地识别，不上传画面。构建前运行仓库根目录的 `python config/ui-extension/build_assets.py` 校验模型资源并同步共享模块。使用和权限见[可视化插件](../../docs/ui-extensions.md)，交付状态见 [P90](../../docs/ROADMAP.md#p90-extension-evolution)。

文件发送及接收见[向设备发送文件](../../docs/file-transfers.md)。直接在聊天中描述发送需求，文件卡片提供预览和下载；独立传输面板与卡片转发按钮已移除。本机启动配置保留独立接收开关，文件保存到原 workspace 下的 NEXUS-Inbox。

对话保留常用操作，权限入口明确可见；设置中的项目、用量和高级选项按需展开，普通回复省去重复状态标签。配合 Core 的聊天 `send_file`，可将电脑工作区原件发送到手机；使用已有 `builtin.present` 导出。当前版本及待验收项见 [P86](../../docs/ROADMAP.md#p86-ui-refresh)。下文旧版本验证保留为对应交付时点。

Tauri 2 + WebView2 的 Windows 外壳，主窗口与启动动画统一为 1280×720，复用现有 `/app` 页面，并把现有 `nexus.node` 构建成随包分发的 `nexus-node.exe`。Core 仍是任务、权限、记忆和费用的唯一权威；Node 是独立进程，App 只负责其启动、停止和本地配置。

0.1.5 修复可视化：脉冲完整抵达设备，工具结束后保留余辉再淡出，相机围绕实际焦点旋转。保留 0.1.4 的统一自动选设备与在线状态同步。覆盖安装前从托盘退出旧版，已有登录、Node 配置和回执保留。语音与 Node 二进制沿用 0.1.3 固定版本。当前交付和测试证据见 [P82](../../docs/ROADMAP.md#p82-visualization)。

## 目录

```text
apps/windows/
├─ ui/                    随包前端（app.html / native-bridge.js / splash.html）
├─ node/entry.py          PyInstaller 入口
├─ scripts/build-node.ps1 重新生成 nexus-node.exe
└─ src-tauri/
   ├─ src/bridge.rs       Core 原生 HTTP 桥（Cookie/CSRF/Origin）
   ├─ src/node.rs         Node 进程与配置
   ├─ src/lib.rs          Splash、主窗口及系统托盘生命周期
   ├─ binaries/           nexus-node.exe（构建产物，不入库）
   ├─ icons/
   └─ tauri.conf.json
```

## 构建

前置：Rust MSVC、Visual Studio 2022 C++、Node/npm、Python 3.12+、WebView2。

### 1. 构建 Node

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File apps/windows/scripts/build-node.ps1
```

或直接运行 PyInstaller（数据路径必须绝对）：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile `
  --name nexus-node --distpath apps/windows/src-tauri/binaries `
  --workpath var/build/pyinstaller --specpath var/build/pyinstaller `
  --paths src `
  --add-data "E:/NEXUS/src/nexus/node/migrations;nexus/node/migrations" `
  --add-data "E:/NEXUS/src/nexus/node/tools;nexus/node/tools" `
  --hidden-import aiosqlite --hidden-import websockets --hidden-import pydantic `
  apps/windows/node/entry.py
```

### 2. 构建安装包

```powershell
cd apps/windows
npm ci
$env:PATH="$env:USERPROFILE\.cargo\bin;$env:PATH"
npm run tauri build -- --bundles nsis
```

产物：

```text
apps/windows/src-tauri/target/release/bundle/nsis/NEXUS_0.1.5_x64-setup.exe
```

### 语音更新（0.1.1起）

语音从 0.1.1 起包含独立 `binaries/nexus-voice.exe` 及校验过的KWS模型；0.1.3更新唤醒运行时并保留0.1.2托盘行为。先在项目虚拟环境运行 `python -m pip install -r requirements-voice.lock pyinstaller==6.22.3`。从 [模型锁](../../config/voice-models.lock.json) 的KWS来源下载、按文件SHA-256核验并解压到本地模型目录，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File apps/windows/scripts/build-voice.ps1 -WakeModelDir var/voice-research/models/kws
# 然后按上节构建 NSIS
```

0.1.20 默认随 App 启动进入本地唤醒待机；在“设置 → 语音 → 常驻语音唤醒”关闭后立即关麦，并记住关闭选择。说“你好奈克斯”或“Hey NEXUS”，听到提示音后提问；最小化/收起到托盘继续工作，托盘退出关麦。原生进程负责到期后的待机续接，不重放旧语音或任务；登录过期时等待重新登录。清除显示偏好不改变此开关，不新增 Windows 开机启动项。

聊天输入区的“语音”提供设备选择、音频外发同意、上下文/工具选项和独立主动权限。原生桥复用已保存的Node身份，令牌不进入网页；退出先关闭音频进程。断线、停止和设备切换后需重新开始。实际部署、安装摘要及验收限制统一见 [P63](../../docs/ROADMAP.md#p63实时语音扩展选型确认待配置实施)，使用方法见[实时语音](../../docs/realtime-voice.md)。下文P62数字保留为旧版时点证据。

P62 本地安装包：约 33.0 MiB，SHA-256 `AAACAA868C680C182070B2F0250F9105074EC62C63021CD8FB6CCA9366CCA2A2`。随包 `nexus-node.exe` SHA-256 仍为冻结版 `6FCDDB72E83253AAF6668BAC98DB828E7001D886D57275B0D4FFA18E02B7B7BF`，包含固定 Pillow 13.0.0 图片解码器和 `plugin.call@1` 静态能力声明；是否实际可用仍取决于 Core/Node 本地配置、Core 静态信任、两端授权及模型图片开关。安装包和页面尚未部署生产。

WebView2 引导器已嵌入安装包，安装时不需要联网下载。NSIS 为当前用户安装，默认目录 `%LOCALAPPDATA%\NEXUS`。

## Core 配置

Windows App 的 WebView 安全来源是 `https://tauri.localhost`。Core 必须显式允许：

```toml
origins = ['http://127.0.0.1:8000', 'https://tauri.localhost']
```

本机开发允许 HTTPS App Origin 与 loopback HTTP 同时存在；不要通过关闭 TLS 校验、伪造 Origin 或放宽来源解决接入问题。生产 Core 的配置和证书由部署方管理。

## 使用

1. 启动 App，动画约 5.6 秒后进入主窗口。启动窗口使用系统标题栏，可以最小化或关闭，不置顶、不主动抢焦点。
2. 登录页填写 Core 根地址与 Owner 凭证。App 只接受 HTTPS，开发时允许 `http://127.0.0.1`/`localhost`。
3. 设置 → 本机 Node，填写：
   - Core WebSocket 地址，例如 `wss://core.example.com/ws/v1/node`
   - Core 中登记的 Node UUID
   - Node 令牌
   - Workspace 绝对路径
4. 点“启动本机 Node”完成首次配置。Node 作为独立进程启动，Core 的设备列表应变为在线；保存成功后，以后 App 启动会自动启动 Node。
5. “停止”只停止本机 Node；不取消 Core 中的任务、审批或模型回复。
6. 关闭主窗口或启动窗口（包括 Alt+F4）会收起到系统托盘，App 和已启动的 Node、语音继续按原状态运行；启动时关闭后不会被计时器重新弹出。左键点击托盘图标，或右键选择“打开 NEXUS”，可恢复主窗口。右键“退出”才退出 App 并停止其管理的语音和 Node 进程。最小化按钮保留普通任务栏最小化行为；不新增开机自启。

Node 可执行文件内含 certifi CA，WSS 不再依赖外部 CA 文件。PyInstaller 单文件不实现 `python -m`，因此 Node 通过 `nexus-node.exe --worker` 重入 worker；该入口必须保留。Node 令牌通过环境变量传给子进程，不写入 TOML 或前端 localStorage。首次在设置中启动成功后，配置会存入 Windows 凭据管理器；以后 App 启动会自动启动 Node，无需手动。登录时勾选“记住此设备”会把 Owner 令牌和当前会话 Cookie 存入 Windows 凭据管理器；下次启动先复用未过期会话，失效后才用 Owner 令牌重新登录。退出登录会清除 Owner/会话；Node 配置需在设置中单独停止或更换。启动动画与主窗口使用同一位置。

## 安全边界

- 前端 `/api/v1/*` 由 Rust 原生桥发送，Core 仍检查 Origin、Cookie、CSRF、任务、权限和费用。
- App 不执行任何 Node 工具，不把模型回答当成执行成功。
- Node 配置由 App 写入 `%APPDATA%\ai.nexus.windows\node.toml`，不含秘密；Workspace 和 Node 数据目录由 App 创建。App 只更新连接、数据目录、workspace、允许工具列表和 owner 显式勾选的 `plugin.call@1`；现有 `[plugins]`、LSP/MCP、commands、interaction 配置按 TOML 语义保留，旧配置损坏时拒绝启动而不是覆盖。App 不会自动信任 Node 自报的 `plugin_digest`；需要 owner 在 Core 中同步静态信任。若独立 Node 已使用同一数据目录/身份，App 启动会因锁或在线检查失败，不能把旧 App Node 显示成新插件执行端。
- 自动更新、系统通知、代码签名不属于本外壳；0.1.2 支持用户启动后的托盘常驻。Android 独立任务见路线图。

## 已验证

- `cargo check`；Rust 7 项桥/Node 配置单测通过；NSIS 安装包构建成功（P62 SHA-256 见上）。
- App 实际启动，Splash 页面显示后关闭并进入主窗口。
- WebView2 CDP 确认 `https://tauri.localhost` 与原生桥；Core 登录成功。
- 安装版 WebView2 实测插件/权限页和原生交付物下载；本机独立 Node 占用同一数据目录时，App Node 启动被拒绝，状态明确显示未接管。首次 P56 版本从 `%LOCALAPPDATA%\NEXUS` 启动并验证 Node 在线；P62 本地安装路径由安装器注册表决定，本轮为 `E:/NEXUS/var/frontend-check/installed`。
- 真实云 Core `https://core.example.com` 使用真实 Owner 凭证登录成功；App 内 bundled Node 实际连接为 `1/1` 在线（`filesystem.list@1`、`system.info@1` 等 6 项），stop/start 已实测。
- `tests/test_security_http.py` 29 项通过。

## 未验证

- 生产 Core 已通过 HTTPS 登录/同步；长期运行和真实付费模型调用未在本轮执行。
- 代码签名、SmartScreen 信誉、自动更新、升级保留数据。
- Android 端、M8B/C、系统通知。
