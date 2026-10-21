# Windows Node 本地执行接口（P05）

本文描述 P05 的两个固定只读工具、本地策略、执行回执和受管理工具进程。P06 已组装 [Node Link](s3-link.md)，P07 已接入 [Core 持久调度与网络执行](s4-harness.md)；调用方必须先认证 Core 并检查当前会话。`grant_id` 和命令摘要不能替代授权。授权与任务进度见 [ROADMAP](ROADMAP.md)，本地技术证据见 [P05 验证记录](P05-VERIFICATION.md)，本轮回归见 [S3](S3-VERIFICATION.md)。

阅读范围：下文原两工具示例保留 M1/P05 基线。后续 Windows Node 已扩展受控文件读写/查找与固定命令，见 [M6 工具说明](m6-projects-tools.md)及[聊天工作](chat-work.md)；注册表不再固定为两项，资源、回执、Job 与未知结果规则继续复用。生成 Tool 由[独立 Sandbox Node](m4-extension-protocol.md)执行，不在普通 Windows 工具进程中运行。

## 配置与固定工具

`nexus.node.config.NodeConfig` 使用严格 Pydantic 模型。Python 配置示例（目录须事先存在）：

```python
from pathlib import Path
from uuid import UUID
from nexus.node.config import NodeConfig

config = NodeConfig(
    node_id=UUID("7cc7b698-b36a-4ecd-944a-6f24420253fe"),
    data_dir=Path(r"E:\NEXUS\var\node"),
    allowed_tools=frozenset({"system.info@1", "filesystem.list@1"}),
    roots={"workspace": Path(r"E:\NEXUS")},
)
```

这是显式本地范围示例，不自动授予 Core 身份权限或目录审批。默认 `allowed_tools` 和 `roots` 均为空；当前只接受 `workspace` 别名。配置只校验已有目录，不创建目录、执行工具或读取凭证；S3 的 NodeServiceConfig 已接入 TOML/环境变量，见 [启动说明](s3-link.md)。数据目录应专用于一个 Node，不能与 Core 数据库混用。

| 项目 | 本地限制 |
| --- | --- |
| 工具注册表 | M1 两项为 `system.info@1`、`filesystem.list@1`，均只读；后续工具按严格契约登记并显式配置，写入/命令不沿用只读重试策略 |
| 系统结果 | 仅 os_name、os_version、architecture、python_version |
| 文件资源 | 绝对本地目录；拒绝 UNC、设备路径、网络映射盘、通配符、`..`、附加数据流及根/祖先 reparse point |
| 目录结果 | 仅直接条目的名称和类型；不递归、不读取正文，`stat(follow_symlinks=False)`，链接/junction 标记 link |
| 目录条目 | 请求 limit 为 1–200；按实际迭代次序取有限前缀，不为排序遍历整个目录 |
| 结果字节 | 默认 32768，可收紧为 128–32768；按紧凑 UTF-8 JSON 计算，数量/字节不足返回 truncated=true |
| 执行时间 | 默认最多 10 秒，可收紧为 1–10；实际取命令剩余期限、命令执行限额和本地限额的最小值 |
| 执行并发 | 每个数据目录一个 Runtime，每个 Runtime 一个工具执行槽；新命令忙时返回 BUSY |
| 回执容量 | 默认最多 10000 条，可收紧；满后拒绝新 attempt，保留旧回执及重复查询，不自动删除 |

启动、每次执行前以及子进程观察前重新校验资源根。应用路径检查不抵御恶意本机管理员或本地路径竞态；Job Object 只负责生命周期，不是执行不受信代码的安全沙箱。

## 命令身份与回执

共享函数 `nexus.contracts.commands.command_identity(command, node_id)` 将命令字段按键排序，以 `ensure_ascii=False`、紧凑 JSON 编码；排除 session_id 与 command_digest，加入可信目标 node_id。`command_digest()` 为上述 UTF-8 文本的 SHA-256。

摘要覆盖 task_id、attempt_id、目标 Node、工具/版本、参数、grant_id、expires_at 和 execution_timeout_seconds。日期先通过契约归一化；会话变化不影响摘要。P07 发送方已复用该算法；不能自行计算一套摘要或延长重传命令的期限。

