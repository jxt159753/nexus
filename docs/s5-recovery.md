# S5 / P08 恢复与全局控制

S5 在 [S4 后端闭环](s4-harness.md)上实现恢复、限额和用户控制。授权及状态以 [ROADMAP](ROADMAP.md)为准，实际验证见 [S5 验证](S5-VERIFICATION.md)，决定见 [ADR 0005](decisions/0005-s5-recovery-controls.md)。沿用原启动配置，无新依赖或模型密钥。

阅读范围：下文第二次尝试与原目录审批描述 M1 只读入口；M6 写入、固定命令及生成 Tool 不自动新建尝试，聊天/网页交换也不因重启自动重发。当前工作范围与取消语义另见[聊天工作](chat-work.md)，持久化、未知先对账及真实停止要求继续适用。

## Owner HTTP 接口

写操作继续要求有效 owner 会话、精确 Origin 和 `X-CSRF-Token`。JSON 正文上限 4 KiB、读取期限 2 秒；响应 no-store。页面和 client WS 属于 S6。

| 接口 | JSON 正文 / 行为 |
| --- | --- |
| GET `/api/v1/control` | 返回持久 paused、revision、event_id |
| POST `/api/v1/control/pause` | `{request_id:UUID}`；停止首次派发、重传和新尝试；已运行的只读任务可完成 |
| POST `/api/v1/control/resume` | `{request_id:UUID}`；恢复调度，仍检查期限、策略和审批 |
| POST `/api/v1/control/cancel_all` | `{request_id:UUID}`；原子申请取消当前任务，返回 affected；不改变 paused |
| POST `/api/v1/tasks/{task_id}/cancel` | `{}`；返回 task 和 disposition=cancelled/requested/already_finished |

控制按 `(owner,request_id)` 去重，重复返回最初响应；同 ID 换操作返回 409。重复 cancel_all 不取消后来创建的任务。旧 pause 重复响应是历史收据，当前状态使用 GET 查询。控制有独立持久事件和审计；S6 再接入浏览器同步。

```powershell
# 使用 S3 登录取得的 $origin、$session、$headers，以及已创建的 $created
$request = @{request_id=[guid]::NewGuid().ToString()} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$origin/api/v1/control/pause" -WebSession $session -Headers $headers -ContentType 'application/json' -Body $request
Invoke-RestMethod -Uri "$origin/api/v1/control" -WebSession $session
Invoke-RestMethod -Method Post -Uri "$origin/api/v1/tasks/$($created.task.task_id)/cancel" -WebSession $session -Headers $headers -ContentType 'application/json' -Body '{}'
# resume、cancel_all 分别用新的 request_id 调用对应路径
```

## 恢复和预算

- Core 每 0.2 秒或由入口唤醒，总期限为创建后 5 分钟，包含审批、离线和重试。到期 timed_out；活动执行保持 unknown，直至完成或停止证据到达。已持久结果能证明执行结束，但过期任务不再变 succeeded。
- 新连接立即查询活动 attempt；首次派发后最多等待 5 秒再查询。每尝试每 5 秒至多一个周期请求，用户取消可立即发送停止。状态/停止请求每 attempt 合计最多 64 次，先持久计数，重启不清零。
- completed：保存、验证和 ACK；started：等待；received/not_found：重传原命令，保持 attempt、grant、摘要和期限。
- 每 attempt 最多 3 次派发（含首次和发送前预留），每任务最多 2 次执行。暂停、取消、过期、策略/审批改变时禁止重传；原授权失效或派发额度耗尽会结束任务并继续尝试停止旧执行。
- 明确 TOOL_TIMEOUT/BUSY 失败，或 `unknown + execution_stopped=true`，可在余下额度和期限内准备第二次只读尝试。新尝试重新检查身份、资源、策略及审批；目录审批失效时重新 awaiting_approval。
- `unknown + execution_stopped=false` 不重试、不释放 Node 槽。任务超时仍占槽；64 次对账额度用尽后停止自动查询，保留 unknown，需操作人员检查，不能通过清库跳过。
- Node 每 5 秒扫描最多 4 条未确认回执，包含 completed、cancelled 和已确认停止的 unknown，单连接每条最多主动重传 3 次；Core 查询仍可取缓存。ACK 丢失不重新执行，重建连接继续认证和对账，零模型调用。Core 仅在停止证据落盘后确认 cancelled/unknown；无停止证据不确认。
- 完成回执的身份、命令摘要匹配但时间不在原 5 秒容差内时，拒绝业务输出并审计 `attempt.result_time_rejected`，同时持久化执行停止；不改成 succeeded，不重跑该动作，保留终态与 unknown 业务结果。这样设备时钟漂移不会在耗尽主动查询后永久堵住队列。
- 认证握手后 Node 用 welcome 信封的 Core UTC 与本机单调钟计算期限，并在父进程保存完成回执时记录观测时间；重传缓存不改写时间。此基准不修改系统时钟，不放宽 Core 容差，避免设备时钟漂移持续拒绝新任务。

暂停和取消与实际 WebSocket 执行发送共用互斥边界；发送前检查持久控制、尝试、授权和期限。暂停先于发送生效时，已排队命令被拦截。停止已经发送的执行需使用取消。

## 取消与证据

未派发或旧执行已确认停止可立即 cancelled。其他任务先持久 `cancel_requested`，再要求 Node 停止；取消等待协程不构成停止证据。verifying 和终态返回 already_finished；待处理取消遇到已完成结果时按结果验证。

Node 先保存取消墓碑，封住尚未收到和 received 的命令，再设置活动执行的取消信号。started 工具经原 Windows Job 终止并确认退出后才保存 cancelled；已完成则上报原结果。墓碑与回执各默认最多 10000 条，满后拒绝新增并保留证据，不能绕过写入声称停止。取消后迟到的同 attempt 不启动工具，重启后仍有效。

终态由事务和 revision 决定；迟到结果只补证据/审计，不改终态，旧 attempt 不覆盖新 attempt。任务详情新增 cancel_requested；尝试详情含 execution_stopped、stop_reason、probe_count。known 表示有结束/结果证据，不等于成功；只有 succeeded 及验证级别才表示结果通过。

## 存储与升级

Core 迁移 004 增加取消、停止/对账计数、system_state、控制请求和事件；Node 迁移 002 增加取消墓碑。升级前都生成并检查一致备份，保留原身份、任务、审批和回执，不自动清库。Core 在异步连接打开/关闭期间取消时等待资源收尾，避免泄漏 SQLite 连接。

`command.status_result` 新增可选布尔 execution_stopped，缺省 false。Node 仅在证据充分时设置 true；缺少该字段的报告保持保守。严格旧 Core 会拒绝未知字段，因此应停机备份并同步升级 Core/Node；不混用新 Node/旧 Core。25 种消息名称和动作摘要不变。

不包含远程 TLS、Android 真机、前端、长期记忆和真实模型调用；对应后续阶段仍需批准与验收。
