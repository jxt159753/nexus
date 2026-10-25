import { api } from "./api.js";
import { el, button, openSheet, closeSheet, notify, errorText } from "./render.js";
import { formatShanghai } from "./plugins.js";

const statuses = { built: "等待测试", testing: "测试中", failed: "测试失败", interrupted: "已中断", observing: "等待客户端加载", accepted: "已验证", rolled_back: "已回退", disabled: "已停用" };
const events = { history_snapshot: "历史版本记录（升级前快照）", built: "构建版本", published: "测试通过，发布版本", test_failed: "测试失败，保留旧版", ready: "客户端加载确认", rolled_back: "运行失败，自动回退", enabled: "手动启用／切换版本", disabled: "停用插件", policy_enabled: "允许开发升级" };
const route = id => `/api/v1/ui-extensions/conversations/${id}`;
const statusName = value => statuses[value] || value;

export function uiPluginRow(item, refresh) {
  const row = el("article", "plugin-row"); row.dataset.uiPlugin = item.conversation_id;
  const head = el("div", "plugin-row-head");
  head.append(el("strong", "", item.name), el("span", "badge muted", "自写 · 可视化插件"),
    el("span", `badge ${item.enabled ? "success" : "muted"}`, item.enabled ? "已启用" : "未启用"));
  row.append(head, el("p", "plugin-row-meta", `${item.plugin_id} · 当前 ${item.active?.version || "无"} · 最新 ${item.latest.version}（${statusName(item.latest.status)}）`),
    el("p", "hint", `所属会话：${item.conversation_title || "未命名会话"}${item.archived ? "（已归档）" : ""}`));
  const actions = el("div", "actions");
  actions.append(button("版本、启停与历史", "quiet", () => openUIPlugin(item, refresh)));
  row.append(actions);
  return row;
}

