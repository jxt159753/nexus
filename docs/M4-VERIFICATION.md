# M4 首版自扩展验证记录

日期：2026-10-19。实施方：DSH。独立复核与最终用户验收：待 Codex / owner。本文只记录实际执行结果；未运行的项不填 passed。

## 1. 基线与环境

- 交接基线：`a8baa7710eb84c14b54d15fbdeb1532184625eee`（工作文档记录）。
- 代码提交：`9220281a91b3dd37bdacccda8e0f8dbf77e3388f`（`feat: implement M4 bounded Tool and Skill self-extension`），最终代码提交 `57ea5a252ec1a24a1fc4f8e704405f2d803d9750`（`fix: bind sandbox Tool execution to its approved node`）。本文与证据文件位于其后的文档提交。
- 分支：`main`（未新建分支、未推送；用户未给本轮推送授权）。
- 平台：Windows 11 `10.0.26200`，PowerShell `5.1.26100.9711`，Python `3.13.9`，项目 `.venv`。
- Core schema：升级前 `010`，新增顺序迁移 `011_self_extension.sql`；未改写 001–010。
- 依赖：未新增运行依赖；`requirements.lock` 的 25 个锁定包不变。
- 生成模型：未配置、未调用。所有候选来自显式 `fixture_generation=true` 的开发夹具或测试注入的模拟 Provider。
- Sandbox：本机无 `runsc`/Docker；WSL2 Ubuntu 处于 Stopped，未安装或改造 runsc，未把它当作合格沙箱。没有专用 Linux 沙箱主机、镜像或新增资源授权。

## 2. 实际改动

| 范围 | 内容 |
| --- | --- |
| 契约 | 严格 manifest、有限 JSON Schema 子集、Skill 步骤/字段引用、规范化 release/摘要、v2 Sandbox 消息 |
| 存储 | 迁移 011：扩展请求、不可变版本、测试、批准、启用、Skill run/步骤、Tool 执行；`model_calls` 增加可空扩展关联 |
| Core | 外发预览/确认、单次生成、共享预算、版本测试/批准/启用/停用、依赖门、Skill 2–3 步运行、Tool 可信控制器 |
| Sandbox | 独立 `/ws/v2/sandbox`、认证/摘要绑定、Sandbox Node、runsc 预检、固定 OCI bundle/限额/清理代码；Windows 配置直接拒绝 |
| Web | “能力”视图：预览、生成、版本内容/差异、测试、批准、启用、停用、使用与运行步骤证据；文本渲染 |
| 文档 | 协议/环境清单、使用说明、ADR 0014、架构与 ROADMAP 同步 |

## 3. 实际命令与结果

