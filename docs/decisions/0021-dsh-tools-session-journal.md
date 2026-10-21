# ADR 0021：文件工具、可重建的请求记录和可控的连续工作

日期：2026-10-21。用户在 P45 后明确要求加入 DSH 内置工具，并继续补齐工具执行、任务范围、会话记录、上下文、中途交互、恢复与记忆；浏览器选择 Edge/Chrome，应用选择明确配置的程序。批准与交付状态只记在 [ROADMAP](../ROADMAP.md)。

## 参考及边界

沿用 [ADR 0020](0020-dsh-context-harness.md) 的固定 DSH 提交 `687f282e8625c6b8d11ff58211a90524be2e4317`。重点参考其 [文件读取](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs/src/read.ts)、[精确编辑](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs/src/edit.ts)、[glob](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs-search/src/glob.ts)、[grep](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs-search/src/grep.ts) 和 [Agent Loop](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/agent.ts)。重新适配现有 Python/Core/Node，不复制其包或替换权威数据库。

保留单活 Core、原 Harness、任务/attempt/回执、原账本及 Windows Job。没有任意 shell、模型生成的宿主代码、后台无限执行、自动保存个人记忆或自动扩大权限。本轮不实施 M7/M8，也不升级生产。

## 文件及项目验证

| 模型函数 | Node 工具 | 语义与限制 |
| --- | --- | --- |
| `read` | `filesystem.read_lines@1` | 1-based offset/limit、编号行、下一页位置、完整文件 SHA-256；文件 ≤1 MiB，最多 200 行/8 KiB，长行明确截断 |
| `glob` | `filesystem.glob@1` | workspace 内相对目录及路径模式，支持 `**`、`*`、`?`、字符类；最多 100 项、2000 条目、8 层 |
| `grep` | `filesystem.grep@1` | UTF-8 字面文本及可选忽略大小写；文件/行号/有限预览；每文件 ≤128 KiB、总读取 ≤4 MiB、最多 100 匹配；二进制/不可读/越界会省略并标记 |
| `edit` | `filesystem.edit_preview/edit@1` | old/new 字符串与读取时摘要；默认唯一匹配，显式 replace_all；原文件和结果 ≤12 KiB；成功预览绑定、旧内容比较、原子替换和读回 |

旧 `read_file/find_files/write_file` 保持兼容；写入及网页工具不重复实现。遍历不跟随链接、重解析点、硬链接及 VCS 目录，参数/结果由 Core 与 Node 分别严格校验。grep 首批不用任意正则或新 rg 依赖，不宣称与 DSH 完全等价。edit 与 write/command 一样禁止新 attempt 自动重试。

项目验证复用管理员固定程序、固定 argv 和程序摘要的 `command.run`。工作步骤将非零退出码记录为失败，同时保留 stdout/stderr；程序运行完成不等于测试或业务成功。仍禁止模型自造命令行。

## 浏览器及应用

用 `interaction.observe/act@1` 统一进入原 Node 执行链。目标在 Node 配置，Core 配置同一 `interaction_digests`；握手、授权摘要和每次执行都检查配置摘要。模型只选 target_id/action_id、观察摘要及可选输入值，不能提交路径、选择器、JS、PowerShell 或程序参数。