async function openUIPlugin(item, refresh) {
  const body = el("div", "plugin-detail");
  openSheet({ title: "可视化插件管理", body, actions: [{ label: "关闭", className: "quiet", onClick: closeSheet }] });
  let detail, pollTimer;
  const read = query => api(route(item.conversation_id) + "/management" + (query || ""));
  async function reload() {
    clearTimeout(pollTimer);
    try {
      detail = await read();
      if (!body.isConnected) return;
      draw();
      // A version switch is only ready after the original client reports it.
      if (detail.active && detail.client?.online && detail.policy.enabled &&
          detail.active.runtime?.deployment_revision !== detail.policy.deployment_revision) {
        pollTimer = setTimeout(() => { if (body.isConnected) reload(); }, 2000);
      }
    } catch (error) { if (body.isConnected) body.replaceChildren(el("p", "error", errorText(error))); }
  }
  async function decide(version, action, control) {
    control.disabled = true;
    try {
      await api(route(item.conversation_id) + "/decision", { method: "POST", body: JSON.stringify({
        action, version_id: version.version_id, bundle_digest: version.bundle_digest,
        expected_revision: detail.policy.revision, expected_deployment_revision: detail.policy.deployment_revision,
      }) });
      await refresh(); await reload();
      notify(action === "disable" ? "已停用插件，源码与历史保留" : "已启用所选版本，等待原客户端加载；摄像头保持关闭");
    } catch (error) { notify(errorText(error), true); await reload(); }
    finally { if (control.isConnected) control.disabled = false; }
  }
  function draw() {
    body.replaceChildren();
    const policy = detail.policy, active = detail.active;
    const ready = active?.runtime?.status === "ready" && active.runtime.deployment_revision === policy?.deployment_revision;
    body.append(el("h3", "", item.name), el("p", "hint", `所属会话：${detail.conversation.title || "未命名会话"}`),
      el("p", "", active && policy.enabled ? `当前版本 ${active.version} · ${ready ? "客户端已加载" : "已启用，等待客户端加载"}` : "当前未启用"),
      el("p", "hint", `原授权客户端：${detail.client?.platform === "windows" ? "Windows" : "网页"} · ${detail.client?.online ? "在线" : "离线"}`),
      el("p", "hint", "仅在所属会话和原授权客户端运行。停用会停止运行并撤销本会话的开发升级授权；重新启用恢复该授权。摄像头始终由你手动开启。"));
    const actions = el("div", "actions");
    if (policy?.enabled && detail.versions.length) {
      const stop = button("停用插件", "quiet danger", () => decide(active || detail.versions[0], "disable", stop)); actions.append(stop);
    }
    actions.append(button("刷新状态", "quiet", reload)); body.append(actions);
    if (detail.conversation.archived) body.append(el("p", "hint", "会话已归档，保留历史，不能启用。"));
    body.append(el("h3", "", "版本与验证"));
    const versions = el("div", "plugin-list");
    for (const version of detail.versions) {
      const row = el("article", "plugin-row");
      row.append(el("strong", "", `${version.version} · ${statusName(version.status)}${active?.version_id === version.version_id ? " · 当前版本" : ""}`),
        el("p", "hint", `创建于 ${formatShanghai(version.created_at)} · 独立测试${version.test.passed === true ? "通过" : version.test.passed === false ? "失败" : "尚无结果"}`),
        el("p", "plugin-row-digest", `固定摘要 ${version.bundle_digest}`));
      const baseline = detail.versions.find(v => v.version_id === version.baseline_id);
      if (version.baseline_id) row.append(el("p", "hint", `升级基线：${baseline?.version || version.baseline_id}`));
      if (version.runtime?.restored_version_id) {
        const restored = detail.versions.find(v => v.version_id === version.runtime.restored_version_id);
        row.append(el("p", "hint", `已回退至 ${restored?.version || version.runtime.restored_version_id}`));
      }
      const reason = version.runtime?.reason || version.runtime?.error || version.test.error;
      if (reason) row.append(el("p", "hint", `原因：${reason}`));
      if (version.status === "accepted" && version.test.passed && policy?.plugin_id === version.plugin_id &&
          (!policy.enabled || active?.version_id !== version.version_id)) {
        const enable = button(active ? `切换到 ${version.version}` : `启用 ${version.version}`, "primary", () => decide(version, "enable", enable));
        enable.disabled = !detail.client?.online || detail.conversation.archived;
        row.append(enable);
      }
      versions.append(row);
    }
    body.append(versions, el("p", "hint", "独立测试及加载记录不等于真人摄像头体验验收。失败或已回退的版本不能直接启用。"));
    if (detail.next_version_before) body.append(button("更多版本", "quiet", async () => {
      try { const page = await read(`?before_version=${detail.next_version_before}`); detail.versions.push(...page.versions); detail.next_version_before = page.next_version_before; draw(); }
      catch (error) { notify(errorText(error), true); }
    }));
    body.append(el("h3", "", "升级、回退与启停记录"));
    const history = el("ol", "plugin-cases");
    for (const event of detail.events) {
      let text = `${formatShanghai(event.created_at)} · ${events[event.kind] || event.kind}`;
      if (event.from_version || event.to_version) text += ` · ${event.from_version || "无"} → ${event.to_version || "停用"}`;
      if (event.reason) text += ` · ${event.kind === "history_snapshot" ? statusName(event.reason) : event.reason}`;
      history.append(el("li", "", text));
    }
    body.append(history);
    if (detail.events.some(event => event.kind === "history_snapshot")) body.append(el("p", "hint", "升级前仅有版本快照；其时间是原记录更新时间，不伪作每次操作发生时间。"));
    if (detail.next_event_before) body.append(button("更多记录", "quiet", async () => {
      try { const page = await read(`?before_event=${detail.next_event_before}`); detail.events.push(...page.events); detail.next_event_before = page.next_event_before; draw(); }
      catch (error) { notify(errorText(error), true); }
    }));
  }
  await reload();
}
