# S7 部署、备份恢复与设备验收

阶段和实际结果见 [ROADMAP](ROADMAP.md) 与 [S7 验证记录](S7-VERIFICATION.md)。本文说明现有 M1 的操作流程，不启用模型或新增工具。S7 首轮先完成本机检查，随后按用户要求完成实际 Tailscale 直连、公信证书及 Android 主流程/恢复；本机隔离 CA 测试与真实设备证据分别记录。实际操作实例的地址、配置和日常命令仅保存在 Git 忽略的 `var/remote/README.md`。

## 首次安装和本机演示

使用 Windows、Python 3.12+，在仓库根目录操作。实际验证环境是 Windows 11 / Python 3.13.9，其他版本尚未实测。已有可用 `.venv` 时无需重复安装：

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements.lock
& ./.venv/Scripts/python.exe -m pip install 'setuptools>=75,<81'
& ./.venv/Scripts/python.exe -m pip install --no-build-isolation --no-deps .
```

25 个运行/开发依赖按锁文件安装；构建工具仍是范围约束，没有声称完全锁定。离线安装可使用已准备的 wheelhouse 和 `--no-index --find-links`，不因网络失败修改系统配置。

1. 准备独立 Core、Node 和合成测试资源目录：

   ```powershell
   New-Item -ItemType Directory -Force E:/NEXUS/var/core,E:/NEXUS/var/node,E:/NEXUS/var/workspace | Out-Null
   Set-Content -LiteralPath E:/NEXUS/var/workspace/demo.txt -Value 'Synthetic demo; directory listing must not return contents.'
   ```

2. 复制 [Core 示例](../config/core.example.toml) 和 [Node 示例](../config/windows-node.example.toml) 到 Git 忽略的 `var/core.local.toml`、`var/node.local.toml`。Node 的 `roots.workspace` 改为 `E:\NEXUS\var\workspace`，避免用整个仓库做演示根。首次只读测试不需要真实个人文件。保留示例 UUID 或同时替换双方配置及下列管理命令中的 UUID。

3. 使用安全随机源分别产生两个独立令牌（至少 32 随机字节，编码为 43–128 个 URL-safe 字符），存入用户自己的安全存储。令牌不写入 TOML、命令参数或日志。以下交互只读取已有令牌，不回显：

   ```powershell
   $env:NEXUS_DATA_DIR='E:\NEXUS\var\core'
   $env:NEXUS_ENROLL_TOKEN=[System.Net.NetworkCredential]::new('',(Read-Host 'Owner enrollment token' -AsSecureString)).Password
   & ./.venv/Scripts/python.exe -B -m nexus.core.security register-owner 33552837-d424-4d3e-a8e3-cda3d3b54c59
   Remove-Item Env:NEXUS_ENROLL_TOKEN
   $env:NEXUS_ENROLL_TOKEN=[System.Net.NetworkCredential]::new('',(Read-Host 'Independent Node enrollment token' -AsSecureString)).Password
   & ./.venv/Scripts/python.exe -B -m nexus.core.security register-node 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d
   Remove-Item Env:NEXUS_ENROLL_TOKEN
   & ./.venv/Scripts/python.exe -B -m nexus.core.security grant 33552837-d424-4d3e-a8e3-cda3d3b54c59 --tool system.info
   & ./.venv/Scripts/python.exe -B -m nexus.core.security grant 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d --tool system.info
   & ./.venv/Scripts/python.exe -B -m nexus.core.security grant 33552837-d424-4d3e-a8e3-cda3d3b54c59 --tool filesystem.list --root-id workspace
   & ./.venv/Scripts/python.exe -B -m nexus.core.security grant 6909ccbe-2f7c-42ec-ab97-9fe1b03c425d --tool filesystem.list --root-id workspace
   Remove-Item Env:NEXUS_DATA_DIR
   ```

   逐条确认退出码为零；失败时停止并检查配置。重新演示已有身份时不要重复 register 或清库，先按 [身份管理](storage-security.md) 检查是否需要明确的 rotate。离线管理操作在 Core 停止时进行。

4. 两个终端分别启动：

   ```powershell
   & ./.venv/Scripts/python.exe -B -m nexus.core --config var/core.local.toml
   ```

   ```powershell
   $env:NEXUS_NODE_TOKEN=[System.Net.NetworkCredential]::new('',(Read-Host 'Node token' -AsSecureString)).Password
   try {
       & ./.venv/Scripts/python.exe -B -m nexus.node --config var/node.local.toml
   } finally {
       Remove-Item Env:NEXUS_NODE_TOKEN
   }
   ```

5. 打开 `http://127.0.0.1:8000/`，以 owner 令牌登录。确认 Node 在线、仅有两个工具及 workspace。提交系统信息，检查 `succeeded` 和 `node_report_schema_checked`；列目录先停在 `awaiting_approval`，批准后显示直接条目和截断标记。再验证暂停、取消、恢复及刷新后的历史。明确勾选同意才能保存合成记忆。

