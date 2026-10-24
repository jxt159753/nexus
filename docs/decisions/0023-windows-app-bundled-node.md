# ADR 0023：Windows App 与随包 Node

- 日期：2026-10-21
- 状态：已采用（Windows 首版实现，待用户验收）
- 相关：ADR 0003、0017；[架构第 17 节](../ARCHITECTURE.md#17-web-前端与-app-设计边界)

## 背景

用户批准 B 方案，要求先完成 Windows 端，并把 Node 功能一起打包进 App，同时使用 `D:\DSH\nexus-splash` 的启动动画。原架构约定“App 不直接操作 Windows Node”，但用户的最新明确指令覆盖了这一限制。Core 仍是任务、权限、记忆和费用的唯一权威，Node 仍是独立执行进程。

## 决定

1. Windows 首版采用 **Tauri 2 + 系统 WebView2**，打包现有 `/app` HTML/CSS/JS，不重写业务前端。
2. WebView 安全来源为 `https://tauri.localhost`。Core 必须显式允许该 Origin；不使用伪造 Origin、关闭证书校验或放宽来源。
3. UI 的 `/api/v1/*` 请求通过 Rust 原生桥转发的 HTTP 客户端发送。登录时勾选“记住此设备”后，Owner 令牌与 `nexus_session` 会话存入 Windows 凭据管理器；启动时优先复用未过期会话，失效后才用 Owner 令牌重新登录。退出登录清除两者。凭据不写日志。
4. Core 地址由登录页显式填写；只接受 HTTPS，开发时允许 `http://127.0.0.1`/`localhost`。切换 Core 会清空旧会话。
5. Node 用 PyInstaller 从现有 `nexus.node` 构建为独立 `nexus-node.exe`，作为 App 资源随安装包分发。App 只管理该进程的启动/停止和本地配置，不执行工具、不写任务结果；任务和权限仍由 Core 判定。
6. Node 令牌通过环境变量传给子进程，不写入 TOML 或前端 localStorage；首次启动成功后将配置与令牌存入 Windows 凭据管理器。以后 App 启动自动读取配置并启动 Node；设置页保留手动启动/停止作为恢复手段。
7. 启动动画使用随包的 `nexus-splash-standalone.html`，独立无边框窗口与主窗口同为 1280×720，播放一次（默认 1.35 倍速），约 5.6 秒后关闭并显示主窗口；主窗口在外框坐标上沿用 Splash 位置，避免启动后跳到别处。
8. 首版不包含 Android、自动更新、代码签名、系统通知或后台常驻；安装包未签名，仅作当前用户安装。

## 影响

- 需要在 Core 配置中加入 `https://tauri.localhost`。为支持本机开发，`TransportPolicy` 允许在 `local_http=true` 时同时列出 HTTPS 来源；HTTPS 来源本身不放宽 TLS 或 Cookie 校验。
- App 通过原生桥访问 Core，因此不受浏览器 CORS 限制，但 Origin、Cookie、CSRF 和 Core 自身的授权检查仍然生效。
- Node 可执行文件约 18 MB，安装包约 20 MB；Node 使用现有 Python 实现，未在 Rust 中重写工具逻辑。
- 仍需在真实安装版、真实 Core TLS 和真实 Node 身份下完成用户验收；本地测试只证明链路和进程管理可用。

## 验证

- `cargo check` 通过；`npm run tauri build -- --bundles nsis` 产出安装包。
- App 实际启动：Splash 页面 `https://tauri.localhost/splash.html` 加载，5.6 秒后主窗口显示。
- 通过 WebView2 CDP 确认 `location.origin=https://tauri.localhost`、原生桥存在、Core 登录成功并读到预置会话；安装版通过原生桥发送聊天 POST 后 Core 轮次由 2 增至 3。
- App 启动 bundled Node 后，临时 Core 与真实云 Core 都返回该 Node 在线（`filesystem.list@1`、`system.info@1` 等 6 项）；真实云 `1/1` 在线。Node 可执行文件内置 certifi CA，WSS 不依赖外部 CA 文件。
- 安装包 SHA-256 `F2060F54EBFC0A86AF2595468339FBFE1C770F94B560AFBC4426F41484A2A4E1`，静默安装到 `%LOCALAPPDATA%\NEXUS`；安装版可从该目录启动，并从安装目录的 `binaries/nexus-node.exe` 启动 Node。
- 2026-10-21 生产唯一 Core `/etc/nexus/core.toml` 已备份为 `/var/backups/nexus/core.toml.before-windows-app-20261021T103954Z`，仅增加 `https://tauri.localhost` 到 `origins`；Core 重启后 active，PID `2919034`，HTTPS 探测 `200`/TLS `0`。安装版使用真实 Owner 凭证登录成功，显示 `1/1 设备在线`、2 个真实会话；本次未发送聊天消息、未新增模型调用。
- 安装版验证 Node 自动启动：保存配置后重启 App，Node 自动运行并恢复 `1/1` 在线（PID 另行分配），无需手动点击。
- 安装版验证“记住此设备”：Owner 令牌与 `nexus_session` 存入 Windows 凭据管理器；重启 App 后自动进入主界面，活动 Core 会话数保持 2，没有新建会话。主窗口在显示前设置到 Splash 外框位置（实测 screenX 相同；内容区因标题栏相差 29px）。
- `tests/test_security_http.py` 29 项通过（含 HTTPS 来源与 local_http 并存）。
