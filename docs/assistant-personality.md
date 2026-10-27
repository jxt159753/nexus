# 人格与表达

网页、Windows 和 Android 的设置中提供“人格与表达”。当前开发、上线与验收状态只见 [P101](ROADMAP.md#p101-personality-settings)。

| 人格 | 表达侧重 |
| --- | --- |
| NEXUS · 沉稳机智（默认） | 从容、可靠、有主见，偶尔轻巧幽默 |
| 温暖伙伴 | 耐心听懂具体感受，用可行的小步骤支持行动 |
| 探索搭档 | 好奇、轻快，善用比喻和实验发现新角度 |
| 严谨顾问 | 先给判断，再说明证据、假设与取舍 |

所有人格都保留坦诚、独立判断和对上下文的承接。人格不改变工具能力、权限、记忆同意或任务成功标准。本轮明确的表达要求优先于预设。

“回复中的 Emoji”可选关闭、适量（默认）、丰富。适量可以不用或偶尔用一个；丰富在轻松语境中可用1–3个，严肃或失败反馈仍应克制。界面示例用于预览风格，不是实时模型生成。关闭指导之后的新回复，保留旧消息、引用、代码与文件原文；不通过过滤文本破坏内容。

保存后，同一 Core 账户的各端共享设置，从新提交的轮次生效。正在生成或执行工具的一轮保持原有风格；重新打开设置读取最新值。打开期间的异步刷新保留未保存选择和正在进行的保存，不重置表单。保存遇到其他设备修改时，页面保留选择并提示读取最新设置，不自动覆盖。旧 Core 暂不支持接口时，设置显示原因，聊天仍沿用原能力。

## Core 接口

`GET /api/v1/assistant/preferences` 返回 `preferences`（personality、emoji、revision）、四项 `personalities`（id、name、description、三种模式的examples）及 `emoji_modes`（id、name）。需要有效 owner 会话。

`PUT /api/v1/assistant/preferences` 沿用 Origin、Cookie、CSRF 校验，示例：

```json
{"personality":"companion","emoji":"natural","expected_revision":0}
```

personality 枚举为 `nexus / companion / explorer / advisor`，emoji 为 `off / natural / expressive`。无保存记录时返回默认值、revision0；成功变更递增修订号，返回 `{"preferences":{...}}`；相同值保存不递增。非法字段/枚举返回400、未认证401、权限或CSRF失败403、修订冲突409。未知保存结果应先GET核对，不自动重放写入。

设置不是模型工具，模型不能通过文字或插件自行更改。变更只保存固定枚举，不接受用户自定义system提示，也不创建长期记忆。

## 实现与迁移

`041_assistant_style.sql` 新增按owner隔离的偏好表，并在 `chat_turns.assistant_style_json` 保存提交时快照。旧行补默认风格，原有消息和执行证据保持。数据库初始化先备份，连续迁移并检查外键；部署前仍须按原单活发布流程验证固定包。代码回退须能识别041，不能用恢复旧数据库覆盖新历史。

普通回复、工具续接、结果/失败解释共用选定短提示；完整准入、持久请求日志和Provider发送一致。内部压缩不附加人格提示。仅选中的风格进入请求，其他人格说明和预览不进入模型上下文；无新模型调用、外部Emoji资源或依赖。Token估算对照和测试限制见路线图及[对照记录](assets/personality-settings-evidence.json)。
