# S4 / P07 技术交付与验证

日期：2026-10-17。授权来源：用户明确要求“完成S4”。范围为 P07 后端执行闭环，不含 S5–S7、GitHub 推送或此前阶段的用户验收确认。技术交付完成待用户验收；状态权威见 [ROADMAP](ROADMAP.md)。

## 实际交付

- 接入 owner HTTP 任务提交、目录审批、分页任务查询及含尝试/观测时间/审批/状态事件的详情；复用既有会话、Origin、CSRF、严格契约和幂等规则，实施新任务速率上限。
- Harness 按单节点单槽调度，派发事务重查身份、工具/资源交集、当前策略、审批和额度，再保存 execution_grant、attempt、固定命令、任务状态、事件与审计；提交后才发送。
- Node 经原 Windows Job Runtime 执行两个真实只读工具，持久回执后发送 accepted/started，结果落盘后发送 result；心跳和状态查询持续响应。
- Core 匹配有效 attempt/节点/摘要/工具版本，先保存原始有限结果，再验证工具约束并确定终态后 ACK。重复证据只可改变传输 session；变更证据不能覆盖终态。verifying 可在重启后重新验证。
- 迁移 003、动作级授权与尝试表、任务结果字段；复用已有版本校验及升级前一致备份。将结果约束从 Node 注册表移至 contracts 复用，Core 不导入 Node 平台代码。没有新增或升级依赖。

接口和实际操作步骤见 [S4 Harness](s4-harness.md)，决定见 [ADR 0004](decisions/0004-s4-durable-execution.md)。

## 环境和实际验证

Windows 11、CPython 3.13.9 x64，原 25 个锁定依赖不变。测试只使用工作区 var/checks 临时目录、随机临时凭证、受控 Core/Node/工具进程及 127.0.0.1 临时端口；不访问真实用户目录或数据库，不修改防火墙、证书信任或系统安全设置。

