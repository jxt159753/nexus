# M4 补充实现与部署验证

本记录对应 2026-10-19 实施与验证、10 月 20 日恢复发布的 M4 缺口，不覆盖正在进行的 M5 改动。当前阶段状态统一见 [ROADMAP](ROADMAP.md)。此前 DSH 原交付与第一次独立复核分别保留在 [原验证](M4-VERIFICATION.md)、[独立复核](M4-CODEX-REVIEW.md)。脱敏摘要见 [JSON](assets/m4-completion-evidence.json)。

## 版本与实际改动

- 发布包 SHA-256：`6f4a88b8c056ac17b52d8e99f0d6a8ded8440099438af86025f4322fe3270b34`；105 个源码/资源文件逐字节匹配该 M4 快照。原 25 个依赖版本不变。
- 新增顺序迁移 012，不修改既有迁移：Skill 请求身份和摘要、持久测试运行、沙箱执行日志与最多 64 次核对计数。已有 schema 010 生产库一致备份后升级到 012，逐表比较全部旧字段和旧行。
- Skill 支持声明字段的 `$input`，非法输入及可提前检查的步骤参数在派发前拒绝；同一 request_id 返回原运行，内容冲突拒绝，历史满后阻止新增。界面保留输入并在不确定响应后沿用请求身份。
- 测试/业务命令均先持久化再发出；命令摘要绑定版本、环境、输入及超时；Node 先存回执，Core 保存结果后再确认。重连查询原 ID，结果不能找回时保留真实未知，不重跑已完成步骤。
- 生成后自动运行受控测试；测试、批准、启用各自独立。环境变化使旧批准失效；取消时等待真实停止/核对；已结算证据不会被迟到断线覆盖。
- 修正模型公开契约，明确 Tool 测试字段、依赖、严格输入与嵌套结果格式。没有放宽解析器接受非法模型输出。

## 实际检查

| 层级 | 实际结果 |
| --- | --- |
| Windows 完整源码与独立安装包 | 最终独立快照的源码和安装包各 695 项通过（243.19 / 232.07 秒），`-W error`，独立包禁用源码 pythonpath |
| 最终包 Linux Core/协议/权限/扩展 | Ubuntu 24.04 / Python 3.12.3，588 通过、2 个 Windows 专用拒绝路径跳过 |
| 包与语法 | 105 文件匹配、Python AST、JS 语法、25 个依赖锁定及 pip check 通过 |
| 真实三进程流程 | Linux Core + Windows Node + runsc KVM；目录未批准时 attempt=0，批准后两项 v1 任务各 1 次，第三步真实沙箱统计成功；重复请求沿用同一运行 |
| 真实截断 | 207 项合成目录，返回 100 项且 truncated=true，统计 total=100；不宣称全目录总数 |
| 真实恢复 | 执行中杀死本任务 Sandbox 进程、结果确认丢失后重启 Sandbox、Core 重启；旧任务/尝试/沙箱执行不增加，恢复不调用模型 |
| 真实生成 | 两次分别获准的单次 DeepSeek 调用；第二次候选自动隔离测试通过，用户批准具体版本后已启用 |
| 云端升级 | 18 任务、11 会话/19 轮、原 24 次调用完整保留；再次更新契约保留第一次失败及第 25 次费用，完成后共 26 次、费用上界 USD 0.008424、未知 0 |
| 页面与手机 | 实际浏览器 390 像素，scrollWidth=375，无横向溢出、错误日志 0，刷新能力列表保留输入；用户对 Android 查看/展开/刷新问题回复“可以”，未提供机型/浏览器，未据此宣布整个阶段已验收 |

Linux 的 2 次 skip 对应 `test_sandbox_config_never_accepts_a_windows_host` 和 `test_runsc_preflight_refuses_on_windows_before_any_candidate_runs`，两者仅适用于 Windows。运行命令曾传入绝对 node id 的 deselect 参数，与实际相对 node id 不匹配；实际由测试内的平台条件跳过，没有额外 2 项被排除。没有删除测试凑数，Linux Core 回归不能替代独立 WSL 的真实沙箱验证。

Windows 命令（独立工作树中）为 `python -B -m pytest tests -q -p no:cacheprovider -W error`；独立包使用 `python -I -B -m pytest tests -q -o pythonpath= -p no:cacheprovider -W error`。OpenSSL 显式选择既有 Git 安装的测试程序。原始日志和测试库放在 Git 忽略的 `var/m4-completion` / 独立工作树 `var/m4-publish`，不上传数据库、凭证和私有结果。