此 HTTP/WS 模式只能在本机访问；手机不能使用 localhost 指向 Windows。模型 API Key 不需要配置，未知意图不会调用外部模型。

## 用户准备好远程条件后配置 HTTPS/WSS

本版本采用 Uvicorn 直接终止 TLS，`proxy_headers=False`；不依赖转发头判断安全传输。不要把远程服务改成明文 HTTP 来适配反向代理。部署层 TLS 终止属于目标架构选项，但当前入口没有交付该模式。

需要用户提供已配置好的网络路径，以及 Android 浏览器实际信任、SAN 匹配访问主机名且未过期的完整证书链和对应私钥。可使用已有 LAN/Tailscale 路径；它们不代替应用认证。本文不自动申请证书、安装 CA、开放防火墙或更改系统信任。私钥留在本地受限目录，不能进入仓库。

在本机配置副本中调整以下字段，保留正确身份与能力表。这里的主机名仅作占位符，必须换成真实可解析且匹配证书的名称：

```toml
# var/core.remote.toml 的顶层字段
data_dir = 'E:\NEXUS\var\core'
owner_id = '33552837-d424-4d3e-a8e3-cda3d3b54c59'
host = '192.168.1.20' # 替换为用户明确选择的 Windows 接口地址
port = 8443
local_http = false
origins = ['https://nexus.example.invalid:8443']
tls_cert = 'E:\NEXUS\var\tls\fullchain.pem'
tls_key = 'E:\NEXUS\var\tls\private-key.pem'

[nodes.'6909ccbe-2f7c-42ec-ab97-9fe1b03c425d']
tools = ['system.info@1', 'filesystem.list@1']
roots = ['workspace']
resource_version = 1
```

```toml
# var/node.remote.toml
node_id = '6909ccbe-2f7c-42ec-ab97-9fe1b03c425d'
data_dir = 'E:\NEXUS\var\node'
core_url = 'wss://nexus.example.invalid:8443/ws/v1/node'
local_http = false
allowed_tools = ['system.info@1', 'filesystem.list@1']
# 私有 CA 时显式设置 ca_file；公开受信链通常无需此项。
# ca_file = 'E:\NEXUS\var\tls\ca.pem'
[roots]
workspace = 'E:\NEXUS\var\workspace'
```

停止旧进程后按上述启动方式指定 remote 配置。清除与目标不符的 `NEXUS_HOST/PORT/DATA_DIR/CORE_URL/NODE_DATA_DIR` 环境覆盖。Origin 必须含实际 scheme、主机和非默认端口，不含路径；浏览器 WSS 由页面同源生成。Node 保持证书链与主机名验证，不能用跳过浏览器证书警告、`verify=False` 或关闭主机名检查完成验收。

