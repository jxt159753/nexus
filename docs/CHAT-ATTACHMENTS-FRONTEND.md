# 图片和文件上传：交给 DSH 的前端要求

用户要求“加入上传图片和文件功能”，并选择“先支持所有文件上传，内容解析分批补齐”。按用户持续有效的协作约定，由用户直接指挥 DSH 修改前端和 Windows 桥；Codex 提供后端接口。本工作单要求复用现有聊天界面。

## 可直接交给 DSH 的实施说明

请先完整读取根 AGENTS、ARCHITECTURE、ROADMAP，再读 [附件接口](chat-attachments.md)和 [ADR 0027](decisions/0027-conversation-attachments.md)。在现有 Web `/app`、旧控制台及 Windows 随包 UI 接入附件，保留当前 Markdown、presentation v1、工具结果与三维首页的并行改动。阶段状态只更新 ROADMAP。

### 选择、上传与草稿

- 增加选择文件、多选、拖入文件和粘贴图片；允许所有扩展名和空文件，不设置只接收图片/文档的 accept 白名单。采用服务器 capabilities：当前单文件 16 MiB、每轮最多 4 个；总容量、数量和速率限制显示真实错误。
- 新聊天先创建会话再上传。每个文件生成稳定 request_id；失败、取消或超时后结果未知时，核对服务器并用原 request_id/原件重试，不能换键重复存储。切会话不能把附件 ID 带到其他会话。
- 显示名称、大小、上传中/成功/失败、解析状态及取消选择按钮。仅有实际传输进度时显示百分比。待上传和失败文件阻止发送并保留草稿，不能静默丢弃附件后发送文字。
- composer 移除只取消本轮选择；删除未使用上传件另调用 DELETE，已关联轮次的 ATTACHMENT_IN_USE 不可绕过。刷新按轮次 attachments 恢复历史，不重新上传或发消息；历史附件再次阅读时明确重新选择。

### Windows 原生传输

当前 `apps/windows/src-tauri/src/bridge.rs` 的 api_request 使用 Option<String> 请求体、application/json 和 2 MiB 请求/响应上限，不能直接承载后端 16 MiB 附件。请增加附件专用有界传输路径，或对严格匹配附件接口的 Base64 JSON 单独设置上限，计入约 4/3 编码膨胀和有限元数据；不要全局放大任意 API 限额。

浏览器优先 POST 原始字节；字符串桥可 POST `?encoding=base64`，下载可 GET `content?encoding=base64`。二者复用同一幂等键和摘要。原生响应在读取过程中限制实际字节，不能先无限 bytes() 再判断大小；取消/断网及时释放传输。后端接收最多 30 秒，图片 helper 最多 10 秒，客户端等待应覆盖正常范围并显示真实超时。

沿用可信 Core 地址、证书校验、Rust 保管的 Cookie、受信 Origin 与 CSRF；凭证不放 URL、日志或 WebView 存储，不增加任意 URL 代理、本机执行或文件系统权限。原件仅由明确下载/另存为动作保存，不自动执行；文件名只是显示/建议名称，不能直接拼接任意目录。

### 预览与模型外发

- 图片仅显示后端验证的 PNG/JPEG 快照，复用现有严格图片组件；保留尺寸、压缩、有损、透明白底、动画首帧与不可预览原因。不要预览原始 SVG/HTML。浏览器可用同源认证 image 路由；Windows Cookie 在 Rust，需经认证桥取预览后生成受限本地资源，不能假设 img 自带原生会话。
- 文本按不受信文本显示并标明截断。PDF、Word、压缩包、程序、音视频等显示“已上传，尚未解析”，允许下载，不能写“模型已读取正文”。损坏图和超过图片源限制的文件仍保留原件，预览失败不等于上传失败。
- 上传不等于模型外发。发送前调用 model-preview，展示实际 text/image/metadata 范围、截断与图片开关状态。用户明确同意后，连同原聊天字段提交 attachment_ids、attachments_digest、allow_attachment_model=true；选择或摘要改变后重新预览，不自动选择全部上传件或沿用历史同意。
- 原 allow_model、记忆/引用/压缩/工具结果等同意继续独立处理；无附件时传 attachment_ids=[]、attachments_digest=null、allow_attachment_model=false。仅附件消息允许 text=""。图片开关关闭时明确“本轮仅发送文件信息”，不擅自改生产配置或安装解码器。
- 原 32 KiB 完整请求及费用上限不提高。MODEL_INPUT_LIMIT 等错误保留原件和草稿，提示减少本轮选择；不能截断 Base64、自动拆分付费调用或承诺理解未解析内容。旧 Core 不支持接口时明确提示不可用。

### 验证与交付

覆盖选择/拖放/粘贴、任意二进制与空文件、中文名称、文本截断、正常/损坏/过大图片、PDF/Word 未解析、重复上传、跨会话拒绝、刷新不重发、未同意不外发、401/403/404/409/413/429、键盘和 390px 布局。验证超过 2 MiB 文件在 Windows 安装版上传/下载后 SHA-256 相同，并覆盖取消、登录过期、断网与重启。

记录实际浏览器、安装包摘要、Core 版本和未验证项；手机尺寸截图不代替 Android 真机。不进行真实付费模型调用或生产部署。后端迁移为 022，上传不依赖 Node；Core 图片预览需已有可选 images extra，图片外发另需 chat_images_enabled。分别说明界面、原生桥、安装版及模型效果的验证范围，不以接口测试代替完整验收。

当前状态查 [ROADMAP](ROADMAP.md)，后端实际检查见 [附件验证](CHAT-ATTACHMENTS-VERIFICATION.md)。
