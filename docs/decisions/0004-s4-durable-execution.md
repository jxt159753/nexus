# ADR 0004：单步持久执行、证据验证与后端入口

日期：2026-10-17。适用范围：用户批准的 S4 / P07。状态：已实施，待用户验收；不扩大至 S5–S7。

## 决定

沿用单活 Core、独立 Node、SQLite 及原认证 Link。Core 增加 Harness 和专用 ExecutionRepository，使用任务尝试表作为持久执行记录，不增加消息中间件或通用工作流引擎。

派发事务同时重查身份/权限/审批/预算并创建动作级 execution_grant、attempt、命令和状态事件；网络发送在提交后。静态 grants 只表达身份许可，不能当作某一任务动作的执行批准。Browser session 结束不会终止已持久的用户意图，调度仍受活动 owner / Node 和最新策略约束。

先持久原始结果并转 verifying，独立短事务完成工具验证与终态；启动只恢复 verifying 的本地确定性验证。将原 Node 结果约束移至 contracts 复用，Core 不导入 Windows 工具实现。成功验证级别明确为 node_report_schema_checked，不能独立证明报告的操作系统事实。

S4 用受现有会话、Origin、CSRF 保护的 HTTP 提交/审批和历史 API 展示后端闭环。浏览器 WS 和页面仍归 S6，不提前搭建前端。Node 使用有界阶段通知把原受管理同步执行接入异步 Link，真实工具不阻塞心跳；连接结束时不遗弃运行线程。

## 影响与限制

增量迁移 003 使用既有备份与版本校验；没有新增依赖。每任务首次尝试、每 Node 单槽由持久状态约束。仅凭连接失败不能判定未执行：结果未知时保留尝试并阻止下一次相关执行。网络对账、重传/新尝试、总期限、取消及全局控制由 S5 接入，S4 不自动重跑未知执行。

接口见 [S4 Harness](../s4-harness.md)，实际验证和限制见 [S4 证据](../S4-VERIFICATION.md)，阶段权威见 [ROADMAP](../ROADMAP.md)。
