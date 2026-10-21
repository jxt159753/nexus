# 文件工具、会话交互与配置目标

功能交付和是否上线以 [ROADMAP](ROADMAP.md) 为准。技术边界见 [ADR 0021](decisions/0021-dsh-tools-session-journal.md)，原预算和工作区使用见 [聊天工作](chat-work.md)。

## 文件和项目

新 Core/Node 一起升级后，在双方工具配置及 owner/Node 持久授权中分别增加 `filesystem.read_lines/glob/grep/edit_preview/edit@1`。新增工具不会由升级自动授予；只有已有交集目录中的函数才提供给模型。旧文件工具继续可用。

可以提出“先找到相关 Python 文件，搜索某段文字，读取对应行，再精确修改”。read 返回完整文件摘要，edit 必须带这个摘要，文件变化就重新读取；仍保留内部真实差异及读回。grep 是字面搜索，结果截断或跳过文件会明确展示。每轮最多 4 个动作，超限需后续请求，不能假装未读取的数据已检查。

项目验证使用预先配置的固定 command_id，例如固定的编译或测试模板；没有任意 shell 或模型参数。非零退出码在工作步骤中是失败，具体输出保留；退出码零也不证明全部用户目标完成。

## 会话

- 工作过程中，在输入方式中选择“纠正当前任务”，发送后在当前已派发操作收敛的下一步生效；未派发的旧动作不会继续，原调用及未执行事实保留。纠正不能增加权限。
- 选择“后续任务排队”可以先发送后续目标，最多 5 条等待；每条按发送时的同意、上下文和五分钟期限处理。可以单独取消，刷新不会重发。
- “查看逐次请求、回复与中断记录”显示每一次实际请求体、摘要、回复/调用及中断，不含认证头。升级前没有保存的交换不会伪造出来。
- 更早普通对话用含来源轮次 ID 的摘录压缩；全部原文仍可查。需要别的会话资料时，在历史资料栏填写 1–3 个轮次 ID，先预览，再明确勾选本轮外发；只允许相同项目/owner、来源仍有效且无递归引用的记录。
- 重启后出现待核对提示，先查看任务与结果，再点“核对后继续”。未知费用、未结束尝试、失效同意或过期范围会拒绝继续；停止仍可用。不能点击重跑来规避未知执行。

`models.chat_input_bytes` 可按容量降低到 8192–32768；计算包括完整 system、工具 schema 和消息。超限在付费预留前拒绝，当前输入和选定资料不会被静默削短。这是字节保护，不是精确 token 计数。

## Edge/Chrome 和已配置程序

这是**专用目标配置**，不是接管任意日常浏览器或全桌面。以下 TOML 片段加入已有 Windows Node 配置；保留原 node_id、Core 连接、data_dir、roots 与身份。不要粘贴占位摘要后直接启动。

```toml
[interaction_targets.docs-page]
kind = 'edge' # 或 chrome
endpoint = 'http://127.0.0.1:9222'
allowed_origins = ['https://example.com']

[interaction_targets.docs-page.actions.home]
kind = 'navigate'
description = '打开已配置首页'
url = 'https://example.com/'

[interaction_targets.docs-page.actions.filter]
kind = 'fill'
description = '填写已配置筛选框'
selector = '#filter'
```

浏览器须预先用独立 profile 启动本机 CDP，端口仅在回环地址监听；专用窗口保持恰好一个允许 origin 的页面。选择器必须由配置者核对实际页面，示例不是 example.com 已存在的控件。Node 不启动或关闭日常浏览器；不配置密码、上传、支付、删除、外发消息或安全设置动作。允许 origin 不限制页面本身的全部网络访问。

已运行的 Windows 应用片段：

```toml
[interaction_targets.editor]
kind = 'windows_app'
executable = 'C:\Path\To\ConfiguredEditor.exe'
executable_sha256 = '<核对实际程序后的64位十六进制SHA256>'

[interaction_targets.editor.actions.edit]
kind = 'set_value'
description = '修改已配置编辑控件'
automation_id = 'configured-editor-input'
```

要求恰好一个匹配路径和摘要的运行实例、唯一且不含密码的 AutomationId，程序与 Node 位于允许 UI Automation 的同一登录桌面；受保护/提权应用及未提供稳定控件的程序可能不可用。首批支持 focus/invoke/set_value，不提供任意坐标键鼠、脚本或自动程序启动。

完成配置后可在不启动 Node、不读取令牌的情况下检查摘要：

```powershell
.venv\Scripts\python.exe -m nexus.node --config 'E:\NEXUS\config\windows-node.toml' --print-policy-digests
```

将输出中的 `interaction_digests` 加入原 Core 的对应 `[nodes.'实际-node-uuid'.interaction_digests]`，双方工具列表和持久授权增加 `interaction.observe@1`、`interaction.act@1`；仍需 workspace 资源别名。Core 只接受双方相同摘要。任何目标、动作、路径、摘要或 origin 修改都要重新核对配置，旧任务范围不复用。

对话会先 observe 得到状态和动作列表，act 带观察摘要；页面/控件变化则重新观察。默认文件权限不自动授权界面操作，首次目标操作会要求本轮范围确认；已授权的固定目标内连续执行。动作返回和界面观察是实际证据，不能直接等同于保存成功或业务成功。停止会阻止下一步并停止 Node 操作桥；已经发出的点击/导航或应用业务效果无法回滚。
