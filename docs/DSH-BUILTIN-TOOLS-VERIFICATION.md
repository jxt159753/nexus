# DSH 内置图片与查询工具：本地验证

日期：2026-10-21。当前批准、交付及验收以 [ROADMAP](ROADMAP.md)为准。设计见 [ADR 0026](decisions/0026-dsh-builtin-image-queries.md)，操作见 [使用说明](dsh-builtin-tools.md)，前端交接见 [DSH 接入要求](DSH-BUILTIN-TOOLS-FRONTEND.md)。

## 环境与范围

- Windows、项目 `.venv` Python 3.13.9，已存在的 Pillow 13.0.0；本轮未安装或升级开发环境依赖。Pillow 作为 Node 的可选 `images` extra 精确固定，Core 基础依赖不变。
- DSH 官方 GitHub 固定提交 `687f282e8625c6b8d11ff58211a90524be2e4317`，与本次读取的 master SHA 相同。GitHub 连接器 Transport closed，网页工具无法读取目录 API 后，使用获准的只读 HTTPS 请求缓存固定源码到 Git 忽略的 var。
- 所有数据库、图片、套接字与进程均属于测试临时资源；模型使用合成 Provider 或本地传输，不调用真实供应商。不修改原 Node/Core 配置、身份、用户图片、生产数据库或系统设置。

## 实际验证

| 检查 | 实际结果 |
| --- | --- |
| 初步受限测试 | 硬链接创建被 Windows 沙箱拒绝，异步检查未得到完整结果后中止该受控测试进程；没有记为通过 |
| 首轮沙箱外新增专项 | 26 项通过，1 项测试夹具未先创建 Node 数据目录而失败；补齐测试目录后重跑 |
| 工作、文件、上下文/压缩、存储和历史迁移相关回归 | 114 项通过，40.82 秒 |
| 新增图片、查询和 schema020→021 专项 | 30 项通过，5.56 秒；覆盖真实解码、目录/身份交集、消息配对、请求准入及备份/旧行保留 |
| 协议/契约、Node 生命周期、权限、Harness、配置与 Skill 兼容批 | 372 项通过，1 项仍断言旧工具白名单而失败，60.85 秒；测试预期更新为包含新图片工具 |
| 首帧边界、新增专项与 Node 工具模块复测 | 49 项通过，5.94 秒，包含修正后的旧白名单检查 |
| 最终代码的结果投影、图片、查询、迁移、真实网络及工作/上下文/Skill 回归 | 85 项通过，37.50 秒；普通 JSON 工具中名为 image 的用户字段不会被误删 |
| 独立 Core / Windows Node | 新增真实 HTTP/WS 场景通过：图片解析、派发、工作进程、严格结果、结果 ack、重复 request_id 与 Core 重启；源图删除后仍读取持久结果，1 个 attempt，模型调用为零 |

图片覆盖 PNG/JPEG/GIF/WebP、无扩展名、透明白底、GIF/APNG 首帧、EXIF 方向、噪声图压缩/尺寸、损坏文件、扩展名冲突、4 MiB/8192px 拒绝、路径越界/硬链接拒绝、数据/摘要/尺寸篡改。真实 Windows Job 工作进程与回执重放/重开通过。

合成聊天覆盖同批图片与文本调用：所有 tool 响应在图片 user 块之前配对，原步骤保留完整快照，请求日志包含实际外发体；工具正文只保留图片元数据。图片默认关闭、远程 URL/错误角色拒绝，每图计入保守 1024 token，完整请求仍受 32 KiB 和原费用准入限制。查询覆盖时间偏移、操作记账、任务状态/输出、其他轮次/节点正文拒绝，以及 Skill 说明和依赖检查。

## 构建与限制

离线 wheel 构建首次因沙箱临时目录权限失败；将本次进程的 TEMP/TMP 指向工作区隔离目录后，`pip wheel . --no-deps --no-build-isolation --no-index` 通过。不安装 wheel 或改变运行服务。最终 wheel SHA-256 `6d10f2cae701c6a0160ed0fddb74bf3653c396f8c236b92ebf702574dbbf430d`，172 个包内运行资源与源码逐字节一致，可选 Pillow 元数据检查通过。提取包的 Core 在禁止导入 PIL 的检查中仍能正常导入，未把可选解码器变成 Core 启动依赖。

最终提取包通过显式 `PYTHONPATH` 和 pytest 路径覆盖，使父进程、Core/Node 及工作子进程均从固定包加载，新增图片/查询/迁移及真实网络专项 **32 项通过（9.28 秒）**。最终文档自检覆盖 9 份文档、253 个本地链接、代码围栏、全部 Python 语法、TOML 与唯一 P54 任务行；Git 差异空白检查通过。没有将分批结果相加为一次完整回归全绿。

