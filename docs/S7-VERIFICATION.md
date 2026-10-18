# S7 / P11–P12 本机交付与验收记录

日期：2026-10-18。首轮用户要求“完成S7”，并确认先完成本机检查；随后明确要求帮助配置实际远程接入，结果见文末“实际远程部署补充”。本机验证及真实部署检查不代替全部 Android 主流程和手机网络恢复，也没有替用户确认 M1 验收。阶段状态见 [ROADMAP](ROADMAP.md)，操作见 [部署与恢复](s7-deployment.md)，决定见 [ADR 0007](decisions/0007-s7-acceptance-boundaries.md)。

## 实际改动与范围

- 新增 `tests/test_acceptance.py`：临时 CA 的真实 HTTPS/WSS、严格证书链/主机名拒绝、Cookie/Origin 拒绝、两工具/审批/幂等、停机备份及新目录恢复。
- 扩充既有独立进程故障矩阵：Node 结果落盘后、回传前真实关闭 WebSocket；重连对账仅结算一次，回执数仍为一。
- 增加从首次安装/身份登记到本机演示、直接 TLS、Android 记录、停机一致备份与恢复的说明，明确当前入口不依赖代理头。
- 同步 README、架构、协议、浏览器说明和 ROADMAP；保留此前未提交的工作。没有更改应用源码、消息字段、迁移、工具列表、运行依赖或系统设置；没有提交或推送 GitHub。

## 环境与实际命令

Windows 11 / CPython 3.13.9 x64，Python SSL 为 OpenSSL 3.0.18；测试证书生成器为现有 Git OpenSSL 3.5.4。25 个依赖沿用 requirements.lock，构建工具仍为既有范围约束。所有运行数据在工作区 Git 忽略的 `var/s7-*`，测试使用随机凭证、合成记录和受控进程，证书只在测试客户端显式信任。

新增验收检查：

```powershell
$env:NEXUS_TEST_OPENSSL='C:/Program Files/Git/usr/bin/openssl.exe'
& ./.venv/Scripts/python.exe -B -m pytest tests/test_acceptance.py -q -p no:cacheprovider --tb=short -W error --maxfail=2 --basetemp E:/NEXUS/var/s7-acceptance-3
```

实际 **2 passed in 7.05s**。首轮测试把 pause 响应的临时 `affected` 字段当作持久状态比较，第二轮误用了项目查询 URL；修正为读取 `/api/v1/control` 的持久快照和 `/api/v1/records/projects` 后通过。这些是新增测试的断言问题，没有改动应用以迎合测试。

源码完整回归：

```powershell
$env:NEXUS_TEST_OPENSSL='C:/Program Files/Git/usr/bin/openssl.exe'
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short -W error --maxfail=3 --basetemp E:/NEXUS/var/s7-source-full
```

实际 **505 passed in 105.84s**，无失败或跳过。此后矩阵复查补充真实断开连接的第七种故障，使用独立安装包定向执行，实际 **1 passed, 8 deselected in 3.16s**；没有把定向计数加到此前完整回归数上。

全新环境离线安装与最终检查：

```powershell
New-Item -ItemType Directory -Force var/s7-build-temp | Out-Null
$env:TMP='E:\NEXUS\var\s7-build-temp'
$env:TEMP='E:\NEXUS\var\s7-build-temp'
$env:PIP_NO_INDEX='1'
$env:PIP_DISABLE_PIP_VERSION_CHECK='1'
& ./.venv/Scripts/python.exe -B -m pip wheel . --no-build-isolation --no-deps --wheel-dir E:/NEXUS/var/s7-package
& ./.venv/Scripts/python.exe -B -m venv --without-pip E:/NEXUS/var/s7-check-env
& ./.venv/Scripts/python.exe -B -m pip --python E:/NEXUS/var/s7-check-env/Scripts/python.exe install --no-index --find-links E:/NEXUS/var/s3-wheelhouse -r requirements.lock E:/NEXUS/var/s7-package/nexus-0.1.0-py3-none-any.whl
$env:PYTHONPATH=''
$env:NEXUS_TEST_OPENSSL='C:/Program Files/Git/usr/bin/openssl.exe'
& ./var/s7-check-env/Scripts/python.exe -I -B -c "from pathlib import Path; import nexus,pytest; location=Path(nexus.__file__).resolve(); print('Installed package:',location); assert location.is_relative_to(Path('E:/NEXUS/var/s7-check-env')); raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','-o','pythonpath=','--tb=short','-W','error','--maxfail=3','--basetemp','E:/NEXUS/var/s7-installed-final']))" | Tee-Object -FilePath E:/NEXUS/var/s7-installed-final.log
```

