# 聊天附件：上传、预览、下载与逐轮发送

适用于任意扩展名文件上传。当前部署与验收查 [ROADMAP](ROADMAP.md)，技术设计见 [ADR 0027](decisions/0027-conversation-attachments.md)。界面由 DSH 按[前端要求](CHAT-ATTACHMENTS-FRONTEND.md)实现。

## 能做什么

原文件按用户和会话保存，单文件最多 16 MiB，每轮最多选择 4 个。所有类型都能保存、列出和下载。当前只对有限 UTF-8 文本、PNG/JPEG/GIF/WebP 提供内容预览；其余显示“已上传，尚未解析”，不代表模型已读取正文。空文件允许上传。

图片使用实际解码后的有界快照，可能缩小或有损，透明白底、动画首帧；具体源/快照上限见原[图片工具说明](dsh-builtin-tools.md)。Core 主机需要原固定 `images` extra 才能生成图片预览；无解码器时仍能保存、下载原件。图片发送还需要管理员显式开启 `models.chat_images_enabled`；文本和未解析文件的元数据不依赖 Node 在线或图片开关。

上传本身不调用模型。先预览本轮要发送的内容，再与消息一起明确同意发送；原费用和请求上限不变。需要下一轮再次读取附件时重新选择并同意，不自动外发已上传的全部文件。

## API

以下 base 为 `/api/v1/chat/conversations/{conversation_id}/attachments`。复用原登录 Cookie，所有写请求携带受信 Origin 与 X-CSRF-Token；附件只能在原 owner/会话访问。

| 请求 | 输入 / 行为 |
| --- | --- |
| `POST base?request_id=<UUID>&filename=<URL编码显示名称>&media_type=<可选声明类型>` | 请求体为原始文件字节，默认 encoding=raw；与真实图片类型判定分开。返回 `{attachment: ...}` |
| `POST base?encoding=base64` | 严格 JSON：request_id、filename、可选 media_type、data_base64；Base64 为普通编码，不带 data: 前缀。返回相同 attachment |
| `GET base?before=<可选游标>` | 返回 attachments、next_before 与 capabilities；每页最多 20 项。capabilities 包含原件/总量上限、每轮数量、解析范围和 image_model_enabled |
| `GET base/{attachment_id}/content` | 原文件字节，强制下载、octet-stream、无缓存；下载不触发模型 |
| `GET base/{attachment_id}/content?encoding=base64` | `{attachment: ..., data_base64: ...}`；供原生字符串桥使用 |
| `GET base/{attachment_id}/preview` | `{attachment: ..., preview: ...}`，preview.kind 为 text、image 或 unparsed；只返回有限预览 |
| `GET base/{attachment_id}/image` | 验证后的 PNG/JPEG 快照，可供浏览器同源 img 使用；不是原始 SVG/HTML/动图 |
| `POST base/model-preview` | `{attachment_ids: [UUID, ...]}`；返回 attachments、payloads、attachments_digest，展示实际外发范围 |
| `DELETE base/{attachment_id}` | 空 JSON `{}`。只允许从未关联聊天轮次的上传件；已关联返回 ATTACHMENT_IN_USE。再次删除同一未引用附件幂等成功 |

filename 只是显示名称，不能带目录、换行或路径分隔符。客户端声明 MIME 不授予执行/内联权限。上传与下载共享两条传输槽，接收/下载各最多 30 秒，忙时返回 429；重试上传沿用原 request_id 和原件，不生成新幂等键。

attachment 字段包含 attachment_id、conversation_id、filename、media_type、size_bytes、sha256、created_at、preview_kind、reason、deleted，以及服务器生成的 download_url、preview_url、image_url。分页能力只显示元数据，不隐式选择附件。

preview.kind=text 时包含 text、truncated；kind=image 时包含 [ImageSnapshot](../src/nexus/contracts/images.py) 的 snapshot（原尺寸、源摘要、实际 image 及限制）；kind=unparsed 时提供明确 reason。model-preview 的 attachments 另有 model_mode=text/image/metadata，图片开关关闭时可能为 metadata，并显示 model_notice。不要仅凭 preview_kind 推断模型已收到图片。

## 提交聊天消息

在原 `POST /api/v1/chat/conversations/{id}/turns` 上增加以下字段：

```json
{
  "attachment_ids": ["11111111-1111-4111-8111-111111111111"],
  "attachments_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "allow_attachment_model": true
}
```

示例只说明附加字段，仍需原 request_id、expected_revision、text、allow_model 等字段。只有已选附件且明确同意时允许 text 为空；未附附件的空白消息仍拒绝。清单、摘要或外发范围改变需重新预览，不能沿用旧同意。上传不改变聊天 revision，消息提交仍核对最新 revision。

轮次列表/详情的 attachments 是提交时的只读清单，不包含整个原文件。刷新后从服务器清单恢复显示，不自动再上传或再提交消息。已经发送的附件不可通过 composer 的“移除”按钮删除历史；该按钮应只取消下一条消息的选择。原文件与上下文按现有保留策略保存，删除历史和保留期调整另行设计。

可能的错误包括：401 未登录、403 不同意/已归档/附件已引用、404 不属于本会话或已删除、409 幂等/摘要冲突、413 文件过大、429 容量/速率/传输槽限制。模型上下文过大使用原 MODEL_INPUT_LIMIT，已经上传的原件仍保留；不会自动追加模型调用或缩短原件。
