# 跨轮代码与保存衔接修复验证

日期：2026-10-21。用户反馈在聊天生成猜拳代码后，“保存到本地”无法承接，要求参考 GitHub 上 DSH 修复。当前任务/阶段状态只更新 [ROADMAP](ROADMAP.md)；技术边界见 [ADR 0022](decisions/0022-dsh-context-compaction.md#跨轮纯文字与代码衔接修复)。

用户随后追加：检查并修复工具失败不反馈给模型的问题，完成后部署到唯一云端 Core 并上传 GitHub。本轮发布与验证同时覆盖这项追加范围。

## 参考与实际改动

固定核对 DSH 提交 `687f282e8625c6b8d11ff58211a90524be2e4317`：

- [agent.ts](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/agent.ts)：将正常 assistant 消息追加到会话 surface，再按实际 tool-call 决定执行或结束。
- [tool-pairing.ts](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/compaction/compaction/src/tool-pairing.ts)：按实际调用/结果事件计算完整配对边界。
- [runtime-context.ts](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/runtime-context.ts)：运行上下文与会话消息分别投影和持久记录。
- [write.ts](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs/src/write.ts)：根据实际工具范围提供写入指导，并提示覆盖前读取原文件。

NEXUS 原来把 `work_grant_id` 本身当成隐私边界。工作区默认开启工具会给纯文字回复也附上该标记，下一轮因此连刚生成的代码都不发送。普通历史、压缩历史及压缩前来源重查使用同一判定；2026-10-21 用户明确批准已完成工具轮次自动进入后续普通上下文，记忆和显式资料引用仍保持边界。

模型提示按实际提供的 `write_file` 指导生成工作区文件，承接“保存到本地”的已有代码，省略文件名时选择合理相对文件名，覆盖前检查原文件；明确只要示例或解释时保留文字回答。另明确聊天历史、显式长期记忆编辑器与文件保存的区别，修正旧 Skill 提示中泛化的“禁止生成代码”措辞。没有开放任意命令、自动执行生成代码或新增设备权限。

追加检查确认普通执行失败、固定命令非零退出与已知网页错误原已回传，但写入预览失败会直接停止，生成 Tool 失败遗漏控制器错误。修复复用原生结果路径返回已知预览/执行失败及公开错误，标明预览未写入；确认无子执行且授权有效的准备错误可返回 `not_started`。权限拒绝、撤销、预算、停止和执行/费用未知继续阻止自动续接。

## 实际验证

Windows / Python 3.13.9，沿用 `.venv` 和现有依赖。全部模型使用替身或本地合成传输，没有真实供应商请求。

修复前运行四种组合（内部工作区范围/旧显式会话授权 × 压缩开/关）：4 项均在 `context_count` 预期为 1、实际为 0 处失败，复现原问题。原输出保留在 Git 忽略的 `var/continuity-fix/before-3.xml`。

修复后第一轮专项检查 13 项通过；随后补全隐私边界后的连续问答、上下文关闭和跨会话隔离，相关回归 148 项通过（114.69 秒），包含 15 项新增场景，最终这 15 项单独重跑也通过：

- 开启工具但未调用时，上轮代码逐字进入下轮模型消息和持久请求正文；普通/压缩模式与两种授权方式均覆盖。
- “生成代码 → 保存到本地”经原 Core 任务创建/派发/验证及 Node 文件函数完成内部预览、实际 UTF-8 写入和读回，临时文件与原代码一致，没有额外写入确认。
- 实际工作步骤、记忆和显式引用仍阻断旧资料；其后新普通回答可继续承接，不越过边界。
- 关闭上下文和新会话不携带前一段代码；手动压缩可以处理纯文字工作轮次，来源重查与原生工具配对保持。
- 原聊天、请求日志、预算/费用、停止/恢复、上下文容量、工具工作及结果回传检查一并通过。

追加失败反馈的相关回归 79 项通过（74.88 秒），最终 7 项失败专项检查通过（7.05 秒），覆盖原任务错误码/公开信息、写入预览不落盘、无子执行的准备错误、权限拒绝不续接、生成 Tool 控制器拒绝和未知预览不重试。全轮新增测试场景共 22 项。

固定包的实际验证：

| 检查 | 结果 |
| --- | --- |
| Windows 固定源码完整回归 | 865 通过、3 失败、2 项可选浏览器检查跳过，587.02 秒；三项失败分别为真实 TLS 握手超时、备份磁盘 I/O、Skill 受控测试 `TEST_BUSY` |
| Windows 固定安装包受影响模块与 TLS/备份 | 61 通过、1 项备份 I/O 失败，78.50 秒；随后原包备份 1 项通过、候选包备份单项 1 项通过 |
| 最终固定包上述三项进程测试单独重跑 | 3 通过，19.90 秒；未修改相关生产代码或放松断言；原 Windows I/O 根因仍未定位 |
| Ubuntu/Python 3.12 固定包 | 281 通过，63.19 秒；141 个运行资源逐字匹配，25 项既有依赖与锁定版本一致 |

候选基于 GitHub 基线 `69a3b5b` 的独立快照，只覆盖本轮 4 个运行代码、2 个测试及 6 个文档；没有打入工作区现有 App 路由、CSS 或打包配置。Windows 源码回归中的子进程存在沿用开发环境的限制，因此最终进程重跑显式继承候选安装包目录。原始输出在 Git 忽略的 `var/continuity-release/`，不把单独重跑记录为完整回归一次通过。

最终相关回归命令：

```powershell
./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -W error tests/test_chat_continuity.py tests/test_chat_context.py tests/test_chat_compaction.py tests/test_chat.py tests/test_chat_work.py tests/test_workspace_work.py tests/test_chat_journal.py tests/test_chat_results.py tests/test_chat_auto_results.py --basetemp var/continuity-fix/regression-1 --junitxml var/continuity-fix/regression-1.xml
```

## 环境限制与未验证项

首次命令未创建 pytest 临时目录的父目录，产生 4 项 setup 错误；创建本轮专用目录后纠正。当前默认沙箱使最小 `asyncio.run(asyncio.sleep(0))` 卡住，受控测试已停止；沙箱外最小检查立即正常结束，后续回归经工具权限审查在沙箱外运行。没有修改系统设置、依赖或网络信任。GitHub 连接有两次瞬时失败，后续按固定路径成功读取；没有把失败读取作为参考证据。

固定包构建使用原 setuptools 后端，没有下载或新增依赖。Windows pip 在默认沙箱临时目录被拒绝，停止该尝试后经工具权限审查将 wheel 安装到专用验证目录；没有修改全局 Python 环境。完整回归先受系统默认 Anaconda OpenSSL 影响，改用项目既定 Git OpenSSL；其余三项进程失败保留上表原始结果，最终固定包单独重跑通过，不据此声称瞬时故障根因已解决。

这是替身模型与真实临时文件/受控进程的技术验证，不证明真实模型一定按提示选择工具，也没有进行 Android、当前用户设备重连或线上这两轮请求的重放。未新增真实供应商调用、设备安装/升级、迁移、依赖或用户最终验收。

## 云端发布与 GitHub 交付

固定 wheel SHA-256 `fb011c9b5aa02f32a90d5a3db7de2292df8718bde19fa6beb88e0c0c93b69e9c`，293659 字节、141 个运行资源。候选部署前核对所有未修改运行文件与原线上版本一致，保留现有页面及配置。

2026-10-21 15:21（北京时间）已切换唯一 Core 至 `/opt/nexus/releases/continuity-20261021`；升级前备份 `/var/backups/nexus/before-continuity-20261021T072140Z`，数据库 SHA-256 `22c5a113f207158123a8905768c805f034410040ebd8b005ebadd32c0438941d`。schema020 不变，完整性/外键与全部原表旧行核对通过，配置及模型密钥摘要保持。

42 个任务、20 个会话/34 轮、52 次模型调用、3 个身份和 14 项持久授权保留。1 条历史未知任务/attempt 的状态、期限、阶段、停止标记、派发次数和摘要精确冻结对照；3 笔未知费用不改写。真实 HTTPS API 的旧任务/聊天/账本与静态资源核对通过，服务 active，恢复原未暂停状态；Node/Sandbox 保持原离线，没有启动设备或产生业务执行。

GitHub 沿用 [`codex/m6-chat-work`](https://github.com/jxt159753/nexus/tree/codex/m6-chat-work) 和[草稿 PR #1](https://github.com/jxt159753/nexus/pull/1)，不合并 main。本轮只提交上述 12 个文件；既有前端/App、根 README、AGENTS 和 pyproject 工作区改动保留，未上传私有运维脚本、生产数据库、用户对话正文或凭证。