项目及 25 个锁定包在全新环境离线安装成功。首轮独立包完整回归 **505 passed in 108.35s**，该次收集时尚未纳入新增断线故障；定向通过后运行最终完整回归，实际 **506 passed in 110.15s**，无失败或跳过。包从独立 site-packages 导入，CLI 和工具子进程使用该环境，不依赖源码目录的 PYTHONPATH。

最终 wheel SHA-256：`3f73e86829f77e107cfddc059d22a44dfea8cadf860580a5cdedfa552186451e`。67 个 Python/SQL/HTML/CSS/JS 文件与源码逐字节一致，没有遗漏或额外文件。锁文件 SHA-256：`9d0bdf2aa3913a66ffff4338af0972b5843d308ffc755062ddb9e35166c6a326`，没有变更锁定版本。

源码环境与独立环境依赖检查均返回 `No broken requirements found.`；实际命令为 `.venv/Scripts/python.exe -B -m pip check` 和 `.venv/Scripts/python.exe -B -m pip --python E:/NEXUS/var/s7-check-env/Scripts/python.exe check`。`node --check src/nexus/web/app.js` 及 `node tests/web_recovery.cjs var/s7-check-env/Lib/site-packages/nexus/web/app.js` 通过，覆盖失败事件游标屏障和稳定 request_id 重试，没有安装新 Node 包。

## 真实 TLS 与恢复证据

HTTPS/WSS 测试通过 Core CLI、Node CLI 和实际 TLS socket 完成，未模拟 TLS 适配器。localhost 证书 SAN 由临时 CA 签发，Core `local_http=false`；Node 通过 `ca_file` 校验 Core，浏览器角色使用显式 SSLContext。默认不信任该 CA 的握手及用 IP 访问 DNS 证书的握手均抛出 `SSLCertVerificationError`，没有关闭验证。真实 WSS 缺 Cookie 或错误 Origin 返回 403；Cookie 含 Secure、HttpOnly、SameSite=Strict。

认证后系统任务 succeeded 并包含 `node_report_schema_checked`，重复 request_id 返回同任务，改内容复用 ID 返回 409；目录任务审批前没有 attempt，批准后返回一个直接条目并显式 truncated。该证据来自 Python 浏览器协议客户端，不能代替 Android 浏览器或新的桌面可视操作证据；S6 的真实浏览器截图保留在原记录。

恢复演练先完成系统任务并等到 Node ACK，创建待审批目录任务、合成项目及明确同意的 plan 记忆，再持久暂停并停止两个进程。使用 SQLite backup 接口分别备份 Core 和 Node，quick_check 与 foreign_key_check 通过，记录 SHA-256；复制到全新目录后启动恢复 Core。重新登录时历史、审批、事件、记忆同意、项目和 pause 原样保留，Node 显示离线；启动恢复 Node 后获得新 session/boot，能力交集不扩大。批准原有效审批仍不能越过 pause，显式 resume 后目录成功且只有一个 attempt。原库仍只有原系统 attempt，备份摘要未变。

原始 `evidence.json`、process.log、备份及各故障记录保存在 `var/s7-installed-final/`，完整测试输出为 `var/s7-installed-final.log`；不提交数据库、凭证或私钥。[永久脱敏摘要](assets/s7-evidence.json)保存最终计数、包/锁摘要、TLS/恢复任务与进程标识、备份摘要及七种故障结果。最终检查确认 12 个受控测试端口均不再监听。

## 故障与安全矩阵

