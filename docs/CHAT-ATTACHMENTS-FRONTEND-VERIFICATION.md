# 会话附件前端与 Windows 原生桥：本地验证

日期：2026-10-22。对应 P56 的前端、旧控制台和 Windows 原生传输接入；批准、交付与验收状态只维护在 [ROADMAP](ROADMAP.md)。后端接口与本地检查见 [附件验证](CHAT-ATTACHMENTS-VERIFICATION.md)，接口契约见 [附件说明](chat-attachments.md)。

本轮由用户直接指令在 DSH 中完成，未修改后端契约、迁移、Core 配置或生产环境。保留现有 Markdown、presentation v1、工具结果、三维首页与会话页并行改动。

## 实现范围

- Web `/app`：底部快速输入和完整会话共用一份“按会话”的待发送选择；支持文件选择、多选、拖入和粘贴图片，不设置 `accept` 白名单。新聊天在选择文件时先创建会话，再上传到该会话。
- 旧控制台：`#chat-form` 增加选择、拖入、粘贴、上传进度、解析状态、逐轮预览与明确同意；历史轮次按 `attachments` 清单恢复，不重新上传或发消息。
- Windows 随包 UI：与 `/app` 共用附件模块；上传和原件下载通过新增的 Tauri `attachment_request` 桥，使用 Base64 JSON 专用传输，不复用 2 MiB 的通用 API 限额。
- 服务器 capabilities 决定单文件上限（当前 16 MiB）和每轮数量（当前 4 个）；总容量、数量、速率和传输槽错误按服务器状态显示。
- 每个文件使用稳定的 `request_id`；失败、超时或取消后先按文件名、大小和 SHA-256 核对服务器，再以原编号和原件重试。上传顺序为单并发，避免与 Core 的两条传输槽竞争。
- `model-preview` 只展示实际外发范围；勾选同意后才提交 `attachment_ids`、`attachments_digest`、`allow_attachment_model=true`。选择或摘要变化会清除旧预览并重新请求同意。无附件时显式发送空清单字段。
- 图片只渲染 Core 验证后的 PNG/JPEG 快照；SVG、HTML、原始动图不进入图片渲染。文本按不受信文本显示并标明 8 KiB 截断；其他类型显示“已上传，尚未解析”并可下载。
- 旧 Core 没有附件路由时明确提示不可用，并在普通消息中省略 `attachment_ids`/`attachments_digest`/`allow_attachment_model`，避免严格旧契约因未知字段拒绝聊天；检测到新 Core 后空附件轮次仍显式发送空清单与 `false`。

## 界面验证（Web）

实际浏览器：Microsoft Edge `155.0.4283.62`（Playwright `playwright-core` 1.64.1，系统 Edge channel，headless）。Core 为独立本地进程、临时数据库、合成 SSE Provider；未访问生产、未调用真实付费模型。

运行：

```powershell
$env:NEXUS_TEST_NODE = "C:/Program Files/nodejs/node.exe"
.venv/Scripts/python.exe -B -m pytest -q tests/test_chat_attachments_browser.py --basetemp var/pytest-attach-final-browser --tb=short
```

结果：**1 项通过**。覆盖：

| 项目 | 实际结果 |
| --- | --- |
| 选择/拖入/粘贴、空文件、任意二进制、中文文件名 | 通过；文件选择器由真实 `#dock-attach` 点击打开 |
| 超过 2 MiB 上传与真实进度百分比 | 通过；CDP 限速后观察到 `aria-valuenow` 变化 |
| 文本截断、PDF 未解析 | 通过；预览与实际同意范围都明确标出截断和“仅文件信息” |
| 正常/损坏/超过图片源限制的 PNG | 通过；正常图显示白底快照，损坏图和超大图保留原件并标注不可预览 |
| 未同意不外发 | 通过；预览阶段 `model_calls` 不变、没有新轮次 |
| 仅附件消息（text 为空） | 通过；轮次绑定 3 个附件 |
| 刷新不重传/不重发 | 通过；轮次附件恢复，服务器附件数与调用数不变 |
| 跨会话 404、已关联附件 403、重复键 409、超限 413、未登录 401 | 通过 |
| 取消上传与排队取消 | 通过；取消后没有遗留原件 |
| 旧控制台选择、预览、同意、发送、刷新 | 通过 |
| 键盘与 390×844 | 通过；首屏快速输入与会话页均无横向溢出，附件按钮可聚焦 |

证据：[浏览器证据 JSON](assets/chat-attachments-browser-evidence.json)、[桌面截图](assets/chat-attachments-desktop.png)、[390px 截图](assets/chat-attachments-mobile.png)。截图使用合成数据，不代表 Android 真机。

## Windows 原生桥验证

