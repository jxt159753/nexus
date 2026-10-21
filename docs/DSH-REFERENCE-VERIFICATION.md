# DSH 参考优化：本地验证记录

日期：2026-10-20。技术决定及 GitHub 固定源码入口见 [ADR 0020](decisions/0020-dsh-context-harness.md)；当前批准、部署与验收状态以 [ROADMAP](ROADMAP.md) 为准。

## 基线与范围

- NEXUS 工作区基线 HEAD：`04fc8d9f7f26ecf9cb37499a070b37bc293d54a9`，分支 `codex/m6-chat-work`，开工时已有未提交的 M6 修改和新增文件。本轮未重置这些内容。
- DSH GitHub 固定提交：`687f282e8625c6b8d11ff58211a90524be2e4317`；对架构、Agent Loop、工具运行时、调用配对、结果裁剪、Skill 加载和 grep 源码进行了对照。七个主要固定路径内容与此次读取的 master 版本一致。
- 本地 Windows / Python 3.13.9，沿用 `.venv` 和锁定依赖。无依赖、数据库迁移、Node 协议、生产配置、凭证或系统信任修改；未启动 DSH Agent 或真实模型请求。
- 改动集中于请求组装、上下文投影、工作目录/能力查询、聊天提示、测试和说明。原任务状态机、Node 回执与原费用账本继续复用。

## 实际检查

| 检查 | 实际结果 |
| --- | --- |
| 改动前聊天/工作/网页四个模块 | 68 项通过，20.82 秒 |
| 首次改动后，额外包含 M5 Skill 模块 | 94 项通过，40.42 秒 |
| 新增上下文与能力测试 | 12 项通过，3.01 秒；包括四次实际临时文件读取及持久证据 |
| 全量源码回归 | 801 通过、2 跳过、2 个 TLS 夹具错误，332.62 秒；没有应用断言失败 |
| 修正测试 OpenSSL 选择后的定向检查 | 82 项全部通过，38.85 秒；包含出错的两个真实 TLS/备份恢复用例及最终聊天/工作/网页/新上下文代码 |
| 无头 Edge + 测试 Core + 独立 Windows Node | 1 项通过，16.33 秒；原生 SSE、节点状态、实际临时文件写入、搜索/抓取模拟传输及桌面/手机尺寸页面 |
| Python 与 JavaScript 语法 | 改动 Python 文件 AST 解析、两份 JavaScript 的 `node --check` 通过 |
| 文档与范围 | 本轮改动文档的本地链接、代码围栏及 Git 差异空白检查通过；范围摘要确认没有文件删除 |

全量运行默认选中 `D:\anaconda3\Library\bin\openssl.exe`，两个测试在生成测试证书时退出，尚未进入应用执行。按原 [S7 文档](S7-VERIFICATION.md) 设置 `NEXUS_TEST_OPENSSL=C:\Program Files\Git\usr\bin\openssl.exe` 后，两项均在上述 82 项定向检查中通过。没有改动 TLS 测试、断言或系统信任，也没有再次运行整个回归来覆盖首次错误记录。

新增四文件结果测试首次因 Windows 文本夹具自动写入 CRLF，完整字节与期望 LF 不一致而失败；测试文件改用显式 LF 后通过。应用原始文件读取行为未为该断言改变。最初受限沙箱中的基线测试无输出挂起，已中止；后续测试在获准的本机执行权限下运行，不把挂起尝试记为通过。

实际命令的主要部分：

```powershell
# 首次全量：记录上述 801 通过、2 跳过、2 夹具错误
./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -W error --basetemp var/dsh-optimization/full-regression --junitxml var/dsh-optimization/full-regression.xml

# 修正测试工具选择后的定向检查：82 通过
$env:NEXUS_TEST_OPENSSL='C:\Program Files\Git\usr\bin\openssl.exe'
./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -W error tests/test_acceptance.py tests/test_chat_context.py tests/test_chat.py tests/test_chat_work.py tests/test_workspace_work.py tests/test_web_work.py --basetemp var/dsh-optimization/final-focused --junitxml var/dsh-optimization/final-focused.xml

# 独立浏览器检查：1 通过
$env:NEXUS_TEST_NODE='C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$env:NODE_PATH='C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -W error tests/test_chat_work_browser.py --basetemp var/dsh-optimization/browser-check --junitxml var/dsh-optimization/browser-check.xml
```

## 关键证据

- 组装后的测量大小等于实际 Provider 发送的 JSON 字节数，含中文、emoji、转义、标题和工具定义。
- 裁剪旧历史保留当前输入与明确选择的记忆；裁剪工具数据保留调用参数与全部对应 ID、状态和验证信息。原始消息对象不被原地修改。
- 四次实际读取 `large.txt` 后，第五次模拟模型交换完成，四条完整结果及原文件均保留；模型可见结果有显式摘录标记。
- 即使当前输入无法容纳，已完成工具仍记录 succeeded 及原始证据；停止本轮不把它改成未执行。
- 不能通过省略 `title` 放松字段校验；名称为 `title` 的真实属性仍保留。能力查询只能使用本轮目录中的固定版本，停用后不返回详情，也不创建设备任务或 Skill 运行。
- 本地超限在费用预留前拒绝，模拟 Provider 没有调用记录；网页已发生的已知费用仍保留。

JSON 字节测量：六个现有内置工具 schema 从 2585 降至 2153 字节，减少约 16.7%；包含六个较复杂扩展的合成目录从 12339 降至 4210 字节，减少约 65.9%。后者为合成契约示例，不代表线上目录、精确 token 数或已节约的供应商费用。简单能力的按需详情可能增加一次模型交换，仍受原预算约束。

机器可读记录及临时数据位于 Git 忽略的 `var/dsh-optimization/`：`baseline.json`、`scope-check.json`、`schema-measurement.json`、四份 JUnit XML 和浏览器临时证据。此处不归档生成的临时数据库、测试身份或证书。

## 验证限制

真实 DeepSeek 对新增详情工具的选择质量、供应商精确用量、Linux 环境、Android 真机及生产升级没有在本轮验证。浏览器与 SSE/网页供应商使用模拟传输，实际 Windows 文件读写与独立进程证据分别记录，不冒充真实供应商或 Android 验收。

未提交、推送、构建/安装候选 wheel 或变更线上 Core/Node；现有生产版本记录保持原值。本轮技术检查不等于用户最终验收，后续部署需明确安排本轮版本及范围。
