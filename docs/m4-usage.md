# M4 能力使用说明：生成、审阅、批准、使用与停用

本文面向 owner，描述 Web“能力”视图和 API 的操作方式。默认配置关闭自扩展；开启前请阅读 [v2 协议与沙箱门槛](m4-extension-protocol.md)。

## 1. 前提

1. Core 与至少一个执行节点已按既有流程登记并授权；目录动作仍需 workspace 根和原审批。
2. 开发/演示可在 Core 配置显式打开：

```toml
[extensions]
enabled = true
generation_enabled = false
fixture_generation = true   # 仅开发夹具，不调用模型，不构成模型生成证据
```

真实模型生成需 `generation_enabled = true`、`[models] enabled = true` 并在 Core 环境安全提供 `DEEPSEEK_API_KEY`；默认仍关闭。夹具与真实模型走同一候选解析、测试、批准和启用路径，但证据分别标注。
3. Tool 的真实测试/执行还要求独立 Sandbox Node；没有时测试显示“不可用”，不能批准。

## 2. 创建一个 Skill（两步：工作区概览）

1. 刷新页面，进入“能力”。
2. 类型选“Skill”，在需求中写“读取系统信息，再列出 workspace 的直接条目”。可选择能力 ID 和目标节点；目标节点会绑定到版本批准（不选则批准不绑定节点，运行时仍要求已配置节点）。
3. 点“预览将外发的内容”。页面显示精确的请求/契约/工具目录和摘要。开发夹具显示“不发往模型”。
4. 勾选确认后点“生成候选”。生成状态依次显示排队、生成中、候选或失败；失败原因可见，**不会自动重试**。
5. 在候选版本卡点“运行受控测试”。Skill 使用可信工具替身检查顺序、字面量、字段引用、输出契约和失败路径；替身不访问真实设备。
6. 测试通过后点“批准此具体版本”，再点“启用此版本”。批准和启用是两步，服务端都重查摘要、测试、环境和依赖。
7. 版本启用后点“使用此 Skill”。运行会创建真实任务：
   - 第一步 `system.info`；
   - 第二步 `filesystem.list` 停在 **原有的目录审批**；批准后才执行。
   页面显示每步任务和 attempt 证据；任一步失败/取消/未知都会停止后续步骤。运行总期限 5 分钟。
8. “停用”立即阻止后续步骤的新派发；已经派发的步骤继续受取消/对账约束。已停用版本可以在批准仍有效、环境和依赖仍符合时重新启用。

## 3. Tool（目录条目统计）

1. 类型选“Tool”，需求写“统计显式传入的 file/directory/link/other 条目数量，不读取文件”。若配置了 Sandbox Node，选择它作为目标节点。
2. 预览、生成后，候选必须由**可信控制器**用固定用例测试：混合输入 2/1/1/1、空数组、200 项、非法类型/未知字段/超限/缺失字段。候选自报的 expected/passed 不作为依据。
3. 只有实际沙箱运行、控制器比较通过且环境摘要匹配时才显示“测试通过”。没有合格沙箱时显示“测试不可用”，批准按钮不会出现，API 也拒绝。
4. 批准后仍需显式启用。Tool 只在 Skill 的固定版本步骤或后续受控入口中执行；不能通过聊天函数列表或 v1 命令绕过。

## 4. 组合 Skill（第三步统计）

1. 先创建 Tool 并完成“候选 → 测试 → 批准 → 启用”。
2. 创建 Skill 时把父版本填为上一步可用的 Skill 版本 ID（如“工作区概览”v1），需求写“在概览第三步统计前一步返回的条目”。
3. 新版本为 revision 2，第三步只允许引用更早步骤的 `entries` 字段；目录审批仍保留。测试替身会验证引用和截断。
4. 若 Tool 未批准/启用、环境或批准过期、目录结果缺失，服务端拒绝批准/执行，不会悄悄降级。
5. `truncated=true` 时结果只统计返回条目；页面和结果必须明确“不是整个目录总数”。

## 5. 修订、停用、回退

- **修订**：新建扩展请求，填写父版本 ID；Core 分配下一 revision，重新生成、测试、批准、启用。已批准旧版本不可原地修改。
- **停用**：能力页“停用”，或在 API `POST /api/v1/extensions/versions/{id}/disable`。停用只影响后续派发，不撤销已经发生的结果/费用。
- **回退**：选择仍启用且批准未过期、测试/环境仍匹配的旧版本使用；不会回滚历史、审计或费用。环境变化后旧批准不能直接复用。
- **失败**：生成失败/被拒绝/中断都显示原因；不会自动消耗下一次模型调用，修订需要新请求。

## 6. API 摘要

所有写请求使用同源会话 Cookie、`X-CSRF-Token` 和允许的 Origin；仅 owner 可操作。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/extensions` | 功能/生成/沙箱就绪状态 |
| POST | `/api/v1/extensions/requests` | 创建草稿和精确外发预览 |
| POST | `/api/v1/extensions/requests/{id}/generate` | 带预览摘要确认，排队生成 |
| GET | `/api/v1/extensions/requests[/{id}]` | 请求/候选状态 |
| GET | `/api/v1/extensions/versions[/{id}]` | 版本、测试、批准、启用、父版本差异 |
| POST | `/api/v1/extensions/versions/{id}/test` | 可信 Skill 替身或沙箱 Tool 测试 |
| POST | `/api/v1/extensions/versions/{id}/approve` | 批准/拒绝具体版本（绑定摘要和 test_id） |
| POST | `/api/v1/extensions/versions/{id}/enable` | 显式启用 |
| POST | `/api/v1/extensions/versions/{id}/disable` | 原子阻止后续派发 |
| POST | `/api/v1/extensions/runs` | 启动已启用 Skill |
| GET | `/api/v1/extensions/runs[/{id}]` | 步骤任务/结果/失败原因 |
| POST | `/api/v1/extensions/runs/{id}/cancel` | 停止本次 Skill |
| GET | `/api/v1/extensions/sandbox` | 已登记沙箱、镜像摘要、在线/原因 |

## 7. 明确限制

- 真实 Tool 示例在当前交付中**没有在 runsc 主机执行过**；因此不能批准、启用或声称 P23/P24 的真实隔离闭环完成。
- Skill 测试用固定替身，不访问真实设备；真实运行才通过 Windows Node 和原审批。
- 手机宽度页面已实现并做了静态检查；未完成真实 Android 真机本轮验收。页面显示的是文本/JSON，不执行候选 HTML 或远程资源。
- 能力生成、测试、批准和运行不改变 M2/M3 的聊天、两工具确认、目录审批和自动回传逻辑；默认关闭扩展时这些入口保持可用。
