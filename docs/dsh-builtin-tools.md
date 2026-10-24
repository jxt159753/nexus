# DSH 内置图片与查询工具使用说明

功能是否部署及验收只查 [ROADMAP](ROADMAP.md)；设计和未直接移植的插件见 [ADR 0026](decisions/0026-dsh-builtin-image-queries.md)。

## 配置图片读取

只在需要图片的 Windows Node 环境安装固定的可选解码器：

```powershell
& ./.venv/Scripts/python.exe -m pip install -r requirements-images.lock
```

Core 不需要安装 Pillow。两端配置的同一节点 tools/allowed_tools 增加 `filesystem.read_image@1`，继续使用原 `workspace`；owner 和 Node 都需由原离线授权命令登记 `filesystem.read_image` / `workspace`。不自动修改已有身份、目录或生产配置。严格旧 Core/Node 不认识新工具，先升级 Core，再升级 Node。Windows App 的随包 Node 也须重建并包含已锁定解码器，不能只更换页面。

完成 [DSH 前端外发提示与图片展示](DSH-BUILTIN-TOOLS-FRONTEND.md)后，管理员才显式设置：

```toml
[models]
chat_images_enabled = true
```

保持模型/聊天的原启用、密钥、价格和预算配置；不开启这个开关时，原文字聊天继续工作。该开关不是新的付费调用授权，运行时仍需本轮允许工具与结果外发。

## 模型工具

| 函数 | 参数 | 结果与限制 |
| --- | --- | --- |
| `read_image` | `root_id="workspace"`、相对 `path`、可选 `max_dimension`（128–1024，默认 1024） | PNG/JPEG/GIF/WebP 与无扩展名图片，源最多 4 MiB/1600 万像素/8192px；实际快照最多 8 KiB/1024px。可有损和进一步缩小，透明白底、GIF/WebP 首帧 |
| `get_current_time` | 可选 `utc_offset_minutes`（-720–840，默认 0） | Core 的 UTC/指定固定偏移时间与本轮经过秒数；北京时间填 480，不推断浏览器时区或夏令时 |
| `job_list` | 可选 `limit`（1–20，默认 10） | 当前会话、所选设备已有任务的状态与标识，不读正文、不创建后台工作 |
| `job_output` | `task_id`、可选 `max_bytes`（512–8192，默认 4096） | 当前轮次、所选设备任务的有界结果/失败/未知证据；不等待、不重跑、不读取其他轮次正文，图片只返回元数据 |
| `inspect_capability` | 原 `version_id` | 已批准 Tool/Skill 输入契约；Skill 额外包含使用说明和固定步骤，不能授予新权限 |

所有查询仍占用当前轮次原动作和操作额度，不另行调用模型。图片占用原请求字节、token 与费用预算；图片太多或上下文太长时可能超过原限额而停止，已经获得的执行记录仍可查询。文件和网页工具沿用原入口，不增加同名别名。需要历史任务资料时使用原逐轮预览并同意的资料引用。
