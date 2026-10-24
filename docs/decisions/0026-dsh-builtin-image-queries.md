# ADR 0026：DSH 内置图片与 Core 查询工具

日期：2026-10-21。用户要求把 DSH GitHub 内置插件中适合作为工具的能力移植到 NEXUS，包含图片且不限于图片；随后明确今后所有前端修改均先提出要求，由用户控制 DSH 实施。范围和交付状态统一见 [ROADMAP](../ROADMAP.md)。

## 来源与取舍

只读核对官方 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness/tree/687f282e8625c6b8d11ff58211a90524be2e4317)，此次 master 仍对应固定提交 `687f282e8625c6b8d11ff58211a90524be2e4317`。参考 MIT 授权的 [read_image](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs/src/read-image.ts)、[时间上下文](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/context/time-context/src/index.ts)、[后台任务工具](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/jobs/tool-jobs/src/index.ts)和 [Skill 加载](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/skill/tool-skill/src/index.ts)的行为，按现有 Python 契约和执行器独立实现，不加载其插件框架。

| DSH 能力 | NEXUS 适配 |
| --- | --- |
| read/write/edit、glob/grep、web | 复用现有工具，不再复制一组 |
| read_image | 新增 `filesystem.read_image@1`，聊天函数名 `read_image`，保留 workspace、身份交集、回执及结果校验 |
| time-context | 使用显式 `get_current_time` 工具查询 UTC/固定偏移时间与本轮经过秒数；没有额外定时器或模型调用 |
| job_list / job_output | 查询原任务表：列表限当前会话和所选节点；正文限当前轮次和节点，保留 unknown、验证及截断，不把已停止和业务成功混同 |
| tool-skill | 扩展原 `inspect_capability` 返回已批准 Skill 的 Markdown 与固定步骤，复查依赖、节点和授权；不新增重复的 skill 工具 |
| Bash/PowerShell、持久 Shell | 沿用已登记 `run_command` 固定模板；不引入任意 Shell、常驻终端或后台命令生产者 |
| schedule、goal、子代理/agent-team、workflow | 需要后台生命周期、目标权威或额外执行设计；此次不直接搬入 DSH 运行时，也不据此宣布 M7 或第二规划中心获批 |
| todo、ask-user、present、session-query、workspace-dependencies、LSP/worktree | 分别与现有项目待办/中途交互/结果界面/资料引用或额外依赖和文件变更有关；保持已有入口。未注册成无实际执行器的空工具 |

## 图片与模型边界

图片源文件最多 4 MiB、单边 8192 像素、1600 万像素。Node 在原受管理工作进程内使用可选且固定的 `Pillow==13.0.0` 解码 PNG/JPEG/GIF/WebP，支持无扩展名文件，扩展名与实际格式冲突拒绝。复用路径句柄保护，拒绝越界、硬链接及重解析点；不获取外部 URL。

返回不含 EXIF 等原始元数据的 PNG/JPEG 快照，最多 1024px、8 KiB；必要时有损压缩及继续缩小，保留实际尺寸、原始尺寸和源/快照摘要。透明区域使用白底；GIF/WebP 只取首帧。该有界快照适合初步看图，不能保证细小文字和像素精度；不声称完整原图外发。原文件保持不变。Core 只使用标准库检查快照格式头、尺寸、字节与摘要，不导入图像解码器，也不把 schema 检查称为恶意 Node 的真实性保证。

当前固定 `deepseek-flash` 已明确支持图片输入；官方 [图像理解说明](https://api-docs.deepseek.com/zh-cn/guides/vision/)规定 Chat Completions 的图片放在 user 内容块，单图最多 1024 输入 token。Core 因此在一批原生 tool 响应全部收齐之后，追加带图片的 user 消息，文字 tool 结果仅含元数据，完整快照仍保存在原任务/步骤/交换日志。完整请求的字节准入不变，token 估计为每图保守增加 1024，并计入原费用预留。超限停止且保留证据，不截断 base64、不提高预算或另起调用。

`models.chat_images_enabled=false` 默认不向模型展示 `read_image`，也拒绝图片请求。Node 可选 `images` extra 与 Core 开关独立；使用说明及前端外发提示完成后才能显式启用。没有新增上传 API、远程图床、生成图片服务或新 Provider。普通后续对话继续按原历史投影使用最终文字，不自动重发图片。

## 持久化与兼容性

迁移 021 仅扩展 grants 的工具枚举，逐行保留旧授权，不自动授予新能力；旧 SQL 迁移不改写。升级前沿用一致备份，Core 与 Node 均需升级以识别严格 v1 新判别项。没有替换原任务、费用、取消或对账状态机。

前端不由本轮修改。给 DSH 的具体契约、提示及验收要求见 [前端接入要求](../DSH-BUILTIN-TOOLS-FRONTEND.md)；安装和工具参数见 [使用说明](../dsh-builtin-tools.md)。
