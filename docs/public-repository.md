# 公开源码的准备与发布

本页说明发布操作；源码准备见 [P109](ROADMAP.md#p109-public-preparation)，历史清理、GitHub 上传及遗留事项见 [P110](ROADMAP.md#p110-git-history)。

## 检查要公开的内容

```powershell
python scripts/check-public-repo.py
python scripts/check-public-repo.py --history
gitleaks git . --log-opts=--all --config .gitleaks.toml --redact=100
git status --short
git diff --check
```

默认检查 Git 跟踪文件和未被忽略的新文件。`--history` 还检查所有本地可达引用中的文件对象；只报告位置与类型，不打印匹配到的秘密。检查涵盖常见凭证、私钥、公开 IP 地址链接、个人目录及不应提交的本地文件；它不是完整密钥扫描器，也不会检查图片内容、远程未获取的分支、PR、Issue 或 Actions 产物。

Gitleaks另行安装；本轮使用8.30.1，默认检测规则保留，`.gitleaks.toml`仅排除指定证据文件里已核对的资源SHA-256字段，不排除源代码、整个文件或一般令牌字段。原始Git备份和脱敏映射只留在私有位置，不能上传到仓库或Release。

不要直接 `git add .`：工作区可能包含不同任务尚未交付的实现。按明确的文件清单审阅后提交，确认新克隆同时包含代码、迁移、锁文件和必要资源。

## 处理历史与展示材料

- 当前文件删除或脱敏后，旧 Git 提交仍保留原内容。正式公开前需决定保留经审阅的历史，还是从审阅后的源码快照建立新的公开仓库；不要未经确认改写共享历史或强推。
- 已执行历史改写后，旧克隆应重新克隆，或依据私有映射仅移植需要的干净改动；不能把旧历史直接合并回来。普通Git推送不能删除GitHub只读PR引用和缓存，需要按[官方清理流程](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)另行处理；分支扫描通过不代表平台缓存已清空。
- 若确有密钥泄漏，先撤销或轮换，再处理历史。仅清除当前文件或加 `.gitignore` 无法撤销已泄露凭证。
- 人格、聊天、设备、语音和部署截图要检查画面中的个人内容。首页展示图使用本地合成夹具；旧验证图片不自动视为可公开素材。
- 公开版文档中的 `core.example.com` 是脱敏占位符，不是可用服务或安装包下载站。旧发布的版本、摘要和测试结论保留；原运维地址留在本地私有资料中。
- 仓库可见性变化还会公开 Actions 历史和日志；发布者需要检查仓库内现有 PR、Issue、Release 及附属产物。[GitHub 官方说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)。

## 发布前确认

1. 随源码携带项目 [MIT 许可证](../LICENSE)，并核对 [第三方资源](../THIRD_PARTY_NOTICES.md) 和拟发布模型/安装包的再分发要求。
2. 在干净环境按 README 和锁文件验证安装，运行受影响检查；明确仍需真实设备验证的能力。
3. 用合成截图展示 UI；生产数据库、会话、凭证、个人配置、回执和设备数据保留在私有位置。
4. 启用仓库私密漏洞报告入口和 GitHub 可用的密钥保护；维护者确认目标仓库与公开方式后再发布。

源码准备、Git 提交、部署与更改仓库可见性是不同操作。公开源码不要求改动当前生产 Core 或用户设备安装。