### 3.1 M4 定向测试

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error tests/test_extensions_m4.py tests/test_extensions_http.py tests/test_sandbox_m4.py tests/test_extension_migration.py
```

结果：`20 passed in 10.50s`，退出码 0，无跳过。覆盖候选 schema/重复键、夹具生成、预览/确认、预算阻断、Skill 测试/批准/启用、两步执行、目录审批、停用、暂停/取消、三步组合（测试替身）、伪造输出拒绝、依赖门、HTTP 鉴权/CSRF/Origin、v2 协议拒绝、Windows/runsc 拒绝、v10→v11 有数据迁移。

### 3.2 Windows 源码完整回归

```powershell
New-Item -ItemType Directory -Force ./var/m4-check | Out-Null
$env:PATH = 'C:\Program Files\Git\usr\bin;' + $env:PATH
& .\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error --basetemp ./var/m4-check/pytest-source
```

结果：`657 passed in 303.30s`，退出码 0，无跳过。使用 Git OpenSSL 3.5.4 是既有测试环境要求；未修改 TLS/信任设置。原始 basetemp 在 Git 忽略的 `var/m4-check/pytest-source`。

### 3.3 独立安装包完整回归

```powershell
& .\.venv\Scripts\python.exe -m build --wheel --outdir E:\NEXUS\var\m4-check\dist
# 离线 wheelhouse + 新 wheel 安装到全新 venv
& E:\NEXUS\var\m4-check\installed-env\Scripts\python.exe -m pip check
$env:PATH = 'C:\Program Files\Git\usr\bin;' + $env:PATH
& E:\NEXUS\var\m4-check\installed-env\Scripts\python.exe -I -B -m pytest -q -p no:cacheprovider -o pythonpath= --tb=short -W error --basetemp E:\NEXUS\var\m4-check\pytest-installed
```

结果：wheel 构建成功；25 个锁定依赖离线安装、`pip check` 通过；禁用源码 pythonpath 后 `657 passed in 267.53s`，退出码 0，无跳过。

- wheel：`nexus-0.1.0-py3-none-any.whl`，SHA-256 `d3605c7e887750fbefb20edcd1bc241fc02ea7134ab5a2644dd2c26c4d06c1a5`。
- wheel 内 `nexus/` 102 个源码/资源文件与 `src/` 逐字节一致；含迁移 011、`extensions.js`、`sandbox` 包。
- 未在 Linux 安装该 wheel；Linux/3.12 本轮未复测。

### 3.4 真实 Edge 能力页

- Edge `155.0.4283.62` headless，CDP；本地隔离 Core（端口 8765，开发夹具，模型禁用），非生产。
- 390×844 与 1280×960；`document.documentElement.scrollWidth > innerWidth` 均为 false；页面异常/日志错误为 0。
- 实际操作：进入“能力”→填写 Skill 需求→预览（显示 fixture、不外发模型）→勾选确认→生成→候选出现→运行受控测试→批准具体版本→启用。结果文本包含“已启用”“测试通过”、摘要、批准期限、参数摘要和“使用此 Skill”。
- 截图：[桌面](assets/m4-capability-desktop.png)、[390 像素能力页](assets/m4-capability-mobile.png)。截图是本机 headless Edge，不是 Android 真机。
- 初次检查发现预览 JSON 的 `<pre>` 未换行导致 390/1280 横向溢出；补 `min-width:0` 和 `pre` 换行/滚动样式后复验无溢出、无 JS 错误。中间失败未计为通过。

### 3.5 迁移与检查

- 非空 v10 临时库升级到 v11：一致备份 `.before-v11-*` 存在，quick_check=ok，外键错误 0，旧 identities/model_calls 行保留，新增扩展表存在；见 `tests/test_extension_migration.py`。
- `compileall -q src tests` 通过；`nexus.core.app` 导入通过。
- `node --check` 对 `app.js`、`chat.js`、`extensions.js` 通过。
- 未读取/打印生产 Key、Node 令牌、DPAPI、TLS/SSH 私钥或生产数据库；未访问生产服务。

## 4. A01–A18 证据索引

| ID | 状态 | 证据/边界 |
| --- | --- | --- |
| A01 | 通过（本机/临时库） | 严格 JSON/重复键/未知字段/Schema/大小上限；候选可追溯到 request；不产生可执行版本。`test_extensions_m4.py`、`test_extensions_http.py` |
| A02 | 部分 | 共享 `model_calls`、预览摘要确认、预算耗尽零新调用、单并发检查；未做真实模型并发生成。`test_generation_preview_and_budget_gate` |
| A03 | 通过（结构） | release/artifact/environment/test 摘要绑定；批准绑定 test_id/digest/环境/节点/参数摘要；无版本原地修改入口。改内容只能新 revision。 |
| A04 | 通过（扩展接口） | 无会话 401、无 CSRF/错 Origin 403、节点角色不能代替 owner；HTTP 测试；既有身份撤销测试包含在 657 回归。 |
| A05 | 通过 | 4 步、后向引用、嵌套/动态工具、表达式均拒绝；`test_plan_limits...`。 |
| A06 | 部分 | 两步“工作区概览”经候选→替身测试→批准→启用→正常 task/attempt/目录审批执行；节点回执由测试注入，**未启动真实 Windows Node 完成本轮 Skill**。 |
| A07 | 未运行 | 无合格 Sandbox Node；生成 Tool 未在真实沙箱运行，未批准。测试替身的三步统计不等于 A07。 |
| A08 | 未运行 | 无 runsc/宿主 cgroup/网络/秘密/进程清理实测；WSL 未安装 runsc，未用普通进程替代。 |
| A09 | 部分 | 可信控制器固定用例、伪造输出被拒；使用 FakeSandbox 传输替身，**未在真实 runsc 中比较候选代码**。 |
| A10 | 部分 | 三步 Skill 结构、字段引用、截断传播、依赖门和替身闭环测试；第三步真实沙箱业务执行未完成。 |
| A11 | 部分 | 停用阻止新步骤、依赖/批准重查有测试；未做真实派发/停用毫秒级竞态与在途对账。 |
| A12 | 部分 | 暂停阻止下一步、取消 Skill run 有测试；未对真实设备执行停止确认。 |
| A13 | 未运行 | 未注入生成/预算/派发/沙箱/结果落盘间崩溃；无真实 Sandbox Node 恢复。 |
| A14 | 部分 | 候选数量/总字节拒绝新增、保留旧证据；未对 Core/Node 回执写入失败做扩展专项注入。 |
| A15 | 通过 | v10 有数据升级、备份/quick_check/外键、旧行保留；旧协议 v1 回归在 657 项内。 |
| A16 | 通过（回归） | Windows 源码与独立安装包各 657 项，含 M2/M3 聊天、工具确认、目录审批、自动回传；无跳过。 |
| A17 | 部分通过 | 真实 headless Edge 390/1280，能力页完整创建/测试/批准/启用，无 JS 错误/横向溢出；不是 Android 真机，未做刷新/跨进程恢复的浏览器专项。 |
| A18 | 通过（拒绝路径） | 扩展关闭时 M3 回归通过、创建新扩展被拒；沙箱未配置/Windows 配置/runsc 缺失均拒绝；未执行生成代码。 |

## 5. 中间失败与修复（如实保留）

1. 首次全量命令的 pytest 临时父目录不存在，导致 419 个 setup 错误；创建 `var/m4-check` 后复跑，不把该次计入通过。
2. 新增 `model_calls.extension_request_id` 后，两个旧迁移测试仍按 10 列/10 个迁移断言失败；更新为 11 列/11 个迁移并复验通过（只调整预期，不改变迁移语义）。
3. 严格 Pydantic UUID 在 Python dict 校验时拒绝字符串；改为先做重复键/NaN 检查、再经 JSON 校验，避免放宽未知字段。
4. Skill 步骤 runner 误把数据库行当步骤定义读取 `tool` 字段，出现 `IndexError`；改为显式传入已解析的 definition，并补两步执行测试。
5. 能力页预览 `<pre>` 在 390/1280 宽度下把 grid 撑到 1666px；补 CSS 后 CDP 复验 `overflow=false`、`errors=[]`。
6. 一次 PowerShell 文本替换破坏了 `tests/test_extensions_m4.py` 的 UTF-8 中文；删除并从完整内容重写后测试通过。仅测试文件，未影响应用实现。
7. 全量回归最初两次 OpenSSL 测试失败，使用既有 Git OpenSSL 3.5.4 的 PATH 后 657 项通过；未关闭 TLS 校验或修改系统信任。

## 6. 未验证与受限

- 真实 runsc/Sandbox 主机、镜像树摘要、宿主 CPU/内存/PID/tmp/网络/秘密/控制 socket、超时/取消/崩溃清理：**全部未实测**；P23 真实隔离、P24 第三步真实业务执行保持未完成。
- 真实 DeepSeek 扩展生成、真实费用/用量、模型截断/非法输出：**0 次真实调用**；只在测试中注入模拟 Provider/夹具。
- 真实 Windows Node 上的 Skill 两步执行：未启动；两步测试通过 `ExecutionRepository` 注入 Node 证据。
- Android 真机、生产部署、生产备份/迁移/凭证/数据库：未触碰。
- 跨进程崩溃/断线/回执丢失专项、停用与派发竞态、Core 写入失败专项：未完成。
- Linux/Python 3.12 安装包回归未复测；wheel 只在 Windows 3.13 独立环境验证。
- 本机页面使用 headless Edge，不是 Android 真机；未做登录恢复/刷新专项的浏览器自动化。

## 7. 复现步骤

1. 按 [M4 使用说明](m4-usage.md) 准备开发配置（默认关闭；夹具只在开发显式打开）。
2. 运行 3.1 的定向命令；应得到 `20 passed`。
3. 运行 3.2 或 3.3 的完整命令；Windows/Python 3.13.9 应得到 `657 passed`。3.3 需要先 `pip install` 锁定 wheelhouse 和新 wheel。
4. 页面验证：本地启动开发 Core，登录后进入“能力”，依次预览→确认→生成→测试→批准→启用；390 像素检查横向滚动与 JS 错误。
5. 真实 Sandbox 验证需另备专用 Linux/runsc 主机并按 [协议第 5 节](m4-extension-protocol.md#5-宿主环境门槛) 完成负向测试；当前无此环境，不得用普通 `subprocess` 替代。

## 8. 范围与复杂度复查

- 未改 v1 消息联合类型、固定两工具、M2 512 token 或 M3 2048 token 限制；未新增依赖或生产配置。
- 扩展表使用现有 task/attempt/event/audit/model_calls 语义；Skill 步骤仍是正常任务，没有第二调度器或独立费用账本。
- Core 不导入/执行候选；Node 固定注册表不加载生成代码；Windows Job 未被当作安全沙箱。
- 未删除/重建历史数据库；迁移只追加表和可空列。未提交 `var/`、数据库、凭证或虚拟环境。
- 本次没有生产部署、真实模型调用、生产凭证/数据库访问，也没有沿用 M3 的三次调用授权。

## 9. 交付边界

本交付是开发与隔离验证的**部分完成**：可复核 P21/P22 的 Core/契约/Skill/页面与拒绝路径；P23/P24 的真实 runsc 隔离、生成 Tool 批准/执行和 Android/生产验收未完成。不得据此宣布 M4 完成、用户验收通过或推进下一阶段；等待 Codex 独立复核与 owner 决定。
