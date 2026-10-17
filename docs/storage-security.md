# Core 存储与安全接口

S2 / P03–P04 的实现说明；阶段状态与验收证据分别见 [ROADMAP](ROADMAP.md) 和 [S2 验证记录](S2-VERIFICATION.md)。目标设计见 [ARCHITECTURE](ARCHITECTURE.md)。这里的仓储与管理接口属于可信 Core 内部接口，不是开放的网络 API。

## 数据库与任务

- `Database(Path)` 只接受绝对本地路径；`initialize()` 在离线管理或后续单活 Core 启动期间运行。不得使用网络共享或把同一数据副本交给多个活动 Core；正式进程单例锁将在应用生命周期接入时落实。
- `initialize()` 创建版本表并执行包内 SQL；验证版本连续性及摘要，拒绝无版本旧库、未知新版本和被修改的已应用迁移。已有库升级前生成带 UUID 的 `.before-vN-*.sqlite3` 一致备份并做 quick_check，失败保留原库，不重建、不降级。
- 每次仓储操作使用一个短生命周期 aiosqlite 连接，显式外键、FULL 同步、3 秒 busy_timeout；初始化启用 WAL。写事务使用 BEGIN IMMEDIATE，异常或取消时回滚；返回值只在 commit 成功后交给调用方。
- `TaskRepository.create(owner_id, request, node_id)` 验证活动身份角色，规则生成不可变单步动作；按 `(owner_id, request_id)` 去重，相同请求和节点返回原任务，变更内容或节点报 `Conflict("IDEMPOTENCY_CONFLICT")`。最多保留 100 个非终态任务作为待处理队列；期限为创建后 5 分钟。
- `get(task_id, owner_id)` 检查归属；`history(owner_id, after_event_id=0, limit=100)` 按身份过滤并分页，limit 为 1–200。网络调用方仍必须先认证并校验角色，不能把用户提交的 owner_id 直接传入。
- 任务创建、revision 状态更新、事件和审计同事务提交。当前只实现 queued / awaiting_approval / ready 到派发前合法状态或失败、取消、超时的转移，终态不可覆盖；过期任务不能转 ready。没有执行工具或记录 succeeded 的接口。
- `TaskRepository.transition` 是可信 Core 持久化原语，不能直接暴露为客户端状态修改接口。权限层调用它完成审批与状态的原子更新；P07/P08 才实现带有效 attempt/工具证据的执行状态和真实取消。

网络、模型和工具调用不得放在数据库事务内。数据库写入失败会向调用方抛错，不返回“已接受”或“已授权”。当前没有自动定时器、清理历史、任务调度、工具派发或执行恢复。

## 身份和离线管理

首版仅一个 owner，每个 Node 独立身份。令牌必须由安全随机源生成，格式为 43–128 个 URL-safe 字符，至少使用 32 随机字节；不能把人类密码填作令牌。数据库只保存 SHA-256 摘要，Python 对象用 SecretStr 隐藏值。摘要方案针对高熵随机凭证，不是密码散列方案。

安装项目后可使用本地管理命令；命令不是网络端点，不允许 Node 或浏览器调用其方法。`NEXUS_DATA_DIR` 指向专用本地数据目录，`NEXUS_ENROLL_TOKEN` 从用户的安全存储进入进程环境；不把令牌放入命令参数、Git 或输出。每次 register/rotate 前分别提供对应身份的新令牌。下面的 UUID 仅作非秘密身份示例。

```powershell
python -m nexus.core.security register-owner 33552837-d424-4d3e-a8e3-cda3d3b54c59
python -m nexus.core.security register-node 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d
python -m nexus.core.security grant 33552837-d424-4d3e-a8e3-cda3d3b54c59 --tool system.info
python -m nexus.core.security grant 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d --tool system.info
python -m nexus.core.security grant 33552837-d424-4d3e-a8e3-cda3d3b54c59 --tool filesystem.list --root-id workspace
python -m nexus.core.security grant 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d --tool filesystem.list --root-id workspace
python -m nexus.core.security grant 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d --tool filesystem.list --root-id workspace --disable
python -m nexus.core.security rotate 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d
python -m nexus.core.security revoke 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d
```

