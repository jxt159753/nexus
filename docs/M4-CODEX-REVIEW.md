# M4 独立复核与修复记录

日期：2026-10-19。复核基线：本地 `codex/m4-self-extension` 的 `694d4ec`，其中 DSH 最终代码为 `57ea5a2`。用户先要求验收，随后明确“你自己改吧别找dsh了”；本次由 Codex 直接修复，没有启动或联系 DSH。

结论：原交付不能直接验收。原有 Windows 657 项回归虽通过，独立补充的 7 项行为检查全部失败。下列缺陷已修复并增加永久回归；真实 Linux/runsc 隔离仍未验证，完整 M4 保持未完成。阶段状态统一见 [ROADMAP](ROADMAP.md)。DSH 原交付快照保留在 [M4-VERIFICATION](M4-VERIFICATION.md)。

## 1. 发现与修复

| 问题 | 实际影响及修复 |
| --- | --- |
| cancel_all 与创建步骤竞争 | 取消提交后仍可能新建并派发设备命令。步骤创建与取消共用 Harness 操作锁；任务与 Skill 步骤关联在同一事务提交，事务内再次检查根运行批准及取消状态。 |
| 停用后遗留待派发任务 | 根 Skill 失败，但先前创建的步骤仍能独立派发。首次派发、重传及实际发送均检查根 Skill；停用与最终发送共用锁，并对遗留任务请求真实取消。 |
| Windows / Sandbox 节点混用 | 第三步误用 Skill 的 Windows 节点，而 Tool 绑定另一台 Linux 节点。现在从 Tool 固定环境选择其沙箱，批准仍分别约束两个节点。新增两个不同 UUID 的组合回归。 |
| 取消生成误杀后台 worker | Provider 子任务取消被当作 worker 本身关闭；后续生成请求无人处理。区分子任务取消与根协程关闭，保留未知费用；测试确认取消后下一条模拟请求能完成。 |
| Node 无法及时接收取消 / 提前释放名额 | 接收循环等待阻塞执行，取消只能在执行后读取；断线或根协程关闭时可能先释放锁。接收与执行分开，传递停止事件，等待所持执行清理返回后才关闭回执和数据目录锁。 |
| 版本批准没有重查当前策略 | 记录了 policy_version，但旧批准仍被接受。统一检查最新测试、最新批准决定、test_id、摘要、owner、当前策略、资源/参数摘要及期限；页面允许对失效批准重新审阅批准。原业务工具授权此前仍存在，不能把该缺陷描述为已绕过所有权限。 |
| 4096 输出 token 未纳入预算配置检查 | 有效的 M2/M3 价格组合可能让生成调用理论上限超过每根 $0.05。配置现在同时验证扩展生成的输入/输出费用上界；测试使用合成价格，不代表新增真实消费。 |

额外核对修复了三个直接相关的问题：

- stdout/stderr 原先先写宿主文件、退出后才查大小；现改为执行期间有界管道捕获（32 KiB / 8 KiB），超限、超时或取消触发所持进程停止及固定 runsc 清理。候选输出不再落宿主 spool 文件。
- Sandbox 远端明文 `ws://` 原先可配置；现只接受 `wss://`，或显式 `local_http=true` 的字面量回环开发地址。没有关闭 TLS 校验或修改系统信任。
- 能力页原先只列 v1 节点，沙箱状态接口调用不存在的方法，且源码字段与页面不匹配。现在分别列设备/沙箱，状态接口有独立 HTTP 回归，源码/Markdown 正确显示，刷新保留已展开的审阅区。生成预览包含最多 20 个有效已启用 Tool 的固定版本声明及所选父版本的 manifest/步骤；只有确认该精确预览才发送，不自动附带源码、工具结果或设备文件。

业务 Tool 执行使用已落盘 tool_run_id 作为 v2 execution_id。Core 中断将 `dispatching` 记录保留为 unknown，不自动重放。未确认停止会阻断当前 Sandbox 连接；Node 启动时遇到 unknown 回执也停止接收新执行。`sandbox.cancelled(stopped=false)` 只是已收到停止请求，不能结束等待或宣称已停止。完整自动对账/结果 ack 尚未实现，见第 4 节。

## 2. 实际验证

全部使用受控开发资源，真实模型调用为零，生产服务/数据库/凭证未访问。

