# S3 Core / Windows Node 认证通信

本阶段实现 P05 本地工具及 P06 连接层。状态与批准入口以 [ROADMAP](ROADMAP.md) 为准，实测证据见 [S3 验证记录](S3-VERIFICATION.md)。Core 只提供身份与 Node 通信；任务调度、派发与成功结算属于 P07。

## 启动与配置

安装锁定依赖和项目后，在两个终端分别运行：

```powershell
# 先按下文完成目录准备、身份登记和环境凭证注入。
& .\.venv\Scripts\python.exe -B -m nexus.core --config config/core.example.toml
& .\.venv\Scripts\python.exe -B -m nexus.node --config config/windows-node.example.toml
```

示例配置位于 [Core](../config/core.example.toml) 和 [Windows Node](../config/windows-node.example.toml)。它们显式启用仅字面 loopback 地址可用的 HTTP/WS 开发模式；当前没有 Web 页面或提交任务接口。目录 `E:\NEXUS\var\core` 与 `E:\NEXUS\var\node` 分别存放 Core 数据和 Node 回执，Node 目录及资源根必须事先存在：

```powershell
New-Item -ItemType Directory -Force E:/NEXUS/var/core, E:/NEXUS/var/node | Out-Null
```

使用 [离线身份管理](storage-security.md) 登记示例中对应的 owner 与 Node UUID，分别使用独立的高熵随机令牌。Core 保存摘要；Node 启动前由安全存储将 Node 原始令牌注入 `NEXUS_NODE_TOKEN`。原始令牌不能放入 TOML、URL、命令参数、Git 或日志。配置中的工具及 workspace 只表示本地能力上限；owner 和 Node 的持久授权仍须分别设置。目录逐任务审批及执行在 P07 接入。

配置优先级为指定环境变量 > 指定 TOML > 模型安全默认值，不自动加载 `.env`。TOML、嵌套策略和 Node 配置拒绝未知字段，读取上限 64 KiB。凭证字段不能写入 TOML。

| 进程 | 可覆盖的环境变量 |
| --- | --- |
| Core | NEXUS_DATA_DIR、NEXUS_HOST、NEXUS_PORT |
| Node | NEXUS_NODE_DATA_DIR、NEXUS_CORE_URL；凭证另由 NEXUS_NODE_TOKEN 提供 |

Core 默认 local_http=false，必须配置 tls_cert / tls_key 后才能启动远程 HTTPS/WSS。Node 默认要求固定 `/ws/v1/node` 的 WSS URL，使用默认 CA 验证；自建 CA 可显式配置 ca_file。禁止关闭证书或主机名校验。Node 拒绝带用户名、密码、query 或 fragment 的 URL；不跟随握手重定向，不使用环境代理，不启用压缩。远程证书部署与 Android 信任仍留待 P11，系统信任、防火墙或 TLS 校验不会自动修改。

Core 固定单 worker；同一规范化数据目录只允许一个 Core 进程，取得进程锁后才初始化权威数据库。锁与 Node 的数据目录锁共用可移植文件锁辅助模块，Core 不导入 Node 或 Windows 工具 API。现有 SQL 模式没有改变；初建或升级仍遵循 S2 的版本、备份和校验规则。

## 身份、能力与在线观测

Core 暴露 `/ws/v1/node`，Node 主动连接。Core 在握手认证独立 Node Bearer 凭证，拒绝 owner 凭证；第一条消息必须为 node.hello，node_id 须等于已认证身份，且在 Core 可信配置中登记。node.connected 审计提交后才发送 node.welcome。节点当前 session_id 来自 Core；boot_id 仅表示 Node 进程身份，不能证明之前未执行。

能力交集为：固定已知工具 ∩ Core 对该 Node 的可信配置 ∩ Node 实际配置的自报能力 ∩ owner 持久授权 ∩ Node 身份持久授权。workspace 同样取资源别名交集；这里不传绝对目录路径。双方授权、身份及资源策略分别校验，Node 的自报信息不能新增权限。查询时重新计算持久授权，撤销授权后即刻收紧显示能力；活动凭证撤销或轮换在空闲时最多约一秒及每条消息前重查。

