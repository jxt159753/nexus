# S4 / P07 后端执行闭环

本阶段在既有 [认证 Link](s3-link.md)、[权限](storage-security.md)和 [Windows Runtime](node-runtime.md)上接入 Harness。批准与状态统一见 [ROADMAP](ROADMAP.md)，实测见 [S4 验证](S4-VERIFICATION.md)，实现决定见 [ADR 0004](decisions/0004-s4-durable-execution.md)。

## 运行与接口

仍按 S3 的两个 CLI 入口启动，先分别登记 owner / Node 身份及两个工具的持久授权。目录资源必须在 Node 显式映射 workspace，并在 Core 可信配置中允许该别名。默认无授权，不自动开放目录。无需新增依赖或模型密钥。

S4 通过已有 Cookie、Origin、CSRF 守卫提供 owner HTTP 入口；浏览器页面及 client WS 属于 S6。

| 接口 | 行为 |
| --- | --- |
| POST `/api/v1/tasks` | 严格 JSON `{node_id, request}`，request 为既有 TaskRequest；返回 `{task}`，HTTP 200 只说明有持久任务快照 |
| GET `/api/v1/tasks?after=0&limit=20` | 按 owner 隔离、按持久插入顺序分页；limit 1–100，返回 tasks / next_after；游标为十进制字符串 |
| GET `/api/v1/tasks/{task_id}` | 返回 task、至多两条 attempts、最近 100 条 approvals 和前 200 条状态 events；含结果、验证级别及原观测时间 |
| POST `/api/v1/approvals/{approval_id}` | 严格 JSON `{decision:"approve"\|"reject"}`；重新检查绑定和有效期，返回 task；不能修改动作 |

任务状态由 Core 决定，HTTP 不接受 status、result、owner_id、attempt_id 或自报授权。写操作需要会话对应的 `X-CSRF-Token` 和精确 Origin；读结果设置 no-store。正文上限 4 KiB、读取等待最多 2 秒；超限/非法 JSON 返回有限错误，不回显输入。未知意图返回 UNSUPPORTED_TASK，不调用模型。新任务每 owner 每分钟最多 10 个、全局非终态最多 100 个；相同 request_id 的重传不消耗新任务配额，内容或节点改变则冲突。

使用 S3 登录取得 `$session`、`$login`、`$origin` 后可运行：

```powershell
$headers = @{Origin=$origin; 'X-CSRF-Token'=$login.csrf_token}
$body = @{
  node_id='6909ccbe-2f7c-42ec-ab97-9fe1b03c425d'
  request=@{request_id=[guid]::NewGuid().ToString(); intent='get_system_info'; arguments=@{}}
} | ConvertTo-Json -Depth 5
$created = Invoke-RestMethod -Method Post -Uri "$origin/api/v1/tasks" -WebSession $session -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod -Uri "$origin/api/v1/tasks/$($created.task.task_id)" -WebSession $session
```

目录任务使用 `intent='list_directory'` 和 `arguments=@{root_id='workspace';limit=100}`。查询详情取得当前 approval_id，以同一会话 POST approve/reject。审批前没有执行尝试；批准只解锁完全相同的任务动作。配置范围改变时需以原 request_id 重新提交取得新的审批，旧审批不能复用。离线时可准备任务/审批，但注册能力、当前身份和持久授权仍在真实派发事务中重查。

## 持久权威与验证

迁移 003 由原迁移器执行；升级前自动生成并校验一致备份。新增 `execution_grants`（逐任务动作授权）和 `task_attempts`，以及任务的当前尝试、有限结果/错误和验证级别。原 `grants` 继续表示身份级静态许可，两者不可混同。

Harness 只调度 ready 动作，每 Node 一个活动尝试。它在同一事务中重查活动 owner、认证 Node、可信/注册工具与资源交集、当前策略、审批绑定/期限、任务状态与执行额度，再保存动作 grant、固定命令、attempt、dispatching、revision、事件与审计；提交成功后才进入有界发送队列。浏览器会话断开不会撤销已经持久化的用户意图；每次派发继续检查持久身份和授权。

Node 在线程中调用原受管理 Runtime，事件循环继续发送心跳和处理状态查询。received / started 回执落盘后才产生对应消息；完成结果仍先落盘。网络断开时等待实际受管理执行结束，保留本地证据，不丢弃后台线程。运行中重复同 attempt 经完整动作身份校验后返回持久状态，完成后返回缓存观测，不重新执行。实际工具完成后释放执行标记，下一条命令等待前一结果发送收尾，再串行启动新工具，避免误报 BUSY。

Core 将连接身份、当前 session、task/attempt 归属、当前有效 attempt、工具版本及摘要匹配后，先持久原始有限结果并进入 verifying，再做确定性验证，最后 ACK。确认包丢失不阻止直接接收结果；重复证据须完全相同（仅允许传输 session 改变），才能返回 already_settled。错误身份、旧 attempt、变更结果、过大结果和不合理观测时间不能覆盖有效证据。

Core 和 Node 复用 contracts 中的工具结果验证器。目录检查 root_id、请求条目数量、字节、严格字段、直接名称及重复条目；不接受路径、目标内容或额外字段。有效身份但不满足工具约束的结果记为 failed / RESULT_INVALID；Node 自报失败保留有限错误，不进行重试。成功必须有结构化结果、known 和 `node_report_schema_checked`。该级别只证明来自认证 Node 的报告满足结构及约束，不能证明恶意 Node 诚实。

原始结果的落盘与确定终态分为两个短事务，verifying 在 Core 启动时重新验证；ACK 只在结果已可靠保存并完成当前处理后发送。数据库或调度器故障使 Harness 停止新提交/派发并关闭 Node 连接，历史仍可查询，不绕过审计继续运行。

## S4 的完成边界

每任务只创建一次尝试、只进行首次派发。派发后执行结果暂记 unknown，收到有效结果并验证后才 known。Core/Node 断线、发送失败、无结果状态或协议错误均不构成成功、失败或安全重试的证据；已存在的活动尝试会占住该 Node 的执行槽。

待审批/离线任务有持久期限，过期不派发；Node 对命令有效期和单次工具执行实施限制。Core 的总期限结算、确认超时、网络对账、有限重试、全局 pause/cancel_all 及取消竞争留在 S5。本阶段保留未知状态并停止相关新执行，可能需要后续恢复工作；不能把它描述成已完成故障恢复。远程 TLS 和 Android 仍须后续真实验收。
