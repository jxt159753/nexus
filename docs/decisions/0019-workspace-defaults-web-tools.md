# ADR 0019：工作区默认读写、多工具交换与网页工具

日期：2026-10-20。状态：用户明确要求实施；实际发布状态以 [ROADMAP](../ROADMAP.md) 为准。

## 背景与授权

用户在原“指定工作区＋写入审批＋受控命令”基础上进一步明确：读写文件不应额外授权；同时要求参考 DSH 源码，加入 DeepSeek 网页搜索与抓取。旧权限问题的延迟答复与免重复读写确认的明确要求存在冲突，实施按后者继续，保留工作区和固定命令限制。

线上报告的轮次在首次模型交换失败，未创建设备任务，当时有效目录已有文件与系统工具。原实现只接收一项原生调用，且上层丢弃具体 ProviderFailure，统一显示 MODEL_OUTCOME_UNKNOWN。多项调用可复现该缺陷；历史没有原始响应，不能断言旧轮次一定返回多项调用。

## 决定

1. 配置好的 Core/Node 工具、资源根及 owner/Node 持久授权交集，是默认文件读写和查找权限。浏览器提交目录摘要与本轮结果外发同意，Core 创建五分钟/20 操作内部快照；无额外会话授权或写入确认。手动文件任务也在原策略检查后记录精确自动批准。任意路径、shell、删除和新扩展版本不开放。
2. 写入继续真实 Node 预览、旧文件摘要、参数绑定、单次尝试、替换及读回。旧显式授权及已经等待答复的操作保留原行为，升级不批准旧任务。
3. SSE 按 index 收集最多 4 项函数，支持交错参数片段及后续空名称/类型。完整结束与用量校验后先验证整批，再按序串行执行；全部结果收齐才进入下一交换。依赖前面结果的动作分轮提出，总量仍为每根轮次 4 动作/5 次模型交换。
4. 保留稳定模型错误分类。有效完整用量已知的失败按已知上界结算；缺用量保留 unknown 和预留。历史未知费用不改写或按零释放，不重试旧付费请求。
5. `web_search`、`web_fetch` 由 Core 调用固定 `https://api.deepseek.com/v1/tools/search`、`/fetch`，只发送相应查询或 URL，不增加辅助模型调用。复用 Core 密钥，默认配置关闭；本次生产启用属于用户追加要求。Node 不拥有密钥或规划循环。
6. 网页步骤在原 model_calls 账本先预留费用，以唯一 web_step_id 关联，work_round 为 NULL，避免占用模型轮次唯一键。金额和次数共用根轮次/每日预算。有效成功结果按配置的单次上界结算；空搜索或非 200 费用为零；无响应或无法判定保留预留。每步只发送一次，中断、重启、超时不重发。
7. 固定 DeepSeek HTTPS 请求拒绝重定向和环境代理；每次 10 秒、响应 256 KiB、最多 5 项来源或 8 KiB 正文。拒绝带凭证、明显内网及非 HTTP(S) URL，Core 不对任意目标 URL 发 HTTP 请求；实际抓取安全与站点策略由供应商执行。
8. 网页/文件内容是不可信资料，不能成为权限指令。只投影有限文本、来源及截断状态，结果持久化，UI 安全文本及链接显示，模型使用资料时须引用。

迁移 017 只增加 auto_files、web_step_id 和唯一索引；升级前一致备份，逐表比较所有旧字段/行。Node 契约不变，现有固定 Node 可继续使用；不原地修改运行环境。

## 参考与限制

- [DeepSeek 官方 Search / Fetch](https://api-docs.deepseek.com/guides/search/)：独立请求、认证、参数、计费与错误规则。
- [DSH Search](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-deepseek-api/src/search.ts)、[Fetch](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-deepseek-api/src/fetch.ts)、[Transport](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-deepseek-api/src/transport.ts)：参考模型工具与传输分工、有限投影、取消及拒绝跳转；未启动外部 Agent，未外发本项目源码。

固定命令可拥有程序本身权限，工作目录不是 OS 沙箱。真实供应商调用、Windows 设备、合成 API 与浏览器测试分别记录；合成响应不代表真实供应商验收。