源码完整回归：

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error --basetemp E:/NEXUS/var/checks/s4-full-de10f34e6a0945cebdb49f1fc4438dec
```

结果：**437 passed in 53.65s**。随后补充执行期间心跳/状态查询测试，并在发送前重新确认当前连接；受影响的 test_node_client / test_node_link / test_harness_http / test_harness_processes 四文件回归 **34 passed in 17.88s**。没有将这两次计数相加冒充完整回归数量。

全新离线安装：

```powershell
New-Item -ItemType Directory -Force E:/NEXUS/var/s4-build-temp | Out-Null
$env:TMP='E:\NEXUS\var\s4-build-temp'
$env:TEMP='E:\NEXUS\var\s4-build-temp'
$env:PIP_NO_INDEX='1'
$env:PIP_DISABLE_PIP_VERSION_CHECK='1'
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s4-package
& .\.venv\Scripts\python.exe -B -m venv --without-pip E:/NEXUS/var/s4-check-env
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s4-check-env/Scripts/python.exe install --no-index --find-links E:/NEXUS/var/s3-wheelhouse -r requirements.lock E:/NEXUS/var/s4-package/nexus-0.1.0-py3-none-any.whl
$env:PYTHONPATH=''
& .\var\s4-check-env\Scripts\python.exe -I -B -c "from pathlib import Path; import nexus,pytest; location=Path(nexus.__file__).resolve(); print('Installed package:',location); assert location.is_relative_to(Path('E:/NEXUS/var/s4-check-env')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--basetemp','E:/NEXUS/var/checks/s4-installed-final']))"
```

项目 wheel 和 25 个锁定依赖全部安装成功，nexus 从独立环境的 Lib/site-packages 导入，禁用源码路径注入，CLI 子进程也使用该解释器。首轮安装包回归结果：**438 passed in 57.66s**，无跳过。最终时序修复后的安装包与完整结果见下文。

之后新增 S3 v2→S4 v3 数据库升级补验，直接使用该安装包：

```powershell
& .\var\s4-check-env\Scripts\python.exe -I -B -m pytest tests/test_storage.py::test_s3_database_upgrade_preserves_task_events_approvals_and_backup -q -p no:cacheprovider -o pythonpath= --tb=short -W error --basetemp E:/NEXUS/var/checks/s4-upgrade-readable-final
& .\.venv\Scripts\python.exe -B -m pip check
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s4-check-env/Scripts/python.exe check
```

补验 **1 passed in 0.20s**；两环境 pip check 均为 `No broken requirements found.`。构建工具仍沿用原 setuptools 范围约束，未声称完全锁定。

## 最终时序复查与安装包回归

延迟 result 发送协程收尾后，复现连续下一条命令误报 BUSY（1 failed）；修复为工具完成后释放执行标记，下一命令等待前一结果发送收尾，再启动新工具。运行中相同 attempt 经过 Runtime 的完整动作身份校验后返回持久状态，不重复执行。另修正单个待派发动作被撤权时不应阻塞同节点其他获准工具。新增对应回归；Node 客户端/独立进程最终相关回归 8 项通过，调度撤权隔离补验 1 项通过。

最终重新构建项目并安装到前述独立环境，依赖和源码路径隔离保持不变：

```powershell
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s4-package-final
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s4-check-env/Scripts/python.exe install --no-index --force-reinstall --no-deps E:/NEXUS/var/s4-package-final/nexus-0.1.0-py3-none-any.whl
$env:PYTHONPATH=''
& .\var\s4-check-env\Scripts\python.exe -I -B -c "from pathlib import Path; import nexus,pytest; location=Path(nexus.__file__).resolve(); print('Installed package:',location); assert location.is_relative_to(Path('E:/NEXUS/var/s4-check-env')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--basetemp','E:/NEXUS/var/checks/s4-installed-complete']))"
```

最终 **441 passed in 60.15s**，无失败或跳过，包含升级与时序补验。wheel SHA-256 为 `a318b4b6713f5a61fc7a05a1f32d30eb9ae073cd81677452405d233f115b1c85`；逐文件确认包内 51 个 Python / SQL 文件与最终源码完全一致，包含 Harness、迁移 003 和历史迁移，不包含 tests。重新安装后 pip check 通过。

## 独立进程实测

安装包完整回归中的受控 Core PID 为 **58324**，Node PID **37048**，Core 重启后 PID **35148**。证据保存在本地 Git 忽略目录 `var/checks/s4-installed-complete/test_independent_process_read_0/evidence.json`；日志不含测试 owner / Node 原始令牌。

| 真实场景 | 实际结果 |
| --- | --- |
| 系统信息任务 | task `b90db26f-66cf-4a69-abee-b2dec90513e8` 经 queued→ready→dispatching→running→verifying→succeeded；结果只有四个白名单系统字段 |
| 目录逐任务审批 | task `e6353272-6794-4663-8c6f-bd73f95cf023` 先 awaiting_approval 且无 attempt；批准后返回临时 workspace 的 read-me.txt / 目录 两项及类型，没有文件正文或绝对根路径 |
| 成功证据 | 两任务均 known、node_report_schema_checked，各只有一个 attempt；Core 共两条 execution_grant，Node 共两条回执且均已 ACK |
| 业务重复请求 | 原 request_id 再次提交返回同一任务，未增加 Node 执行回执 |
| 重启历史 | 停止并重新启动受控 Core，重新登录后两任务的结果、原观测时间、审批、尝试和事件快照完全相同；Node 自动重新认证连接 |
| 零模型 | 两意图只经既有规则路由；没有 Provider 实现或模型密钥配置，权威库没有 model_calls 表，不填造调用记录 |

## 事务与负向证据

- 错 task/attempt/节点/摘要/工具/版本、过旧或未来观测时间、非当前 attempt 均不能写入有效结果；真实 Core WS 错摘要关闭连接且任务保持 dispatching / unknown。
- 无 accepted 时可直接保存并验证有效结果；ACK 到达时结果及终态已可从 HTTP 读取。相同证据重传返回 already_settled，变更观测证据拒绝，终态不回退。
- 当前身份和摘要匹配仍不足以成功：目录条目超请求上限、路径名称或重复名称触发 RESULT_INVALID；超协商字节预算拒绝接收。Node 结构化失败保留对应错误，不自动重试。
- 派发前撤销 owner/Node 授权、身份或改变策略/资源版本、审批过期/未批准，均没有 attempt 或 execution_grant。真实 HTTP 旧审批在策略变化后拒绝，需要新审批。
- execution_grants / task_attempts / task_events / audit_events 注入写入失败时，派发整笔回滚。结果落盘失败不进 verifying；终态写入失败保留 verifying 及原结果，恢复后可验证。
- 并发派发只有一个持久 Node 执行槽；没有回执或断线不会创建第二次尝试。数据库派发失败关闭 Node Link，拒绝新提交，已接受任务仍可查且无执行记录。
- 新任务每分钟十个、幂等请求不占新配额、全局队列上限、历史分页、正文上限、会话/Origin/CSRF 和非法输入均通过。未知意图明确拒绝。
- 真实受管理阻塞工具运行超过两个心跳间隔时，Node 仍发送带活动 attempt 的心跳并响应 started 状态查询；本地期限到达后原 Job 工具进程实际停止。该测试证明 Link 接入没有阻塞事件循环，不代替 S5 的全局取消验收。
- S3 v2 数据库升级时自动生成 before-v3 一致备份，原任务可通过正式仓储读取，身份、审批和事件保持一致，外键检查通过。既有 P05/S3 的真实 junction、进程退出、回执恢复及认证测试包含在完整回归中。

## 环境问题和剩余限制

首轮默认临时目录无法写入；改用工作区临时目录后，Windows asyncio 又阻塞在沙箱的 socketpair 初始化。只清理已确认由本轮启动的阻塞测试父子进程，经工具权限审查允许本机测试后重跑。迁移新增第三版本后，修正原测试中硬编码的版本数量/未知新版本值；未将环境错误或旧断言失败计为通过。

S4 每任务仅首次执行、首次派发；只有已经落盘的 verifying 会自动恢复验证。派发后断线、发送失败或无结果状态保留 unknown，仍占用 Node 执行槽，不推断未执行、不新建尝试。Core 总期限结算、确认超时、网络对账、重试、取消、pause/cancel_all 与终态竞争属于 S5，未实施或验收。

未验证远程真实 TLS、Android 真机、Windows 10、Python 3.12、其他操作系统和大规模负载。没有 Web 页面或浏览器 client WS、长期记忆或真实模型适配器。本阶段不能称为完整 MVP。完成待用户确认验收，下一阶段须另行批准 S5/P08。

交付范围复查：149 处本地文档链接、2 个 JSON 示例、代码围栏和 Core/Node AST 导入边界检查通过；README/架构/路线状态一致；P07 勾选、P08 保持未批准且未勾选；无第二套架构或路线文件，无依赖变更、无无关重构。git diff --check 通过（仅既有行尾转换提示）。

## 后续 GitHub 交付授权

用户随后于 2026-10-17 明确要求上传 GitHub。目标为现有私有仓库 `https://github.com/jxt159753/nexus`，确认当前账号有 push 权限，本地 main 与远端 main 同为 S3 提交 `defc08c70d345e06ea79efd6dd4cae4ce7e08c53`。本次提交 S4 的代码、测试和文档，原有 441 项验证结论保留；仅因上传授权同步文档，不改动生产代码或重复运行应用测试。最终提交及远端 SHA 见推送后的工具核验和交付消息；授权上传不等于用户验收 S4，也不批准 S5。