`NodeRuntime` 是同步本地 API，可由后续异步 Link 在受控工作线程调用。对外 JSON 仍先经过 v1 编解码入口；Runtime 再校验构造后被改坏的命令实例。

| 接口 | 行为 |
| --- | --- |
| `with NodeRuntime(config) as runtime` | 获取数据目录独占锁，初始化并校验回执库、Node 身份及 SQL 摘要，恢复 started；关闭时请求停止当前执行并等候确认 |
| `execute(command)` | 新命令先本地校验、落盘 received，再落盘 started，然后执行；结果先落盘，再返回 CommandStatusResult |
| 重复 `execute(command)` | 完全相同 attempt 只返回原阶段或缓存结果；不同动作返回 IDEMPOTENCY_CONFLICT；started/unknown 不再执行 |
| `status(attempt_id, session_id)` | 查询 not_found/received/started/completed/cancelled/unknown；缓存结果换用当前 session，保留原观测时间和动作身份 |
| `cancel(attempt_id)` | 仅请求停止当前活动尝试，返回是否找到；不能把这个返回值当成停止确认，最终查 status |
| `receipts.unacknowledged(after="", limit=100)` | 按 attempt UUID 文本分页读取未确认 completed，limit=1–100；游标使用上一页最后一个 attempt_id 的字符串 |
| `receipts.acknowledge(attempt_id)` | P06 Link 在收到可信 result.ack 后调用；标记已确认但不删除结果或去重记录；Core 仍须由后续持久消费者决定何时 ACK |

没有 Core 任务终态写入入口。completed 指 Node 已持久化成功结果或结构化失败；不是 Core 已验证成功。目录结果还会检查 root_id、请求条目数及结果字节；Core 的权威验证已在 P07 接入，并与 Node 共用 contracts 中的结果约束。

回执库为专用 `receipts.sqlite3`，包含 `execution_receipts`、取消墓碑及模式/Node 身份元数据。只记录有限命令身份、boot_id、局部阶段、Job 名称、停止确认及结果，不记录个人长期记忆。使用版本 SQL 001/002、摘要校验、FULL 同步及短事务；拒绝无版本库、未知模式、篡改摘要或其他 Node 的数据库，不覆盖或重建旧库。S5 已加入前向升级及升级前经 quick_check 的一致备份。

received 落盘失败或 started 落盘失败时不启动工具。结果落盘失败时不返回缓存成功，并停止新的执行；重开时将原 started 保留为 unknown。已完成回执即使命令后来过期或连接更换仍可重传；这不会再次运行工具。Core 对重传授权及当前会话的验证由后续 Link 完成。

## Windows 进程与恢复

父进程先持久化 started 和随机 Job 名，再启动固定 `python -B -m nexus.node.worker`。父进程将子进程加入设置了 KILL_ON_JOB_CLOSE 的 Windows Job，成功后才通过 stdin 开启工具输入门；子进程也检查自身属于该 Job。Job 管理句柄不继承，子进程临时查询句柄在观察前关闭。没有输入门、归属验证失败或纳管失败时，工具不运行。

执行采用单调时钟，stdin 写入和 stdout 读取不会阻塞监督循环。内部输入和输出最多 64 KiB；超量、子进程退出异常或证据无效保留 unknown。超时/取消关闭 Job 并等待本进程和 Job 内进程结束，确认失败时保留 unknown。stdout 只承载有限 JSON，stderr 不作为工具结果暴露。

Node 意外退出时 OS 清理 Job；重启后查询原 Job 是否还有进程。started 永远转为 unknown，即使已确认进程结束，也不能据此推断读取没有发生。若停止尚未确认，阻止新执行；确认停止后可接受 Core 创建的另一 attempt，但绝不自行重试原 attempt。仅 received 的命令可以在重新校验策略和期限后继续。

本地进程停止是 P05 的基础设施；P08 已接入 Core 重试预算、持久暂停、取消竞争和网络对账，见 [S5 说明](s5-recovery.md)。Node 同时增加取消墓碑、停止证据和模式升级前一致备份。Windows API 依据 [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 和 [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject) 封装，平台代码仅在 Node 内。
