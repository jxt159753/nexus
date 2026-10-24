# ADR 0024：Three.js 三维过程视图

- 日期：2026-10-21
- 状态：已采用（Web 与 Windows 随包 UI 已实现，待用户验收）
- 相关：[ADR 0023](0023-windows-app-bundled-node.md)、[架构第 17 节](../ARCHITECTURE.md#17-web-前端与-app-设计边界)

## 背景

用户要求把首页从静态过程占位改为可旋转、缩放、点击的 3D 空间；场景包含 Core、各 Node 和任务脉冲，右侧抽屉显示详情。未来可能接入摄像头/手势，但本批不申请摄像头权限。用户已确认允许状态色、Three.js 和真实数据。

## 决定

1. 使用 Three.js（r180，MIT）随页面/安装包分发，放在 `app/vendor/`，不依赖运行时 CDN。
2. 场景由 Core 能量旋涡/吸积盘、Node 粒子星团、Core-Node 曲线粒子流和任务脉冲组成；不画直线连接，任务颜色来自公开状态，不创建第二套任务状态。
3. 点击对象只在右侧（手机底部）抽屉显示只读详情；场景不写任务、权限或执行结果。
4. 使用本地 `EffectComposer`/`UnrealBloomPass` 做 Bloom 辉光；桌面高画质，移动端降低粒子数、像素比和辉光强度。指针输入支持拖动惯性、滚轮/双指缩放、点击聚焦和双击复位；输入通过可替换的 `input` 桥（rotate/zoom/select）抽象，未来摄像头或手势源可接同一桥，不直接改场景状态。
5. 首页移除中间标题/说明，仅保留角落图例与操作提示；任务/审批/错误仍以 Core 原有卡片和状态为准。
6. 无 WebGL2 时显示明确回退文字；Bloom 初始化失败时退回直接渲染，不伪装能力。

## 影响

- 新增约 750 KiB 的本地 Three.js + Bloom 资源；不新增网络请求。
- 三维视图只读，Core 仍是唯一状态权威；Node、任务和审批动作仍走原接口。
- 本批不接入摄像头、手势识别或权限申请；后续需要在输入桥之外单独评审摄像头隐私、权限和真机验证。
- 首页数据来自 `/api/v1/nodes`、`/api/v1/client/snapshot` 和现有轮询；任务状态变化会触发场景重建，不依赖 `turn.revision`。

## 验证

- `tests/test_app_page.py` 检查 `visual-canvas`、Three.js 本地资源和移除旧中心文案。
- 隔离 Playwright 夹具（源 UI 与 Windows UI 副本）验证 canvas、Core/Node/任务点击抽屉、旋转/缩放交互和 390px 无横向溢出。
- Windows 安装版实际启动：3D canvas 出现、自动登录、Node 自动启动为 `1/1`、Core 点击抽屉可见。
- 未执行真实摄像头/手势、真实设备工具执行或生产部署。
