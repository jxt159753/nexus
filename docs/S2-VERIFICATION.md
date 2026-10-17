# S2 / P02–P04 验证记录

日期：2026-10-17。授权：用户明确要求“完成S2”。结论与最终计数见下方实测结果；用户验收状态统一见 [ROADMAP](ROADMAP.md)。本轮没有推进 S3–S7。

## 实际交付

P02 保留既有 v1 契约、严格编解码与零模型规则路由并回归。P03 新增 aiosqlite 仓储、两份版本 SQL、迁移摘要与升级前一致备份；实现幂等任务、派发前状态、revision、事件/审计原子提交、有界查询与待处理数量。P04 新增离线身份管理、独立 Node 凭证、浏览器会话、HTTP/WS 守卫、固定能力交集与绑定审批。

接口、管理步骤、状态边界及后续接入约束见 [storage-security.md](storage-security.md)，实现决策见 [ADR 0001](decisions/0001-s2-storage-security.md)。不重新引入此前删除的实验 app、脚本或数据库。没有生产监听服务、真实设备操作或模型请求。

## 环境与依赖

Windows、Python 3.13.9、FastAPI 0.143.3、Starlette 1.8.0、Pydantic 2.14.1。增加架构指定的 aiosqlite 0.22.1；现有 Starlette TestClient 在 -W error 下拒绝旧 httpx 的弃用路径，故以 httpx2 2.13.1 替换无实际调用方的旧 httpx 开发依赖，并固定 httpcore2 2.13.1、truststore 0.10.5。其他依赖版本保留；不屏蔽警告、不降级框架。锁定安装为 24 个依赖及 nexus，共 25 个 distribution。

## 可复现命令

测试和打包的临时文件位于 Git 忽略的 `var/s2-verification`，不使用用户数据库。先创建父目录；沙箱内 Windows asyncio 的本机 socketpair 会阻塞，本轮在经执行审批允许的环境运行受控测试，未修改系统安全设置。

```powershell
New-Item -ItemType Directory -Path .\var\s2-verification\tmp -Force | Out-Null
$env:TMP = (Resolve-Path .\var\s2-verification\tmp).Path
$env:TEMP = $env:TMP
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error --basetemp .\var\s2-verification\pytest-final-audit --junitxml=.\var\s2-verification\source-results.xml
& .\.venv\Scripts\python.exe -m pip wheel . --no-deps --no-build-isolation --no-index --wheel-dir .\var\s2-verification\wheels --disable-pip-version-check
& .\.venv\Scripts\python.exe -m pip download -r requirements.lock --dest .\var\s2-verification\wheels --disable-pip-version-check --retries 0
& .\.venv\Scripts\python.exe -m venv --without-pip .\var\s2-verification\clean-env
& .\.venv\Scripts\python.exe -m pip --python .\var\s2-verification\clean-env\Scripts\python.exe install --no-index --no-cache-dir --no-compile --find-links .\var\s2-verification\wheels -r requirements.lock nexus --disable-pip-version-check
& .\.venv\Scripts\python.exe -m pip --python .\var\s2-verification\clean-env\Scripts\python.exe check --disable-pip-version-check
```

独立安装验证复制 tests 到临时 `installed-check/tests`，放置仅含 `[pytest]` 的 pytest.ini；从该目录运行，避免项目配置的 `pythonpath=src`。解释器使用 `-I`，确认 nexus 从新环境 site-packages 导入。源码测试及独立安装测试分别保存 JUnit 结果。

```powershell
& E:\NEXUS\var\s2-verification\clean-env\Scripts\python.exe -I -B -m pytest -c pytest.ini tests -q -p no:cacheprovider --tb=short -W error --basetemp E:\NEXUS\var\s2-verification\pytest-installed-final --junitxml=E:\NEXUS\var\s2-verification\installed-results.xml
```

## 实测覆盖

