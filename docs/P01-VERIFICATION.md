# P01 工程准备验证记录

复核日期：2026-10-17。任务：S1 / P01。阶段批准、任务状态及用户验收统一见 [ROADMAP.md](ROADMAP.md)，本文件只记录技术证据和复现方式。

## 范围与环境

用户明确说明 S1/P01 已完成，要求验收并更新文档。本轮检查现有工程，不推进 P02–P12，不启动服务。原有 Git 历史、实验 API 和数据库不能证明后续任务已完成或获准。

- Windows 11，64 位 CPython 3.13.9；本机仅发现 Python 3.13，未实测 Python 3.12。
- 工程为 `nexus==0.1.0`，使用 src 布局及 setuptools 构建。
- 验证工具为 uv 0.10.10；原 `.venv` 中 pip 26.2.1。
- 本轮独立临时目录为 `E:\NEXUS\.p01-acceptance-1792208525162`，其中虚拟环境未继承系统包，安装前无法导入 Pydantic。临时产物在复核结束后清理，不作为项目文件交付。

## 交付核对

| 文件 | 本轮检查结果 |
| --- | --- |
| `pyproject.toml`、`src/nexus/__init__.py` | 声明 Python >=3.12、项目元数据、src 包发现与可编辑安装；独立环境安装及隔离导入通过 |
| `src/nexus/settings.py` | 环境变量配置，缺失/非法值明确失败；只允许 127.0.0.1；不创建目录或数据库 |
| `.env.example` | 只有数据目录、监听地址和端口，没有真实密钥；当前需手动设置环境变量，不自动加载此文件 |
| `requirements.lock` | 固定 23 个运行/开发依赖版本，独立安装通过；不包含本项目及完整构建工具锁定 |
| `tests/test_settings.py` | 6 项配置测试，两套环境分别通过 |
| `.gitignore` | 本轮补上 `/var/` 与 SQLite 的 WAL/SHM/journal 旁文件规则；原有环境、主数据库、密钥配置、构建与测试产物规则仍生效 |

P01 只建立配置函数。当前配置来源为环境变量和默认值，未实现 TOML，也未接入实验 API 的启动流程。目标配置层和正式服务集成仍见 [架构设计](ARCHITECTURE.md)。

## 实际执行与结果

下表是本轮重新执行的结果，不把原交付文档中的自述当作独立证据。命令从仓库根目录执行；`$reviewRoot` 指向上面的本轮临时目录。

| 检查 | 实际命令或步骤 | 结果 |
| --- | --- | --- |
| 原环境依赖 | `& .\.venv\Scripts\python.exe -m pip check` | `No broken requirements found` |
| 原环境测试 | `& .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=short --basetemp "$reviewRoot\pytest-existing"` | 6 passed |
| 独立环境 | `python -m venv --without-pip "$reviewRoot\venv"`；检查解释器、`pyvenv.cfg` 和安装前 Pydantic 可用性 | Python 3.13.9，无系统包继承，Pydantic 安装前不可用 |
| 按锁安装 | `uv --cache-dir "$reviewRoot\uv-cache" pip install --python "$reviewRoot\venv\Scripts\python.exe" -r requirements.lock --index-url https://pypi.org/simple --no-config --no-progress` | 23 个依赖安装成功 |
| 项目安装 | `uv --cache-dir "$reviewRoot\uv-cache" pip install --python "$reviewRoot\venv\Scripts\python.exe" --editable . --no-deps --index-url https://pypi.org/simple --no-config --no-progress` | 构建及安装 nexus 0.1.0 成功 |
| 独立环境依赖 | `uv pip check --python "$reviewRoot\venv\Scripts\python.exe" --no-config` | 24 个包兼容，包括 nexus |
| 安装后导入 | 切换至临时目录，以独立解释器的 `-I -c` 导入 `nexus`、`nexus.settings.load_settings`，读取包版本及路径 | 版本 0.1.0；可通过安装入口导入包和配置函数 |
| 独立环境测试 | 在临时目录执行 `& .\venv\Scripts\python.exe -m pytest E:\NEXUS\tests -q -p no:cacheprovider -o pythonpath= --import-mode=importlib --tb=short --basetemp "$reviewRoot\pytest-clean"` | 6 passed；覆盖 pytest 的 src 路径注入配置，验证已安装工程 |
| 额外配置边界 | 标准输入传入临时 Python 脚本，逐项重置环境变量，调用 `load_settings()` 并断言异常/默认值/边界值 | 下述 13 项均通过；不是新增的仓库测试用例 |
| 无秘密示例/输出 | 人工读取 `.env.example`、settings 与测试代码；额外脚本捕获 stdout/stderr，检查顶层错误文本不包含测试标记 | 示例无密钥；配置函数无日志调用；捕获输出为空；不代表未来认证日志已经验收 |
| Git 忽略 | `git check-ignore -v` 检查下列路径名；再检查交付文件不被忽略 | 忽略项均命中；`.env.example`、pyproject、锁文件、settings、测试仍可被 Git 跟踪；未创建假凭证文件 |
| 文档与范围 | 检查 5 个 Markdown、27 个本地链接、2 个 JSON 示例、代码围栏、PowerShell 示例语法、任务勾选及 `git diff --check` | 全部通过；仅 P01 勾选，S1 待用户验收，S2–S7 未批准；实验代码未修改，原数据库复核前后哈希一致 |