以下项目对应 ROADMAP 的矩阵；“通过”指本机所列层级的检查，不扩大为手机/远程网络证明。

| ROADMAP 场景 | 本轮覆盖及证据层级 |
| --- | --- |
| 非法 JSON、未知消息/版本、超限 | protocol/contract/client_link/node_link：严格解析、真实 WS/ASGI 拒绝，不创建任务 |
| 未登录、错误 Node 令牌、错角色 | security_http/node_link/client_link：握手/方向拒绝；新增真实 WSS 缺 Cookie 拒绝 |
| Origin、会话失效、凭证撤销 | security/client_link/link_processes：撤销旧连接、轮换与重新认证；新增真实 WSS 错 Origin 403 |
| 任意路径、UNC、越界根、junction | node_tools/node_runtime：真实 Windows junction 根/祖先拒绝及本地策略 |
| 链接、HTML、截断 | node_tools 不跟随链接，真实 TLS 目录数量截断；前端文本渲染沿用 S6 浏览器证据及原回归 |
| 审批参数/节点/策略变化 | security/recovery：绑定不符不能解锁，重试重新授权 |
| 派发落盘后发送前崩溃 | recovery_network/before_send：强制退出受控 Core，重启沿同 attempt 重传 |
| command.accepted 丢失 | recovery_network/lost_receipts：丢弃接收/开始/结果帧，经状态查询取回，回执一条 |
| Node 结果落盘后连接断开 | 新增 after_result_disconnect：实际关闭 WS，重新连接并对账，回执一条、单 attempt、唯一终态 |
| Core 落结果后 ACK 前崩溃 | recovery_network/before_ack：Core 重启、重传去重、Node 最终 acknowledged |
| started 后 Node 崩溃 | recovery_network/node_crash：真实 Node 退出，旧 attempt unknown 且有停止证据才建立第二次 |
| 父进程死亡、无法纳管 | node_runtime/node_process：Windows Job 真 PID 退出，纳管失败不打开执行门 |
| 工具超时与取消 | runtime/recovery_network：受控阻塞工具真实停止；超时预算内最多两次；取消终态有停止确认 |
| 取消/超时/结果竞争、迟到消息 | recovery/harness：事务唯一终态，旧 attempt 不覆盖新结果 |
| Core 重启且 Node 离线 | recovery/link_processes：保留任务、按期限和 unknown 处理；恢复演练先确认离线 |
| pause/cancel_all/重启 | recovery_network/recovery/browser_processes：持久控制及发送屏障；恢复后暂停仍阻止执行 |
| 重试/并发/队列/输出上限 | storage/harness/recovery/node_runtime：2 次执行、3 次派发、64 次对账、有界队列及输出；未知占槽 |
| 数据库/回执无法写入 | storage/security/client_link/node_runtime：事务回滚、停止派发或执行，不绕过证据 |
| 浏览器重连、慢客户端 | browser_processes/client_link 与 web_recovery.cjs：独立进程回放、有界慢发送、失败事件屏障；Android 弱网未测 |
| 模型误调用 | records/router：禁用接口 MODEL_DISABLED、网络保护、路由 rule；真实 model_calls 零行 |
| 一致备份恢复 | 新增 acceptance：停止旧实例、双库一致备份、新目录恢复、身份范围/审批/历史/记忆/暂停及重新认证 |

七类独立进程故障注入仅针对测试自己的进程；不拔网线、不禁用网卡、不停止其他应用。上述连接恢复不代表手机网络切换或物理断网已验收。

范围与复杂度复查：只补充必要的两类验收和一个既有故障参数，复用原进程/权限/WS 辅助函数，不新增应用层抽象。Core/Node 双向 AST 导入边界、文档本地链接、3 个 JSON 示例、2 个 TOML 示例、Python 备份脚本及 S7 PowerShell 代码块语法、围栏闭合、变更文本空白和 `git diff --check` 均通过；仅有既有 LF/CRLF 提示。文档检查脚本先发现尚未创建的 S7 记录链接，创建后复查通过；备份 Python 内嵌脚本改用文件解析验证，避免 PowerShell 内联引号干扰。没有把这些检查工具自身的首轮问题计为应用通过证据。

