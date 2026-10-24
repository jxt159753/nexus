# 原生插件与权限模式前端验证

记录时点：2026-10-22（Asia/Shanghai）。任务要求见 [工作单](PLUGINS-FRONTEND.md)，后端与生产交付见 [P57–P61 验证](PLUGINS-VERIFICATION.md) 和 [路线图](ROADMAP.md)。本记录只说明本地源码、临时真实 Core、冻结 Windows Node 和重新构建的 Windows 安装版的实际检查；不代表生产部署或最终用户验收。脱敏摘要见 [plugins-frontend-evidence.json](assets/plugins-frontend-evidence.json)。

## 改动范围

- `/app`：新增权限/插件页、全局与会话权限、插件生命周期、自写插件入口、目标与提醒、结构化业务问题、Shell/后台任务停止、`present` 交付物认证下载/文本预览。
- 旧控制台：新增全局权限面板、会话权限行、插件页、目标与提醒页、插件工作卡；`ask_user_question` 复用工作步骤的待输入区域。
- Windows 随包 UI：同步 `/app` 页面，并接入 `read_deliverable` 原生桥；本机 Node 设置保留工作区、插件策略、LSP/MCP 等配置，显示本地是否具备 `plugin.call@1`，但不把 Node 自报值当成 Core 信任。
- Windows `node.rs`：不再每次重写只有旧工具列表的 `node.toml`；读取现有 TOML，仅更新连接、数据目录、workspace、owner 显式选择的 `plugin.call@1` 和允许工具列表，保留 `[plugins]`、LSP、MCP、commands、interaction 等表。损坏的旧配置直接拒绝启动，不覆盖。
- 没有修改后端权限、模型费用、TLS 或 CSRF；测试仅使用临时数据目录和本地回环。

## 实际验证

### 权限

真实临时 Core 上通过页面（Edge）执行：

- 全局 normal → full → normal，页面显示模式与 revision，并重新读取服务端。
- 会话“跟随全局”时，全局改为 full 后重新读取，生效模式为 full、source=global。
- 会话覆盖 normal 后，全局再次修改，覆盖保持 normal、source=conversation。
- 清除覆盖后恢复继承。
- 并发修改：页面保存旧 `expected_revision` 时 Core 返回 HTTP 409；页面提示“服务端状态已经变化，已重新读取”，并显示新的 revision（本次观察到 revision 6）。
- full 选中时显示作用范围：当前设备当前 Windows 用户权限内免执行确认；身份、静态能力、预算、取消、审计和结果核验仍生效，不是安全沙箱。
- 页面没有通过模型回复、业务问题回答或执行结果打开模式的入口；模式只能由 owner 设置页的 PUT 修改。

截图：[桌面权限页](assets/plugins-permissions-desktop.png)、[390px 插件页](assets/plugins-permissions-390.png)。

### 插件

真实临时 Core 的 `/api/v1/plugins`：

- `bundled` 显示为“内置随应用发布”，不显示成生成插件测试通过；`test_status=passed` 的用例、验证级别和“业务正确性未验证”分开显示。
- 用户插件 `user.add_numbers@1.0.0` 测试时返回 `qualified_sandbox_unavailable`，页面显示“测试不可用”，没有成功或启用按钮。
- `builtin.present` 在页面上停用后再启用，服务端记录与页面徽标同步。
- 完全模式 + 本地 Core 的**模拟沙箱传输**：候选测试通过后服务端自动批准并启用；页面显示“契约测试通过 / 已启用”，该检查不是真实 runsc 证据，见未验证项。
- 普通模式的具体版本批准/启用按钮按 `test_status=passed`、`approved`、`enabled` 分别显示；测试未通过时不可批准。真实 runsc 下的人工批准仍待验证。

截图：[完全模式自动启用（模拟沙箱）](assets/plugins-full-auto-enable.png)。

### 自写插件与业务问题

- 自写插件页可选择会话、设备和目标；预览显示实际发送给 DeepSeek 的完整文本、发送范围、费用边界和“当前不可生成”的原因（工作目录没有 `plugin_install` 时）。
- 在模型关闭的本地 Core 上点击“确认外发并提交生成”，Core 返回 `MODEL_DISABLED`；页面显示失败提示并保留预览，没有把请求显示为已生成或已启用。
- `ask_user_question` 的 `kind/question/options` 在 `/app` 和旧控制台都渲染为结构化选项与自由输入；回答绑定当前 `step_id` 和轮次 revision。实际提交“继续/今晚”后服务端记录 `permission_decision=approve`，页面没有改变普通/完全模式。

截图：[结构化业务问题](assets/plugins-question-desktop.png)。

### Shell、后台任务与交付物