| 验证层级 | 实际检查 |
| --- | --- |
| P02 回归 | 原有 199 项配置、契约、字节限制与规则路由检查保留，不新增模型实现 |
| SQLite 持久性 | 关闭连接后重新初始化/查询；另起 `python -I` 进程读取原任务与事件；历史按 owner 过滤且分页有界 |
| 幂等与竞争 | 8 个并发同键请求只产生一个任务；改参数/节点冲突；两个 revision 更新或相反审批决定只有一个胜出 |
| 原子性与故障 | 外键拒绝孤儿；非法/未来执行状态拒绝；事件/审计写入失败和提交失败时任务、revision、事件和审批一并回滚 |
| 迁移 | v1→v2 先生成一致备份，原身份保留，备份 quick_check 为 ok；无版本旧库、未知新版本、摘要变化拒绝且保留数据；SQL 失败回滚 DDL 与版本记录 |
| 身份与会话 | 单 owner、凭证独立；错角色/空/未知凭证拒绝；撤销/轮换后旧 principal 与会话失效；会话重开可读、期限严格、CSRF 错误拒绝；最多 10 个有效会话 |
| 权限与审批 | 默认无授权；逐一移除 owner/Node/注册/本地工具或资源均拒绝；节点匹配；8 类错绑定、到期、资源版本/策略变化、跨任务复用与禁止工具不能获权；批准/拒绝与审计同事务 |
| 临时 ASGI HTTP/WS 集成 | Cookie 属性、Origin、CSRF、4 KiB 输入与错误脱敏；无凭证及角色替代握手拒绝；合法 owner/Node 角色来自认证；已有连接撤销/过期后关闭，空闲 Node 撤销也在下一次周期检查关闭 |
| 真实管理子进程 | 使用临时目录，逐个离线命令登记 owner/Node、配置授权、轮换、撤销；退出后独立进程能查，输出无原始令牌 |
| 打包与安装 | wheel 含 001/002 两份 SQL，未包含旧 core/main.py 或 core/database.py；锁定依赖离线安装成功，pip check 通过；独立配置与 -I 从 site-packages 运行测试 |

## 中间失败与修正

- 第一轮遗漏创建 pytest basetemp 的父目录，2 passed / 59 setup errors；创建父目录后重跑，未跳过测试。
- 沙箱中 asyncio 创建 Windows 事件循环时阻塞；faulthandler 显示停在 socketpair/accept，尚未进入数据库。通过执行工具停止了本轮受控进程，再经审批在适合该运行时的环境重跑。
- 定向测试最初 59 passed / 2 failed：同步 SQLite 上下文未显式 close 产生 ResourceWarning，另有测试中的异步生成器不能直接 join。实现备份和测试都改为显式 closing，并修正测试；保持 -W error。
- HTTP 测试初次收集被 Starlette 的 httpx 弃用警告拒绝，改用锁定的 httpx2 后 90 项定向测试通过。
- 首次沙箱打包因 pip 默认临时目录写入权限失败；将该命令进程的 TMP/TEMP 指向项目临时目录后成功，不修改系统环境配置。
- 第一轮完整实现与子进程测试为源码/独立 wheel 各 291 passed。范围复查进一步区分离线审计 actor/subject，并将批准查询限制为单条；最终结果以下方记录为准。

## 最终结果与限制

| 最终检查 | 实际结果 |
| --- | --- |
| 源码环境全量 `-W error` | **292 passed，15.18 秒**；含既有 199 项和本轮新增 93 项 |
| 独立 wheel / `-I` / 独立 pytest 配置 | **292 passed，17.05 秒**；从 clean-env 的 site-packages 导入，没有借用 src 路径 |
| 独立锁定依赖 | 24 个依赖及 nexus 离线安装成功；`pip check` 返回 No broken requirements found |
| 最终 wheel | 27242 字节；SHA-256 `11d9009ca4cfeadd12936c171f4c1ac76f2f0247d85274fb526d868effa6823d`；SQL 资源随包安装并由上述测试实际加载 |
| 范围与文档复查 | 10 个 Markdown、66 个本地链接、2 个 JSON 示例、PowerShell 示例语法、31 个 Python 文件 AST、wheel 摘要及任务勾选边界通过；`git diff --check` 无空白错误（仅 Git 的 CRLF 提示） |

阶段状态为“完成待验收”，不代替用户接受。临时结果 XML 位于 `var/s2-verification/source-results.xml`、`installed-results.xml`；这些是 Git 忽略的本机验证产物，长期技术结论保存在本文件，阶段事实只在 ROADMAP 维护。

本轮未实现或验证正式 Core 组装/单例锁、真实 Node Link/心跳/重连、TLS 证书握手、Windows 工具/进程取消、attempt/结果验证与恢复、Android 真机。HTTP/WS 证据属于临时 ASGI 集成，不是实际网络部署；P06/P07 必须按接口说明接入认证与派发事务。Python 3.12 和其他系统未测，构建依赖仍为范围约束。没有真实个人数据或长期个人记忆，没有真实模型调用。仅生成测试用临时身份与数据库，没有默认授权真实目录。
