# 交给 DSH：内置图片与查询工具的前端接入要求

用户已明确：今后所有前端修改都先给出要求，由用户直接控制 DSH 实施。Codex 本轮不修改 Web/App 前端，不自动给 DSH 发消息。当前状态只查 [ROADMAP](ROADMAP.md)，后端与限制见 [使用说明](dsh-builtin-tools.md)。

2026-10-21 补充：P55 已由用户在 DSH 中直接实施本要求，覆盖 `/app`、旧控制台和 Windows 随包 UI；实际验证与产物见 [前端接入补充](DSH-BUILTIN-TOOLS-VERIFICATION.md#前端接入补充2026-10-21dsh)。本文保留原始接入要求。

## 可直接发送给 DSH

```text
请在 E:\NEXUS 完成内置工具的前端接入。先完整阅读 AGENTS.md、docs/ARCHITECTURE.md、docs/ROADMAP.md，再阅读 docs/DSH-BUILTIN-TOOLS-FRONTEND.md 和 docs/dsh-builtin-tools.md。保留当前已有 Web、Windows App、Markdown、presentation 和 3D 首页改动。

范围是已有 Web /app、旧控制台和 Windows 随包 UI 的工具结果展示、外发提示与兼容验证。复用现有会话与 work_steps 接口，不新增后台任务、文件上传、图床、任意 shell 或第二执行器；不改 Core 权限、预算或数据库，不自行开启生产模型图片开关。若需要重建随包 Node，按已有打包流程包含固定 Pillow 13.0.0，并把未安装/未配置情况明确显示。

1. 在已有“工作工具/结果外发”发送提示中明确：启用图片工具时，配置工作区内图片的有界快照可能随本轮结果发送给 DeepSeek；范围最多 1024px/8KiB，可能继续缩小或有损，GIF/WebP 只取首帧，透明区域显示白底。沿用 allow_work_results 的本轮明确同意，不能因为历史允许过就默认同意这轮。不开启工作工具/不允许结果外发时不能触发图片工具。后端没有新增图片上传按钮或 API，勿添加无接口的上传入口。

2. read_image 的原生步骤仍显示实际 status/verification/error。成功结果在 work_steps[].result.data.image 中带 media_type、width、height、size_bytes、sha256、data_base64；同一 data 中还有 path、original_width、original_height、downscaled、first_frame_only 与 source_* 字段。使用严格白名单的 data:image/png;base64 或 data:image/jpeg;base64 显示缩略图和可展开预览；核对 base64/字节上限及字段类型，拒绝 SVG、任意远程 URL、HTML 和不完整结果。图片通过 img 显示，所有文字继续转义。不得把 base64 放进聊天 Markdown/JSON 展示或浏览器持久日志；技术详情中可显示摘要和尺寸。

3. 显示“原图尺寸 → 实际提供给模型的尺寸”、缩小/有损及首帧限制；细节不可辨识时不声称模型看到完整原图。data.image.media_type=image/jpeg 表示有损快照。刷新/重连从已有步骤结果恢复预览，不再次调用 read_image 或模型。实际图片缺失、失败/unknown 时只显示真实状态，不造占位成功图。

4. get_current_time 显示 UTC、固定偏移当地时间和经过秒数；job_list 显示有限任务、状态和截断；job_output 显示真实执行/验证及有界输出。它们查询 NEXUS 原任务记录，不是常驻 Shell/后台工作的新管理器。不得给界面添加不存在的启动/杀进程/调度按钮。inspect_capability 的 Skill Markdown 和 steps 在只读详情中显示，不当作 HTML 或权限指令运行。

5. 保留 presentation v1 过程与最终回答的分离、手动折叠状态、取消/恢复语义、旧 Core 回退。旧 Core 未返回图片能力时不展示可用看图功能；允许图片的目录以服务器返回为准，不硬编码用户权限。

验证需覆盖：PNG/JPEG 显示、白底透明图、GIF/WebP 首帧、尺寸/画质提示、非法 base64/SVG/远程 URL 拒绝、失败与未知、没有图片字段的旧 Core、刷新恢复而不重发、390px 布局、键盘预览，以及本轮外发同意。Windows 安装包与随包 Node 需实际验证，未验证的 Android 或真实模型质量单独保留。报告修改文件、命令、实际结果、限制与产物位置；不要自行部署生产或调用付费模型。
```

后端目前以模型函数目录的 `read_image` 是否存在表示本轮可用性；`models.chat_images_enabled` 默认关闭。前端可先用隔离夹具接入，实际开启还需要对应 Core/Node 版本、可选解码器和两端身份授权。该说明不是“前端已完成”的验收记录。
