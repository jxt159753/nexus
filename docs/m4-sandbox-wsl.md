# M4 的 WSL2 沙箱部署

Core 保持云端单活，Windows Node 执行原有只读工具；本机 WSL2 只运行独立 Sandbox Node。生成代码始终在 runsc 中执行，不在普通 WSL Python 进程中执行。当前批准和验证结果见 [ROADMAP](ROADMAP.md#m4自定义工具与流程)。

## 已核对的环境

本轮环境为 Ubuntu 22.04.5 / WSL2 Linux 6.18.54.1、Python 3.12.15、runsc `release-20261012.0`。Ubuntu 自带 Python 3.10 未替换；额外 Python 运行环境放在独立目录。runsc 官方发布包及 Python 运行包分别经过官方 SHA-256 校验。宿主依赖为 rootlesskit、uidmap 和 slirp4netns，项目 Python 依赖仍按原锁文件安装。

这台 WSL 上 systrap 连固定 `/bin/true` 都启动失败；没有按失败推断沙箱可用。KVM 模式通过后，用户明确批准给专用 `nexus-sandbox` 用户加入 `kvm` 组。KVM 是本环境明确选择的配置，不自动降级或自动切换运行模式。其他 WSL 内核仍需重新实测。

## 部署边界

- 使用专用无 sudo 权限的系统用户与独立 UID/GID 映射，rootlesskit 新建用户、挂载和网络命名空间。容器内的用户为 65534，无 Linux capabilities。
- 沙箱宿主不保存 Core 数据库或模型密钥。只在 Node 环境提供本节点凭证，启动 runsc 时重新构建环境，不向生成程序传递凭证。
- `[sandbox_nodes]` 显式登记节点 UUID、`runtime='runsc'`、`platform`、`image_digest` 与 `runsc_digest`。Node hello 必须匹配。`image_digest` 覆盖镜像树；`runsc_digest` 用 `runtime_install_digest` 计算，同时覆盖 launcher 和同目录的 `gvisor-bin` 组件。
- 系统服务在宿主 cgroup v2 中强制不超过 1 CPU、512 MiB、64 个任务以及零 swap。Node 启动和每次运行前都读取宿主祖先 cgroup 核对实际限制；不满足即拒绝执行。OCI 内的资源声明不能替代该检查。rootless runsc 显式忽略不能可靠管理的 OCI cgroup，限制由已核对的宿主父 cgroup 强制执行。
- 单次 10 秒，结果 stdout 32 KiB、stderr 8 KiB，固定只读镜像与源码，16 MiB tmpfs；附加 guest 地址空间、文件大小和进程 rlimit。压力可能导致整组被宿主终止；重启后必须按回执对账，不能当作未执行。

配置示例见 [Sandbox TOML](../config/sandbox-node.example.toml)与 [systemd 服务](../config/nexus-sandbox.service.example)。先审阅并替换其中的固定路径、用户、摘要、Core 地址；凭证放在仅管理员可读的 `/etc/nexus/sandbox.env`，不写入仓库或 TOML。生产连接必须使用默认校验开启的 WSS。

镜像须放在只包含可信运行文件的独立 rootfs 中；不能把整个 WSL 根目录、`/mnt/c`、`/mnt/e`、用户家目录或控制 socket 映射进沙箱。生成 Tool 只接受明确传入的有界 JSON。

## 运行与恢复

WSL 的 systemd 服务本身不保证 Windows 会一直保持该发行版运行。启动端需要持有一个前台 `wsl.exe` 会话（窗口可以隐藏），或由用户明确选择的本机启动方式维持。电脑关机、休眠、WSL 退出或网络断开时，云端 Core 仍可聊天和查询历史，生成 Tool 显示离线；Windows Node 与 Sandbox Node 的在线状态分别判断。

测试/业务命令先记入 Core `sandbox_executions`，Node 先保存接收回执再执行；Core 保存结果后才发送 `sandbox.result_ack`。同一执行 ID 不重跑。重连时查询原执行，Node 重发未确认结果；Core 每条记录最多主动查询 64 次，耗尽仍未知则保留记录并阻止新执行，不清空历史。

Node 重启将未结算回执标为未知，按其容器 ID 调用固定 runsc 清理，并查询确认容器已不存在。找回完整结果时沿用原结果；只能确认已停止、找不回结果时保留 `EXECUTION_RESULT_UNKNOWN`，不能标成功。Core/Node 数据库或回执写入失败时停止相关执行。

完整测试使用专用身份、临时目录和受控程序；不得对生产 Core 做逃逸或资源压力测试。用户批准能力版本和批准具体设备动作仍是两回事，目录审批继续有效。

官方依据：[gVisor 安装](https://gvisor.dev/docs/user_guide/install/)、[运行平台](https://gvisor.dev/docs/user_guide/platforms/)、[rootless 模式](https://gvisor.dev/docs/user_guide/rootless/)。实际是否隔离有效以本项目列明的宿主和执行证据为准。

本机部署的隐藏 WSL 保持会话由 Git 忽略的 `var/cloud/Start-Sandbox.ps1` 启动，`Stop-Sandbox.ps1` 只停止该沙箱服务及保存 PID/启动时刻匹配的会话。没有新增 Windows 登录自启动任务；重启电脑后需运行启动脚本。升级镜像或 runsc 安装树前应先停止 Node，重新计算两项摘要并审阅环境变化；旧批准不可沿用。
