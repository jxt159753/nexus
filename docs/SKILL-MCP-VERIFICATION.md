# Skill Shell 与受控 MCP 验证

日期：2026-10-23。当前进度与批准边界只在 [P65](ROADMAP.md#p65skill-shell-与受控-mcp) 维护；本文件记录本次技术证据。接口见 [Skill/MCP](skill-mcp.md)。

环境为 Windows 11 build 26200、Python 3.13.9、项目锁定依赖、系统 Windows PowerShell。所有 Core/Node/MCP 服务器、文件和 SQLite 均为任务临时资源；没有连接生产或调用真实模型/网页 API。候选 Skill 由受控夹具构造并通过原测试/批准/启用链，不能据此称真实模型已成功自行生成安装流程。

## 实际检查

| 检查 | 结果与范围 |
| --- | --- |
| 新增专项与原聊天 Skill | 42 通过，46.84秒；真实 stdio/HTTP JSON/SSE 工具发现与调用、中文结果、目录变化先拒绝调用、普通/完全模式、聊天计划/结果预览、非零退出、计划模式、模式变更、幂等及 owner/CSRF/修订冲突 |
| 受影响后端回归 | 177 通过、2 失败，115.32秒；覆盖原 Tool/Skill、聊天工作、插件、权限、存储、安全和新流程。失败均为旧测试硬编码 schema26；代码已升至28。更新断言为实际资源数后，存储完整21通过，7.06秒；该批179个用例均有最终通过证据，非一次完整全绿 |
| 真实三步 Skill | Shell 写入实际服务器脚本 → register 实际初始化/发现 → discover 新目录；测试替身阶段文件不存在，正式运行后实际存在，Core active revision2；已完成发现的历史证据仍可按权限读取 |
| 完成证据边界 | 人工构造错误目录摘要，即使 Node 任务结果结构成功，Core 不激活配置且 Skill 停止，不执行后续 Shell |
| 最终结束证据与回归 | Shell 必须退出码为整数0、状态成功且进程树已停止；缺少/伪造停止证据均拒绝。补充检查暴露快照早于 Job 清理，修正为清理并读完输出后取证；最终 Skill/Shell/PTY/旧协议18通过，24.14秒；此前权限/API/Skill18通过，21.84秒 |
| 独立 Core/Node + 固定 EXE | 两组2通过，22.58秒；分别使用源码 Node 和新 PyInstaller Node，通过真实 HTTP/WS 派发 Shell 安装与 MCP stdio 发现，Core 重启/Node 重连后目录和已完成 Skill 保留；失败更新保留原 active；移除后拒绝读取活动目录；模型调用数0 |
| 原命令兼容 | 缺省 binding 不序列化 null；旧参数 JSON 与规范化摘要完全一致。旧 Skill 仍使用原执行与批准流程 |
| 文档和源码静态检查 | 最终8份文档324个本地文件链接有效、代码围栏闭合；19个Python文件可解析；脱敏JSON、Node示例TOML有效，差异空白检查通过；最终包摘要/字节数与证据一致 |
| 真实旧协议 | 前序19通过，26.38秒，涵盖新 Shell/MCP 与旧 MCP 资源、LSP、原生 Shell/PTY；最终相关批次再次包含旧协议用例 |

最终后端批次命令（PowerShell，在仓库根目录）：

```powershell
.venv/Scripts/python.exe -I -X utf8 -m pytest tests/test_extensions_m4.py tests/test_extensions_http.py tests/test_chat_work.py tests/test_chat_skills.py tests/test_permission_modes.py tests/test_plugin_lifecycle.py tests/test_plugin_http.py tests/test_plugin_protocols.py tests/test_native_plugins.py tests/test_storage.py tests/test_security.py tests/test_security_http.py tests/test_managed_mcp.py tests/test_native_skills.py tests/test_skill_mcp_processes.py -q --tb=short --basetemp var/mcp-skill-20261023/backend-regression
```

固定 EXE 检查额外设置 `NEXUS_TEST_FROZEN_NODE` 为本地验证包绝对路径，再运行 `tests/test_skill_mcp_processes.py`；未设置时该模块的固定 EXE 组合明确 skip。实际运行日志在 Git 忽略的 `var/mcp-skill-20261023/`，脱敏结果见 [JSON 摘要](assets/skill-mcp-evidence.json)。

## 打包与中间问题

PyInstaller 6.22.3 使用现有随包 Node 入口及既有依赖，构建到任务临时目录，增加模块由现有 `--collect-submodules nexus.plugins` 包含；没有修改或覆盖前端二进制/安装包。固定 Node 为 40,003,542 字节，SHA-256 `4d1afd956489ec1f106ca0cfb845c1a92d4828baf37821bd481ba2bbb1aa0fc5`，真实运行证明新 MCP 工作进程与 UTF-8 管道可用。本次不重复宣称整个旧插件集在此 EXE 均已验收。

首次默认 Windows 沙箱临时目录不可写，未进入功能验证；改用获准本地进程与专用仓库临时目录后正常运行。注册冲突曾因 Conflict 继承 ValueError 被错误映射为400，调整异常顺序后409/idempotency复测通过。首次临时 PyInstaller 构建使用相对数据文件路径，因 spec 位于临时目录找不到文件；改绝对路径后构建成功。构建提示 WinPTY 的可选系统 DLL 解析警告，本次 Shell/MCP EXE 实测通过，不据此宣称所有终端平台组合均可用。

严格检查 Shell 停止证据时出现1失败/8通过：退出码0的快照是在 finally 清理之前创建，偶尔携带 execution_stopped=false。修正返回时点后相关18通过，并重建最终 EXE 通过上表两组真实进程验证。重建也暴露测试只终止 PyInstaller 外层启动器，留下自己的临时 Node 子进程；已核对可执行文件、PID及临时配置后清理，测试为固定 Node 增加 Windows Job 覆盖整个进程树。最终只读进程检查确认无该临时 EXE 残留，不终止其他程序。

## 未验证与保留限制

- 未部署本次 Core/Node，未改生产策略/密钥/数据库，未提交或推送 GitHub；已有并行 P64/语音/前端改动保留。
- 未做新版 Linux Core 与 Windows Node 的跨主机/TLS、Android 或前端/原生桥测试；Core/Node 独立进程证据是本地 loopback HTTP/WS。
- 没有从任意真实 GitHub 仓库下载/安装服务器、付费真实模型生成或 OAuth 流程。依赖、凭证、安装耗时及服务器自身约束仍需按具体仓库处理。
- stdio 不保留跨调用进程内状态，Skill 前台 Shell 最多10000ms；发现64工具/20KiB、八页、有限 schema 子集，输出有界。上述边界不能被 Skill 或 MCP 声明提高。
- 技术自检不代替最终用户验收；生产升级还需处理未上线迁移及信任摘要，不能把本地全部并行工作自动打包上线。