## 剩余限制与最终验收

- Android NX789J 的主流程和恢复已在下述实际部署中完成；Android/Chrome 具体版本未采集，不推广为其他版本、设备或所有网络环境均已验证。
- 原 S7 公信证书及远程访问绑定 Tailscale IPv4，包含现有网络及 DERP；后续获批的自有云公网 IP 部署另见下节，不推广为多用户或任意网络验收。
- Windows 10、Windows Node 的 Python 3.12、其他组合/负载、长期增长及灾难性磁盘故障未测；后续 Ubuntu/Python 3.12 Core 证据另行记录。
- 不验证恶意 Node 的诚实性；成功验证级别仍为 node_report_schema_checked。模型、Android 原生 Node、第三种工具和副作用能力均未启用。

P11/P12 的技术交付和实际设备检查已完成，等待用户明确最终验收 M1；逐项实测反馈不自动改变此前所有阶段的用户验收状态。后续新增功能仍须单独批准。

## 实际远程部署补充（同日，技术交付完成）

用户随后要求“你都需要什么，你帮我配置”，并确认 Android Tailscale 已连接同一账户。只读核对发现 Windows Tailscale 已运行，MagicDNS/HTTPS 证书功能已开启，Android NX789J 在线。原测试数据保留，新建 `var/remote` 操作实例，不采用测试凭证、不覆盖任何旧库。

- 使用现有 Tailscale CLI 为本机已启用的完整 ts.net 域名申请 Let's Encrypt 证书；私钥只写本地目录，未关闭验证、安装 CA 或修改防火墙/全局系统设置。证书有效期至 2027-01-16 11:50:31（Asia/Shanghai），需提前续期并受控重启加载。
- Core 仅绑定 Tailscale IPv4 的 8443，Node 主动连接同域名 WSS。使用全新 owner/Node UUID 和各自独立的高熵凭证；Core 保存摘要，原始凭证按当前 Windows 用户 DPAPI 加密，证书/凭证目录仅当前用户及 SYSTEM 可读写。Node 原始令牌仅注入进程环境。
- 只允许 system.info@1 与逐任务审批的 filesystem.list@1；workspace 仅映射 `var/remote/workspace`，合成 demo.txt，未扩大到整个仓库或用户个人目录。
- `var/remote/Start-Nexus.ps1` 以隐藏窗口启动独立进程，记录 PID/创建时间防止重复启动；没有安装系统服务、自启或定时任务。登录复制脚本仅写本机剪贴板，不回显或向聊天发送令牌；停止脚本按 PID/创建时间核对本部署，再停止其进程树，调用前应先暂停并确认活动任务结束。
- 实际执行 `var/remote/Verify-Nexus.ps1`：默认公有 CA/主机名验证通过，HTTPS 页面与 owner 会话、真实 WSS、Node 在线和能力交集、系统信息 succeeded、目录审批前无 attempt/批准后 demo.txt 结果均通过；model_calls 为零。原始脱敏结果保存在 `var/remote/deployment-verification.json`。
- 操作实例的配置、数据库、私钥及 DPAPI 文件均通过 `git check-ignore`；脚本语法和日志凭证泄漏检查通过。Tailscale 到手机的 ping 有响应，使用 DERP 转发；未为追求直连改网络配置。
- 用户实际在 Android Chrome 访问部署地址，并回复“能看到登录页，没有证书警告”。这确认手机端页面可达及证书信任，不代表已经登录、建立应用 WSS 或完成工具任务；这些检查继续进行。

随后用户回复“已登录，两个任务均成功”。服务端核对排除自动检查的两条任务后，确认另有手机系统任务 `59a51a21-aab2-4ef6-b79c-865c5878a753` 与目录任务 `22e666a4-1a1c-4c2f-8a65-5cd2bb27ec90`，两者均 succeeded、各有有效 attempt 与工具证据。确定所有任务终结后，仅按已保存 PID/创建时间停止并重启本实例 Core，保留 Node。重启前后四条完整任务详情一致；Node boot 不变、会话更新且在线，模型调用仍为零。现场快照在 `var/remote/before-restart.json` 和 `after-restart.json`。

