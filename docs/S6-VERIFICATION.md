# S6 / P09–P10 技术交付与验证

日期：2026-10-18。用户授权：“完成S6”。完成待用户验收；不包含 S7、GitHub 推送或此前阶段的用户验收确认。接口见 [S6 说明](s6-browser-records.md)，重要决定见 [ADR 0006](decisions/0006-s6-browser-records.md)，阶段状态见 [ROADMAP](ROADMAP.md)。

## 实际改动

- Python 包内原生 HTML/CSS/JavaScript 控制台，支持 owner 会话恢复、选择节点、WS 提交/审批、结果与验证、历史、真实取消及持久全局控制。所有操作复用现有 Harness/权限服务。
- 客户端 WS 具有方向、帧、速率、连接数、发送超时和队列边界；任务/控制事务内追加统一持久通知索引，支持游标回放与 HTTP 快照恢复。页面有界处理消息和显示卡片，工具/用户内容只作为文本渲染。
- 显式同意的记忆 API、幂等保存、信息类型/验证/时效、替代/撤销和历史查询；可选项目关联；路由与任务创建原子写入；禁用模型适配器和真实调用表计数。
- Core 005 版本迁移、升级前一致备份、历史路由补记及旧请求幂等兼容。Node 沿用 S5，不增加工具、记忆或模型逻辑。本轮没有新增依赖。

## 环境与命令

Windows 11 / CPython 3.13.9 x64，既有锁定的 25 个依赖。测试均使用工作区临时库、受控进程、合成记录及 loopback；没有修改系统配置或操作真实个人目录。原有未提交 S5 工作保留。