## 真实沙箱边界

使用专用 WSL2 Ubuntu 22.04.5、Python 3.12.15、runsc release-20261012.0 KVM。systrap 连固定程序也失败；KVM 基础探针通过后，用户明确批准专用用户使用 `/dev/kvm`。Ubuntu 系统 Python 不替换；Core 仍在云端单活，生成代码只在独立沙箱运行。配置和运行方法见 [WSL 部署](m4-sandbox-wsl.md)与 [ADR 0015](decisions/0015-m4-durable-sandbox-wsl.md)。

固定镜像摘要 `753aed32346183e5820e0a9a0631339d213607d22fd000ed4ec0b85681512d16`；runsc 安装树摘要 `be25aff8bad3f0ad4adafa525107b22475802c1c0f5a8ae334c5320fb697a21a`。启动及每次运行读取实际 cgroup 限制：1 CPU、512 MiB、64 tasks、零 swap。容器内 UID 65534、无 capabilities、无外部网络，16 MiB tmpfs，stdout 32 KiB、stderr 8 KiB，单次最多 10 秒。

实际探针无法读取已存在的宿主标记文件、宿主环境 canary 或指定宿主路径；无法连接 loopback、公网及元数据地址。内存、临时文件、stdout 压力和超时均停止。fork 压力触发宿主整组 OOM，随后确认所持容器已停止并清理；这证明资源边界阻止持续扩张，不能记作“成功创建恰好 64 个 guest 进程”。控制 socket 路径不可见的检查不等于对所有宿主控制接口的穷尽证明。

## 真实生成、失败与费用

首次调用生成了缺 name、使用 expected 的测试对象，被严格契约拒绝，未创建可执行版本；费用上界 USD 0.000724。根因是公开提示缺少测试字段说明。补全契约和标准结果形状后，用户另行同意再调用 1 次；新增 USD 0.001285。两次合计 USD 0.002009，都进入原全局账本，没有自动重试或绕过费用预留。

第二次生成 `user.directory_entry_stats@r1`，内容摘要 `be8e572c860bbf53a6c499612277cb234c46d5fcc6df5587852246101a036b5e`。可信控制器在真实 KVM 中运行混合、空数组、200 项三个有效样本；另五类非法输入在合同入口被拒绝。用户明确批准该版本后启用，批准/启用未新增模型调用。测试通过不代表任意未来输入都正确。

三步跨平台业务演示最初使用确定性开发候选；真实模型候选在生产完成生成、隔离测试与逐版批准，两种证据分别记录。没有自动在生产创建或启用未经批准的 Skill。M4 的能力管理与手动运行入口不等于聊天已经能自动选择 Skill；后者属于 M5。

## A01–A18 索引与限制

| ID | 证据 |
| --- | --- |
| A01–A05 | 严格生成/Schema/重复键、共享预算、绑定/环境变更、接口鉴权、有限计划；现有扩展测试及 `test_m4_regressions.py`、`test_m4_completion.py` |
| A06–A07 | 真实 Windows 审批与一次执行、真实统计 Tool 控制器用例；上述三进程及模型记录 |
| A08–A09 | 专用 WSL KVM 探针、实际宿主限额、可信控制器拒绝伪造报告；上述明确的探针范围 |
| A10–A14 | 真实三步/截断、停用与取消竞态、持久核对及写入/容量拒绝；`test_m4_completion.py`、`test_m4_sandbox_lifecycle.py`、受控崩溃证据 |
| A15–A16 | schema 010→012 生产一致备份/旧行比较、v1 真实工具；完整 Windows 与 Linux 适用回归 |
| A17–A18 | 原实际浏览器全生命周期、新输入保留/390 像素检查；缺环境/错误摘要拒绝；Android 用户回复仅限所问操作，详细设备信息缺失 |

中间失败如实保留：Windows 测试环境需正确 OpenSSL；强制终止进程后的只读 SQLite 备份出现间歇 I/O 错误，测试备份改用存在文件的 `mode=rw` 允许正常 WAL 恢复，断言未削弱；真实 runsc rootfs 权限、KVM 选择、命令摘要和迟到结果竞争均在修复后复验。第一次模型失败不能算通过。

本机休眠、关机、WSL 退出或网络断开会让生成 Tool 离线；云端 Core 与原聊天可继续使用。最终用户整阶段验收仍需明确确认；本报告不把“可以查看”或单个版本批准当作整阶段验收。GitHub 提交只包含固定 M4 快照，保留其他正在进行的工作。