- 冻结 Windows Node（SHA-256 见下）真实执行 `pwsh`：`Write-Output "frozen-ok"; exit 3` 返回实际输出 `frozen-ok`、退出码 3、工作目录；后台 `Start-Sleep` 返回 `status=running`，页面显示“运行中”。
- 页面“停止后台任务”提交真实 `job_kill` 任务；Core 返回 `status=cancelled` 且 `execution_stopped=true` 后，页面显示“停止任务：Core 已收到真实停止确认”。停止任务的审批仍由 Core 产生，页面等待实际终态，不本地假称停止。
- `present` 交付物走服务端 `download_url`：页面文本预览显示纯文本，下载原件 SHA-256 与服务器登记一致（`76459e0b…`）。Windows 安装版通过原生 `read_deliverable` 桥完成同一预览/下载，WebView2 Edg/155.0.4283.62。
- 页面加载、刷新、重连只做 GET；不自动重跑命令、停止任务或付费生成。

截图：[交付物](assets/plugins-present-desktop.png)、[Windows 安装版插件页](assets/plugins-windows-app.png)。

### 目标与提醒

- 从真实 `/api/v1/plugins/conversations/{id}` 读取待办、目标、提醒和触发事件。
- 时间同时显示 Asia/Shanghai 和服务器原始 UTC/规则时区；下次时间、次数/上限、截止、暂停状态均来自服务器。
- 计划模式 PUT 成功；提醒暂停、目标暂停、提醒删除分别 PATCH/DELETE，服务端记录分别为 `paused=1`、`status=paused`、`deleted=1`。错误由页面显示服务端错误，没有自动创建替代目标。
- 页面不依赖本地计时器触发提醒；Core 后台调度仍独立运行。

截图：[目标与提醒](assets/plugins-automation-desktop.png)。

## 桌面、390px 与键盘

- `/app`：Edge 1440×1000 和 1200×900 检查权限、插件详情、工具卡和错误提示；390×844 下首页、权限页、插件列表、聊天页横向溢出均为 0。
- 旧控制台：1440×1000 检查权限面板、插件页、自动化页和业务问题；390×844 下三个页面横向溢出均为 0，使用 Tab 聚焦页签并用 Enter 切换。
- 键盘：聚焦顶部“权限与插件”按钮后 Enter 打开，Escape 关闭；页签、选项按钮和表单均可聚焦。390px 截图见[插件页](assets/plugins-permissions-390.png)。
- Windows 安装版：WebView2 CDP 1600×900 实际打开插件页、原生交付物预览/下载（该交互在 P62 构建 `10DAC06E…E819` 上完成；最终 `AAACAA86…2A2` 重构建只清理未使用导入并收紧 `download_url` 正则，桥命令未变，最终构建已做启动页/插件视图加载检查）。不是只替换页面文件。

## Windows 与 Node

- 安装包：`var/frontend-check/installed/nexus-windows.exe`（本地 NSIS 最终构建），SHA-256 `aaacaa868c680c182070b2f0250f9105074ec62c63021cd8fb6cca9366cca2a2`；原生下载交互的构建为 `10dac06e…e819`，最终构建差异见上。
- 随包 Node：SHA-256 `6fcddb72e83253aaf6668bac98db828e7001d886d57275b0d4ffa18e02b7b7bf`，与本轮已验证的冻结 Node 相同；没有改换旧版 Node。
- `node.rs` 合并配置的 3 个 Rust 单测、`bridge.rs` 的 4 个单测通过；含保留 `[plugins]`/LSP/MCP/commands 和拒绝损坏旧配置。
- 本机独立 Node 正在使用同一 `%APPDATA%\ai.nexus.windows\node` 数据目录。App 启动本机 Node 时被数据目录锁拒绝，页面显示“Node 已退出 / 本机 Node 未接管插件执行”，没有把旧 App Node 显示成新插件执行端，也没有停止生产独立 Node。
- 本地 UI 测试曾写入测试 owner/node 凭据；已从 `var/remote/secrets/*.dpapi` 恢复 owner/node 凭据并清除测试会话，临时 `node.toml` 已移除。生产 Core、生产配置和生产 Node 未被修改。

## 未验证项

- 真实 runsc 沙箱上的完全模式自动启用（本轮仅在真实 Core 中使用模拟 sandbox transport；真实沙箱自动启用仍以 P57–P61 后端测试为准）。
- 真实付费模型生成、真实生成测试失败/取消、生成过程各阶段的人工批准。
- 生产 Core 部署新前端、生产 Windows 自动更新、Android 真机。
- 旧 App Node 的完整配置迁移：本机独立 Node 仍在线，本轮没有停止它来接管同一身份。
- 未配置的 MCP/LSP/浏览器目标不会显示为可用；其真实外部环境未在本轮开通。

因此本轮只能记录为“本地前端、旧控制台、Windows 安装版和桥接技术自检完成；生产及未列出的真实环境仍待联调/用户验收”，不能宣称完整交付。