源码完整回归：

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error --maxfail=3 --basetemp E:/NEXUS/var/s6-full-1
```

实际 **498 passed in 99.86s**，无跳过。此后补充 5 项 WS 方向/二进制/超限/速率及通知写入回滚检查，并收紧空白项目/验证依据，最终通过源码相关检查 **31 passed in 10.29s**；未将两次计数相加冒充完整回归。最终全部检查采用下述独立安装包。

```powershell
New-Item -ItemType Directory -Force var/s6-build-temp | Out-Null
$env:TMP='E:\NEXUS\var\s6-build-temp'
$env:TEMP='E:\NEXUS\var\s6-build-temp'
$env:PIP_NO_INDEX='1'
$env:PIP_DISABLE_PIP_VERSION_CHECK='1'
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s6-package
& .\.venv\Scripts\python.exe -B -m venv --without-pip E:/NEXUS/var/s6-check-env
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s6-check-env/Scripts/python.exe install --no-index --find-links E:/NEXUS/var/s3-wheelhouse -r requirements.lock E:/NEXUS/var/s6-package/nexus-0.1.0-py3-none-any.whl
$env:PYTHONPATH=''
& .\var\s6-check-env\Scripts\python.exe -I -B -c "from pathlib import Path; import nexus,pytest; location=Path(nexus.__file__).resolve(); print('Installed package:',location); assert location.is_relative_to(Path('E:/NEXUS/var/s6-check-env')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--maxfail=3','--basetemp','E:/NEXUS/var/s6-installed-full-1']))"
```

首轮独立安装包完整回归：**503 passed in 103.02s**，无失败或跳过。包从独立环境 site-packages 导入，独立 CLI 和工具进程使用该解释器。

收尾审查发现：某条前端事件详情查询失败后，后续已排队事件可能推进游标。修复为中止该连接剩余事件并请求完整同步，新增纯 Node 事件循环回归 `tests/web_recovery.cjs`。去掉此屏障的受控旧行为样本会失败（游标错误推进到 2）；最终源码和安装包脚本均通过失败事件屏障及稳定 request_id 重试检查。此测试不启动浏览器、不访问网络，也不增加包依赖。

同步 README 后重新构建、安装并运行最终包：

```powershell
& .\.venv\Scripts\python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s6-package-final
& .\.venv\Scripts\python.exe -B -m pip --python E:/NEXUS/var/s6-check-env/Scripts/python.exe install --no-index --force-reinstall --no-deps E:/NEXUS/var/s6-package-final/nexus-0.1.0-py3-none-any.whl
& .\var\s6-check-env\Scripts\python.exe -I -B -c "from pathlib import Path; import nexus,pytest; assert Path(nexus.__file__).resolve().is_relative_to(Path('E:/NEXUS/var/s6-check-env')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--maxfail=3','--basetemp','E:/NEXUS/var/s6-installed-final']))"
```

最终独立包完整回归：**503 passed in 93.25s**，无失败或跳过。最终 wheel SHA-256：`0271e59930be466d01906d7b8ca08ceb5f30e9fc7249be39b06ad923b3fad397`；67 个 Python/SQL/HTML/CSS/JS 文件逐字节与最终源码一致，包含 005 和三份前端资源，无遗漏或额外注入。

双环境 `pip check` 均返回 `No broken requirements found.`。使用已提供的 Node 运行 `--check src/nexus/web/app.js` 通过，没有安装 Node 工具或依赖。构建工具仍沿用原范围约束，未声称全量构建依赖已锁定。

## 真实进程与浏览器证据

新增进程测试在真实 HTTP/WS socket 连接独立 Core 与 Windows Node：浏览器角色会话提交系统信息、目录审批后成功；同 request_id 返回原任务；Core 重启后浏览器沿原游标收到暂停事件，记忆与项目/路由可查；暂停下提交的新任务经 cancel_all 取消，resume 可恢复。权威库仅两个执行尝试、三个规则路由，模型调用零行。

最终安装包此场景记录：旧 Core PID `52352`、重启 Core PID `13908`、Node PID `20384`；系统任务 `564ca65b-12d7-4322-b930-a35d4fa63056`，目录任务 `5c7eb970-2e40-40da-89d8-ec8cbcf12839`；目录后游标 `11`；验证级别 `node_report_schema_checked`。原始 evidence.json、进程日志及其余 S5 故障证据保存在 Git 忽略的 `var/s6-installed-final/`，这些受控进程由测试回收。日志检查不含原始测试凭证。

另外通过 Codex 内置浏览器实际操作本地临时 Core/Node：

1. Owner 登录，真实系统信息成功；目录任务停在审批，批准后看到中文目录名和 `hello & world.txt`，未返回文件正文。
2. 暂停派发，提交任务保持待派发；取消后显示已取消；恢复派发状态可见。页面刷新使用 Cookie 恢复会话和持久历史。
3. 创建合成项目，勾选明确同意后保存合成 plan；页面刷新后仍可查询，未将其自动标为事实或已验证。
4. 记忆包含 `<img src=x onerror=alert(1)>`，页面显示原文字，DOM 内注入图片数量为 0。
5. 390×844 与 320×740 视口检查均无横向溢出；单列按钮和表单可操作。390 像素时内容宽 375（其余为滚动条），320 像素时内容宽 305。
6. 最终浏览器 error/warn 日志为空；验证结束恢复默认视口并停止临时服务，清除该临时登录文件。

截图：[桌面](assets/s6-desktop.png)、[手机尺寸](assets/s6-mobile.png)。这些是实际浏览器截图，手机尺寸截图不代表 Android 真机验收。

## 边界与恢复验证

| 检查 | 实际结果 |
| --- | --- |
| Cookie、Origin、CSRF、角色、节点选择、空闲会话撤销 | 拒绝未登录/错误来源/Node 令牌冒充，撤销关闭旧客户端连接；新会话 CSRF 可稳定恢复 |
| 非 JSON、反向消息、二进制、超 64 KiB、速率 | 无任务创建；非法帧有限错误，错方向 1008，超速 1013 |
| 慢发送与队列 | 32 条后拒绝入队，真实阻塞发送 2 秒超时；同期间 Harness tick 在 1 秒内返回 |
| 事件恢复与身份过滤 | 任务/控制统一递增，Core 重启后原游标回放；无效游标要求快照，其他身份无可见任务事件 |
| 前端事件处理失败 | 后续排队事件不能越过失败位置推进游标；重连保留原任务请求 ID 和节点绑定 |
| 通知写入故障 | 触发器写入失败回滚任务、路由、任务事件及任务审计，不产生已接收假象 |
| 同意与输入边界 | 缺省、false、数字/字符串同意、非法类型/时效/验证依据和超限内容均拒绝，无同意/记忆残留 |
| 替代、撤销、过期、项目、幂等 | 默认查询排除失效条目，历史保留；项目归属校验；重提同请求无重复记录；重开数据库与应用可查 |
| 记忆写入故障 | 同意记录和条目整笔回滚，并停止后续新派发 |
| v4→v5 升级 | 升级前备份 quick_check 通过，原列/事件/身份/授权/审批/控制保留，外键通过；旧格式请求仍去重，补记路由耗时 null |
| 模型误调用 | estimate/invoke/decide 全部 MODEL_DISABLED，网络保护测试未触网，真实 model_calls 零行；费用零、成功率不适用 |
| 既有功能 | S1–S5 权限、Windows 路径/Job、进程停止、恢复和故障测试在 503 项完整回归中通过 |

## 失败记录与限制

默认 pytest 临时目录访问被拒，改用工作区 `--basetemp` 后继续，未把环境失败算作通过。旧迁移数量断言按新增模式由 4 更新为 5；旧进程检查改为 model_calls 存在但零行。新增速率测试首轮假定关闭前一定收到 BUSY 帧，实际有界关闭可能先发生；修正为接受有限错误后关闭或直接 1013，并复测通过。

未运行 Android 真机、远程可信 TLS、Windows 10、Python 3.12、其他系统或大规模负载。未验证超长运行数据库增长和归档；不提供记忆物理删除、自动提取或模型启用。浏览器最多 10 连接、页面/查询有界；不是公开多用户服务。S6 前会话重新登录行为见接口文档。弱网中的手机原生行为仍留 S7 实测。

交付自检另确认 187 个本地文档链接、3 个 JSON 示例、代码围栏、Node/Core AST 导入边界、67 个包文件一致性、Git diff 及新增文本文件空白检查通过；仅有既有 LF/CRLF 提示。临时端口已停止监听、登录文件不存在，临时 owner 会话已撤销。

S6 技术交付完成待用户验收；没有进入 P11/P12、执行用户真实个人记忆保存、变更依赖、修改系统设置、提交或推送 GitHub。
