# 原生插件 P57–P61 验证记录

记录时点：2026-10-22。任务批准、当前状态和待办统一查 [ROADMAP](ROADMAP.md#p57p61原生插件与权限模式)。本记录说明实际证据，不代表最终用户验收。设计见 [ADR 0028](decisions/0028-native-plugins-permission-modes.md)，协议、DSH 能力映射和限制见 [plugins](plugins.md)，前端接入见 [DSH 工作单](PLUGINS-FRONTEND.md)。脱敏数据见 [plugins-evidence.json](assets/plugins-evidence.json)。

## 固定对象

| 对象 | 固定版本或摘要 |
| --- | --- |
| Core wheel | `nexus-0.1.0-py3-none-any.whl`，SHA-256 `189e7e73a1eae84e2b9b8a65ee3ba1fe0933df9cc2719c8d0b32860c8ff5e75b`，212 个运行资源 |
| Windows Node | `nexus-node.exe`，SHA-256 `6fcddb72e83253aaf6668bac98db828e7001d886d57275b0d4ffa18e02b7b7bf` |
| 数据库 | schema 020→026，新增图片/附件及插件、模式、测试和生命周期表；原迁移 checksum 保留 |
| 原生目录 | 18 个内置插件包、56 个工具声明；按实际 Node 配置缩减可用能力 |
| 宿主依赖 | Python 3.13.9、Windows PowerShell 5、Git Bash/Git；pywinpty 3.1.0、Pillow 13.0.0；Python LSP 固定 pyright 1.1.414 |
| 独立测试 | runsc KVM；镜像 `753aed32…12d16`、runsc `be25aff8…a21a`，完整摘要见脱敏证据 |

Core、Node 和 SDK 源码来自同一固定 wheel；冻结 Node 额外包含 UTF-8 管道设置。打包后的 9 个并行前端文件发生 DSH 修改，保留在工作区，没有覆盖回滚。GitHub 的运行资源从上述 wheel 提取，明确对应线上版本；本轮不发布这些后续页面改动，也不宣称前端联调完成。

## 实际验证

| 层级 | 结果与覆盖范围 |
| --- | --- |
| Windows 完整回归 | 975 通过、2 跳过；覆盖既有任务/预算/聊天/图片附件及新插件、权限、执行与恢复 |
| 最终改动检查 | 最终策略/连接/传输等 40 项通过；固定包相关 14 项通过；不把重复运行相加成独立用例总数 |
| Ubuntu 固定包 | Python 3.12、独立候选 venv，252 通过、8 项 Windows 专用跳过；`pip check` 通过 |
| 真实宿主执行 | Windows 中文 Shell、文件预览/旧内容绑定/读回、Git worktree/LSP/协议进程、后台退出与取消、真实 Edge CDP，以及认证文件上传/下载有对应测试证据 |
| 冻结 Node | 中文 `frozen 中文` 输出正确；文件内容经程序化 SDK 读回为原文；示例自写插件实际返回 sum=10；ConPTY 保留变量状态、返回 pty-17，关闭后真实停止 |
| 独立 runsc | 实际加法输出 sum=5；宿主文件不可见、网络被拒绝、无限循环超时停止。测试使用独立临时目录，没有复用生产执行记录 |
| 模式与生命周期 | normal/full/继承、owner/CSRF、旧授权失效、并发和撤销、测试失败/取消、停用与自动启用竞争、重启后不静默续发付费工作、原回执对账均有回归 |

冻结版首轮发现系统 GBK 编码污染程序化 JSON，已修复 Windows Node 入口；重新构建后上述冻结测试全部通过。Windows 冷备份测试遇到 pager/WAL 恢复的 I/O 错误，在测试备份前显式读取 `quick_check` 后，真实 TLS/离线恢复及最终全套通过。Anaconda 附带 OpenSSL 不可用，测试使用现有 Git OpenSSL；未更改系统 PATH、证书或 TLS 策略。

## 生产切换与真实结果

1. 旧唯一 Core 空闲；先通过认证 owner API 暂停派发，再停止服务。一致备份到 `/var/backups/nexus/before-plugins-20261022T110653Z`，保留数据库、原配置及 systemd 启动信息。
2. 独立候选已先用生产数据库副本验证迁移。正式迁移逐表比较原 43 张表的原字段/行；原 56 任务、54 尝试、29 会话、53 轮、85 次调用保留。身份、密钥、TLS、原预算和旧未知费用不变。仅新增所需 owner/Node 静态授权，旧授权保留为历史，配置不再对新任务开放 `command.run`。
3. Core 切换至 `/opt/nexus/releases/plugins-20261022`；配置语义只改变 `plugins.enabled`、Windows Node 的工具集合与 `plugin_digest`。单活锁、systemd 用户和资源限制沿用。
4. 检测到旧 App 启动同一 Node 身份后，核验并停止对应旧 Node 进程树；备份旧 App 和独立部署的回执库，沿用 App 的最新回执库启动新版冻结 Node。原 App 界面未修改，独立启动器只启动 Node，不另启动 Core。
5. 真实模型生成 `user.text_length@1.0.0`。第一次 WSL 已退出，返回 `qualified_sandbox_unavailable`，版本未启用、源码未在宿主执行；该失败和 USD 0.003193 记录保留。恢复既有 WSL 常驻启动器后，原生成版本在 runsc 实测 `NEXUS→5`、空字符串→0，随后在该会话的 full 有效授权下自动启用。后续真实模型查询版本并派发 Node，返回 `{length:5}`、known/succeeded。后续轮费用上界 USD 0.004726。
6. 原生 Web 插件实际查询 DeepSeek 官方搜索文档，并抓取其[官方页面](https://api-docs.deepseek.com/guides/search/)，搜索和抓取均成功；抓取正文按 8 KiB 截断并保留标记。4 次模型交换与 2 次网页 API 的本地账本上界 USD 0.009072；该值不是供应商精确账单。
7. 两个验证会话均恢复继承并归档，全局保持 normal、revision=1，当前会话覆盖数为 0。再次空闲暂停/重启唯一 Core，节点重新认证连接，插件 passed/approved/enabled 状态保留；重启没有新增模型调用或费用，随后恢复未暂停。

最终快照：57 任务、55 尝试、31 会话、56 轮、96 条 API 调用记录，其中新增 9 次模型调用与 2 次网页 API，总账本上界 USD 0.016991。原 85 条调用逐行未变，4 笔历史未知费用继续占用预留；无活动任务/尝试/轮次/API。数据库 quick_check/FK 检查通过，进程核对仅一个 Core。

## 边界与待验收项

- 插件权限开关、管理/终端/交付物界面及 Windows 原生桥尚待用户控制 DSH 接入。旧 App 的 Node 启动仍会重写旧配置，不能用它代替本次独立执行端。
- 生产未配置 MCP 外部服务器或浏览器目标，相应工具明确 unconfigured；本地真实协议/CDP 测试不等于这些外部环境已开通。
- Stagehand 对应能力采用主模型规划加确定性 CDP，不增加另一个 Stagehand 模型；workflow 是有限节点序列/SDK，依赖子代理的 DSL 不在本轮。
- 自写插件测试镜像当前只支持标准库和 SDK 契约桩；第三方依赖不会自动安装，隔离测试不连接宿主工具。契约用例通过不证明任意业务正确或宿主代码安全。
- 既有 M4 Tool/Skill 保留独立规则，不把旧固定命令 Skill 自动升级为宿主权限。子代理、Agent Team、Ralph、市场及 Android 未实施；未宣称最终用户验收。

## 本机证据位置

原始运行证据保存在被 Git 忽略的 `var/plugins-delivery/`：`full-final.log`、`frozen-runtime-check.log`、`frozen-evidence.json`、`runsc-evidence.json`、`production-proof.json`、`web-proof.json`、`activation.json`、`check-after-restart.json`；最终策略临时资源在 `final-policy-bound/`，通过结果在本次会话工具输出；云端 Linux 日志为 `/root/nexus-plugins-20261022/linux-final.log`。分享/提交只使用脱敏摘要；不上传生产配置、数据库、Cookie、DPAPI 凭证或 SSH 私钥。早期构建候选已被本记录的最终摘要替代，不能用于判断当前部署。