`apps/windows/src-tauri/src/bridge.rs` 新增命令 `attachment_request` 与 `cancel_attachment_request`：

- 只允许两个严格匹配的 Base64 端点：会话附件集合的 `POST ?encoding=base64` 和附件原件的 `GET content?encoding=base64`；其他 `/api/v1` 路径继续使用原 2 MiB 限额。
- 附件请求体、响应与 16 MiB 原件的 4/3 编码开销使用独立上限（23 MiB）；响应在读取过程中按实际字节截断，不再先 `bytes()` 再判断。
- 传输在 75 秒上限内使用独立 HTTP client；取消标记在发送和分块读取期间轮询，取消后丢弃连接。Cookie 仍只保存在 Rust，Origin、证书校验和 CSRF 头沿用原桥；不接受任意 URL、目录或命令。
- 前端只在明确下载/另存为时通过桥取回原件；文件名只作为建议名称，不拼接路径。

静态与单元检查：

```powershell
cd apps/windows/src-tauri
cargo check
cargo test --lib
```

结果：`cargo check` 通过；`cargo test --lib` **2 项通过**，覆盖两个 Base64 端点白名单、拒绝其他路径/方法/查询，以及 Core 地址的 HTTPS/凭证/路径校验。

## Windows 安装版验证

安装包由本轮源码构建并静默安装（覆盖原安装位置）：

- 安装包：`apps/windows/src-tauri/target/release/bundle/nsis/NEXUS_0.1.0_x64-setup.exe`
- SHA-256：`5496eedc5b5f31f6857619fa8b70aa4faf5dd62716bf25f5144b840544ea9e55`（34,491,005 字节）
- 安装后主程序：`E:\NEXUS\var\frontend-check\installed\nexus-windows.exe`（12,325,376 字节）
- WebView2：`Edg/155.0.4283.62`；Core：本地源码（附件迁移 022，测试库当前 schema 026），合成 Provider

运行：

```powershell
$env:NEXUS_TEST_WINDOWS_APP = "E:/NEXUS/var/frontend-check/installed/nexus-windows.exe"
$env:NEXUS_TEST_NODE = "C:/Program Files/nodejs/node.exe"
.venv/Scripts/python.exe -B -m pytest -q tests/test_windows_attachments.py --basetemp var/pytest-win-attach-final --tb=short
```

结果：**1 项通过**。实际覆盖：

| 项目 | 实际结果 |
| --- | --- |
| 5,243,201 字节（>2 MiB）二进制上传 | 通过；安装版经 Base64 专用桥上传，超过旧 2 MiB 限额 |
| 下载 SHA-256 | 通过；原生桥返回原件与浏览器下载保存的文件都与上传文件 SHA-256 相同 |
| 发送前逐轮同意 | 通过；同意后轮次显示 1 个附件，模型调用 1 次（合成 Provider） |
| 取消 | 通过；观察到“取消上传”，无遗留原件 |
| 断网 | 通过；显示上传失败，Core 重启后以原 `request_id` 重试，只产生 1 条原件 |
| 重启 App | 通过；同一 WebView 配置自动恢复会话，历史附件仍显示 |
| 登录过期 | 通过；将持久会话过期后回到登录页 |
| 无伪造进度 | 通过；原生 IPC 没有真实传输进度时不显示百分比 |

证据：[Windows 证据 JSON](assets/windows-attachment-evidence.json)、[安装版会话截图](assets/windows-attachment-chat.png)。

## 验证边界与未验证项

- 模型效果：浏览器与安装版测试使用合成 SSE Provider，只证明请求被准入、附件范围和轮次关联；未验证真实 DeepSeek 对图片/文档的理解质量，未发起付费调用。
- 429：界面和后端均有真实错误映射，但本轮未在在线环境强制触发 30 秒传输超时或 20/分钟上传速率上限；不宣称已实测。
- 上限：安装版实测 5,243,201 字节；未逐一验证 16 MiB 精确上限，也未强行触发服务端 30 秒接收超时。
- 迁移 022：沿用 P56 后端验证；本轮未升级生产数据库，未部署。
- Android：390px 浏览器截图不等同 Android 真机；未做 Android 浏览器或 App 验证。
- 旧控制台：已用真实 Edge 完成选择、同意、发送、刷新，但没有重复 Windows 安装版全部错误矩阵。
- Windows 安装版测试会写入临时 Core 的“记住此设备”凭据，并在结束时清除；如测试前已有 App 凭据会被覆盖。测试使用隔离的 `%APPDATA%`/WebView2 目录，没有修改生产 Core 配置或密钥。
- 文件名、SHA-256、截图均来自临时合成数据；不含 owner 令牌或模型密钥。
