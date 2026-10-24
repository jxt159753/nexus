# 原生插件与权限模式：DSH 前端工作单

日期：2026-10-22。由用户直接控制 DSH 实施；Codex 不改前端或自动发送任务。接口交付状态见 [ROADMAP](ROADMAP.md)，决定见 [ADR 0028](decisions/0028-native-plugins-permission-modes.md)。[后端接口与插件格式](plugins.md)已经实现，具体部署版本及未完成的验证仍看路线图，不能把本地接口误认为生产可用。

## 可以交给 DSH 的要求

请在 E:\NEXUS 现有 `/app`、旧控制台和 Windows 随包 UI 接入原生插件与权限模式。完整阅读 AGENTS、ARCHITECTURE、ROADMAP 和本文件，保留已有 3D、Markdown、图片、附件、presentation v1 及原生认证桥改动。不得修改后端权限、模型费用或绕过 TLS/CSRF。安装版 Node 使用本轮验证过的构建，不能仅换页面。

1. 全局设置读取/更新 `/api/v1/permissions/default`。展示 normal（普通）与 full（完全）、revision；更新携带 `request_id` 和 `expected_revision`。full 表示当前设备用户权限下免执行确认，明确启用时显示作用范围；不要把它叫安全沙箱。
2. 会话设置读取/更新 `/api/v1/chat/conversations/{id}/permission`。三个选择：跟随全局、普通、完全；继承值为 null。显示 effective_mode、source 和 revision，修改须刷新服务端返回，冲突重新读取。全局修改后，只有无覆盖会话跟随；模型回复不能操作开关。
3. 插件页读取 `/api/v1/plugins`，显示 id、名称、固定版本/摘要、来源、运行位置、测试、批准及启用状态。查看详情、测试、批准、启停/回退分别调用实际后端；未提供按钮对应能力时明确不可用。无插件市场，不提供子代理/团队/Ralph 的伪入口。
4. 自写插件入口允许提交目标、目标设备和会话；先显示实际外发预览及费用范围。生成、检查、测试、批准、启用分开显示。普通模式需要批准具体版本，完全模式只在服务端确认自动启用后显示可用。失败、取消、未知、测试不可用不显示成功。
5. `ask_user_question` 复用当前工作步骤的待输入区域，显示结构化选项，回答绑定 step_id/revision。权限批准与业务问题回答分开，普通回答不能打开 full。
6. Shell/终端/后台任务卡展示实际退出码、运行/已停止/未知、截断与工作目录；停止按钮调用后端并等待真实停止确认。刷新、重连、重启不自动重新执行命令或付费请求。
7. `present` 交付物使用认证下载/预览接口，文件文本安全显示，不执行 HTML 或任意插件提供的前端脚本。插件界面首版复用声明式字段与已有结果组件。
8. 目标/提醒页面只展示实际后端状态、下次时间、次数/截止/预算及暂停/修改/删除操作；所有时间展示 Asia/Shanghai 并保留原时区。后台任务不依赖页面计时。

## 验收

插件详情使用 `/api/v1/plugins/versions/{id}`；测试为 POST `.../test`，输入 conversation_id/digest；批准或启停为 POST `.../decision`，输入 conversation_id/digest/action（approve、enable、disable）。状态 `bundled` 表示随应用发布的内置版本，不能显示成“已通过生成插件测试”；`passed` 的具体用例在 test 字段。插件查询返回 execution 配置状态时保留 unconfigured/node_offline。

会话待办、目标与提醒事件从 GET `/api/v1/plugins/conversations/{id}` 读取；计划模式 PUT `.../plan`，输入 expected_revision/enabled。业务问题在工作步骤 result 中有 kind/question/options，提交仍走原 permission 接口并绑定 step_id/revision；回答业务问题不改变模式。默认提醒只是会话事件，execute=true 的定时任务和目标有有界续跑及共享费用上限。

提醒 PATCH/DELETE `.../schedules/{schedule_id}`；目标 PATCH `.../goals/{goal_id}`。输入均为 request_id/changes；提醒 changes 使用 schedule_update 字段，目标只接受 status=paused/active。重复 request_id 返回同一结果，不能在重试时换参数。恢复范围不再匹配、预算不足或过期时显示服务端错误，不在页面自动创建替代目标。

文件传输由 Node 完成，前端只读取交付物 download_url；不能自行提供一个未经服务端登记的宿主路径作为可下载文件。Windows HTTP 桥需要支持该认证下载和前述新路径；这些桥接变更由用户控制 DSH。

Windows `src-tauri/src/node.rs` 当前每次启动仍重写旧版 `node.toml`，没有插件配置。请接入固定 Node 构建并保留本地插件策略、工作区、LSP/MCP/浏览器配置；部署时由 owner 在 Core 同步信任新的 `plugin_digest` 与静态能力，不能由模型或 Node 自报值自动扩大。未完成桥接时沿用已部署的独立 Node 启动器；启动旧 App Node 会造成能力降级或身份重复，不能把它显示成新插件执行端已接管。

真实 Core + 更新后的 Windows Node；验证全局默认、会话覆盖与恢复继承、并发修改、模式降级、已派发操作、归档/撤销、生成测试失败及 full 自动启用；刷新不重复操作，错误与 unknown 保留。桌面/390px/键盘检查分别记录。没有实际后端证据的 UI 只记为待联调，不能宣布本轮完整交付。