同一 node_id 的新会话替换旧连接；先转移当前连接权威，再关闭旧连接，旧消息即使已在途中也不能继续入队。取消或断线清理先移除连接和容量占用，再停止子协程并屏蔽 ASGI 取消对审计清理的干扰，避免旧连接残留。

Node 默认每 10 秒发送应用心跳，Core 默认 30 秒超时；可信配置只可收紧到间隔 1–10 秒、超时 2–30 秒且超时大于间隔。Core 使用接收端单调时钟，不信任消息 sent_at。心跳离线仅改变连接观测，不更改任务、尝试或 execution_outcome。Node 用协议 ping/pong 检查连接响应；断线后重新认证、重新 hello，以含抖动的 1–30 秒指数退避重连，成功连接后重置退避。重连不改变本地回执、不重跑工具、不调用模型。

登录 owner 后可查询节点：

```powershell
# NEXUS_OWNER_TOKEN 同样须从安全存储注入，不能在终端回显。
$origin = 'http://127.0.0.1:8000'
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$login = Invoke-RestMethod -Method Post -Uri "$origin/api/v1/session" -Headers @{Origin=$origin} -ContentType 'application/json' -Body (@{token=$env:NEXUS_OWNER_TOKEN} | ConvertTo-Json) -WebSession $session
Invoke-RestMethod -Uri "$origin/api/v1/nodes" -WebSession $session
```

`GET /api/v1/nodes` 只允许已登录 owner，最多返回 32 个可信配置中的节点，每项包含 node_id、online、有效 tools / roots、当前 session_id / boot_id。不会输出令牌、摘要或本地路径。连接观测保存在可重建内存中；重启后必须重新连接才能在线，不把旧在线标记当作事实。

## 有界传输与后续接口

| 限制 | 实际行为 |
| --- | --- |
| 握手/连接 | 最多 32 个活动及待认证连接；所有握手合计每 10 秒最多 60 次；hello 最多等 5 秒 |
| 输入 | 每连接每 10 秒最多 60 条应用消息；协议消息最多 64 KiB，可由 Core 收紧；未知字段、版本、方向或会话拒绝 |
| 缓冲 | Core 每连接发送/接收队列各 32 条；Node 底层接收缓存 16 帧，单帧最多 64 KiB，写缓冲高水位 32 KiB |
| 阻塞 | 发送最多等 2 秒；连接建立最多等 5 秒、关闭最多等 2 秒；满发送队列返回 BUSY，满接收队列关闭连接 |
| 结果 | 只允许完整有限结构；结果字节按协商预算检查，Core 配置至少为元数据预留 2048 字节 |

`app.state.node_link` 是可信 Core 内部接口，没有面向 HTTP 的任意发送入口。`send(node_id, kind, payload)` 目前仅允许 command.status / result.ack，并绑定当前 session；不允许 command.execute。Node 可回复已有回执的 command.status_result，并在可信 ACK 后标记本地回执；查询结果中的 session 换成当前连接，保留原观测时间和动作身份。若缓存结果超过收紧后的预算，保留本地证据并断开，不错误确认或删除它。

Core 收到的 status_result / error 进入当前连接的有界 incoming 队列，仅是尚未信任、未结算的传输证据。P06 不自动 ACK 结果、不推进任务状态、不建立持久派发队列；未来 P07 消费者须先校验归属及有效 attempt、落盘，再发送 result.ack。断线可能丢失内存消息，本地回执保留以便后续对账。

当前 Node 对网络 command.execute 明确返回 POLICY_DENIED，Core 内部发送入口也拒绝执行派发，避免绕过尚未实现的 P07 权威事务。P05 本地执行接口保持可用，其真实工具与进程验证仍见 [Node Runtime](node-runtime.md)。全局控制、有限重试和网络故障结算属于 P08；S3 交付不表示后端任务闭环或 MVP 完成。

WebSocket 客户端选用锁定的 websockets 17.1，同时供 Uvicorn 的 Sans-I/O 后端使用；已有框架及其他 24 个依赖版本不变。API 边界参考 [websockets asyncio client 官方文档](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)；本项目关闭重定向、代理和压缩的行为由本地源码与测试验证。
