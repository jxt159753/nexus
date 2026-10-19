# M4 自扩展协议与 Sandbox Node 规格

状态：2026-10-19 首版实现。v2 协议与 Core/Node 代码已实现并完成协议级负向测试；**尚未在专用 Linux/runsc 主机完成真实隔离实测**。本文记录实现接口和启用前检查清单，不把配置写成已验证能力。

## 1. 边界

- Core 继续唯一掌握扩展需求、候选、版本、测试记录、批准、启用、预算和业务状态。
- 旧 `JARVIS Link v1` 和 Windows Node 固定两工具不变。v2 使用独立端点 `/ws/v2/sandbox`，不把候选源码塞入 v1 `tool` 字段。
- 生成代码只在具备 v2 协议、已登记镜像摘要、通过 runsc 预检的 Sandbox Node 中运行。Core、Windows Node 和测试控制器都不 import/exec 候选代码。
- 缺失沙箱时测试状态为 `unavailable`，不创建批准，也不以普通子进程、Windows Job、受限 import 或审计钩子替代。

## 2. 版本与摘要

候选发布 `release` 是规范 JSON，包含：

```json
{
  "schema_version": 1,
  "kind": "tool",
  "capability_id": "user.directory_entry_stats",
  "revision": 1,
  "parent_version_id": null,
  "manifest": {
    "kind": "tool",
    "capability_id": "user.directory_entry_stats",
    "description": "count explicit entries",
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
    "verification_profile": "directory_entry_stats_v1",
    "dependencies": ["python-stdlib"]
  },
  "source": "...",
  "tests": [],
  "environment": {
    "type": "sandbox",
    "runtime": "runsc",
    "image_digest": "<64 hex>",
    "available": true,
    "node_id": "<sandbox node uuid>"
  }
}
```

- `artifact_digest = SHA-256(canonical(release))`；`environment_digest = SHA-256(canonical(environment))`。
- 规范化使用排序键、紧凑分隔符、UTF-8、拒绝 NaN/Infinity 和重复 JSON 键。解析器不接受未知字段。
- 修改 manifest、源码/说明、步骤、测试、依赖、环境或镜像摘要都会产生新的 `artifact_digest`；已批准版本不可原地修改。
- 候选总大小上限 64 KiB；源码 32 KiB、Markdown 16 KiB、输入 16 KiB、结果 32 KiB；每请求候选最多 64 个、总存储 4 MiB（Core 配置可收紧）。
- 声明的 JSON Schema 只允许 `type/properties/required/additionalProperties/items/enum/const/minimum/maximum/minLength/maxLength/minItems/maxItems/title/description`；对象最多 32 字段、深度 6、节点 256，`additionalProperties` 必须为 false，不支持 `$ref`、正则、浮点或远程引用。

## 3. Lifecycle 状态

| 概念 | 取值 |
| --- | --- |
| 请求 | `draft / queued / generating / candidate / failed / cancelled` |
| 测试 | `untested / passed / failed / unavailable` |
| 批准 | `none / approved / rejected` |
| 启用 | `enabled = 0/1`，另有 enablement 历史 |
| Skill run | `running / cancelling / succeeded / failed / cancelled / timed_out` |

批准记录绑定 `artifact_digest`、最新通过测试 `test_id`、`environment_digest`、请求登记的目标节点、资源范围、参数摘要、策略版本和期限（最长 168 小时）。每次启动 Skill、每个后续步骤、Tool 派发前都会重查版本启用、批准期限、测试摘要、环境摘要和节点范围。

## 4. v2 端点与消息

端点：`/ws/v2/sandbox`，只接受已登记为 Node 角色、且出现在 Core `[sandbox_nodes]` 中的身份。浏览器不能连接。

Envelope：

```json
{
  "protocol_version": 2,
  "type": "sandbox.hello",
  "message_id": "<uuid>",
  "correlation_id": null,
  "sent_at": "<UTC RFC3339>",
  "payload": {}
}
```

未知 `protocol_version`、未知 `type`、未知字段、超限帧均拒绝。

