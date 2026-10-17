# S3 / P05–P06 技术交付与验证

日期：2026-10-17。用户明确要求“完成S3并上传github”，授权本阶段 P05/P06 及向既有 GitHub 仓库提交推送。技术状态为完成待用户验收，不将授权实施解释为验收此前阶段。阶段权威记录见 [ROADMAP](ROADMAP.md)。

## 实际交付与范围

P05 的两个只读工具、Windows Job、持久回执与 unknown 恢复已在上一轮实现，本轮包含其尚未提交的改动并完整回归，历史证据见 [P05](P05-VERIFICATION.md)。P06 本轮新增：

- Core 与 Node 独立启动入口、严格 TOML/指定环境变量配置、无秘密示例；复用可移植数据目录锁，Core 单 worker，进程锁先于数据库初始化。
- 复用 S2 的握手及逐消息认证；认证 node_id 绑定、hello/welcome、可信配置/自报能力/owner 与 Node 授权交集、凭证撤销和轮换、当前会话替换。
- 应用心跳、仅在线观测、单调时钟超时、含抖动的 1–30 秒退避、重连重新认证和注册；GET /api/v1/nodes 仅供已登录 owner 查询有效能力与连接状态。
- 帧、消息速率、握手速率、连接数、队列、结果及发送等待限额；缓存回执查询换会话、可信 ACK 本地标记。无网络 command.execute 派发入口或自动结果 ACK，任务状态不被连接层修改。
- 唯一新增安装包 websockets 17.1，同时支持客户端和 Uvicorn Sans-I/O；其他 24 个依赖固定版本保持不变。未新增 Core SQL 模式，连接审计复用现有 audit_events。

接口和启动说明见 [S3 Link](s3-link.md)，决定见 [ADR 0003](decisions/0003-s3-authenticated-link.md)。未推进 P07–P12、未调用模型、未接入其他项目。

## 环境与实际命令

Windows 11 / CPython 3.13.9 x64。所有测试使用 var/ 下的临时目录、随机临时凭证、自行创建的进程及 127.0.0.1 临时端口；不终止其他进程、不使用真实身份或数据库、不修改系统信任、防火墙或安全设置。Windows junction 仅指向同一临时测试目录内部的目标。

