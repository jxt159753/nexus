# P05 Windows Node 技术验证记录

日期：2026-10-17。授权：用户明确要求“完成P05”，仅执行 S3 中 P05，不批准 P06 或后续阶段。状态为技术交付完成、待用户确认验收，阶段记录见 [ROADMAP](ROADMAP.md)。

## 实际交付与决定

- 复用 P02 契约，建立两个固定只读工具：system.info@1 的四字段白名单、filesystem.list@1 的配置根及直接条目观察；执行前重查根/祖先 reparse point，不跟随链接，数量/UTF-8 字节不足时明确截断。
- 新增严格 NodeConfig、默认拒绝本地策略、共享命令规范化摘要、单执行槽和数据目录进程锁。没有任意 shell、脚本、可执行文件或第三种生产工具入口。
- 专用 receipts.sqlite3、版本 SQL 和模式/Node 身份校验；received / started / 结果分别先提交；同 attempt 相同命令回放，不同命令冲突；结果重传保持观测时间，换用当前会话。
- Windows Job Object 设置 KILL_ON_JOB_CLOSE；父进程成功纳管后开启输入门，子进程再验证自身归属。实际超时、取消和 Node 崩溃均停止受控工具；started 无结果重启为 unknown，不盲目重跑。
- 回执数量、分页、执行时间、并发、子进程输入/输出和目录结果均有界。回执持久化失败不执行或不回报成功，并停止新的执行。
- 不新增或升级依赖，requirements.lock 未改；pyproject 只增加 Node 迁移 SQL 的打包资源。接口见 [node-runtime.md](node-runtime.md)，实现选择见 [ADR 0002](decisions/0002-p05-node-execution.md)。

## 环境与实际命令

Windows 11 10.0.26200、CPython 3.13.9 x64；使用项目已有 .venv 和锁定依赖。所有测试使用仓库 var/ 下的临时目录、测试身份和自行创建的进程；未启动监听服务或使用真实凭证。junction 创建只指向对应测试目录内部目标，需要工具权限审查允许本轮测试。

