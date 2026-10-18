# ADR 0008：自有云端单活 Core 与 IP 直连 TLS

日期：2026-10-18。状态：用户明确批准并实施；验收事实见 [ROADMAP](../ROADMAP.md) 和 [部署补充](../s7-deployment.md#云服务器与公网-ip-证书补充检查)。

用户要求取消 Tailscale 依赖，没有域名，先检查公网 IP 证书；在系统、端口、staging 签发和 Linux Core 检查后，明确确认“按 IP 直连方案部署并迁移”。该确认包含将原私有网络入口开放至用户自有云服务器公网的安全边界变化。

采用 Ubuntu 24.04 上唯一 Core，Windows 保留原独立 Node。Core 由非登录用户和 systemd 管理，单 worker，以 Uvicorn 直接终止 HTTPS/WSS，不引入反向代理的明文上游或代理头模式。应用身份认证、Origin/CSRF、授权交集、目录逐任务审批和零模型边界保持不变。

使用 Let's Encrypt 正式 IP 证书和隔离 Certbot。IP 证书有效期约 6 天，定时器每 8 小时检查，失败在 60 秒后有限重试，每小时最多两次启动。成功续期 hook 校验公信链、IP、到期及密钥匹配，原子切换证书对后受控重启 Core。公网 80 用于 ACME standalone，应用只走 443。客户端不经 VPN 或应用中转服务；公开 CA 仍参与签发和续期。[CA 官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

迁移必须停止旧 Core/Node，取得双库一致备份并核对哈希、完整性和暂停状态，再恢复到云端空目录。原身份及数据延续，Core 只保存凭证摘要，原始 owner/Node 令牌及 DPAPI 文件留在 Windows。先暂停启动新 Core、让原 Node 重新认证并核对旧历史，再恢复派发。Windows 启动器识别迁移标记，只启动 Node，旧 Core 数据和备份保留；回迁也须移动最新权威数据，禁止双活。

不修改云安全组、防火墙、系统 CA 或 TLS 校验。服务器和设备版本、长期续期、整机重启等验证边界按实际证据记录；enabled、单次演练或模型零调用均不等同于长期可靠性或 M1 最终用户验收。
