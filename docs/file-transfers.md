# 向设备发送文件

本功能把 Core 中的原始附件保存到已授权的 Windows/Python Node 或 Android Node，传输执行不调用模型。自然语言入口复用当前聊天模型及原轮次预算。当前界面与验证状态见 [P86](ROADMAP.md#p86-ui-refresh)；原件传输设计见 [P85](ROADMAP.md#p85-chat-files)。

## 使用

- 聊天可直接说“把电脑工作区的 demo.txt 传给我的手机”。Core 在来源电脑使用已有 `builtin.present` 读取并登记真实原件，再在目标手机调用 `send_file`；手机无需安装文件插件。来源电脑必须具备已授权、实际可用的 `builtin.present`，目标设备必须在线并支持接收。模型选择和失败说明仍以具体设备、工具证据为准。
- 网页、Windows、Android 的回复中显示文件卡片，支持文本／图片预览、原件下载或打开。卡片来自 Core 已验证的文件记录，不把模型写出的路径或链接当作真实文件。PDF、Office 等未解析格式提供原件打开／下载；不直接执行 HTML 或脚本。
- Android 回复使用与网页相同的 Markdown 解析和净化器，支持标题、列表、代码块、表格和链接；内嵌视图只能读取随包资源，没有原生 JavaScript 桥，不加载模型提供的远程图片，链接仅在用户点击后交外部应用。
- 独立文件传输面板和手动转发按钮已移除。网页／Windows 仍可在聊天中添加附件，再用自然语言说明发送对象；Android 可直接要求助手发送电脑上的原件，或处理会话中已有的文件。
- Android 在“设置 → 收到的文件”查看本机收件并选择应用打开，系统只向所选应用授予该文件的临时读取权限。旧版未核对上传仍在设置中提供恢复入口，不因界面精简丢失原操作标记。
- 单文件最多 **16 MiB**。上传到 Core 与设备收到是两个状态；只有目标设备保存并读回，大小及 SHA-256 匹配后，才显示已送达。不会自动打开、执行或交给模型。
- 聊天来源电脑的 `builtin.present` 沿用现有 **4 MiB/文件、8 MiB/调用** 上限；较大文件可从网页／Windows 聊天附件上传，接收仍为 16 MiB。不通过模型读取的文本片段重建原件。
- Windows 保存到配置的 `workspace/NEXUS-Inbox/<request_id>/<安全文件名>`；Android 保存到 App 私有收件区中的同一相对目录。每次新传输单独建目录，不覆盖同名文件。Android 卸载/清除应用数据会删除私有收件箱，请先用其他应用保存需要长期保留的文件。
- 设备离线、未授权或版本不支持时明确显示原因。升级 Windows/Android 客户端后，开启设备执行和“允许接收文件”；原配置、身份和回执保留。安装包不会自动覆盖正在运行的旧客户端。
- 超时或断线先核对原任务；前端不自动换编号重发。网页同一待确认发送使用原 `request_id`，Android 待核对请求加密保存。传输取消复用任务取消；已发生的保存不会因取消而删除。

## 接入与权限

新增工具 `filesystem.receive@1`、意图 `receive_file`、资源 `workspace`。Core 迁移 `035_file_receive.sql` 仅扩展 grants 契约并保留原记录；迁移前备份，连续执行既有迁移。Core 的可信节点配置、owner grant、目标 Node grant、本地 advertised tools/roots 必须全部允许，注册工具本身不产生授权。

Windows/Python 配置将 `filesystem.receive@1` 加入 `allowed_tools`，沿用原 `roots.workspace`。Windows App 的本机接收复选框只增删这一工具，不更改插件、命令或其他权限。Android `receive_files` 本地配置控制是否注册该工具和 `workspace`；该资源只表示 App 私有收件区，**不会注册**普通 `filesystem.read/write` 等桌面文件工具，也不申请共享存储的全盘访问权限。

离线管理操作必须在既有批准范围、停机且可靠备份后进行；通过 `SecurityService.set_grant(identity, 'filesystem.receive', root_id='workspace')` 分别设置 owner 和对应 Node。不能直接复制其他设备令牌，不能以 Tailscale、上传成功或模型同意代替设备授权。

## API

| 请求 | 含义 |
| --- | --- |
| `GET /api/v1/file-transfers/targets` | owner 查询节点名称、在线状态、可接收状态及原因；返回单文件大小和不覆盖策略 |
| `POST /api/v1/chat/conversations/{conversation_id}/attachments/{attachment_id}/transfers` | owner 提交 `{request_id, node_id}`，Core 从所属附件推导路径、大小及 SHA-256，返回原 Harness task |
| `GET /api/v1/chat/conversations/{conversation_id}/attachments/{attachment_id}/transfers` | 查询该附件最近 20 个传输任务 |
| `GET /api/v1/file-transfers/{attempt_id}/content` | 仅绑定的 Node Bearer 凭据可下载当前有效执行内容；拒绝 owner/其他 Node、停止/撤权/过期/终态尝试 |
| `GET /api/v1/tasks/{task_id}` / `POST .../cancel` | 沿用原任务详情及取消接口，取消请求体为 `{}` |
| `GET /api/v1/chat/conversations/{conversation_id}/files/{source_kind}/{source_id}/preview` | owner 查看所属会话真实附件或已验证交付物的有限文本／图片预览 |
| `POST .../files/{source_kind}/{source_id}/attachment` | `{}`：把已验证交付物按稳定来源编号复用为附件，保留幂等及原字节；保留旧客户端兼容 |

上传仍复用 [附件接口](chat-attachments.md)。普通 owner 写接口保留 Origin/CSRF；Node 下载不接受任意 URL、路径或重定向。`receive_file` 通用任务入口同样校验附件属于该 owner/会话、未删除/归档、大小/摘要及由请求编号推导的目标路径，不能伪造参数绕过专用接口。

工具参数为 `root_id, conversation_id, attachment_id, path, sha256, size_bytes`；结果为 `root_id, attachment_id, path, sha256, bytes_written`。Core 对结果身份、路径、长度及摘要逐项验证，不接收原始二进制作为 WebSocket 结果。验证级别是设备执行证据及读回摘要，不代表文件安全扫描或用户验收。

聊天函数 `send_file({source_kind: "attachment" | "deliverable", source_id})` 按目标设备进入原 work grant 和 Harness。来源必须属于同 owner、同会话；交付物的导出任务须成功、结果已知并完成验证。Core 推导附件、目标路径、字节数及摘要，提交前重新核对原步骤、权限和计划模式。结果仍绑定原任务／attempt；导入不产生新的模型调用或接收权限。`presentation.files` 最多8个有证据的文件卡片，刷新不重新执行传输。

## 有界执行与恢复

- HTTP 下载与既有上传共享两个传输槽，每任务最多三次下载请求；Node 执行仍受既有 10 秒上限和全局单执行槽约束。网络慢或磁盘不足会失败/超时，不自动增加执行预算；不提供断点续传。
- 接收命令沿用任务、attempt、摘要、持久 Node 回执、结果确认和恢复查询。该工具标为修改类，不自动创建新执行尝试。未知执行先对账，不能凭连接断开认定未发生保存。
- Windows 下载在可信父进程中进行，凭据不进入工作进程；有界流式下载、大小和 SHA-256 检查后，由受管子进程在原安全访问根中创建文件并读回。只创建新目标，临时文件不当作成功结果。
- Android 下载在独立 `:node` 进程，认证通道可即时取消网络调用；10 秒 watchdog 保留实际进程停止证据。App 私有目录以独占创建的请求子目录声明一次写入，再在该目录原子改名，避免 Android 对硬链接的限制。旧请求目录不能复用，崩溃遗留保持待核对。
- 被任务引用的附件不能作为未使用草稿删除，避免正在下载的原件消失；聊天外发仍需要独立同意。基础上传、状态轮询、下载和验证均不调用模型。

## 验证入口

`tests/test_chat_file_delivery.py` 覆盖实际电脑原件导出、第二 Node HTTP 接收、相同字节／摘要、聊天卡片、同会话绑定、撤权、计划模式和幂等。`tests/chat_file_cards.cjs` 检查浏览器与 Windows 桥返回格式、预览净化、原件校验和手机宽度；Android `MarkdownUiTest` / `FileCardsCoreTest` 检查真实 WebView Markdown、主动内容拦截和认证文件原件读取。模拟器通过不代替用户真机验收。
`tests/test_file_transfer.py` 覆盖来源归属、独立授权、CSRF、结果证据、幂等、拒绝覆盖、Windows 真实下载/工作进程、取消及错误摘要。`tests/test_file_transfer_process.py` 可指定 `NEXUS_FILE_NODE_EXE` 验证固定 EXE；界面精简与文件卡片分别由 `ui_refresh_browser.cjs`、`chat_file_cards.cjs` 验证。

Android `FileReceiveTest` 验证实际文件系统接收和读回；`FileReceiveCoreTest` 使用项目专用模拟器与 `scripts/serve-fixture.py` 临时身份验证完整 Core/独立 Node 链路。测试替身不能当作生产真人设备验收；真实手机及 Windows 安装升级体验由用户确认。