P54 后端轮次当时没有真实 DeepSeek 图片理解质量、实际前端图片展示/外发提示、Windows App 随包解码器/安装包、Linux 安装组合或 Android 真机证据。真实网络场景是 loopback HTTP/WS，不冒充 TLS/WSS 验证。未部署、提交或推送，不宣称最终用户验收；M7 调度和新后台生命周期未实施。原并行前端/App 修改保留，前端文件未由该后端轮次写入；随后 DSH 前端接入的验证见下节。

## 前端接入补充（2026-10-21，DSH）

本轮按用户直接指令修改现有 Web `/app`、旧控制台和 Windows 随包 UI，未新增后台任务、上传接口、图床、任意 shell 或第二执行器，未改 Core 权限、预算或数据库，未调用真实模型或生产服务。

| 检查 | 实际结果 |
| --- | --- |
| 前端模块静态检查 | `node --check` 覆盖新 `image.js`、`render.js`、`api.js`、`main.js`、旧控制台 `chat-work.js`/`app.js` 及 Windows 副本，全部通过 |
| 隔离浏览器夹具（Edge 155 headless） | **1 项通过**，覆盖 PNG/JPEG 显示、透明 PNG 白底、GIF/WebP 首帧、尺寸/有损/首帧提示、严格模块拒绝 SVG/远程 URL/HTML/不完整 base64、失败/未知不造占位图、技术详情 base64 脱敏、刷新不产生 API 调用、键盘 Enter 预览与 Escape 焦点恢复、旧 Core 无 `presentation` 回退、旧控制台渲染、1440×1000 与 390×844 布局；页面错误为 0 |
| 本轮图片外发同意 | 合成目录含 `read_image` 且 `resultFeedback=true` 时 `prepareSend` 仍返回 `consent`；目录不含图片工具时返回 `send`，证明历史偏好不代替本轮同意 |
| 真实 Core HTTP/API 浏览器 | 独立 Uvicorn + 真实会话/`work_steps` 接口返回 `get_current_time`、`job_list`；浏览器登录 `/app`、打开会话、显示 UTC/固定偏移/经过秒数/任务列表/验证级别，390×844 无水平溢出；页面错误为 0 |
| 真实 Core 旧控制台 | 浏览器登录 `/`，聊天标签显示查询时间/任务列表步骤，无图片占位，页面错误为 0 |
| 静态资源与 CSP | `/static/app/vendor/markdown/purify.es.mjs` 以 `text/javascript` 提供；`/app` CSP 含 `img-src 'self' data:` 与 import map 的 SHA-256；导入映射改为 `/static/app/vendor/`，Web `/app` 可实际加载 Three.js 模块 |
| 随包 Node（重建） | `build-node.ps1` 先断言 Pillow 13.0.0，再以固定 hidden imports 打包；`nexus-node.exe` SHA-256 `CF8353C2B2EE93765A682545C481BC377086273B0F43D2AA6A51AE8B1EC2A325` |
| 随包 Node 真实图片读取 | 独立 Core + 安装后 `installed/binaries/nexus-node.exe`（验证后已卸载），真实 HTTP/WS 派发 `read_image` 成功，`node_report_schema_checked`，120×60 红图解码一致，模型调用 0 |
| Windows 安装包 | NSIS 静默安装到隔离目录成功（exit 0），安装后 Node 与构建产物哈希一致；安装版 WebView2 CDP 确认 `image.js`、`render.js` 图片接入和图片外发提示存在，登录页可见 |
| Python 定向回归 | 图片、查询、工作、presentation、失败、安全与页面共 **91 项通过**（22.82 秒）；修复模块 MIME/import map/CSP 后，前端、图片、查询、工作与安全相关 **83 项再次通过**（16.89 秒） |

安装包：`var/windows/NEXUS_0.1.0_x64-setup-frontend-tools.exe`，31,189,240 字节，SHA-256 `35E3EB1881ED1FD26711517F7EAE02B80616957DFC1043F5B34534832BCA6020`。隔离安装与浏览器证据在 `var/frontend-check/`（构建与临时验证材料，不提交）。

限制：隔离浏览器夹具使用合成 `work_steps`，真实 Core 浏览器只接入只读查询步骤，未在真实 Core 中端到端执行图片步骤；真实 DeepSeek 图片理解质量、生产部署、Android 真机未验证。Windows App 的真实云登录/设备长跑未在本轮重做。未提交或推送。