源码完整回归：

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error --basetemp E:/NEXUS/var/p05-full-tests
```

实际结果：**346 passed in 26.03s**，其中已有 292 项、P05 新增 54 项；无跳过。

离线构建及独立目录安装：

```powershell
New-Item -ItemType Directory -Force E:/NEXUS/var/p05-build-temp | Out-Null
$env:TMP='E:\NEXUS\var\p05-build-temp'
$env:TEMP='E:\NEXUS\var\p05-build-temp'
$env:PIP_DISABLE_PIP_VERSION_CHECK='1'
$env:PIP_NO_INDEX='1'
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/p05-package
& .\.venv\Scripts\python.exe -B -m pip install --no-deps --no-index --target E:/NEXUS/var/p05-installed E:/NEXUS/var/p05-package/nexus-0.1.0-py3-none-any.whl
$env:PYTHONPATH='E:\NEXUS\var\p05-installed'
& .\.venv\Scripts\python.exe -B -c "from pathlib import Path; import nexus, pytest; package=Path(nexus.__file__).resolve(); print('Installed package:', package); assert package.is_relative_to(Path('E:/NEXUS/var/p05-installed')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--basetemp','E:/NEXUS/var/p05-wheel-tests']))"
& .\.venv\Scripts\python.exe -B -m pip check
```

构建、安装成功；明确确认 nexus 从 p05-installed 导入，PYTHONPATH 同样进入子进程，pytest 的源码路径配置已关闭。安装包完整回归 **346 passed in 25.30s**，实际覆盖 Node 迁移资源及独立工具进程；pip check 返回 `No broken requirements found.`。这是使用现有依赖的独立 wheel 目录验证，没有声称本轮重新建立全新依赖环境。

测试与 wheel 产物保存在被 Git 忽略的 var/p05-* 路径，未写入真实 Core/Node 数据库或变更系统配置。上述命令的路径仅供复现，pytest 重跑会重置其指定 basetemp，不能把用户数据放入测试目录。

## 真实观察到的验收结果

| 要求 | 实际证据 |
| --- | --- |
| 系统信息白名单 | 实际 Windows 子进程结果只有 os_name、os_version、architecture、python_version；固定注册表和本地工具的网络禁止检查通过 |
| 只能列配置根 | 默认拒绝、未配置 workspace、路径参数、相对/UNC/设备/通配路径、普通文件根均被拒绝；只返回根的直接条目，无绝对路径/正文 |
| 链接不跟随 | 实际创建目录 junction，条目标记 link，目标 secret 条目未返回；junction 根、祖先 junction、配置后替换为 junction 被拒绝；符号链接分支以不跟随 stat 的受控条目补查 |
| 数量/字节边界 | 恰好数量时不误报截断；超过数量明确截断；128/1000/32768 字节预算下多字节中文名称结果均不超限并标记截断 |
| 重复 attempt | 修改目录后重发同命令，仍得到原结果；新会话/已过期已完成命令回放原观测时间；换参数、任务、grant、超时和摘要冲突被拒绝 |
| 结果重启恢复 | 独立 Python 进程重开回执，在禁止启动新工具的条件下返回完全一致的缓存结果；ACK 可重复且不删除去重证据 |
| 单活与有界执行 | 第二 Runtime 无法获取相同数据目录锁；活动 attempt 重传返回 started，新 attempt 返回 BUSY；满容量拒绝新记录，保留旧查询 |
| 真实超时/取消 | 测试专用阻塞实现仅注入测试进程的注册表，记录真实工具 PID；本地限额到期或取消后该 PID 实际终止，停止后才记录结果/取消阶段 |
| Node 强制退出 | 另起独立 Node 测试进程，终止测试记录的真实 Node PID，确认其 Job 工具 PID 退出；重开回执为 unknown、停止已确认，禁止原 attempt 再运行 |
| 未开始与未知区分 | received 可在重启、重新校验后继续；started 无结果一律 unknown；无法查询旧 Job 时阻止另一 attempt 执行 |
| 纳管与输入门 | 注入 Job assignment 失败时阻塞工具标记文件从未产生；无输入门或不属于指定 Job 的 worker 拒绝观察 |
| 输入输出有界 | 超过 64 KiB 的输入不启动进程；超量输出和进程异常拒绝为有效证据；子进程不读 stdin 时仍被执行期限实际终止 |
| 持久化故障 | SQLite trigger 注入 received/started/结果写入失败；前两者不运行，后者不回报成功并停止新执行，重启后 unknown；模式/摘要/Node 身份错误不重建旧库 |

相关测试：[tools](../tests/test_node_tools.py)、[receipts](../tests/test_node_receipts.py)、[runtime](../tests/test_node_runtime.py)、[process](../tests/test_node_process.py)。测试注入代码未打包进生产 Node。

## 失败修正与限制

首轮 P05 检查为 42 通过、3 失败：构造后被篡改的命令产生序列化警告，已改为严格序列化错误并在后续完整回归通过；两个真实 junction 用例因沙箱拒绝创建链接失败，经工具权限审查后同用例 2 项通过，最终两轮完整回归均覆盖这些真实操作。没有把权限失败计为通过或跳过。

首次 wheel 构建使用系统默认临时目录时发生权限错误；改用本仓库 var/p05-build-temp 后离线构建及安装成功。不为该问题修改系统权限、证书、防火墙或依赖。

尚未验证：Windows 10、Python 3.12、其他平台、真实 TLS/Node Link、Core 与 Node 派发验证闭环、网络结果重传、全局暂停/取消竞争和 Android。符号链接分支由受控 stat 验证，真实 Windows 链接证据为 junction；本轮没有建立需要额外系统特权的真实符号链接。目录检查不构成对恶意本机路径竞态的句柄级沙箱。回执目前只有初始版本；未来升级不得直接覆盖旧库，需先实现一致备份和前向迁移。

本轮没有真实模型调用。P05 的 completed 不是 Core 的 succeeded；用户验收、P06 通信和后续任务不能由本轮技术测试替代。

交付范围检查：92 处本地文档链接、JSON 示例和代码围栏校验通过；AST 检查确认 Node 不导入 Core；wheel 包含 Node 迁移 SQL 且不包含测试注入代码；P05 已勾选、P06 未勾选且没有 link.py 占位实现。`git diff --check` 通过，仅提示已有 Git 换行策略会将 LF 转为 CRLF。复杂度复查移除了无调用的工具集合常量、Job terminate 包装和测试导入。

最后的无调用代码清理后，源码按以下命令重跑相关范围，**54 passed in 10.54s**：

```powershell
& .\.venv\Scripts\python.exe -B -m pytest tests/test_node_tools.py tests/test_node_receipts.py tests/test_node_runtime.py tests/test_node_process.py -q -p no:cacheprovider --tb=short -W error --basetemp E:/NEXUS/var/p05-final-tests
```

重新构建并以 `pip install --no-deps --no-index --upgrade --target` 更新同一独立安装目录，沿用上方 wheel 验证方式、只选上述四个测试文件、basetemp 改为 var/p05-final-wheel-tests，最终 wheel 相关范围 **54 passed in 10.00s**。没有因纯文档更新再次扩大测试范围。新增 18 个 Python/SQL 文件的行尾空白检查通过。
