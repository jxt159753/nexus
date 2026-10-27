# 参与 NEXUS

先阅读 [开发规范](AGENTS.md)、[架构](docs/ARCHITECTURE.md)和[路线图](docs/ROADMAP.md)。路线图区分代码交付、部署和用户验收；历史记录中的个人环境不是新安装的配置要求。

## 开发环境

Core 与 Python Node 使用 Python 3.12+。从仓库根目录创建独立环境：

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.lock
./.venv/Scripts/python.exe -m pip install --no-deps -e .
```

Linux 将 Python 路径换为 `.venv/bin/python`。图片、原生插件与语音依赖分别在 `requirements-images.lock`、`requirements-plugins.lock`、`requirements-voice.lock`，按要测试的功能安装。Windows 和 Android 的工具链与构建步骤见各自 [README](apps/windows/README.md)、[README](apps/android/README.md)。

按[部署说明](docs/s7-deployment.md)在本地数据目录生成独立身份和凭证；模型默认关闭。使用自己的配置与环境变量，不复制历史验证环境，不提交凭证、聊天记录、数据库、签名文件或安装包。

## 提交修改

- 围绕一个具体问题修改现有实现，描述触发条件、改动后的行为与验证结果。
- 保持 Core 单活和唯一权威；Node 只执行获准操作，未知结果先对账。
- 网页和 Windows 共享的 UI 模块需要一起更新；保留 Windows 登录、原生桥和设备配置差异。
- 不重写已执行的 SQL 迁移，不通过清空数据库解决兼容问题。
- 新增或替换第三方资源时固定来源与版本，并更新 [第三方声明](THIRD_PARTY_NOTICES.md)。

先运行与修改相关的测试；基础检查示例：

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_settings.py tests/test_contract_boundaries.py tests/test_service_config.py
python scripts/check-public-repo.py
git diff --check
```

浏览器测试需要 Node、Microsoft Edge 和 Playwright Core；部分现有夹具使用 `var/attachment-browser/node_modules/playwright-core`。设备、语音与 Android 测试有独立前提，跳过和未验证项要写明，不调用真实模型或操作个人设备来替代合成测试。

仓库的 [基础 CI](.github/workflows/checks.yml)运行公开内容检查、契约、配置与页面服务测试；它不代替浏览器交互或原生设备验收。Git 历史扫描仍在公开前单独运行。

## 报告问题

普通问题使用 Issue 模板，附操作步骤、期望与实际结果、版本及脱敏日志。安全问题按 [SECURITY.md](SECURITY.md) 私下报告。截图应使用合成内容，遮蔽地址、凭证、个人会话和设备身份。