rotate 替换凭证并重新启用身份；旧凭证失效。owner 撤销或轮换同时撤销其浏览器会话。授权默认全空，owner 与目标 Node 必须分别配置。命令不修改任何 Node 的文件映射，不创建真实节点或自动开放 workspace。

## 会话、HTTP 与 WebSocket

`SecurityHTTP(SecurityService(db), TransportPolicy(origins=...))` 提供可组装的 APIRouter，目前没有生产 app 或服务启动命令：

| 接口 | 行为 |
| --- | --- |
| POST `/api/v1/session` | 精确检查 Origin 与安全传输；最多 4 KiB 严格 JSON，字段只有 token；owner 凭证换取会话 Cookie 和 CSRF 值 |
| DELETE `/api/v1/session` | 会话、Origin 和 CSRF 全部通过后撤销会话并清除 Cookie |
| `owner_http(request)` | Cookie 会话认证；写操作另检查 Origin 与 `X-CSRF-Token` |
| `browser_ws(ws)` | 检查安全传输、精确 Origin 与 owner Cookie，会话身份来自数据库 |
| `node_ws(ws)` | 检查安全传输与独立 Node Bearer 凭证；owner 令牌、浏览器会话或 JSON 自报身份不能替代它 |
| `receive_text(ws, principal)` | 每秒检查空闲连接身份，每条消息返回前再次检查；撤销、轮换、过期抛出 1008 并由 FastAPI 关闭连接 |

会话有效期 8 小时，最多 10 个有效会话；会话和 CSRF 均使用随机令牌摘要。Cookie 为 HttpOnly / Secure / SameSite=strict，响应 no-store。仅显式 `local_http=True` 且仅 loopback 的 HTTP/WS 可关闭 Secure；默认 HTTPS/WSS。远程模式没有证书配置时不能启动后续服务，不关闭 TLS 校验或调整系统安全配置。

P06 必须接入守卫和接收接口，并校验消息方向、当前连接/会话与 JSON 中的节点标识，实施帧/速率/连接/队列限制；本轮只以临时 ASGI 应用证明安全边界，没有 Node 协议生命周期、心跳或重连。后续前端须管理登录响应中的 CSRF 值；当前不提供会话恢复页面或续期 API。

## 权限和审批

`SecurityService.prepare(task_id, principal, scope)` 在事务内重查身份和 owner 归属，并计算已知工具、owner 授权、Node 身份许可、注册能力和本地范围的交集。`NodeScope` 来自后续认证 Link/可信配置，绑定目标 node_id、工具和 root 别名的允许集合、resource_version；它只能收紧持久授权。配置或资源映射变更必须递增版本；本地路径的实际安全检查仍属于 P05。

- system.info 需双方显式授权，成功转 ready；filesystem.list 需允许的 workspace 资源并逐任务审批，先转 awaiting_approval。
- 审批绑定 task/owner/node、工具与版本、规范化参数 SHA-256、全局策略版本、当前范围摘要（含资源版本）及期限。有效期最多 2 分钟且不晚于任务期限。
- `decide(approval_id, principal, "approve"|"reject", scope)` 只允许活动 owner，重新检查所有绑定；决定、任务状态、事件和审计同事务提交。同决定重传幂等，反向决定失败。拒绝进入 failed，不能再批准。
- 审批过期可通过 prepare 重新申请；参数、节点、资源范围、策略或身份变化后的旧批准不可复用。取消或终态不能重新解锁。
- 硬性禁止/未实现工具没有配置与审批升级路径。目录审批无法转成任意路径、shell 或写文件权限。
- `require_authorized` 只证明检查时刻有效，不是可转交的执行令牌；P07 必须在同一派发事务中重新检查当前策略、审批期限、任务状态与执行预算，再创建 attempt/grant。S2 没有派发入口。

审计区分 actor 和 subject；本地离线管理的 actor 为空、subject 为被管理身份，认证会话和任务操作记录真实 owner。审计不保存原始凭证或任意工具输出。
