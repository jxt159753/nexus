# M2：DeepSeek 与自然语言任务

阶段批准、进度和实测证据统一见 [ROADMAP](ROADMAP.md#8-m2真实模型与受约束自然语言任务)，设计见 [ARCHITECTURE 第 14 节](ARCHITECTURE.md#14-m2真实模型与自然语言任务)和 [ADR 0009](decisions/0009-m2-deepseek-language.md)。本文件说明使用与运维，不另维护阶段状态。

## 使用流程

1. 按原部署流程登录同源控制台，选择已配置 Node，可选关联项目。
2. 输入最多 2000 字、6000 UTF-8 字节的单步任务。已知固定短语如“读取系统信息”“列出 workspace 目录”先匹配规则，零模型调用。
3. 需要模型解释时，勾选本次外发同意后提交。发送给 DeepSeek 的是当前文本及静态允许动作说明；不自动附带历史、记忆、目录内容或设备凭证。不要在输入中填写秘密或不希望外发的信息。
4. 页面展示解释状态和拟执行动作；核对目标节点、参数和到期时间后点击确认。未知/不支持/非法输出不会创建执行任务；单次请求失败后不会自动重新调用模型。
5. 确认后进入原任务历史与 Harness。系统信息依赖已有只读授权，目录任务仍需原审批。成功仍以真实工具证据和原验证级别为准。
6. 可取消解释请求或使用全局取消。关闭已开始的 HTTP 交换不能撤销此前数据外发或供应商计费。已创建的执行任务应在任务历史中取消；全局取消同时覆盖解释和执行任务。

自然语言请求与单次任务输入属于有限审计历史，不自动保存为长期个人记忆。刷新后可查询最近 20 条解释请求，分页加载更早记录；浏览器最多保留 100 条。用户手动重试同一未确认提交时沿用 request_id；页面刷新后如需确认先查询历史，避免重新创建一次付费请求。

## 启用与密钥

默认 models.enabled=false，原只读按钮和规则解释可以继续使用。Core 启用模型需要同时满足：安全注入 Key、有效价格快照、剩余预算、未暂停和本次 allow_model=true。

在 **Core 所在机器** 的私密终端注入 DEEPSEEK_API_KEY。不要把 Key 写到 TOML、聊天、URL、浏览器、Node 配置或仓库文件中。本机 PowerShell 可使用遮蔽输入：

```powershell
$m2Key = Read-Host 'DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $m2Key).Password
Remove-Variable m2Key
```

这是进程环境配置，启动 Core 后由其继承；终端结束后不保留。之后在可信 Core TOML 的 `[models]` 下设置 `enabled = true`，其余字段参考 [示例配置](../config/core.example.toml)。启用却缺 Key 时明确拒绝启动。云端 systemd 应使用运行用户可读、权限受限且不入库的 EnvironmentFile 注入，沿用 [停机备份与恢复](s7-deployment.md)；不要把 Windows 环境变量误认为已传到云端。

升级前停 Core 并取得一致备份，再部署新代码及锁定依赖、启动唯一 Core。迁移 006 自动在旧库旁生成 `before-v6` 一致备份，扩展调用账本并保留旧记录；Node 契约和工具不变。旧代码不认识新 schema，回退须停止新 Core 后恢复升级前备份及旧代码，不能直接用旧代码打开新库。恢复备份会回到备份时刻，发生真实调用后的费用对账必须另行保留，不能通过恢复旧账本再次消费额度。

## 预算与费用含义

用户批准上限为每天 USD 1、每请求 USD 0.05；配置只允许收紧。内部采用整数 USD microunits（1000000 = USD 1），不使用浮点货币。每天按 UTC 划分，每请求最多一次、全局每天最多 100 次、同时一次；输出最多 512 token，总网络时限 30 秒；HTTP 请求体/响应分别最多 8/32 KiB。输入正文最大 6000 字节，连同固定提示词受 8 KiB 请求体限制；响应报告输入 token 超过 16384 会拒绝。

调用前在同一事务预留整项请求费用上限并记录 call_id。用量可信且返回时，按快照峰值缓存未命中价格计算费用上界，释放多余预留；即使解释被拒绝，也记录已报告用量。供应商账单可能因时段/缓存等更低，此数不是供应商扣款回执。没有用量、断线、超时或重启时保留完整预留，费用未知不显示为零，跨日也不释放；不自动重试或自动对账扣款。

默认价格核对于 2026-10-18、有效至 2026-10-25（UTC，含当日）：deepseek-flash 输入 USD 0.30/百万 token、输出 USD 1.20/百万 token，按公开峰值费率。到期后停止新调用，需人工核对官方价格并更新可信快照；不自动扩大预算。应用只能按所配置价格计算和限额，供应商临时改价或账户其他应用的费用不受本账本控制。[DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)

未知费用的调用记录持续占额度；在供应商记录核对和恢复流程尚未扩展前，不通过改库、清库或恢复旧备份释放额度。

## API 与记录

HTTP 写操作沿用 owner 会话、Origin、CSRF 和有限请求体检查：

| 接口 | 用途 |
| --- | --- |
| POST `/api/v1/language` | 严格 LanguageSubmission：request_id、node_id、可选 project_id、text、allow_model |
| GET `/api/v1/language?before=...` | 逆序查询 20 条记录，cursor 用于加载更早项 |
| GET `/api/v1/language/{language_id}` | 查询有限输入、解释状态、动作、错误及关联 task_id |
| POST `/api/v1/language/{language_id}/confirm` | 空对象；确认不可变建议，原子创建并关联执行任务，重复返回同一任务 |
| POST `/api/v1/language/{language_id}/cancel` | 空对象；取消尚未提交执行的解释请求 |
| GET `/api/v1/models/usage` | 调用数、费用上界或未知、未知费用数及启用状态 |

主要前置状态为 queued → interpreting → proposed → submitted；规则跳过 interpreting。其余状态为 rejected / failed / cancelled / expired。解释及确认总期限为 5 分钟，确认后执行任务沿用原 5 分钟期限。Core 重启将 interpreting 置为失败、调用费用未知；尚未解释的 queued 可以在剩余期限内继续。暂停只阻止新调用与确认，新请求可先持久排队。

language_requests.mode 标记 rule / llm；model_calls 保留请求/任务关联、模型标识、用量、价格、预留和结算状态。执行 routing_decisions 仍记录已经验证契约的确定性动作路由，不把它当作模型调用计数。模型不拥有任务终态或授权接口。