源码完整回归：

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error --basetemp E:/NEXUS/var/s3-full-tests
```

实际结果：**399 passed in 38.74s**。其中此前 P01–P05 共 346 项，本轮 P06 新增 53 项；没有跳过。

全新环境离线安装及完整回归：

```powershell
New-Item -ItemType Directory -Force E:/NEXUS/var/s3-build-temp | Out-Null
$env:TMP='E:\NEXUS\var\s3-build-temp'
$env:TEMP='E:\NEXUS\var\s3-build-temp'
$env:PIP_DISABLE_PIP_VERSION_CHECK='1'
# 下载已经锁定的 wheel，下载完成后以下构建/安装不访问包索引。
& .\.venv\Scripts\python.exe -B -m pip download --only-binary=:all: -r requirements.lock --dest E:/NEXUS/var/s3-wheelhouse
$env:PIP_NO_INDEX='1'
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s3-package
& .\.venv\Scripts\python.exe -B -m venv --without-pip E:/NEXUS/var/s3-check-env
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s3-check-env/Scripts/python.exe install --no-index --find-links E:/NEXUS/var/s3-wheelhouse -r requirements.lock E:/NEXUS/var/s3-package/nexus-0.1.0-py3-none-any.whl
$env:PYTHONPATH=''
& .\var\s3-check-env\Scripts\python.exe -I -B -c "from pathlib import Path; import nexus,pytest; location=Path(nexus.__file__).resolve(); print('Installed package:',location); assert location.is_relative_to(Path('E:/NEXUS/var/s3-check-env')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--basetemp','E:/NEXUS/var/s3-installed-tests']))"
& .\.venv\Scripts\python.exe -B -m pip check
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s3-check-env/Scripts/python.exe check
```

25 个锁定依赖及项目 wheel 均在全新环境离线安装成功。确认 nexus 从该环境的 Lib/site-packages 导入，pytest 源码路径配置已禁用、PYTHONPATH 清空，子进程也使用该环境解释器。完整回归 **399 passed in 46.95s**；两边 pip check 均返回 `No broken requirements found.`。构建仍使用既有 setuptools 范围约束，没有声称构建工具完全锁定。

上述 basetemp 是可重置测试目录，不能放入用户数据；wheel、日志、临时库和校验环境均位于 Git 忽略的 var/，不作为源代码上传。

## 实际观察到的要求

| 要求 | 验证证据 |
| --- | --- |
| 独立 Node 连接 Core | 两个不同 PID 的真实 CLI 进程，在临时 loopback 端口完成 Bearer 认证、hello/welcome 和持续心跳；等待超过两倍心跳间隔仍在线 |
| 伪造身份或能力不能授权 | 缺失/错误/owner 凭证、伪造 node_id、未配置信任节点和假工具被拒绝；自报两个工具及 workspace 时，结果仍被 Core 可信工具配置和双方授权收紧；缺 owner 授权时有效工具为空 |
| 旧会话不再有效 | 新会话 ID 不同且关闭旧连接；当前连接带旧 session 消息也被拒绝；重复 hello、反向消息、二进制帧或超大消息均拒绝 |
| 心跳离线不等于任务失败 | 停止应用心跳后连接关闭，既有任务快照保持完全一致；真实 Core 重启/Node 重连同样不改写任务或 execution_outcome |
| 重连重新认证 | 强制停止测试 Core，再以同一权威数据库重启，原 Node 自动连接、新 session、同一 boot；凭证轮换关闭旧连接，旧令牌再次握手返回 403，新凭证的新 Node 可连接且 boot 更新 |
| 空闲凭证撤销 | ASGI 中撤销或轮换凭证，空闲连接也在认证轮询中关闭；节点不能替代 owner 登录 |
| 回执换会话和 ACK | 真实 loopback 测试服务查询 P05 已持久结果，返回当前 session 与原观测时间；可信 ACK 只更新本地标记；随后网络执行请求被明确拒绝且未产生新 attempt 回执 |
| 消息与本地边界 | 收紧的帧限额、错误 welcome、错误 session 令 Node 断开；TLS context 检查证书和主机名验证保持启用，代理/压缩关闭且重定向被拒绝，离线退避可被停止信号中断 |
| 有界队列与限速 | 真实 Core 连接的发送队列收紧至 1 后第二次发送返回 BUSY；速率窗口满后拒绝且时间窗后恢复。其他连接/缓冲上限由配置和代码检查确认，未声称进行大规模负载压测 |
| 进程与审计可靠性 | 同一 Core 数据目录的第二实例无法启动，退出后可重开；node.connected 审计失败时不发 welcome；断开/取消清理不残留当前连接 |
| P05 回归 | 真实 junction、白名单字段、数量/字节截断、去重、结果跨进程恢复、Job 纳管/超时/取消/父进程死亡以及存储故障全部包含在两轮完整回归中 |

主要新增测试：[认证连接](../tests/test_node_link.py)、[Node 客户端](../tests/test_node_client.py)、[严格配置](../tests/test_service_config.py)、[独立进程](../tests/test_link_processes.py)。Core 与 Node AST 导入边界检查通过，互不导入对方业务模块。

最终移除两个无用测试导入，并将日志检查补齐为 owner、轮换前 Node、轮换后 Node 三份临时凭证后，只复跑受影响的独立进程用例：`var/s3-check-env/Scripts/python.exe -I -B -m pytest tests/test_link_processes.py -q -p no:cacheprovider -o pythonpath= --tb=short -W error --basetemp E:/NEXUS/var/s3-final-process-test`，**1 passed in 5.18s**。没有修改生产代码或因文档变更重复完整回归。

## 失败、修复与尚未验证

首轮沙箱检查在 Windows asyncio 建立 socketpair 时阻塞；中断自行启动的测试，启用经工具权限审查允许的本机 socket 后继续，没有把阻塞计为通过。TestClient 相对 WS 地址默认指向 testserver，修正为明确 loopback URL 后，正向握手实际通过，负向测试重新验证。

正向断线用例随后发现 ASGI 取消可能中断 finally、遗留连接记录；改为先移除连接和容量占用，再屏蔽取消完成有界清理。修复后 21 项连接测试、27 项配置/真实进程测试、5 项客户端测试通过，并进入两轮完整回归。

未验证：远程真实 HTTPS/WSS 证书握手、Android、Windows 10、Python 3.12、其他操作系统、大规模并发或负载。TLS 的配置和默认校验检查不是实际证书部署证据。

P07 的持久授权/attempt 派发、有效结果归属验证与 succeeded 结算未实现。P08 的网络对账、全局控制和终态竞争未实现；S3 的心跳、缓存查询和本地 Job 不能替代这些验收。当前能力不能称为完整后端执行闭环或 MVP。

## GitHub 交付范围

已读取现有远程 `https://github.com/jxt159753/nexus.git`，仓库为用户有 push 权限的私有仓库。开始时 origin/main 与本地 main 同为 `f884e3b401c1200156feeb0a54bc6acc2a665a28`。本次提交包含此前 P05 尚未提交的文件及本轮 P06 代码/测试/文档；不上传 var、虚拟环境、数据库、原始凭证或测试日志。最终提交及远程 SHA 以推送后的 Git 查询和交付消息为准，不将实施批准记作用户验收。

提交前复核：49 个变更文件均属于本轮源代码、测试、配置示例或文档；凭证特征、运行产物排除及行尾空白检查通过；130 处本地文档链接、JSON 示例、代码围栏和 TOML 示例有效；wheel 含两个启动入口及迁移 SQL，不含测试注入代码；P06 已勾选、P07 未勾选且没有 Harness 占位目录。`git diff --check` 通过，未变更 AGENTS、未创建第二套计划，只有 Git 的既有 LF/CRLF 提示。
