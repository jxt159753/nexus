# M6 本地交付验证

本文件保留首轮本地交付时点；后续追加的会话工作、自然语言权限答复及已授权生产升级见 [M6 上线验证](M6-WORK-VERIFICATION.md)，当前状态只查 [路线图](ROADMAP.md)。

记录日：2026-10-20。阶段批准与当前状态唯一入口为 [ROADMAP](ROADMAP.md#m6项目与可控记忆)。这是本次固定交付的技术证据，不代替用户验收，也不表示生产已升级。

## 范围与实际实现

用户批准完成 M6 及基础工具，解释权限影响后明确选择指定工作区、写入审批和受控命令。完成项目会话/任务/结果关联、聊天内显式记忆管理、逐轮预览与独立外发同意、带证据的待办与修改历史；同时增加受限读文本、名称查找、写入预览/审批与固定命令。设计见 [ADR 0017](decisions/0017-m6-projects-basic-tools.md)，使用见 [M6 说明](m6-projects-tools.md)。

复用现有 Core/Node、预算、审计、审批、attempt 和回执。迁移为 014 / 015，未新增运行依赖、任意 shell、自动目录扫描、后台模型循环、删除工具或新的生产权限。工作区原有 M4/M5 未提交改动保留；没有提交、推送或替换线上服务。

## 环境与证据层级

- Windows / Python 3.13.9，使用项目 `.venv` 及锁定依赖。
- 独立 Core 与 Windows Node 真实进程、真实 loopback HTTP/WS、Windows Job，全部使用测试临时数据库、目录、身份和受控子进程。
- 浏览器使用本机 Edge，通过已有 Playwright 库操作，桌面 1440×1000、手机尺寸 390×844；不是 Android 真机。
- 聊天采用本地合成 SSE 或测试 Provider，覆盖真实解析/持久化路径；真实 DeepSeek 外发为零，未消费生产账本。

## 实际检查

| 检查 | 实际结果 |
| --- | --- |
| 项目隔离与记忆同意 | 其他项目记忆拒绝；保存不外发；逐次预览/同意；撤销/过期阻止新调用；旧含记忆轮次不无声带入历史；重开 Core 后来源快照保留 |
| 记忆替代与来源 | 聊天来源必须对应同项目轮次；跨项目替代拒绝且事务不改变旧条目；保存/修改/撤销实际浏览器流程通过 |
| 项目待办 | 重复请求去重、陈旧 revision 拒绝、缺失/未成功/跨项目证据不能标完成；修订历史与重启查询通过 |
| 旧库升级 | 有项目、记忆同意、授权、会话/轮次和任务的 v13 夹具升级至 v15；原表全部原列/行逐项比对、一致备份和外键检查通过；旧会话无项目、旧轮次无外发记忆 |
| 文件边界 | 绝对/穿越/ADS/设备名等拒绝；真实 Windows junction 与硬链接不跟随；UTF-8 截断、文件名递归边界、完整写入摘要与变更拒绝通过 |
| 真实设备工具 | Core→Windows Node 读取、查找、写入预览、逐次审批后实际修改文件、固定命令输出均通过；重复提交仍为原任务 |
| 真实停止与恢复 | 固定命令生成受控后代进程，取消后整个 Windows Job 停止；Core 重启后写入结果和项目任务保留；写入与命令不自动创建第二次 attempt |
| 浏览器 | 项目切换、明确保存、选取/预览/外发、来源查看、刷新恢复、替代/撤销、待办修订历史及手机宽度无横向溢出通过；无页面 JavaScript 错误 |
| 源码回归与固定包 | 完整源码运行 744 通过 / 1 浏览器等待竞态失败；修正测试后浏览器单独通过，另补 v13 迁移通过。独立安装定向 179 通过 / 1 相同浏览器时序失败，修正后单独通过。最终包 23 项全部通过，118 文件与源码一致 |

浏览器截图：[桌面聊天](assets/m6-desktop.png)、[手机尺寸项目工作台](assets/m6-mobile.png)。所有内容均为测试项目及测试数据。

## 验证期间的纠正

最初沙箱临时目录不可写，Windows asyncio 的 loopback socketpair 亦在沙箱内挂起；改用仓库内独立临时目录，并按客户端授权流程运行本机受控测试，没有改变系统网络或信任设置。系统 PATH 首选 Anaconda OpenSSL 无法生成测试证书，改为现有 Git OpenSSL，仅写测试目录，不安装依赖或导入系统信任。

实现检查发现并修复：新 UUID 参数的 JSON 序列化、Windows DirEntry 的链接计数缓存、Skill 项目关联查询、未知路由输入兼容性；同步了新请求字节上限与 hello 字段的原测试断言。浏览器检查修正测试脚本在刷新后未展开记忆面板，以及使用系统 Edge 取代未安装的 Playwright 下载浏览器。首次完整运行 741 通过 / 3 失败，均在交付前处理并重测，不作为最终通过数。

后续完整运行 744 通过 / 1 浏览器时序失败（355.60 秒）：测试在异步保存后过早点击旧的历史按钮，等待界面显示服务端 revision 2 后重测，源码通过（12.33 秒）、独立包通过（11.90 秒）。没有为此放宽断言或删除验收项。随后修正了记忆编辑表单保留原有效期的细节，并将有效期及退出清理纳入最终固定包检查。

## 固定包与可复现检查

最终 wheel：`var/m6-dist/nexus-0.1.0-py3-none-any.whl`，229401 字节，SHA-256 `18be516066142a815ba6ddcdec12594db5fd3046917fdaa8e9b89e77bb5d8ef0`。包内 118 个 Python/SQL/HTML/CSS/JavaScript 文件与最终源码逐字节一致，包含 014/015 迁移与两个新页面脚本。只安装到独立 `var/m6-delivery-installed` 目录，未替换 `.venv` 或生产环境。

实际主要命令（所有临时目录均在仓库 `var/`，不是用户数据库）：

```powershell
$env:NEXUS_TEST_OPENSSL='C:\Program Files\Git\usr\bin\openssl.exe'
$env:NEXUS_TEST_NODE='C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$env:NODE_PATH='C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
& ./.venv/Scripts/python.exe -B -m pytest -q --basetemp=E:/NEXUS/var/m6-complete-final --junitxml=E:/NEXUS/var/m6-complete-final.xml
# 上述完整运行保留 744/1 记录；修正浏览器测试等待后分别独立复测通过。
$env:PYTHONPATH='E:\NEXUS\var\m6-delivery-installed'
& ./.venv/Scripts/python.exe -B -m pytest -q -o pythonpath=E:/NEXUS/var/m6-delivery-installed --basetemp=E:/NEXUS/var/m6-delivery-check --junitxml=E:/NEXUS/var/m6-delivery-check.xml tests/test_projects_m6.py tests/test_basic_tools.py tests/test_m6_processes.py tests/test_m6_browser.py
```

最后一条最终包验证 **23 通过，23.15 秒**。有数据旧库迁移、真实 Windows 双进程工具/取消/重启、浏览器实际操作及有效期保留/退出清理均在其中。源码未为这次补验改变后端逻辑；最终新增的有数据迁移测试亦已单独通过。没有把不同运行计数简单相加冒充一次全量运行。

文档链接/章节、示例 JSON、Python AST、JavaScript 语法及差异空白检查通过。脱敏记录见 [m6-evidence.json](assets/m6-evidence.json)。

## 限制与下一步批准

没有修改生产 Core、Windows Node 或其工作区/命令配置。线上仍为 M5 / schema 013；生产升级、迁移与基础工具授权需要单独批准并先一致备份。示例配置不自动启用新工具。

真实 DeepSeek、Android 真机、Linux Core 新版本运行以及用户最终整阶段验收尚未补验；既有 M2–M5 的未验收项未改记完成。首版新工具从任务页明确操作，未接入聊天/生成 Skill 的自动目录。文件最后校验与原子替换之间仍有受信本机并发修改窗口；固定命令工作目录不是 OS 沙箱，命令效果需管理员理解并允许；失败/取消不承诺回滚副作用。

本阶段交付停止于本地实现与受控技术验证，未进入 M7。
