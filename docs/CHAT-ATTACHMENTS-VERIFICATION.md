# 会话附件：本地后端验证

日期：2026-10-21。对应 P56；批准、交付及验收状态只维护在 [ROADMAP](ROADMAP.md)。用户选择所有文件先可上传，正文解析分批补齐；前端继续由用户直接控制 DSH。

## 实现及环境

Windows / Python 3.13.9、仓库已有 `.venv`，复用已有固定 Pillow 13.0.0；没有安装或升级依赖。迁移 022 增加原子附件 BLOB/预览及轮次关联，支持任意扩展名、空文件、原始字节和 Base64 JSON 上传/下载。单件 16 MiB、合计 256 MiB、1000 条、每轮 4 件；上传/下载共享两条传输槽、各最多 30 秒，图片 helper 单并发/10 秒/32 KiB 输出。

图片/UTF-8 文本有界预览；其他文件只保存原件和提供元数据，不执行或解压。附件明确选择并逐轮预览/同意后才进入模型请求；选定原件及草稿不会因模型输入超限消失，超限不预留模型费用。引用附件的旧轮次不隐式进入后续普通历史或压缩。

使用临时数据库、合成 Provider 和受控本机 Core/Node；真实图片解码使用现有 Pillow。实际网络和进程检查在获准的沙箱外运行，未访问其他程序或生产数据。

## 实际运行

| 检查 | 实际结果 |
| --- | --- |
| 附件与原图片专项（修正初始测试夹具后） | 47 项通过，6.31 秒 |
| 聊天、工作、结果、上下文/压缩/日志、存储和迁移相关回归 | 201 项通过，81.89 秒；随后增加的传输边界另见下两行 |
| 最终源码：附件、媒体进程、022迁移、独立 Core 和图片进程 | 37 项通过，12.76 秒 |
| 最终固定 wheel：上述附件模块、原图片/查询/Node 工具与进程 | 92 项通过，17.46 秒 |
| 禁止导入 PIL 后加载固定包 Core | 通过，Core 主进程不依赖图像解码器导入 |
| 交付静态检查 | 7 份文档、254 个本地链接、124 个 Python 文件语法、示例严格契约、TOML、P56 唯一任务行及差异空白检查通过 |

201 项命令：

```powershell
.venv/Scripts/python.exe -B -m pytest -q tests/test_chat_attachments.py tests/test_image_tools.py tests/test_builtin_queries.py tests/test_chat.py tests/test_chat_work.py tests/test_chat_results.py tests/test_chat_context.py tests/test_chat_compaction.py tests/test_chat_continuity.py tests/test_chat_journal.py tests/test_storage.py tests/test_extension_migration.py tests/test_chat_skill_migration.py tests/test_image_migration.py tests/test_projects_m6.py --basetemp var/pytest-attachments-related --tb=short
```

最终源码命令：

```powershell
.venv/Scripts/python.exe -B -m pytest -q tests/test_chat_attachments.py tests/test_attachment_media.py tests/test_attachment_migration.py tests/test_attachment_processes.py tests/test_image_processes.py --basetemp var/pytest-attachments-transfer-final --tb=short
```

固定包命令（PYTHONPATH 同时指向提取目录，真实子进程沿用相同包）：

```powershell
$env:PYTHONPATH = (Resolve-Path var/attachments-wheel).Path
.venv/Scripts/python.exe -B -m pytest -q -o pythonpath=var/attachments-wheel tests/test_chat_attachments.py tests/test_attachment_media.py tests/test_attachment_migration.py tests/test_attachment_processes.py tests/test_image_tools.py tests/test_image_processes.py tests/test_builtin_queries.py tests/test_node_tools.py tests/test_node_process.py --basetemp var/pytest-attachments-fixed-wheel --tb=short
```

关键证据：

- PNG、ZIP、程序、PDF、中文 UTF-8 和空文件上传；损坏图保留、16 MiB 二进制原件逐字节一致；文本按 UTF-8 边界截断，透明图片白底、实际 image_url 块与持久请求日志一致。
- 未解析文件正文不在模型请求；图片关闭时只有元数据，图片配置改变后旧同意失效；无附件的后续聊天不隐式读取旧私有附件，重新选择可显式复用。
- 并发同 request_id 只有一条原件和审计；内容/会话冲突、跨会话读取、删除已引用附件、归档上传、CSRF/Origin/未登录、实际流超限和非法 Base64 均拒绝；失败不留下半条原件/轮次。
- 4 份选定文本超过完整模型请求限制时，压缩开/关均无模型调用或费用预留；上传原件仍在。下载分块，断开/30秒时限释放槽和大块正文；媒体 helper 超时、取消、输出溢出后真实进程已退出，环境没有测试模型密钥。
- 独立真实 Core HTTP 进程无模型、无 Node：上传超过 2 MiB 的不透明 DOCX 字节，幂等重传、409 冲突、原件/Base64 下载、Core 停止重启后列表与原件保持；模型调用为 0。没有用此证明 Windows 原生桥已支持大文件。
- 从带非空历史的 021 测试库升级 022：一致备份存在，所有旧表/行保持，chat_turns 只追加默认空附件字段，历史未知费用不改写，quick_check/外键检查通过。

初始运行存在测试夹具问题：严格 UUID 构造、TestClient DELETE 参数写法，以及附件隐私边界后普通新轮次可互相承接的预期不正确，修正后 47 项通过。追加并发审计测试曾写成不存在的 action 列，改为实际 kind 列后最终 37 项通过。没有把中间失败描述为首次一次全绿，也未运行或声称当前完整仓库回归全绿。

## 固定本地包

离线 `pip wheel . --no-deps --no-build-isolation --no-index` 构建，临时目录位于 var。wheel 为 `var/attachments-dist/nexus-0.1.0-py3-none-any.whl`，SHA-256：

```text
b33eca82cced0744f7dd9d4b94728f5692b9434477309e2c8143025c3f4e804b
```

182 个包内运行资源与构建时源码逐字节一致，包含迁移022、固定图片 helper 和新附件 API，images extra 仍锁定 Pillow 13.0.0。固定包提取后运行上列 92 项。它是当前共享工作区的验证快照，包含已有并行界面资源，不能直接视为获准的整包生产发布范围。

## 限制与交接

未改前端、Windows Rust 桥或安装包；现有桥有 2 MiB 字符串限制，需 DSH 按[前端工作单](CHAT-ATTACHMENTS-FRONTEND.md)适配。没有实际上传按钮、粘贴/拖放、安装版传输或 Android 真机的本轮证据，没有真实供应商图片理解或文档分析质量证据。PDF/Office/音视频/压缩包内容解析尚未实现，均只能保存/下载及发送明确未解析的元数据。

未部署、提交或推送，未修改生产配置、启用图片模型、发起付费调用或扩展 Node 权限；不宣称整个上传体验或用户验收完成。Linux helper 的 POSIX 限额本轮未在 Linux 实测。已引用附件不提供物理删除，保留期调整另行设计。