| 方向 | 类型 | 关键字段 |
| --- | --- | --- |
| Node → Core | `sandbox.hello` | node_id、boot_id、runtime=`runsc`、runtime_version、image_digest、available、reason、verification_profiles |
| Core → Node | `sandbox.welcome` | session_id、心跳/帧/结果/执行限额 |
| Node → Core | `sandbox.heartbeat` | session_id、active_execution_id |
| Core → Node | `sandbox.execute` | session_id、execution_id、version_id、artifact_digest、environment_digest、release_json、input_json、timeout_seconds |
| Node → Core | `sandbox.result` | execution_id、artifact_digest、outcome=`succeeded/failed/unknown`、output_json 或 error_code、stopped |
| Core → Node | `sandbox.cancel` | execution_id |
| Node → Core | `sandbox.cancelled` | execution_id、stopped |

- Core 只在持久化 Tool 执行记录后发送 `sandbox.execute`；`sandbox.result` 的 attempt/execution、版本摘要和环境摘要必须匹配，否则记为 unknown，不标成功。
- Node 在启动沙箱前持久化接收回执；相同 execution_id 只返回原结果，不重跑；同 ID 不同摘要拒绝。
- 结果由 Core 的可信控制器与固定用例比较；候选自报的 `passed` 不作为通过依据。
- 超时发送取消并保留 unknown；Node 无法确认停止时不得声称已停止。
- 候选源码以 `release_json` 内联传输（≤64 KiB，按摘要核验），不接受模型 URL、压缩包或宿主路径。

## 5. 宿主环境门槛

以下必须在专用 Linux 执行主机上逐项实测，当前仓库没有完成这些实测：

1. 独立执行主机（优先与 Core 不同主机/VM），专用非特权用户；不把生产 Core 宿主改造为沙箱。
2. `runsc` 已安装，版本可核验；镜像 rootfs 的树摘要与 Core 登记 `image_digest` 完全一致；不使用 `latest`。
3. 每次执行新建短生命周期 bundle；固定只读镜像 + 只读 `/work` 候选文件 + 16 MiB tmpfs；不挂载宿主目录、设备或控制 socket，不传 Node/Core 凭证和数据库。
4. `--network=none`、非特权、无 host network/privileged/自定义 OCI 参数；固定 Python 入口，候选不能选择宿主程序。
5. 宿主实际强制 1 vCPU、512 MiB、64 PID、16 MiB 临时空间、单工具 10 秒、整测试 60 秒、stdout 32 KiB；以宿主 cgroup/runsc 证据为准，不把沙箱内打印当成限制证据。
6. 无外部网络、云元数据、宿主文件、环境秘密、Docker/控制 socket；用受控负向程序确认不可达。
7. 超时、取消、输出/PID/内存爆炸、Node 崩溃后，实际执行被终止或明确 unknown；确认旧沙箱停止前不得新建重复执行。
8. 每环境并发 1；Node 写入回执后执行；结果落盘后回传；Core ack 后保留证据。
9. 测试与正式运行使用同一镜像/策略摘要。
10. 在独立测试主机运行，不对生产机做逃逸/破坏性测试。

缺少任一前提时，Tool 测试保持 `unavailable` 或 `failed`，不产生可批准版本，不得以普通 subprocess 代替。

## 6. 当前实现入口

| 组件 | 位置 |
| --- | --- |
| Core 扩展资格/批准/启用 | `src/nexus/core/extensions/service.py` |
| Skill 受控测试与 3 步运行 | `src/nexus/core/extensions/skills.py` |
| Tool 可信测试与执行控制器 | `src/nexus/core/extensions/tools.py` |
| v2 Core 端点 | `src/nexus/core/link/sandbox.py` |
| v2 契约 | `src/nexus/contracts/sandbox.py` |
| Sandbox Node | `src/nexus/sandbox/` |
| 示例配置 | `config/sandbox-node.example.toml` |

`python -m nexus.sandbox --config <file>` 仅在 Linux 主机启动；preflight 失败时会以 `available=false` 登记原因，Core 不会派发执行。Windows 上 `SandboxConfig` 直接拒绝，防止把 Windows 主机误当沙箱。

## 7. 启用检查

在真实主机验证后，owner 才能：

1. 在 Core 配置登记 sandbox node UUID、`runtime='runsc'` 和镜像树摘要；
2. 用该 Node 身份启动 Sandbox Node（`NEXUS_NODE_TOKEN` 仅由环境提供）；
3. 生成 Tool 候选，运行可信测试；
4. 审阅摘要/用例/环境，批准具体版本，再显式启用；
5. 在 Skill 版本中固定引用已批准的 Tool revision。

没有第 1–2 步的真实证据时，只能在文档和测试中使用替身验证拒绝路径；替身不构成 runsc 隔离实测。