Android 实测时依次记录：设备型号、Android/浏览器版本、真实地址、证书信任结果、两个进程 PID、登录与 WSS、系统任务 ID/结果/验证级别、目录审批/结果、刷新及 Core 重启历史、手机网络断开/恢复后的同步。截图和记录不能包含令牌、Cookie、CSRF 或私钥。最终设备测试覆盖 [ROADMAP 主场景及矩阵](ROADMAP.md)，不能用桌面移动视口、User-Agent 或本机 CA 通过结果替代。

## 云服务器与公网 IP 证书补充检查

2026-10-18，用户购买 Ubuntu 24.04 云服务器，并明确取消云端 Tailscale，先检查无域名的 IP 证书方案。已完成密钥 SSH 登录、系统核对和公网 80/443 固定响应测试；云端 Tailscale 已停止、卸载并清理配置，未登录账户。现有 Windows Core/Node 和数据未切换，具体地址、密钥路径及过程日志只保存在 Git 忽略的 `var/cloud/`。

Let's Encrypt 已正式支持公网 IP 证书；Certbot 5.4+ 支持该流程。IP 证书使用 `shortlived` 配置，有效期约 6 天，必须配置并验证自动续期，以及成功续期后的服务证书更新。客户端通信可直接连接自有服务器，CA 负责签发和续期，不承担应用流量中转。官方说明见 [IP 证书与 Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

预检先使用 staging CA；该证书不受浏览器信任，不能替代正式证书验收。`standalone` 验证需要公网 TCP 80 可达，应用 HTTPS/WSS 使用 TCP 443。未修改安全组、防火墙或系统信任。生产部署已获用户后续明确确认，并完成下述 Linux Core 验证、一致备份及单活迁移；没有将端口可达等同生产可用。

预检阶段：Ubuntu 24.04.2 / Python 3.12.3；既有 wheel 与 67 个源码/资源文件一致，上传包及锁文件哈希相符，25 个锁定依赖安装后 pip check 通过。非登录用户运行 16 个已有 Core/协议/权限模块，`409 passed in 26.32s`，无跳过。staging 首次请求超时、重试后成功；候选配置经实际加载器校验，服务和续期脚本静态检查通过。随后用户明确批准公网边界与实施，实际部署见下。

正式部署：用户电脑 Tailscale 已关闭且旧域名失效，没有重新启用。停止准确记录 PID/创建时间的 Core/Node，在持锁时通过既有身份/控制仓储离线认证和暂停，核对终态及真实停止，再取得双库一致备份，检查 SHA-256、quick_check 和外键。只将 Core 备份经 SSH 恢复到云端空目录；原身份摘要/授权及五条历史延续，Node 回执、DPAPI、原始令牌和原数据留在 Windows，受限备份保留。

正式 IP 证书由默认 CA/IP SAN 校验通过；唯一 Core 使用非登录 nexus 用户、systemd、单 worker/直接 TLS 443、自启 enabled、512 MiB 限额。Windows 启动器按迁移标记只启 Node，直接公网 WSS，工具/资源不变。新两工具通过后七条详情一致，部署 hook 实际重启 Core 后仍一致、Node 重连。手机按关闭 Tailscale、Chrome 新地址无证书警告/登录/历史/两任务步骤确认成功，目录 9ac1bb94… 与系统 70e6d49a… 均 succeeded/各一次执行。最终九条成功、九条 rule、零模型。

八小时续期 timer 已启用，最多随机延迟十分钟，补执行错过检查；失败六十秒后有限重试，每小时最多两次启动。hook 校验链/IP/到期/密钥匹配，原子切换证书对并重启活动 Core。一轮完整 CA dry-run/hook 通过，正式续期 service 实际 Result=success；额外 staging 演练曾遇 CA ReadTimeout，已如实记录。整机重启和长期续期/增长未观测，配置与演练不代表长期保证。

操作证据和命令在 Git 忽略的 `var/cloud/`（README、preflight、core-tests、server-state、迁移/手机快照），公开 [脱敏摘要](assets/cloud-ip-migration-evidence.json)不含真实地址、私钥或凭证；决策见 [ADR 0008](decisions/0008-self-hosted-core-ip-tls.md)。旧 S7 私有网络证据和迁移前同名记录保留，不与本轮混用。没有修改安全组、防火墙、系统 CA 或 TLS 校验。