13 项额外配置检查：数据目录缺失、空值、普通相对路径、盘符相对路径、根相对路径；远程 host、空 host；非数字端口、0、65536；默认 host/port；端口 1、65535。非法输入均明确抛出 `ValueError`，合法边界和默认值符合预期。

Git 检查覆盖 `.venv/`、`nexus.db`、`src/nexus.egg-info/`、`.env`、`.env.local`、`var/runtime.log`、`var/node-token.txt`，以及 `receipt.db` / `receipt.sqlite3` 的 `-wal`、`-shm`、`-journal` 旁文件。

## 复现安装与配置检查

以下命令适用于已安装 Python 和 uv 的 Windows PowerShell，在干净工作副本中从根目录执行。uv 是本次采用的验证工具，不是项目运行依赖。

```powershell
python -m venv --without-pip .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.lock --index-url https://pypi.org/simple
uv pip install --python .venv\Scripts\python.exe --editable . --no-deps --index-url https://pypi.org/simple
uv pip check --python .venv\Scripts\python.exe

$env:NEXUS_DATA_DIR = Join-Path (Get-Location).Path 'var'
$env:NEXUS_HOST = '127.0.0.1'
$env:NEXUS_PORT = '8000'
& .\.venv\Scripts\python.exe -c 'from nexus.settings import load_settings; print(load_settings())'
& .\.venv\Scripts\python.exe -m pytest -q
```

`NEXUS_DATA_DIR` 必填且为绝对路径；host/port 未设置时分别使用 `127.0.0.1` 和 `8000`。加载配置不要求该目录已存在，也不检查其可写性或创建资源；正式存储启用时仍需对应校验。上述命令不启动服务。

受限沙箱内可能需要将 `TEMP`、`TMP` 和 pytest `--basetemp` 指向工作区的一次性目录。每个验证步骤退出非零时应先处理失败，不继续认定后续步骤已通过。

## 环境失败与恢复记录

- 首次直接运行 pytest：1 passed、5 errors，均因默认沙箱临时目录无法创建 fixture 目录。改用工作区的独立 `--basetemp` 后 6 passed，没有修改测试或应用代码来跳过失败。
- 默认 `python -m venv` 在 ensurepip 步骤失败。改用 `--without-pip` 创建独立环境，再由 uv 安装，不是复用原环境的已装依赖。
- 离线安装因空缓存缺包失败；沙箱联网尝试因 socket 权限失败。在获准联网的执行环境中从 PyPI 安装锁定依赖及项目成功，未关闭 TLS 校验、修改证书信任或系统设置。
- uv 构建时出现缓存目录 rename 的暂时访问拒绝，工具重试后构建成功，最终安装、兼容性检查及测试均退出 0。

这些是验证环境的问题及实际恢复结果，不被记录为配置代码缺陷，也不掩盖首次失败。

## 实验实现与未验证边界

保留 `src/nexus/core/main.py`、`database.py`、`start_nexus.bat` 和本地 `nexus.db`。本轮只读检查代码，不启动实验 API，不查询业务数据、不迁移或删除原数据库。

实验 API 没有正式身份认证、授权、任务幂等、状态机或版本迁移，数据库路径也没有接入 `load_settings()`。它不是正式 Core 或 P03/P07 的验收成果，不能用实验启动入口证明缺失配置会阻止正式服务启动。正式存储实现前应另行处理旧数据兼容和启动入口，不能静默复用或覆盖旧库。

尚未验证或未实现：

- Python 3.12、其他 Python 版本和其他操作系统的安装兼容性；当前锁文件由 Python 3.13 环境生成，未声明跨平台验证通过。
- 构建工具的精确锁定与依赖哈希校验；当前 setuptools 仍使用 `>=75,<81` 的范围约束，构建隔离仍需要取得构建依赖。
- TOML 优先级、数据目录创建/可写性/本地磁盘要求，以及正式服务的配置接入与启动失败行为。
- 正式 Core/Node、WebSocket、TLS、Android、持久化恢复与故障矩阵；P01 通过不代表这些能力已通过。

本轮技术结论：P01 现有安装、必要配置失败、安全默认值、无秘密示例、忽略规则和基础测试符合工程准备验收条件。用户验收确认及下一阶段批准分别由 [ROADMAP.md](ROADMAP.md) 记录，不由本技术记录自动产生。