- 原始源码复核：`657 passed in 263.80s`；独立行为检查：`7 failed in 4.46s`。这些失败是修复依据，未计入通过。
- 修复后 M4 定向：`45 passed in 23.49s`，`-W error`、无跳过。覆盖原 20 项及新增 25 项行为/生命周期/进程/HTTP 检查。
- 真实独立 Core + Windows Node：从夹具候选、替身测试、具体版本批准和启用进入两步 Skill；系统信息为实际 Windows 结果，目录任务在原审批前零执行；在待目录审批时重启开发 Core，沿用原 task_id 完成，每步仅一次 attempt、模型调用零。
- 故障注入：关联写入触发 SQLite ABORT 后，task、routing、步骤关联一起回滚；已落盘但未结算的 Tool 执行恢复为 unknown，不创建新执行。模拟传输覆盖取消/停用信号、最终发送门和停止证据；不冒充真实 runsc 对账。
- 受控固定 Python 探针验证 stdout/stderr 超限、超时、取消和正常输入交换；只终止测试自己创建的进程，不执行模型生成源码。它证明管道/进程控制逻辑，不证明 Linux runsc、cgroup、网络或宿主隔离。
- 浏览器：真实应用内浏览器、独立回环开发 Core、合成身份。类型切换分别出现设备 UUID 与独立沙箱 UUID；Tool 候选在沙箱离线时显示“测试不可用”，没有批准/启用入口；源码和沙箱绑定可审阅，刷新后展开状态保留。实测视口 392×719，无横向溢出，不是 Android 真机。见[页面证据](assets/m4-codex-sandbox-refusal.png)。
- 最终完整源码回归：Windows/Python 3.13.9，`682 passed in 246.86s`，`-W error`、无跳过。
- 最终独立安装包回归：新候选环境，`-I -B -W error -o pythonpath=` 禁用源码路径，`682 passed in 234.06s`，无跳过；原 25 个锁定依赖及 `pip check` 通过。

中间检查如实保留：第二次模拟生成测试最初漏传 fixture 必填参数而超时，修正后通过；源码审阅检查揭示 API 返回 `source_text` 而页面读取 `source`，已修正并复验。浏览器首次打开沙箱列表出现 500，定位为 `ToolService.status` 缺失，修复后重新打开页面并补 HTTP 回归。首次无隔离构建因本机 setuptools 84 不满足既有 `<81` 要求失败，改用项目原隔离构建流程的 setuptools 80.10.2 成功；没有修改运行依赖。

## 3. 候选包与复现

- wheel：`var/m4-codex-review/dist/nexus-0.1.0-py3-none-any.whl`。
- SHA-256：`843abaecd96201ec55b4d7541e5e1272de28295478aca8df8a8d8bfcf14622af`。
- wheel 内 103 个源码/资源与 `src/nexus` 完全同集、逐字节一致；新候选 venv 离线安装原 25 个锁定依赖，`pip check` 通过。Python 130 个源码/测试文件语法、三份 JS 语法通过。
- 7 份相关文档的 119 个本地链接、代码围栏、脱敏 JSON、示例 TOML 和正常 Git 行尾配置下的 diff 空白检查通过。复查确认未改写迁移、锁定依赖或普通 Windows Node，没有无关重构。
- 仍为 schema 011；未改写迁移、增加运行依赖、推送 GitHub 或部署。所有原始日志、临时库和合成测试凭证留在 Git 忽略的 `var/m4-codex-review/`。本轮临时开发 Core 与浏览器标签已关闭，未操作生产进程。

```powershell
$env:NEXUS_TEST_OPENSSL='C:/Program Files/Git/usr/bin/openssl.exe'
# 每次 basetemp 选择未使用的项目内路径。
& ./.venv/Scripts/python.exe -X utf8 -B -m pytest -q -p no:cacheprovider --tb=short -W error --basetemp E:/NEXUS/var/m4-codex-review/reproduce-source
& ./var/m4-codex-review/installed-env/Scripts/python.exe -I -X utf8 -B -m pytest -q -p no:cacheprovider -o pythonpath= --tb=short -W error --basetemp E:/NEXUS/var/m4-codex-review/reproduce-installed
```

脱敏结果汇总在 [m4-codex-evidence.json](assets/m4-codex-evidence.json)。本次所有修改仍在原本地分支，未代替用户给具体能力版本批准，也未改生产配置。

## 4. 保留未完成项

1. 专用 Linux/runsc 主机、镜像和宿主限额/隔离/实际进程清理没有实测。生成 Tool 的实际“生成→测试→批准→执行”及第三步业务统计因此仍未完成，不能用普通 Windows 子进程替代。
2. v2 当前没有完整的持久测试 attempt、Core 结果 ack 与自动状态对账。采取 unknown 阻断和不重放只是保守失败处理，不能等同于 A13 全部通过；真实接收/落盘/ack 间故障仍需补齐。
3. Skill 目前只支持字面量和前步字段引用；用户输入引用、运行入口 request_id 幂等与长期运行记录容量控制仍未完成工作文档的完整要求。两步示例输入为空不受影响，但不能宣称通用 Skill 已完成。
4. 真实 DeepSeek 扩展生成、Linux/Python 3.12 候选回归、Android 真机和生产迁移均未新增验证。没有借用先前 M3 的部署/调用批准。

本次结论限于已确认缺陷的本地修复和列明的验证。P23/P24 及完整 M4 验收保持未完成；下一步应先具备独立沙箱环境并完成剩余协议/故障项，再决定具体版本上线。