## 停机一致备份与恢复

Core 是单活权威。备份不产生新的可同时运行的 Core；跨主机也必须停止旧实例。

1. 页面暂停新派发，等待活动工具完成，或取消并确认真实停止。保留 unknown 时先对账，不假定暂停已经停止工具。停止自己启动的 Node 和 Core，确认两个进程已退出；不要按进程名称终止其他程序。
2. 保存匹配版本的 Core/Node 配置、包/锁文件版本及资源映射；原始身份令牌仍由安全存储单独管理。Core 与 Node 回执都需备份，不能用旧 Node 回执配更新的 Core 历史。
3. 使用 SQLite backup 接口生成新文件，拒绝覆盖目标；检查一致性和 SHA-256。下面的路径对应本指南；目录名唯一，复制前再次确认它们是目标测试实例的数据：

   ```powershell
   $backupDir=Join-Path 'E:\NEXUS\var' ('backup-'+[guid]::NewGuid().ToString('N'))
   New-Item -ItemType Directory -Path $backupDir | Out-Null
   @'
   import hashlib, json, sqlite3, sys
   from contextlib import closing
   from pathlib import Path
   target = Path(sys.argv[1]).resolve(strict=True)
   pairs = [(Path('E:/NEXUS/var/core/core.sqlite3'), target/'core.sqlite3'),
            (Path('E:/NEXUS/var/node/receipts.sqlite3'), target/'receipts.sqlite3')]
   hashes = {}
   for source, dest in pairs:
       source = source.resolve(strict=True)
       with dest.open('xb'):
           pass
       with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as src:
           with closing(sqlite3.connect(dest)) as out:
               src.backup(out)
               assert out.execute('PRAGMA quick_check').fetchall() == [('ok',)]
               assert out.execute('PRAGMA foreign_key_check').fetchall() == []
       hashes[dest.name] = hashlib.sha256(dest.read_bytes()).hexdigest()
   with (target/'sha256.json').open('x', encoding='utf-8') as manifest:
       json.dump(hashes, manifest, indent=2)
   print('Verified two SQLite backups; manifest saved.')
   '@ | & ./.venv/Scripts/python.exe -I -B - $backupDir
   ```

   非零退出表示备份失败，不使用残留文件。不要在运行中只复制主 `.sqlite3` 文件而丢掉 WAL。迁移生成的 before-vN 文件只包含当时版本和状态，不代表当前完整恢复点。

4. 在**全新且为空**的 `var/restored-core`、`var/restored-node` 中恢复两个备份，复制前按 sha256.json 校验；保留原数据和备份。不要覆盖现有数据库或将旧 WAL/SHM 混入新目录。配置副本只改两个 data_dir，保持身份、工具版本、资源映射与 resource_version 一致；只有 Node 的实际资源映射改变时才更新范围版本并重新授权。
5. 确保旧 Core/Node 已停止后启动恢复的 Core。重新登录，检查任务、结果、审批、事件、项目/记忆及 pause 状态。此时 Node 必须显示离线。再启动恢复的 Node，确认新 session/boot、原能力交集和回执；检查未知执行再考虑恢复派发。未过期且所有绑定仍有效的审批可以继续使用，过期审批须重新评估。最后明确恢复派发。

恢复只重建备份时刻的状态。备份之后的授权撤销、凭证轮换或任务完成不在旧副本中：真实恢复时必须核对这些变化，必要时离线轮换凭证使旧会话失效，再开放访问。不能通过恢复旧库悄悄重新启用已撤销权限。

本轮自动演练恢复到新目录，检查历史/身份范围/有效审批/显式同意/暂停、重新认证及零模型记录；不是原地破坏性恢复，也没有迁移用户真实数据。命令和结果见 [S7 证据](S7-VERIFICATION.md)。
