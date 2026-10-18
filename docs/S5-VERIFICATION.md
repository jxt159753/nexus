# S5 / P08 技术交付与验证

日期：2026-10-18。授权：用户明确要求“完成S5”。仅实施 P08，不包含 S6–S7、GitHub 推送或此前阶段的用户验收。阶段状态见 [ROADMAP](ROADMAP.md)，操作和升级要求见 [S5 恢复](s5-recovery.md)，决定见 [ADR 0005](decisions/0005-s5-recovery-controls.md)。

## 实际修改

- Core 增加总期限结算、重连/丢确认后的状态查询、同 attempt 有限重传及最多两次只读执行；未知结果必须有旧进程停止证据才能重试。当前权限、策略、目录审批和期限在重试/实际发送前重新检查。
- owner HTTP 取消、pause/resume/cancel_all；控制请求幂等、控制状态及事件/审计持久。暂停/取消与实际发送互斥，已排队但尚未发送的命令也受保护。
- 取消先落请求，真正停止或已完成结果到达后结算；超时与执行未知分开。迟到/旧尝试结果保留原证据，不覆盖终态或新尝试。验证前崩溃、期限与验证竞争均有对应处理。
- Node 持久取消墓碑，防止取消后的旧命令迟到执行；继续用 Windows Job 停止真实工具。上报 execution_stopped 及有界重传未确认结果，不根据 boot_id 推断未执行。
- Core 迁移 004、Node 迁移 002；升级前一致备份，保留原身份/任务/授权/审批/尝试/回执。修复异步 SQLite 打开期间取消可能遗留连接的窗口，增加直接关闭证据测试。
- 没有新增依赖、生产工具、模型适配器、前端或长期记忆。生产工具仍只有 system.info / filesystem.list；阻塞注册表和故障注入仅在测试进程生效。

## 环境及实际命令

Windows 11 / CPython 3.13.9 x64。使用既有 25 个锁定依赖。仅工作区临时目录、随机凭证、127.0.0.1 临时端口和受控父/子进程；没有修改系统设置、访问真实用户目录或数据库。日志检查不含测试原始令牌。