用户再回复“重启历史、网络切换和新任务都正常”。首轮服务端核对时仍只有四条任务，因此没有直接将新增执行记为已验证；请用户再次提交后，用户提供 ID 前缀 `5a652919`，最终匹配完整新任务 `5a652919-c3ed-4fe4-99f8-00d37d8c75aa`，状态 succeeded，验证级别 node_report_schema_checked，单一有效 attempt。最终服务端共五条成功任务及五条 rule 路由，model_calls 为零；原四条完整详情仍与重启前相同，Node 在线。现场记录在 `var/remote/after-phone-reconnect.json`，不把用户反馈与服务端对照混为同一种证据。

[真实远程与真机脱敏摘要](assets/s7-remote-evidence.json)归档公有证书指纹/有效期、用户操作确认、三条手机任务、Core/Node 恢复及 rule/零模型记录，不包含登录令牌、私钥、数据库或真实文件内容。手机网络切换由用户操作及反馈，新任务 ID 又与服务端匹配；没有声称工具独立控制或抓包观察了手机网络。Android 与 Chrome 具体版本未采集，作为环境限制保留。

部署地址、身份、配置、文件位置和日常命令见仅本地保存的 `var/remote/README.md`、`deployment.json`。未将部署私钥/凭证/数据库写入版本文档；前述 506 项安装包回归仍为代码验证证据，本轮未改应用源码或依赖，没有为配置变更重复全套测试。

## 自有云 IP 直连迁移与 Android 补验

用户在检查后明确批准自有服务器公网 IP 直连、正式证书、自动续期和迁移，见 [ADR 0008](decisions/0008-self-hosted-core-ip-tls.md)和 [脱敏证据](assets/cloud-ip-migration-evidence.json)。Ubuntu 24.04.2 / Python 3.12.3 上相同锁定依赖/wheel 的 pip check、上传哈希、67 个源码/资源文件一致；非登录用户下 16 个已有 Core/协议/权限模块 `409 passed in 26.32s`，无跳过。

旧电脑 Tailscale 已关闭、旧域名失效，没有重新启用。停止已核对 PID/创建时间的进程，通过持锁离线认证与控制仓储暂停，确认终态和 execution_stopped，持双锁生成一致备份。Core 备份经哈希/完整性核验恢复至云端空目录，原身份/范围/五条历史延续；Node 回执、原始令牌及 DPAPI 留在 Windows，原数据与受限备份保留。

正式 IP 证书默认 CA/SAN 和非 root 读取通过。云端唯一 Core 由 nexus/systemd 管理，直接 TLS 443、自启 enabled；Windows 启动器根据迁移标记只启 Node、公网 WSS。新两任务后七条详情相同；正式部署 hook 使 Core PID 8607→15750，七条详情仍相同且 Node 重连。八小时续期 timer/service 已启用，一轮完整 CA dry-run/hook 通过；额外 staging 演练遇 CA ReadTimeout，增加失败六十秒后有限重试、每小时最多两次启动。正式续期 service 实际 Result=success / ExecMainStatus=0。

用户按手机关闭 Tailscale、Chrome 新地址无警告/登录/历史/两任务步骤回复“成功9ac1bb94”。该前缀对应目录任务 `9ac1bb94-c8c5-4e79-98f2-8421248fa9c7`；系统任务为 `70e6d49a-55ae-4d9d-bc25-80de496dfb65`。二者 succeeded、各一次 attempt；最终九条成功/九条 rule/四次目录审批、零模型，原七条详情保持。手机设置与 UI 来自用户报告及任务关联，未声称独立控制手机。

旧本机 Core 已停止、旧 8443 无监听，当前仅 Windows Node；云端 Tailscale 已卸载，电脑 Tailscale 为非运行状态。无安全组、防火墙、系统 CA 或 TLS 放宽操作。整机重启及长期续期/增长未观测，Android/Chrome 版本未采集，Windows Node 未配置登录自启。上述部署反馈不自动改变 M1 及此前阶段最终用户验收。