- Edge/Chrome：连接专用配置/profile 的 `127.0.0.1` CDP 端口，精确 origin 列表，恰好一个匹配页面。仅固定 URL 导航、固定选择器的唯一元素点击/填写；拒绝密码及上传控件。观察包含有限正文与配置控件状态。使用固定内部 JS，不暴露 evaluate 接口。CDP 依据 [Runtime](https://chromedevtools.github.io/devtools-protocol/tot/Runtime/) 和 [Page](https://chromedevtools.github.io/devtools-protocol/tot/Page/) 官方协议。
- Windows 程序：只连接恰好一个已运行的、路径和可执行文件摘要匹配的程序；固定 AutomationId、focus/invoke/set_value。使用随包固定脚本及 [UI Automation](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement?view=windowsdesktop-9.0)，没有通用命令入口。拒绝密码、外进程或不唯一控件，不自动启动/关闭用户应用。
- 操作前比较观察摘要，变化则拒绝并要求重新观察。摘要只覆盖返回的有限界面，不是整个页面/进程的事务锁；其他软件仍可能并发改变界面。返回证明是配置动作返回及随后观察，不宣称业务目标完成。
- 会话 standard 授权允许所列固定目标动作；只有默认文件范围或 read 授权时，首次操作一个目标须明确答复，批准仅对本任务、该目标及当前配置生效。后续相同目标范围内连续执行，配置/策略变化失效。
- act 无新 attempt 自动重试。停止关闭模型/网页交换、取消关联任务并停止 Node 工作进程树；已发出的页面导航、点击或外部程序业务效果不能回滚，也不能据此声称它们未发生。origin 限制不是浏览器网络沙箱，固定命令不是操作系统沙箱。

## 请求日志及上下文

迁移 019 增加 `chat_exchanges`：每次聊天模型及 Search/Fetch 请求在原费用预留事务中先保存完整 UTF-8 body 和 SHA-256，再发送同一序列化结果；不保存认证头或密钥。每次交换单独保存解析回复（含原生调用）、有限流式文本、结束/失败/停止/中断事实；原请求列由触发器保持不可改写。旧版本没有保存的逐次请求无法补造。

完整工具结果仍在任务、步骤及 Node 回执；模型投影与事实分开。查询日志要求 owner 认证。日志总容量 256 MiB，单次预留空间有界；容量不足先拒绝新交换。请求快照支持重建当时的请求体，不是自动重发按钮。

上下文使用当前输入、当前工具事实/状态、明确选择的记忆/历史资料、最近最多 10 轮普通完整问答，以及更早最多 30 轮的确定性摘录。摘录含 turn_id、说明不完整，原文保留；不为摘要额外调用模型。先前含记忆、工作结果或显式资料引用的轮次不自动混入普通历史。容量按 `chat_input_bytes`（8–32 KiB 完整请求）选择；仍保留 token、输出和费用限制，字节不冒充精确 token。

跨会话引用只接受相同 owner、相同项目（含双方无项目）的明确 1–3 个完成轮次；先预览正文/来源/摘要，再逐轮同意外发，启动前再次校验。最多 8 KiB；检查源记忆撤销/过期，不递归引用含历史引用的轮次。引用不是保存长期记忆；长期记忆仍需单独明确同意、来源、类型、验证状态和有效期。

## 纠正、排队、停止与恢复

- 中途纠正单独保存 request_id/text，逐轮最多 8 条，明确外发同意；不改变授权或原生参数。已派发操作先收集结果，尚未派发的旧调用返回明确 not_started；补齐 tool_call 配对后，纠正在下一模型边界生效。纯聊天追问使用后续队列。
- 同会话最多 5 条等待消息，逐条保留用户原输入、同意及发送时上下文；不自动追加排队期间产生的数据。只有前序工作/恢复 hold/真实停止收敛后才执行，不抢占前序工具结果。仍受原五分钟期限、预算及撤销检查。
- 停止记录事实、取消真实交换与关联执行，不只改变等待状态；结果未知保持 unknown。队列中的单条消息可以取消。
- 重启时，正在发送的付费请求标记中断/未知，不自动重发。已生成工具调用但尚未完成的工作进入 recovery hold；原 Harness 继续核对既有 attempt/回执，hold 阻止新派发。
- 显式“核对后继续”只在原范围/期限仍有效、原请求费用已知、已派发尝试结束且结果已知时解除 hold，复用原步骤及结果。未知执行、未结束的动作或失效同意继续阻止恢复；不会靠新 task/attempt 绕过。复杂扩展仍依赖原 v2 对账，未核实的执行不放行。

每轮仍最多 4 个原生动作/5 次模型交换、20 个底层操作，共用 USD 0.05、每日 USD 1/100 次 API；这次增强连续推进、纠正与排队，不取消资源边界。
