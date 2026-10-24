# Windows-MCP 会话安装、调用与 Skill 验证

2026-10-24，技术交付完成，待用户验收。批准范围与当前状态见 [P83](ROADMAP.md#p83-windows-mcp)，机器证据见 [脱敏记录](assets/windows-mcp-evidence.json)，设计见 [ADR 0042](decisions/0042-conversation-capability-readiness.md)。原始 API/模型交换、构建、依赖快照与日志保留在本地忽略目录 `var/mcp-integration-release/`，不提交生产凭证或数据库。

## 结果

正式会话 API、真实 `deepseek-flash`、Linux Core 和已安装 Windows Node 已走通：安装 Windows-MCP → 登记并发现 → 实际调用 → 创建 Skill 候选及契约测试 → 用户批准确切版本 → 原会话执行 → 新会话复用。过程包含分轮排错和修正，不能描述成首次请求一次无干预完成。

| 环节 | 实际证据 |
| --- | --- |
| 安装 | 模型调用宿主 Shell，通过已有 uv 创建独立 Python3.14.3 环境并安装 `windows-mcp==0.8.8`；后台 job `8029bcf4135041b0b3d3065ccc3011b9`，退出0、真实停止，日志三个阶段退出均为0 |
| 登记 | `windows-desktop` revision2 active，目录摘要 `bc25e9d2e246b6b27ba20c5d910f08e03f48f9a93f7762593dbcd3abc4f881d8`；10个工具契约均可接受 |
| 调用 | 通过 `builtin.mcp/mcp_call_tool` 真实执行 Process list；`isError=false`、`mcp_tool_response_checked`，Core任务为 `node_report_schema_checked` |
| 候选 | 模型创建 `user.windows_mcp_health r1`，版本 `851cd461-95c3-4fe6-8688-3abdd6523942`；契约测试 passed，明确 `native_execution=not_run` |
| 批准 | 用户批准摘要 `c691a06d1757f8d930e50219fca017ef7919a3c8cd08c9df16d7f3edcb4cc506`；批准ID `ad7775e3-cc32-479b-9198-6a91680f3a11`，绑定Windows设备、测试和策略33 |
| 原会话执行 | run `f19e25eb-00dd-4f0d-8ba1-0925bacaa34b` / task `72ab47d6-8199-4384-aebe-944164908c24`，`skill_steps_verified`；一次派发、执行已停止 |
| 新会话复用 | turn `329fb4c7-3f84-4c09-b637-c651973a6436`，模型自动选择现有版本；run `a2d01fec-0f44-400b-9a5d-a680e50e75f9` / task `a6b502fa-e887-4d48-b405-e2110c735641`，`skill_steps_verified`，没有再次安装/登记/创建 |

Skill 无自由输入，固定一步 Process `mode=list,name=nexus-node,limit=2`。最后调用读到 `nexus-node.exe` PID61796、CPU0.0%、内存72.9MB，另返回相似名称 `node.exe` PID40676；后者不能被推断为 Nexus 子进程。这证明进程存在及当次资源采样，不是完整业务健康检查。

保留该 Skill 启用，批准有效至 **2026-10-31 11:47:43 北京时间**；之后继续启用也不绕过批准有效期。测试期间仅五个专用会话使用 owner 设置的 full，结束后全部恢复继承 normal，全局 normal revision1保持。版本批准有效时跨会话不重复申请同一版本批准；normal 的具体执行确认与 full 的范围预授权仍按现行策略处理。

P77 此前已用真实模型验证“用户直接要求 / 模型发现复用缺口”分别创建插件与 Skill 的四条路径，见[原验证](CHAT-CREATION-VERIFICATION.md)。本次新增的是 Windows-MCP 安装到复用链路；不把本次明确要求创建检查 Skill 的提示说成无提示自主发现，也不重复安装同一第三方包来制造第二份证据。

## 修复与失败记录

- 原生 Skill/MCP 后端原先只存在本地，线上 Core/Node、托管开关和完整信任摘要未配套；旧提示还暗示没有 Shell/安装能力。此次配套部署，插件检查暴露真实可用性和具体阻塞，Node 原生启动拒绝执行器配置不一致。
- 最初32KiB请求无法容纳目录与安装指导；自动记忆提示未计入部分工作上下文准入。统一准入选项，设置有界16动作/17交换、64KiB请求，迁移034保留历史与外键；默认配置仍4动作、32KiB。
- 第一次信任配置误用插件子策略摘要，实际需要完整Node摘要；请求被派发前拒绝，没有执行受阻命令。修正部署值和可用性诊断，full没有绕过该检查。
- 后台安装已完成，但模型把 Node job_id 用在 Core task 查询；对账实际job后确认完成，没有重复安装。指导现区分两个接口及 started/finished。
- Linux Core 与 Windows Node 的 MCP executable 路径序列化不同导致命令摘要不一致；r1未开始，修复规范化后r2激活。FastMCP输出包装注解原先被当作非法schema，修复只忽略该布尔注解，继续验证结果字段。
- 无 active 的 `mcp_list(server)` 原先直接中止工作，现返回最新修订和真实状态供判断。一次部署后探测早于Node上线被权限阻止；验证脚本现等待实际Node配置就绪再发付费请求。
- Skill步骤缺少 `tool_version` 时，非区分联合校验给出大量无关错误。改为按intent区分、保留嵌套字段路径及确定性提示，未放宽Skill契约。
- 批准后的首个执行入口仍按旧平面目录查版本，误报 `VERSION_NOT_APPROVED`。改为按目标设备查版本；批准未重新发放，另一设备不能复用该版本的可见性和授权。
- 首次跨会话Skill调用遭上游 `USER_CONTROL` 主动让权，底层 `isError=true`，Skill正确失败，但错误详情被丢弃导致模型误猜摘要问题。保留用户优先保护；现在原生失败步骤与其已校验结果原子保存，完整错误按原外发授权送回模型。已有历史记录不改写。随后新会话同一批准版本真实成功，未改Skill内容。
- 首次Core切换受到旧systemd drop-in排序遮盖，升级后的schema034被旧运行时读取失败，造成短时不可用。修复最终生效的ExecStart后恢复；之后每次切换前验证该值。未恢复旧数据库或删历史。前一版本读取最新数据库副本现已通过；没有把该检查宣称为生产回滚演练。

## 发布与回退

以实际发布的P82 wheel `d79ef941…f495c39c7`为基线，只覆盖列明后端/协议资源；保留P82网页可视化、P80跨设备和Android、语音、P81自主基础。并行工作区未整体发布。

- Core：`/opt/nexus/releases/mcp-integration-20261024-0fc31da1`，wheel SHA-256 `0fc31da1cbd93c8e33d4415bb8bbae86698d3b0b8e2a75da6b35baa00f12ea37`。最终257运行资源逐一核对，schema034，唯一PID398439，`paused=false`，7个后台worker运行。
- Windows：已覆盖安装0.1.7，Node SHA-256 `902fd7be48d8dcc95a8efe47a631b5be83669ff5bd6a9a0892d4996756013e6e`，原身份/配置/回执保留，完整plugin_digest为 `16b829dba85e5f042f9d09f20bf4058eec793a6b8c781ef0862172022cbf6a02`。最终实际MCP调用来自该安装版Node。
- [Windows0.1.7安装包](https://core.example.com/static/downloads/NEXUS_0.1.7_x64-setup.exe)：104,075,319字节，SHA-256 `0efd60a96c9e300b65d2f0492a8f7a14e6f4584296eaed12ed49bbf74796f96b`；通过正常TLS完整下载并核对。保留发布版可视化和语音资源。
- 最新切换前备份 `/var/backups/nexus/before-mcp-skill-evidence-20261024T035920Z`；初次升级前备份 `/var/backups/nexus/before-mcp-integration-20261024T030611Z`。每次暂停/确认无活动执行后备份、单活切换并核对历史摘要。最终96任务/91尝试、66会话/109轮、230模型调用；原3条未知任务与6笔未知费用保留，健康degraded仅来自旧未知任务。
- 立即回退目标为已安装的 `mcp-integration-20261024-e3f827b3`，可读取当前schema034数据库。回退前暂停、核对活动任务、备份最新库，切换固定运行版本并恢复派发；不覆盖最新库或倒退账本。独立Windows备份保留在本地验证目录；若回退到不支持托管MCP的旧Node，必须停用相应能力并同步配置/摘要，不能保留“可用”假象。

第三方环境位于workspace的 `.nexus/mcp/windows-mcp-0.8.8`，92个依赖版本快照已留存；uv按需下载的Python在其托管缓存中，未替换系统Python。包版本固定、实际下载wheel已审阅并记录SHA，未把本次解析结果声称为所有未来平台的哈希锁定安装。服务使用stdio，关闭该子进程遥测，无端口、防火墙、TLS信任或开机启动更改。

## 验证范围

测试批次有重叠，不累加为总用例数：

| 批次 | 结果 |
| --- | --- |
| 首轮固定Windows候选 | 77通过、1跳过，56.01秒 |
| 首轮Linux固定候选 | 152通过、10平台跳过，62.11秒 |
| 执行可用性/诊断 Linux | 45通过，23.94秒 |
| MCP协议 Linux / 查询修正Linux | 12通过、8跳过 / 9通过、1跳过；Windows对应managed用例最终10通过 |
| Skill参数反馈 Linux | 43通过，21.87秒；Windows修正夹具后参数专项11通过 |
| 跨设备Skill查找 Windows / Linux | 各21通过，13.42 / 13.62秒 |
| 最终原生失败证据与会话流程 Windows / Linux | 24通过，15.89秒 / 19通过、5平台跳过，10.97秒；跳过项已在Windows通过 |

上下文专项37通过、1个已有失败：`test_chat_context.py::test_capability_inspection_is_bounded_authorized_and_does_not_execute[True]` 仍预期旧 `stopped`，P76自然语言收尾为 `completed`；该已知失败在P77/P79/P81已有记录，本次未改无关预期。中间路径测试夹具以str构造严格Path、参数测试夹具使用错误插件摘要导致断言失败，均修正后定向复跑通过。受限环境异步测试挂起的进程已仅按本次PID停止，未算作通过。

真实模型共12轮、60次调用，费用 **213532 microunits（USD0.213532）**，本轮未知费用0。新会话成功复用为3次调用、USD0.007811。总账本由170增至230，与本轮60次匹配；模型回复不能替代上述Task/Skill/Node证据。

交付复查：7份文档的463个本地链接、88个章节引用有效，代码围栏闭合，证据JSON与示例TOML可解析；差异空白检查通过。线上257资源与固定发布清单一致，已安装Node哈希仍匹配。收尾时32个覆盖资源中30个仍与工作区一致，`core/app.py` 和 `core/storage/tasks.py` 已有并行文件传输追加；逐差异检查确认该追加不在本次固定包中，未覆盖或发布这些并行改动。

本次没有点击、键入、截图或读取其他应用文件正文；10项目录通过契约检查不等于10项桌面行为都已实测。stdio的跨调用内存状态、上游用户让权、工具超时/输出限制仍需按实际任务处理。未实现Core/Node源码自修改、自动发布器或无限期Skill自动批准；已修复的误判有确定性检查和回归用例，但不能保证模型与第三方服务以后绝不失败。
