# ADR 0034：原生 Skill 步骤与 Core 管理的 MCP

日期：2026-10-23。范围：用户要求补齐现有 Skill 对新版 Shell 的支持，以及 MCP 受控配置登记、工具发现与重载两个通用能力。交付状态见 [ROADMAP](../ROADMAP.md#p65skill-shell-与受控-mcp)。

## 决定

扩展既有 SkillService，不新建执行器或规划循环。新 profile `skill_native_steps_v1` 的步骤可显式固定 `builtin.shell` 或 `builtin.mcp` 的工具及发布摘要，通过原 `plugin.call@1`、task/attempt、Node 回执、Windows Job、取消及验证执行。旧 `skill_steps_v1` 不接受新步骤，不重解释旧批准。生成器按实际内置目录提供新契约，仍走原候选、契约测试、具体版本批准和启用流程。

每个原生运行绑定 owner 会话、有效 normal/full 权限指纹及目标 Node。普通模式对具体执行任务审批，完全模式免执行确认；两种模式都重查身份、静态权限、节点插件策略摘要、计划模式、取消和版本。模式变化停止后续步骤；已发生的效果不回滚。Skill 仍最多三步、五分钟；Shell 必须前台完成，非零退出或未确定执行不推进。契约测试明确标为宿主执行未运行，不把替身输出冒充实际设备结果。

MCP 配置与目录只在 Core 保存，不让模型编辑 Node TOML，也不把 MCP 服务器当作可信规划者。注册先落盘 pending 版本和稳定 operation_id，再提交真实 Node 发现任务；Task 和 operation 关联同事务。执行成功、目录摘要与当前权限/取消屏障均有效后才原子激活；重载失败保留旧 active。中断于提交前可沿用原 request_id 重试，或显式移除 pending 登记。没有执行任务的 pending 不自动获得可调用资格。

工具调用绑定 owner、Node、会话、配置版本/摘要和已发现目录摘要，Node 再做一次真实 tools/list 核对后 tools/call；目录变化先拒绝调用，需显式重载。MCP 提供的 annotations 不授予只读或自动执行权限，调用统一按有副作用的具体操作检查。已完成发现的历史结果可按当前身份/会话权限读取，不允许重新派发该任务。

Node 必须显式配置 `allow_managed_mcp=true` 并在 Core 同步信任相应 plugin_digest；默认关闭。保留旧静态资源配置；管理过又移除的名字不能回落至静态配置继续执行。原插件命令未携带 binding 时的 JSON 规范化保持不变，避免升级改变旧回执摘要。新增迁移 028，不改写已存在的 027 或旧迁移。

## 取舍与限制

- 复用执行、审批和恢复证据，避免独立安装器在 Shell 之外绕过 Harness。安装行为由有限 Shell 步骤完成，本接口登记已存在的程序/远程 URL。
- stdio 每次操作初始化并清理进程；HTTP 支持初始化、会话/版本头以及 JSON/SSE 响应。没有永久 stdio 连接或跨调用内存状态，持久数据须由服务器自身保存。
- 协议支持 2025-11-25、2025-06-18、2025-03-26。发现最多八页、64 工具/20 KiB，保留不支持 schema 的工具但禁止调用。采用现有有界 schema 子集，未实现完整 JSON Schema、OAuth 或通用凭证注入。
- 本次没有插件市场、子代理、团队或前端实施。Windows 桥接要求另交用户控制的 DSH；生产 Core/Node/配置升级与验收独立记录。

官方协议依据：[工具发现和调用](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[生命周期与版本协商](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)。接口、数据示例和精确限额见 [使用说明](../skill-mcp.md)。