源码完整回归：

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp E:/NEXUS/var/s5-final-source-93c975d9ef1943d49cc9f5c44d291385 --tb=short -W error --maxfail=3
```

结果：**470 passed in 88.14s**。包括六类独立进程故障、真实进程停止、暂停发送屏障及 SQLite 取消关闭检查，无失败或跳过。此后补充 S4→S5 升级检查，以及将已知 cancelled 状态按原摘要报告，最终验证采用下述重新构建的安装包。

全新离线环境安装：

```powershell
New-Item -ItemType Directory E:/NEXUS/var/s5-build-temp -Force | Out-Null
$env:TMP='E:\NEXUS\var\s5-build-temp'
$env:TEMP='E:\NEXUS\var\s5-build-temp'
$env:PIP_NO_INDEX='1'
$env:PIP_DISABLE_PIP_VERSION_CHECK='1'
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s5-package
& .\.venv\Scripts\python.exe -B -m venv --without-pip E:/NEXUS/var/s5-check-env
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s5-check-env/Scripts/python.exe install --no-index --find-links E:/NEXUS/var/s3-wheelhouse -r requirements.lock E:/NEXUS/var/s5-package/nexus-0.1.0-py3-none-any.whl
```

项目及原 25 个依赖全部离线安装成功。禁用源码路径注入，确认 nexus 从独立环境 Lib/site-packages 导入；CLI 和工具子进程使用该环境解释器。首轮安装包 **470 passed in 86.75s**。补充升级检查用该包独立运行 **1 passed in 0.68s**，不把两次计数相加冒充完整回归。

最终包重新构建并安装后运行：

```powershell
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s5-package-final
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s5-check-env/Scripts/python.exe install --no-index --force-reinstall --no-deps E:/NEXUS/var/s5-package-final/nexus-0.1.0-py3-none-any.whl
$env:PYTHONPATH=''
& .\var\s5-check-env\Scripts\python.exe -I -B -c "from pathlib import Path; import nexus,pytest; location=Path(nexus.__file__).resolve(); print('Installed package:',location); assert location.is_relative_to(Path('E:/NEXUS/var/s5-check-env')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--basetemp','E:/NEXUS/var/checks/s5-installed-final-20261018']))"
& .\.venv\Scripts\python.exe -B -m pip check
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s5-check-env/Scripts/python.exe check
```

最终安装包完整回归：**471 passed in 83.63s**，无失败或跳过。wheel SHA-256：`cf155e472f03d3c9c4b4a5ed022c00adc76e64f3b42b8ebe4f605e78ccf746ce`；逐文件确认包内 54 个 Python/SQL 文件与最终源码完全一致，包含两套新增迁移，不包含测试注入。双环境 pip check 均为 `No broken requirements found.`；构建工具仍沿用原范围约束，不声称完全锁定。

## 真实进程故障证据

六类测试通过真实 HTTP/WebSocket 连接独立 Core/Windows Node，在测试进程注入丢包、退出或阻塞工具；不是只模拟状态字段。每类 evidence.json 包含真实 PID、task/attempt、派发/查询次数及结果。最终证据保存于 Git 忽略目录 `var/checks/s5-installed-final-20261018/test_independent_process_fault0` 至 `fault5`。

| 注入点 | 实际观察与断言 |
| --- | --- |
| before_send | Core 在 attempt/grant/dispatching 提交后、发送前退出；重启先查 not_found，再以同 attempt 重传；dispatch_count=2，Node 仅一条执行回执，最终成功 |
| before_ack | Core 结果与成功已提交、ACK 入队前退出；Node 重连重传缓存，Core 去重返回确认；Node acknowledged=1，原历史不变且仅执行一次 |
| lost_receipts | Node 丢 accepted/started/主动 result；Core 五秒查询取回已完成结果，只有一次执行 |
| node_crash | 受控工具正在阻塞时终止所属 Node；OS 关闭 Job 并确认旧工具 PID 已退出；新 Node 上报 unknown/stopped，Core 创建第二次只读尝试后成功；第一条回执仍 unknown，无第三次执行 |
| cancel | 阻塞工具运行中提交取消，先看到 cancel_requested，再收到真实停止证据并 cancelled；原工具 PID 不存活，不以取消等待协程冒充停止 |
| timeout | 真实阻塞工具连续两次触发单次期限；两个 PID 均退出，第二次后 BUDGET_EXCEEDED / known，无第三次尝试 |

原 S4 的真实系统信息、目录审批、结果 ACK 和 Core 重启历史测试也包含于完整回归；目录仍仅访问测试 workspace，不返回正文或绝对根路径。没有配置模型密钥或真实 Provider，回归检查权威库没有 model_calls 表，真实模型调用为零。

最终六个场景的 Core/Node PID 依次为：before_send `58060/46540`（旧 Core `58740`）、before_ack `56748/56536`（旧 Core `56720`）、lost_receipts `4504/34336`、node_crash `54600/15260`（旧 Node `24508`、原工具 `52332`）、cancel `4712/48372`（工具 `43340`）、timeout `10148/50888`（首工具 `3760`）。这些受控进程均由测试完成清理，详见各 evidence.json。

## 状态、权限和恢复边界

- unknown 无停止证据始终占槽；有停止证据才可准备重试。旧回执迟到不覆盖当前 attempt，最多两次后显式停止；同 attempt 重传最多三次，不改变摘要、grant 或期限。
- 对账/停止预算 64 次先持久，重开不清零；耗尽不释放 unknown 槽。暂停不重传；暂停先于实际发送时拦住已排队命令；重启仍 paused，resume 后继续受控派发。
- 取消覆盖 ready、awaiting_approval、活动执行、verifying 和终态；待取消遇到已完成证据按结果验证；取消/结果竞争只产生唯一终态。cancel_all 需要 owner 且等待活动执行真实停止；重复控制不影响后来任务。
- 审批/离线等待进入总期限；超时后的迟到结果只补执行证据；结果已落盘但未验证时到期也释放已结束槽，不声称成功。
- 新尝试遇策略/审批变化重新审批；错 task/attempt/节点/摘要/工具/时间及非法结果不能成功；原负向权限、路径、进程和存储失败检查继续通过。
- Core 控制审计失败整笔回滚；Node 写取消墓碑失败不能报告停止；默认容量保护保留已有证据。S4→S5 迁移保留活动/完成尝试及全部原列、备份为 v3 且 quick_check/外键通过；Node v1→v2 保留 received 回执及经验证的一致备份。

## 失败记录和限制

默认 pytest 临时目录访问被拒，改用工作区唯一临时目录后继续，未把环境错误算作通过。首轮严格回归发现 SQLite 资源警告，定位为连接后台打开期间取消，修复资源所有权并增加受控打开/取消/真实 close 测试后通过源码完整回归。旧测试中版本数量及 TOOL_TIMEOUT 不重试的预期已按 S5 更新；进程故障注入先导入 asyncio 后再替换测试 Popen，避免破坏 Windows 事件循环初始化。失败均保留事实，不作为通过证据。

远程 TLS、Android 真机、Windows 10、Python 3.12、其他系统及大规模负载未测。没有前端/client WS、长期记忆或真实模型调用。unknown 且停止无法确认时保持占槽；对账额度耗尽需操作人员检查。升级需同步停机备份并更新严格 v1 Core/Node；不混用新 Node 和旧 Core。

技术交付完成待用户验收；ROADMAP 已勾选 P08，S6–S7 仍未批准。未新增后续阶段模块、未变更依赖或生产工具、未提交或推送 GitHub。Core/Node AST 导入边界、文档本地链接/JSON 示例/代码围栏、包一致性及 git diff --check 通过（仅原行尾转换提示）。用户验收仍需用户确认，不自动进入 S6。
