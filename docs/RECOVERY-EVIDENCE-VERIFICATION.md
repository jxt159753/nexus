# P51：Windows 回执、时钟与执行槽恢复

2026-10-21，用户授权修复“列出工作区待派发、固定 Python 命令无后续”，并部署上线、上传 GitHub。本轮技术交付完成，最终用户验收仍待用户实际使用确认。

## 根因与修复

旧任务 `3cc3efec-257f-4a14-b353-b903a5d8ce3f` 已超时，但尝试 `0ff44d7c-a86e-4efd-b0ba-4105fec6909d` 仍占用单执行槽，主动对账计数达到 64。Windows 的持久回执实际为 completed、execution_stopped=1、尚未确认；回执观测时间比 Core 创建任务早 5.095 秒，超出原 5 秒容差，Core 因此拒收，重连重传也无法结算。单看设备在线无法发现这一阻塞。

修复区分时间有效性与停止事实：完成回执通过认证、task/attempt、工具版本和命令摘要检查后，时间异常的业务输出仍被拒绝，记录 `attempt.result_time_rejected`，但其停止证据落盘并释放槽。原终态和 unknown 结果保留，不把旧任务改成成功，也不重跑动作。重复回执确认不会重复修改任务；不匹配的身份或摘要仍被拒绝。主动查询仍最多 64 次，不因重连增加额度。

Node 的 cancelled、unknown 且 execution_stopped=true 回执现在也进入原有每页 4 条、每连接每条最多 3 次的重传，Core 持久化后确认。无停止证据的 unknown 不确认、不释放槽。

上线检查发现 Windows 仍慢约 9 秒，时间服务停止，当前进程没有启动服务权限。未修改系统时间、启动类型或服务器配置。Node 改用认证 welcome 信封的 Core UTC 与本机单调钟计算命令期限，在父进程完成工具执行后记录一次回执观测时间；缓存重传不重新校时。Core 原时间容差保持，未新增协议字段、依赖或数据库迁移。

## 实际验证

| 环境与范围 | 实际结果 |
| --- | --- |
| 首轮停止证据、Harness、回执、Node、网络和 Windows 进程回归 | 114 通过，90.49 秒 |
| 增加 Core 时钟后，Windows 真实进程、网络、Node 恢复相关模块 | 48 通过，59.77 秒 |
| 最终 Windows 固定 wheel：新恢复场景、Harness、恢复、回执 | 74 通过，16.81 秒；测试缓存写入权限警告 1 项，应用测试无失败 |
| 最终 Ubuntu/Python 3.12 固定 wheel | 165 通过、2 项 Windows 专用真实 worker 测试跳过，33.32 秒；该两项已在 Windows 运行通过 |
| 两端固定包 | 141 个运行资源逐字一致；云端 25 个既有依赖版本与锁文件一致 |

新增场景覆盖时钟快/慢、64 次查询耗尽、终态保持、重复证据、停止回执重传/确认、无停止证据不确认、真实 WebSocket 解锁后派发下一任务、Core 时间基准下的过期命令及真实 Windows worker、跨重连缓存时间不变。首轮新增测试有 5 项因测试辅助函数传入 datetime 而非 JSON 字符串失败；修正测试输入后重跑通过，没有放松生产断言。

没有重跑全项目完整回归；并行 Windows App、HTTP Origin、前端和打包改动排除在本轮安装包与提交之外。真实模型生成、整轮聊天写文件未在生产重复调用；既有连续上下文和失败反馈测试在 Linux 最终包中通过，相关历史证据见 [P50 验证](CHAT-CONTINUITY-VERIFICATION.md)。

## 生产部署与真实设备证据

最终 wheel SHA-256：`bade76b64ce4f5e5b7e2262029548fb07d46dbd58105c170a66de8dd381d3760`。Core：`/opt/nexus/releases/recovery-clock-20261021`，schema 020，唯一 Core 服务 active。Windows Node 使用同一 wheel 离线升级，保持原解释器、固定命令摘要、身份、工作区和配置。

部署前已暂停新派发，备份并核对数据库完整性、外键及全部旧行，配置与模型密钥摘要保持。最终备份：`/var/backups/nexus/before-recovery-clock-20261021T101735Z`，数据库 SHA-256 `e9675826e6e9d521f815506467e46ec19dd708b3acc5821f57894654b1f99496`。Windows 升级前另备份安装和 40 条回执。中间停止证据修复包已先上线以恢复旧回执，再用独立最终目录加入运行时时钟修复，未覆盖旧发布目录。

原 44 个任务、21 个会话、36 轮聊天及 54 次模型调用在 API 快照中核对；旧任务仅允许并核实其尝试 phase 从 running 到 completed、execution_stopped 从 0 到 1。通过真实回执协议对账，Node acknowledged 从 0 到 1，Core 已无未结束尝试。旧任务仍 timed_out/unknown，结果未采信，probe_count=64、dispatch_count=1 保持。已取消的两次用户请求没有重新执行。

恢复原未暂停状态后，通过真实云端 Core → Windows Node → 回执验证链路新增三个有界任务：

| 验证动作 | 任务 ID | 结果 |
| --- | --- | --- |
| 系统信息 | `5fedf0b2-2c03-4bc0-90da-6f189c1f7068` | succeeded/known，1 次尝试，1.08 秒 |
| 列出 workspace | `82ad5594-0f80-44a6-b793-bc445b95adc3` | succeeded/known，1 次尝试，0.59 秒 |
| 固定 python-version | `86f4681c-49e5-479a-9dee-c05fc486f58c` | succeeded/known，1 次尝试，退出码 0，0.58 秒 |

验证级别均为 `node_report_schema_checked`，不是独立 OS 业务证明。最终 47 个任务，Windows Node 在线，派发未暂停；模型账本仍 54 次、3 笔未知费用保留，本轮真实模型调用 0，无用户文件写入。Sandbox 保持升级前离线，本轮没有新增沙箱需求或扩大授权。

原始快照、构建、测试与发布元数据保存在 Git 忽略的 `var/recovery-evidence-release/`、`var/recovery-clock-release/`，不提交生产配置、数据库、回执或凭证。
