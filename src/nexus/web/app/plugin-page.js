"use strict";

/*
 * Owner-facing permission, plugin lifecycle and conversation automation UI.
 * Every state shown here is read from Core. Buttons map one-to-one to the
 * documented endpoints; when the endpoint/capability is absent the control is
 * disabled and explains why instead of pretending a preview is executable.
 */

import {
  ApiError,
  api,
  conversationTitle,
  nodeName,
  state,
  taskDetail,
  uuid,
} from "./api.js";

import {
  closeSheet,
  el,
  notify,
  openSheet,
  ui,
} from "./render.js";

import {
  TEST_KIND,
  TEST_LABELS,
  changeGoal,
  changeSchedule,
  decidePlugin,
  formatShanghai,
  loadAutomation,
  loadConversationPermission,
  loadGlobalPermission,
  loadPluginConversations,
  loadPluginDetail,
  loadPlugins,
  latestGenerationTurn,
  modeLabel,
  originalTime,
  parseRule,
  pluginById,
  pluginState,
  readDeliverable,
  removeSchedule,
  ruleTimeZone,
  safeTextPreview,
  saveBytes,
  saveConversationPermission,
  saveGlobalPermission,
  savePlanMode,
  sourceLabel,
  startPluginTest,
  submitPluginGeneration,
  waitPluginTest,
} from "./plugins.js";
import { uiPluginRow } from "./plugin-ui.js";

const $ = (id) => document.getElementById(id);
let activeTab = "permissions";
let pendingPermission = new Map();
let pendingControl = new Map();
let pluginConversationId = "";
let generationRequest = null;
let generationPoll = null;

function button(label, className, onClick) {
  const node = el("button", className || "", label);
  node.type = "button";
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

function errorText(error) {
  const code = error instanceof ApiError ? error.code : error?.message;
  const labels = {
    IDEMPOTENCY_CONFLICT: "服务端状态已经变化，已重新读取；请核对后再操作",
    INVALID_PERMISSION_MODE: "权限模式无效",
    NO_CONVERSATION: "请先选择会话",
    NO_NODE: "请先选择目标设备",
    PLUGINS_DISABLED: "服务端未启用原生插件",
    PLUGIN_NOT_TESTED: "该版本尚未通过独立测试，不能启用",
    PLUGIN_TEST_ENVIRONMENT_CHANGED: "测试环境已变化，必须重新测试",
    PLUGIN_VERSION_UNAVAILABLE: "该插件版本当前不可用",
    PLUGIN_VERSION_IMMUTABLE: "同一版本号不能覆盖，请提升版本",
    PLUGIN_RELEASE_CHANGED: "版本摘要已经变化，请重新读取",
    PLUGIN_STORAGE_FULL: "插件存储已达到上限",
    PLUGIN_CONTROL_INVALID: "提醒/目标操作参数无效",
    PLUGIN_AUTONOMY_SCOPE_CHANGED: "作用范围、工具版本或权限已变化，不能恢复",
    PLUGIN_AUTONOMY_PAUSED: "该目标或定时任务已暂停",
    PLUGIN_AUTONOMY_AUTHORITY_CHANGED: "权限或取消状态已变化，不能恢复",
    PLUGIN_AUTONOMY_SCOPE_CHANGED: "作用范围已变化，不能恢复",
    PLUGIN_GOAL_LIMIT: "目标已到轮次或期限上限",
    PLUGIN_GOAL_USER_ACTIVITY: "目标期间会话有新活动，续跑已停止",
    PLUGIN_TEST_STORAGE_FULL: "测试记录已满",
    FORBIDDEN: "当前身份无此操作权限",
    AUTH_REQUIRED: "登录会话已过期，请重新登录",
  };
  return labels[code] || error?.message || "操作失败";
}

async function run(control, fn) {
  if (control) control.disabled = true;
  try {
    await fn();
  } catch (error) {
    notify(errorText(error), true);
  } finally {
    if (control?.isConnected) control.disabled = false;
  }
}

function badge(text, kind = "muted") {
  return el("span", `badge ${kind}`, text);
}

function testBadge(status) {
  return badge(TEST_LABELS[status] || status || "未知", TEST_KIND[status] || "muted");
}

function section(title, ...children) {
  const box = el("section", "settings-section plugin-section");
  box.append(el("h2", "", title), ...children);
  return box;
}

function row(...children) {
  const node = el("div", "settings-row");
  node.append(...children);
  return node;
}

function field(labelText, input) {
  const wrap = el("div", "field");
  const label = el("label", "", labelText);
  label.htmlFor = input.id;
  wrap.append(label, input);
  return wrap;
}

function shortDigest(value) {
  return typeof value === "string" && value.length > 20 ? `${value.slice(0, 16)}…` : (value || "—");
}

function requestKey(...parts) {
  return parts.join("|");
}

function stableRequest(map, key) {
  if (!map.has(key)) map.set(key, uuid());
  return map.get(key);
}

function releaseRequest(map, key) {
  map.delete(key);
}

/* ---------------------------------------------------------------- permissions */

export function permissionRevisionText(value) {
  if (!value) return "尚未读取";
  const parts = [`生效：${modeLabel(value.effective_mode || value.mode)}`];
  if (value.source) parts.push(sourceLabel(value.source));
  if (Number.isInteger(value.revision)) parts.push(`revision ${value.revision}`);
  if (value.global_revision !== undefined) parts.push(`全局 revision ${value.global_revision}`);
  return parts.join(" · ");
}

function fullScopeNotice() {
  const box = el("div", "plugin-warning");
  box.append(el("strong", "", "完全模式的作用范围"));
  const list = el("ul");
  list.append(el("li", "", "仅对当前设备上、当前 Windows 用户权限内已获准的能力免去逐次执行确认。"));
  list.append(el("li", "", "身份认证、节点静态能力交集、预算、取消、审计、执行期限和结果核验仍然生效。"));
  list.append(el("li", "", "已派发操作不会被回滚；降权也不会撤销已经发生的效果。"));
  list.append(el("li", "", "这不是安全沙箱，也不扩大工作目录或文件访问范围。"));
  box.append(list);
  return box;
}

function modeChoices(current, { inherited = false, disabled = false } = {}) {
  const wrap = el("div", "choice-row");
  const options = inherited
    ? [["inherit", "跟随全局", "不覆盖全局默认"], ["normal", "普通", "本会话要求逐次确认"], ["full", "完全", "本会话免执行确认"]]
    : [["normal", "普通", "需要时逐次确认"], ["full", "完全", "免执行确认"]];
  const inputs = [];
  for (const [value, label, hint] of options) {
    const item = el("label", "choice" + (value === current ? " on" : ""));
    const input = document.createElement("input");
    input.type = "radio";
    input.name = `permission-${inherited ? "conversation" : "global"}`;
    input.value = value;
    input.checked = value === current;
    input.disabled = disabled;
    item.append(input, document.createTextNode(label));
    item.title = hint;
    input.addEventListener("change", () => {
      for (const node of wrap.querySelectorAll(".choice")) node.classList.remove("on");
      item.classList.add("on");
      wrap.dispatchEvent(new CustomEvent("mode-change", { detail: value }));
    });
    inputs.push(input);
    wrap.append(item);
  }
  wrap.value = () => wrap.querySelector("input:checked")?.value || "";
  return wrap;
}

export function renderGlobalPermission(container, { compact = false } = {}) {
  const sectionNode = el("section", "settings-section plugin-section");
  sectionNode.append(el("h2", "", "全局权限模式"));
  const status = el("p", "hint", "正在读取全局权限…");
  sectionNode.append(status);
  const body = el("div");
  sectionNode.append(body);
  container.append(sectionNode);

  const draw = () => {
    const value = pluginState.globalPermission;
    body.replaceChildren();
    if (!value) {
      status.textContent = "尚未读取。";
      body.append(button("重新读取", "quiet", () => run(null, async () => {
        await loadGlobalPermission();
        draw();
      })));
      return;
    }
    status.textContent = `当前：${modeLabel(value.mode)} · revision ${value.revision}`;
    const choices = modeChoices(value.mode);
    body.append(choices);
    const notice = fullScopeNotice();
    notice.hidden = value.mode !== "full";
    body.append(notice);
    choices.addEventListener("mode-change", (event) => { notice.hidden = event.detail !== "full"; });
    body.append(el("p", "hint", "普通模式需要逐次确认；完全模式表示当前设备用户权限下免执行确认。修改只影响没有单独覆盖的会话；已有覆盖的会话保持原设置。"));
    const save = button("保存全局模式", "primary", () => run(save, async () => {
      const selected = choices.value();
      if (selected !== "normal" && selected !== "full") throw new ApiError(400, "INVALID_PERMISSION_MODE");
      const key = requestKey("global", selected, value.revision);
      try {
        const updated = await saveGlobalPermission(selected, value.revision, stableRequest(pendingPermission, key));
        releaseRequest(pendingPermission, key);
        pluginState.globalPermission = updated;
        await refreshConversationPermissionIfInherited();
        notify(`全局模式已更新为${modeLabel(updated.mode)}，revision ${updated.revision}`);
        if (activeTab === "permissions") await renderPluginPage();
        else draw();
      } catch (error) {
        if (error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT") {
          releaseRequest(pendingPermission, key);
          pluginState.globalPermission = await loadGlobalPermission();
          draw();
          notify(errorText(error), true);
          return;
        }
        throw error;
      }
    }, save));
    body.append(el("div", "actions").appendChild(save).parentElement);
    if (compact) {
      body.append(button("打开权限与插件页", "quiet", () => openPluginPage("permissions")));
    }
  };
  draw();
  return { refresh: draw };
}

async function refreshConversationPermissionIfInherited() {
  const conversationId = state.conversation?.conversation_id;
  if (!conversationId) return;
  try {
    const value = await loadConversationPermission(conversationId);
    if (value.source === "global") {
      pluginState.conversationPermission = value;
    }
  } catch {
    /* the next explicit open reports the real error */
  }
}

export function openConversationPermission() {
  if (!state.conversation) {
    notify("请先选择会话", true);
    return;
  }
  const conversationId = state.conversation.conversation_id;
  const body = el("div", "plugin-sheet");
  const status = el("p", "hint", "正在读取会话权限…");
  body.append(el("p", "scope-label", "会话"), el("p", "", conversationTitle()), status);
  const content = el("div");
  body.append(content);

  const draw = async () => {
    try {
      const value = await loadConversationPermission(conversationId);
      content.replaceChildren();
      status.textContent = `${conversationTitle()} · 全局默认 ${modeLabel(value.global_mode)}（revision ${value.global_revision}）`;
      content.append(el("p", "permission-summary", permissionRevisionText(value)));
      const choices = modeChoices(value.mode === null ? "inherit" : value.mode, { inherited: true });
      content.append(choices);
      const notice = fullScopeNotice();
      notice.hidden = value.effective_mode !== "full";
      content.append(notice);
      choices.addEventListener("mode-change", (event) => {
        notice.hidden = !(event.detail === "full" || event.detail === "inherit" && value.global_mode === "full");
      });
      content.append(el("p", "hint", "选择“跟随全局”会清除会话覆盖。只有无覆盖的会话在全局修改后跟随；有覆盖的会话保持不变。"));
      content.append(el("p", "hint", "这是 owner 的设置操作。模型回复、业务问题回答或执行结果都不能打开完全模式。"));
      const actions = el("div", "actions");
      const save = button("保存会话模式", "primary", () => run(save, async () => {
        const selected = choices.value();
        const mode = selected === "inherit" ? null : selected;
        if (mode !== null && mode !== "normal" && mode !== "full") throw new ApiError(400, "INVALID_PERMISSION_MODE");
        const key = requestKey("conversation", conversationId, String(mode), value.revision);
        try {
          const updated = await saveConversationPermission(conversationId, mode, value.revision, stableRequest(pendingPermission, key));
          releaseRequest(pendingPermission, key);
          pluginState.conversationPermission = updated;
          await draw();
          await ui.refresh(true);
          notify(`会话权限已更新：${permissionRevisionText(updated)}`);
        } catch (error) {
          if (error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT") {
            releaseRequest(pendingPermission, key);
            await draw();
            notify(errorText(error), true);
            return;
          }
          throw error;
        }
      }, save));
      const reload = button("重新读取服务端", "quiet", () => run(reload, draw));
      actions.append(save, reload);
      content.append(actions);
    } catch (error) {
      status.textContent = errorText(error);
      content.replaceChildren(button("重新读取", "quiet", () => run(null, draw)));
    }
  };
  openSheet({
    title: "会话权限模式",
    body,
    actions: [{ label: "关闭", className: "quiet", onClick: closeSheet }],
  });
  draw();
}

function renderConversationPermissionSection(container) {
  const box = el("section", "settings-section plugin-section");
  box.append(el("h2", "", "当前会话权限"));
  if (!state.conversation) {
    box.append(el("p", "", "尚未选择会话。打开一个会话后可以设置本会话覆盖。"));
    container.append(box);
    return;
  }
  const status = el("p", "hint", "正在读取会话权限…");
  box.append(status);
  const value = pluginState.conversationPermissionId === state.conversation.conversation_id
    ? pluginState.conversationPermission : null;
  if (value) {
    status.textContent = permissionRevisionText(value);
  } else {
    loadConversationPermission(state.conversation.conversation_id)
      .then((fresh) => { if (status.isConnected) status.textContent = permissionRevisionText(fresh); })
      .catch((error) => { if (status.isConnected) status.textContent = errorText(error); });
  }
  box.append(el("div", "actions").appendChild(button("设置本会话权限", "quiet", openConversationPermission)).parentElement);
  container.append(box);
}

/* ---------------------------------------------------------------- plugin page */

export function openPluginPage(tab = activeTab) {
  activeTab = tab || "permissions";
  const view = $("plugin-view");
  if (!view) return;
  view.hidden = false;
  $("shell")?.classList.add("plugin-open");
  renderPluginPage();
}

export function closePluginPage() {
  const view = $("plugin-view");
  if (view) view.hidden = true;
  $("shell")?.classList.remove("plugin-open");
}

async function ensurePluginData() {
  const jobs = [loadPlugins()];
  if (!pluginState.conversations.length || Date.now() - pluginState.conversationListAt > 30000) {
    jobs.push(loadPluginConversations());
  }
  jobs.push(loadGlobalPermission());
  await Promise.all(jobs);
  pluginConversationId = pluginConversationId
    || state.conversation?.conversation_id
    || pluginState.conversations[0]?.conversation_id
    || "";
}

function tabButton(id, label) {
  const node = button(label, "plugin-tab" + (activeTab === id ? " active" : ""), () => {
    activeTab = id;
    renderPluginPage();
  });
  node.setAttribute("aria-pressed", String(activeTab === id));
  return node;
}

export async function renderPluginPage() {
  const view = $("plugin-view");
  if (!view || view.hidden) return;
  const tabs = $("plugin-tabs");
  const body = $("plugin-body");
  if (tabs) tabs.replaceChildren(
    tabButton("permissions", "权限"),
    tabButton("plugins", "插件"),
    tabButton("author", "自写插件"),
    tabButton("automation", "目标与提醒"),
  );
  if (!body) return;
  const requestedTab = activeTab;
  try {
    await ensurePluginData();
  } catch (error) {
    body.replaceChildren(el("p", "error", errorText(error)));
    return;
  }
  if (view.hidden || requestedTab !== activeTab) return;
  body.replaceChildren();
  if (activeTab === "permissions") renderPermissionsTab(body);
  else if (activeTab === "plugins") renderPluginsTab(body);
  else if (activeTab === "author") renderAuthorTab(body);
  else renderAutomationTab(body);
}

function renderPermissionsTab(body) {
  renderGlobalPermission(body);
  renderConversationPermissionSection(body);
  body.append(section("说明",
    el("p", "", "全局 normal/full 是未覆盖会话的默认值；会话可以覆盖或恢复继承。修改由已认证 owner 的 HTTP 请求完成，模型工具不提供权限开关。"),
    el("p", "hint", "完全模式不是安全沙箱。它只表示当前设备用户权限下的执行确认被免除，认证、静态能力交集、预算、取消、审计和结果核验仍然保留。"),
  ));
}

function conversationSelect(onChange) {
  const select = el("select");
  select.id = "plugin-conversation";
  const rows = pluginState.conversations;
  if (!rows.length) select.append(new Option("没有可用会话", ""));
  for (const conversation of rows) {
    select.append(new Option(conversation.title || "新会话", conversation.conversation_id));
  }
  if (pluginConversationId && rows.some((item) => item.conversation_id === pluginConversationId)) {
    select.value = pluginConversationId;
  }
  select.addEventListener("change", () => {
    pluginConversationId = select.value;
    onChange?.(pluginConversationId);
  });
  return select;
}

function pluginRow(item, conversationSelectNode) {
  const box = el("article", "plugin-row");
  const head = el("div", "plugin-row-head");
  head.append(el("strong", "", item.name || item.plugin_id), testBadge(item.test_status));
  if (item.enabled) head.append(badge("已启用", "success"));
  if (item.approved) head.append(badge("已批准", item.builtin ? "muted" : "success"));
  if (!item.builtin) head.append(badge("用户生成", "muted"));
  box.append(head);
  const facts = el("p", "plugin-row-meta");
  facts.textContent = `${item.plugin_id} · ${item.version} · ${item.runtime === "core" ? "Core 运行" : item.runtime === "node" ? "Windows Node 运行" : "独立测试/生成"}`;
  box.append(facts);
  const digest = el("p", "plugin-row-digest");
  digest.textContent = `固定摘要 ${item.digest}`;
  box.append(digest);
  const source = el("p", "hint");
  source.textContent = `${item.origin || "未知来源"} · ${item.tools?.length || 0} 个工具${item.builtin ? " · bundled（随应用发布，不是生成插件测试）" : ""}`;
  box.append(source);
  if (item.execution) {
    const execution = el("p", "hint");
    execution.textContent = `执行配置：${item.execution.status === "unconfigured" ? "未配置" : item.execution.status === "node_offline" ? "节点离线" : item.execution.status === "available" ? "可用" : item.execution.status}`;
    box.append(execution);
  }
  const actions = el("div", "actions");
  actions.append(button("查看详情", "quiet", () => openPluginDetail(item.version_id)));
  const target = () => conversationSelectNode?.value || pluginConversationId || state.conversation?.conversation_id;
  if (!item.builtin && item.test_status !== "passed") {
    const test = button(item.test_status === "running" ? "测试进行中…" : "开始独立测试", "quiet", () => run(test, async () => {
      const conversationId = target();
      if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
      const started = await startPluginTest(item.version_id, item.digest, conversationId);
      if (started.status === "unavailable") {
        notify(`测试不可用：${started.reason || "合格沙箱离线"}；未显示成功`, true);
      } else {
        notify("独立测试已开始；页面会读取服务端测试结果");
        await waitPluginTest(item.version_id);
      }
      await loadPlugins();
      renderPluginPage();
    }, test));
    test.disabled = item.test_status === "running";
    actions.append(test);
  }
  if (!item.builtin && item.test_status === "passed" && !item.approved) {
    const approve = button("批准此固定版本", "primary", () => run(approve, async () => {
      const conversationId = target();
      if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
      await decidePlugin(item.version_id, item.digest, "approve", conversationId);
      await loadPlugins();
      renderPluginPage();
      notify("已批准该固定版本；启用是另一个操作");
    }, approve));
    actions.append(approve);
  }
  if (item.approved && !item.enabled) {
    const enable = button(item.builtin ? "启用内置版本" : "启用此版本", "primary", () => run(enable, async () => {
      const conversationId = target();
      if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
      await decidePlugin(item.version_id, item.digest, "enable", conversationId);
      await loadPlugins();
      renderPluginPage();
      notify("Core 已确认启用该版本");
    }, enable));
    actions.append(enable);
  }
  if (item.enabled) {
    const disable = button("停用", "quiet danger", () => run(disable, async () => {
      const conversationId = target();
      if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
      await decidePlugin(item.version_id, item.digest, "disable", conversationId);
      await loadPlugins();
      renderPluginPage();
      notify("已停用；后续新派发不会使用该版本");
    }, disable));
    actions.append(disable);
  }
  const samePlugin = pluginState.list.filter((row) => row.plugin_id === item.plugin_id && row.version_id !== item.version_id);
  const rollback = samePlugin.find((row) => row.approved && !row.enabled && !item.builtin);
  if (rollback && !item.enabled) {
    actions.append(button(`回退到 ${rollback.version}`, "quiet", () => run(null, async () => {
      const conversationId = target();
      if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
      await decidePlugin(rollback.version_id, rollback.digest, "enable", conversationId);
      await loadPlugins();
      renderPluginPage();
      notify(`已切换启用 ${rollback.version}`);
    })));
  }
  if (actions.childElementCount) box.append(actions);
  return box;
}

function renderPluginsTab(body) {
  body.append(section("插件注册表",
    el("p", "", pluginState.enabled ? "统一管理工具插件和自写的可视化插件。" : "服务端插件功能未启用；保留已有版本记录。"),
    el("p", "hint", "可视化插件按所属会话和授权客户端运行；工具插件沿用各自的设备权限。"),
  ));
  const selectWrap = el("div", "plugin-toolbar");
  const select = conversationSelect(() => {});
  selectWrap.append(el("label", "", "操作/测试使用的会话"), select);
  selectWrap.append(button("刷新目录", "quiet", () => run(null, async () => {
    await loadPlugins();
    await loadPluginConversations().catch(() => {});
    renderPluginPage();
  })));
  body.append(selectWrap);
  const list = el("div", "plugin-list");
  if (!pluginState.list.length && !pluginState.uiList.length) {
    list.append(el("p", "hint", pluginState.enabled ? "当前没有已注册的插件版本。" : "插件功能未启用。"));
  }
  for (const item of pluginState.uiList) list.append(uiPluginRow(item, renderPluginPage));
  for (const item of pluginState.list) list.append(pluginRow(item, select));
  body.append(list);
}

async function openPluginDetail(versionId) {
  const body = el("div", "plugin-detail");
  body.append(el("p", "hint", "正在读取固定版本详情…"));
  const actionsNode = el("div", "actions");
  openSheet({
    title: "插件固定版本详情",
    body,
    actions: [{ label: "关闭", className: "quiet", onClick: closeSheet }],
  });
  let detail;
  try {
    detail = await loadPluginDetail(versionId);
  } catch (error) {
    body.replaceChildren(el("p", "error", errorText(error)));
    return;
  }
  const draw = () => {
    body.replaceChildren();
    body.append(el("p", "plugin-title", detail.name || detail.plugin_id), testBadge(detail.test_status));
    const facts = el("dl", "plugin-facts");
    const add = (label, value) => {
      facts.append(el("dt", "", label), el("dd", "", value));
    };
    add("ID", detail.plugin_id);
    add("版本", detail.version);
    add("固定摘要", detail.digest);
    add("来源", detail.origin || "未知");
    add("运行位置", detail.runtime === "core" ? "Core" : detail.runtime === "node" ? "Windows Node" : "生成插件（独立 runsc 测试）");
    add("批准", detail.approved ? "已批准" : "未批准");
    add("启用", detail.enabled ? "已启用" : "未启用");
    add("测试状态", TEST_LABELS[detail.test_status] || detail.test_status);
    if (detail.execution) {
      add("执行配置", detail.execution.status === "unconfigured" ? "unconfigured（未配置）" : detail.execution.status === "node_offline" ? "node_offline（节点离线）" : detail.execution.status === "available" ? "available（可用）" : String(detail.execution.status));
      if (Array.isArray(detail.execution.configured_tools)) add("已配置工具", detail.execution.configured_tools.join(", ") || "无");
    }
    body.append(facts);
    if (detail.builtin) {
      body.append(el("p", "hint", "bundled：这是随应用发布的内置版本，不能显示成“已通过生成插件测试”。"));
    } else {
      body.append(el("p", "hint", "生成的测试只证明声明用例在独立 runsc 环境中匹配，不证明任意业务正确或宿主代码安全。"));
    }
    const test = detail.test;
    if (test && typeof test === "object" && Object.keys(test).length) {
      body.append(el("p", "scope-label", "实际测试记录"));
      if (test.verification) body.append(el("p", "hint", `验证级别：${test.verification}${test.business_verified === false ? " · 业务正确性未验证" : ""}`));
      if (test.release_digest) body.append(el("p", "hint", `测试绑定摘要：${test.release_digest}`));
      if (test.environment) body.append(el("p", "hint", `测试环境：${JSON.stringify(test.environment)}`));
      const cases = Array.isArray(test.cases) ? test.cases : [];
      const list = el("ol", "plugin-cases");
      for (const item of cases) {
        const rowNode = el("li");
        rowNode.textContent = item.matched === true
          ? `用例 ${item.case ?? ""} 匹配`
          : item.matched === false
            ? `用例 ${item.case ?? ""} 不匹配：${JSON.stringify(item.actual ?? "")}`
            : `用例 ${item.case ?? ""}：${item.status || "无结果"}${item.error ? " · " + item.error : ""}`;
        list.append(rowNode);
      }
      if (cases.length) body.append(list);
      if (test.reason) body.append(el("p", "hint", `原因：${test.reason}`));
      body.append(el("p", "hint", "失败、取消、未知、测试不可用都不显示成功。"));
    }
    if (detail.release) {
      const details = el("details", "plugin-release");
      details.append(el("summary", "", "只读包内容（manifest / tests / source）"));
      const pre = el("pre");
      pre.textContent = JSON.stringify(detail.release, null, 2).slice(0, 18000);
      details.append(pre);
      details.append(el("p", "hint", "以上内容只按文本显示，不执行 HTML 或插件提供的前端脚本。"));
      body.append(details);
    }
    actionsNode.replaceChildren();
    const target = () => pluginConversationId || state.conversation?.conversation_id || pluginState.conversations[0]?.conversation_id;
    if (!detail.builtin && detail.test_status !== "passed") {
      const testButton = button(detail.test_status === "running" ? "测试进行中…" : "开始独立测试", "quiet", () => run(testButton, async () => {
        const conversationId = target();
        if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
        const started = await startPluginTest(detail.version_id, detail.digest, conversationId);
        if (started.status === "unavailable") {
          notify(`测试不可用：${started.reason || "合格沙箱离线"}；没有显示成功`, true);
        } else {
          notify("独立测试已开始，等待服务端实际结果");
          detail = await waitPluginTest(detail.version_id);
        }
        await loadPlugins();
        draw();
      }, testButton));
      testButton.disabled = detail.test_status === "running";
      actionsNode.append(testButton);
    }
    if (!detail.builtin && detail.test_status === "passed" && !detail.approved) {
      const approve = button("批准此固定版本", "primary", () => run(approve, async () => {
        const conversationId = target();
        if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
        detail = await decidePlugin(detail.version_id, detail.digest, "approve", conversationId);
        await loadPlugins();
        draw();
        notify("已批准该版本；启用仍需单独操作");
      }, approve));
      actionsNode.append(approve);
    }
    if (detail.approved && !detail.enabled) {
      const enable = button(detail.builtin ? "启用内置版本" : "启用此版本", "primary", () => run(enable, async () => {
        const conversationId = target();
        if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
        detail = await decidePlugin(detail.version_id, detail.digest, "enable", conversationId);
        await loadPlugins();
        draw();
        notify("Core 已确认启用该版本");
      }, enable));
      actionsNode.append(enable);
    }
    if (detail.enabled) {
      const disable = button("停用", "quiet danger", () => run(disable, async () => {
        const conversationId = target();
        if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
        detail = await decidePlugin(detail.version_id, detail.digest, "disable", conversationId);
        await loadPlugins();
        draw();
        notify("已停用该版本");
      }, disable));
      actionsNode.append(disable);
    }
    if (actionsNode.childElementCount) body.append(actionsNode);
  };
  draw();
}

/* ------------------------------------------------------------- self-authored */

function generationText(goal) {
  const device = "由 Core 根据目标自动选择已授权设备";
  return `请为 NEXUS 编写并提交一个自写原生插件。目标：${goal}。目标设备：${device}。要求：使用 user. 命名空间和固定版本；提供 handle(tool,input,context) 源码，并用标准库或 nexus_plugin SDK；为每个工具提供至少一个输入/预期输出契约测试；不要修改 Core 权限、预算或执行模式。提交后由 Core 在独立沙箱测试，测试结果不是业务正确性的证明。`;
}

function renderAuthorTab(body) {
  body.append(section("自写插件生成",
    el("p", "", "这个入口复用当前会话的有界工作请求，不会创建第二套生成器或子代理。先核对实际外发文本和费用范围，再提交。"),
    el("p", "hint", "生成、检查、测试、批准、启用是分开的状态。普通模式只批准通过测试的具体版本；完全模式只在服务端确认测试通过并自动启用后显示可用。"),
  ));
  const wrap = el("section", "settings-section plugin-section");
  const conversation = conversationSelect(() => {});
  const goal = el("textarea");
  goal.id = "author-goal";
  goal.rows = 4;
  goal.maxLength = 1200;
  goal.placeholder = "例如：提供一个把两个整数相加并返回 sum 的插件，输入/输出都有边界检查。";
  wrap.append(field("目标会话", conversation), field("插件目标", goal));
  const preview = el("div", "plugin-preview");
  const submit = button("预览实际外发内容", "primary", () => run(submit, async () => {
    const text = goal.value.trim();
    if (!text) throw new ApiError(400, "EMPTY_MESSAGE");
    if (!pluginConversationId) throw new ApiError(400, "NO_CONVERSATION");
    const catalog = await api("/api/v1/chat/work-catalog");
    const entries = catalog.catalog || [];
    const canInstall = entries.some((item) => (item.route_intent || item.name) === "plugin_install");
    const textValue = generationText(text);
    pluginState.generationPreview = { text: textValue, conversationId: pluginConversationId, canInstall, catalog };
    preview.replaceChildren();
    preview.append(el("p", "scope-label", "将发送给 DeepSeek 的内容"));
    const pre = el("pre");
    pre.textContent = textValue;
    preview.append(pre);
    preview.append(el("p", "scope-label", "目标与范围"));
    const list = el("ul");
    list.append(el("li", "", `会话：${pluginConversationId} · 设备：Core 自动选择`));
    list.append(el("li", "", "本轮输入和本次工作目录摘要；不包含历史记忆、凭证或其他会话。"));
    list.append(el("li", "", "模型生成候选后，Core 自动进行契约检查和独立 runsc 测试；不会在宿主执行未测试代码。"));
    list.append(el("li", "", "费用范围：本轮最多 1 次生成调用的预算边界，仍受每轮 USD 0.05、每日 USD 1 和共享账本约束；实际费用以 Core 账本为准，未知费用不会按 0 计算。"));
    preview.append(list);
    if (!canInstall) {
      const warning = el("div", "plugin-warning");
      warning.append(el("strong", "", "当前不可生成"), el("p", "", "服务端返回的工作目录没有 plugin_install。可能是插件未启用、设备离线或未授予 plugin.call。页面不会用伪入口假装可用。"));
      preview.append(warning);
    }
    const errorLine = el("p", "error");
    errorLine.hidden = true;
    const confirm = button("确认外发并提交生成", "primary", () => run(confirm, async () => {
      errorLine.hidden = true;
      if (!canInstall) throw new ApiError(403, "PLUGIN_GENERATE_UNAVAILABLE");
      try {
        const submitted = await submitPluginGeneration({ conversationId: pluginConversationId, text: textValue });
        generationRequest = { ...submitted, conversationId: pluginConversationId };
        notify("生成请求已提交；等待真实工作轮次和插件注册表状态");
        drawGenerationStatus();
        startGenerationPoll();
      } catch (error) {
        errorLine.textContent = `生成请求未提交：${errorText(error)}。没有把本次操作显示为成功。`;
        errorLine.hidden = false;
        throw error;
      }
    }, confirm));
    preview.append(el("div", "actions").appendChild(confirm).parentElement, errorLine);
  }, submit));
  wrap.append(submit, preview);
  body.append(wrap);
  body.append(generationStatusSection());
}

function generationStatusSection() {
  const box = el("section", "settings-section plugin-section");
  box.id = "generation-status";
  box.append(el("h2", "", "生成与生命周期状态"));
  box.append(el("div", "generation-body"));
  return box;
}

async function drawGenerationStatus() {
  const box = $("generation-status");
  if (!box) return;
  const body = $("plugin-body");
  if (!body || !document.body.contains(box)) {
    renderPluginPage();
    return;
  }
  const target = box.querySelector(".generation-body");
  if (!target) return;
  const conversationId = generationRequest?.conversationId || pluginConversationId;
  if (!conversationId) {
    target.replaceChildren(el("p", "hint", "尚未提交生成请求。"));
    return;
  }
  let turn = null;
  try {
    turn = generationRequest?.request_id
      ? await latestGenerationTurn(conversationId, generationRequest.request_id)
      : null;
  } catch {
    /* registry status is still useful below */
  }
  await loadPlugins().catch(() => {});
  target.replaceChildren();
  const stages = el("div", "generation-stages");
  const candidate = [...pluginState.list]
    .filter((item) => !item.builtin && item.origin === "nexus-generated")
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] || null;
  const turnFailed = turn && ["failed", "stopped", "interrupted"].includes(turn.status);
  const turnDone = turn && turn.status === "completed";
  const stage = (label, state, detail) => {
    const rowNode = el("div", `generation-stage ${state || ""}`.trim());
    rowNode.append(el("strong", "", label));
    rowNode.append(el("span", "", detail || ""));
    stages.append(rowNode);
  };
  stage("1. 生成", turn ? (turnFailed ? "failed" : candidate ? "done" : turnDone ? "empty" : "running") : "waiting",
    turn ? (turnFailed ? `实际状态 ${turn.status}${turn.error_code ? " · " + turn.error_code : ""}` : candidate ? "已出现生成候选" : turnDone ? "本轮已完成，但注册表中没有新生成候选" : `工作状态 ${turn.work_status || turn.status}`) : "尚未提交");
  if (candidate) {
    stage("2. 检查", "done", `服务端已登记固定版本与摘要 ${shortDigest(candidate.digest)}`);
    stage("3. 测试", candidate.test_status === "passed" ? "done" : ["failed", "unavailable", "interrupted"].includes(candidate.test_status) ? "failed" : "running",
      `${TEST_LABELS[candidate.test_status] || candidate.test_status} · 版本 ${candidate.version}`);
    stage("4. 批准", candidate.approved ? "done" : "waiting", candidate.approved ? "服务端记录已批准" : "等待 owner 批准固定版本");
    stage("5. 启用", candidate.enabled ? "done" : "waiting", candidate.enabled ? "服务端记录已启用" : "未启用");
    const list = el("div", "actions");
    list.append(button("查看候选详情", "quiet", () => openPluginDetail(candidate.version_id)));
    if (!candidate.builtin && candidate.test_status !== "passed") {
      list.append(button("开始/继续独立测试", "quiet", () => run(null, async () => {
        const cid = generationRequest?.conversationId || pluginConversationId;
        if (!cid) throw new ApiError(400, "NO_CONVERSATION");
        const started = await startPluginTest(candidate.version_id, candidate.digest, cid);
        if (started.status === "unavailable") notify(`测试不可用：${started.reason || "合格沙箱离线"}`, true);
        else await waitPluginTest(candidate.version_id);
        await loadPlugins();
        drawGenerationStatus();
      })));
    }
    if (candidate.test_status === "passed" && !candidate.approved) {
      list.append(button("批准具体版本", "primary", () => run(null, async () => {
        const cid = generationRequest?.conversationId || pluginConversationId;
        if (!cid) throw new ApiError(400, "NO_CONVERSATION");
        await decidePlugin(candidate.version_id, candidate.digest, "approve", cid);
        await loadPlugins();
        drawGenerationStatus();
      })));
    }
    if (candidate.approved && !candidate.enabled) {
      list.append(button("启用具体版本", "primary", () => run(null, async () => {
        const cid = generationRequest?.conversationId || pluginConversationId;
        if (!cid) throw new ApiError(400, "NO_CONVERSATION");
        await decidePlugin(candidate.version_id, candidate.digest, "enable", cid);
        await loadPlugins();
        drawGenerationStatus();
      })));
    }
    target.append(list);
  } else if (turnFailed) {
    target.append(el("p", "hint", "生成未完成。失败/取消/未知不会显示为成功，也不会自动再调用模型；请回到会话查看实际错误后决定是否重新提交。"));
  }
  target.append(stages);
}

function startGenerationPoll() {
  clearInterval(generationPoll);
  let ticks = 0;
  generationPoll = setInterval(async () => {
    ticks += 1;
    try { await drawGenerationStatus(); } catch { /* keep the last real state */ }
    if (ticks > 120) {
      clearInterval(generationPoll);
      generationPoll = null;
    }
  }, 2500);
}

/* --------------------------------------------------------------- automation */

function goalStatusLabel(status) {
  return { active: "进行中", paused: "已暂停", complete: "已完成", blocked: "受阻" }[status] || status;
}

function scheduleStatus(row, now) {
  if (row.deleted) return "已删除";
  if (row.paused) return "已暂停";
  if (new Date(row.expires_at) <= now) return "已过期";
  if (row.runs >= row.max_runs) return "已达次数上限";
  return "进行中";
}

function scheduleDisplay(row) {
  const rule = parseRule(row);
  const parts = [];
  if (rule?.daily) parts.push(`每日 ${rule.daily}${rule.weekdays ? ` · 周 ${rule.weekdays.join(",")}` : ""}`);
  if (rule?.cron) parts.push(`cron ${rule.cron}`);
  if (rule?.every_seconds) parts.push(`每 ${rule.every_seconds} 秒`);
  if (rule?.after_seconds) parts.push(`延时 ${rule.after_seconds} 秒`);
  if (rule?.at) parts.push(`绝对时间 ${rule.at}`);
  const zone = ruleTimeZone(row);
  return parts.length ? `${parts.join(" · ")} · 原时区 ${zone}` : `规则未提供 · 原时区 ${zone}`;
}

function renderAutomationTab(body) {
  body.append(section("目标与提醒",
    el("p", "", "只展示 Core 中真实存在的计划、目标、提醒和触发事件；页面不自己计时，也不会自动创建替代目标。"),
    el("p", "hint", "所有时间先显示 Asia/Shanghai，并保留服务器原始 UTC/规则时区。默认提醒只写会话事件、不调用模型；execute=true 的定时任务和目标由 Core 有界续跑并共用原费用上限。"),
  ));
  if (!state.conversation) {
    body.append(el("p", "", "请先打开一个会话，再查看该会话的目标与提醒。"));
    return;
  }
  const conversationId = state.conversation.conversation_id;
  const toolbar = el("div", "plugin-toolbar");
  toolbar.append(button("刷新实际状态", "quiet", () => run(null, async () => {
    await loadAutomation(conversationId);
    renderPluginPage();
  })));
  body.append(toolbar);
  const status = el("p", "hint", "正在读取 Core 记录…");
  body.append(status);
  loadAutomation(conversationId).then(() => {
    status.textContent = `会话：${conversationTitle()} · plan revision ${pluginState.automation?.revision ?? "?"}`;
    renderAutomationContent(body, conversationId);
  }).catch((error) => {
    status.textContent = errorText(error);
  });
}

function renderAutomationContent(body, conversationId) {
  const value = pluginState.automation;
  if (!value) return;
  const plan = el("section", "settings-section plugin-section");
  plan.append(el("h2", "", "计划模式"));
  const planText = el("p", "hint", `当前：${value.plan_mode ? "已启用（只读计划）" : "未启用"} · 会话 revision ${value.revision}`);
  const toggle = button(value.plan_mode ? "关闭计划模式" : "启用只读计划模式", "quiet", () => run(toggle, async () => {
    await savePlanMode(conversationId, !value.plan_mode, value.revision);
    notify("计划模式已由 Core 确认更新");
    renderPluginPage();
  }, toggle));
  plan.append(planText, toggle);
  body.append(plan);

  const todos = el("section", "settings-section plugin-section");
  todos.append(el("h2", "", "会话待办"));
  const todoRows = Array.isArray(value.todos) ? value.todos : [];
  if (!todoRows.length) todos.append(el("p", "hint", "当前没有待办记录。"));
  const todoList = el("ul", "plugin-todos");
  for (const item of todoRows) {
    const rowNode = el("li", `todo-${item.status || "pending"}`);
    rowNode.textContent = `${item.status === "completed" ? "✓" : item.status === "in_progress" ? "…" : "○"} ${item.text || item.id}`;
    todoList.append(rowNode);
  }
  if (todoRows.length) todos.append(todoList);
  todos.append(el("p", "hint", "待办状态是计划声明，不等于业务任务已经验证成功。"));
  body.append(todos);

  const goals = el("section", "settings-section plugin-section");
  goals.append(el("h2", "", "持续目标"));
  const goalRows = Array.isArray(value.goals) ? value.goals : [];
  if (!goalRows.length) goals.append(el("p", "hint", "当前没有目标记录。"));
  for (const goal of goalRows) {
    const box = el("article", "plugin-row");
    const head = el("div", "plugin-row-head");
    head.append(el("strong", "", goal.objective), badge(goalStatusLabel(goal.status), goal.status === "active" ? "success" : goal.status === "complete" ? "muted" : "pending"));
    box.append(head);
    box.append(el("p", "hint", `轮次 ${goal.rounds}/${goal.max_rounds} · 截止 ${formatShanghai(goal.expires_at)} · 原始 ${originalTime(goal.expires_at)}`));
    if (goal.completion_verified) box.append(el("p", "hint", "完成声明已记录；具体业务证据仍需查执行记录。"));
    const actions = el("div", "actions");
    if (goal.status !== "complete" && goal.status !== "blocked") {
      const key = requestKey("goal", conversationId, goal.goal_id, goal.status === "active" ? "paused" : "active", goal.status);
      actions.append(button(goal.status === "active" ? "暂停目标" : "恢复目标", "quiet", () => run(null, async () => {
        const next = goal.status === "active" ? "paused" : "active";
        try {
          await changeGoal(conversationId, goal.goal_id, next, stableRequest(pendingControl, key));
          releaseRequest(pendingControl, key);
          notify(next === "active" ? "目标已恢复；服务端会重查范围与预算" : "目标已暂停");
          renderPluginPage();
        } catch (error) {
          if (error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT") releaseRequest(pendingControl, key);
          throw error;
        }
      })));
    }
    if (actions.childElementCount) box.append(actions);
    goals.append(box);
  }
  body.append(goals);

  const schedules = el("section", "settings-section plugin-section");
  schedules.append(el("h2", "", "提醒与定时任务"));
  const scheduleRows = Array.isArray(value.schedules) ? value.schedules : [];
  if (!scheduleRows.length) schedules.append(el("p", "hint", "当前没有提醒或定时任务。"));
  const now = new Date();
  for (const schedule of scheduleRows) {
    const box = el("article", "plugin-row");
    const head = el("div", "plugin-row-head");
    head.append(el("strong", "", schedule.title || "提醒"), badge(scheduleStatus(schedule, now), schedule.paused ? "pending" : "success"));
    box.append(head);
    box.append(el("p", "hint", `${schedule.prompt || ""}`));
    box.append(el("p", "hint", `下次：${formatShanghai(schedule.next_at)} · 原始 ${originalTime(schedule.next_at)}`));
    box.append(el("p", "hint", `次数 ${schedule.runs}/${schedule.max_runs} · 截止 ${formatShanghai(schedule.expires_at)} · 原始 ${originalTime(schedule.expires_at)}`));
    box.append(el("p", "hint", scheduleDisplay(schedule)));
    const actions = el("div", "actions");
    const key = requestKey("schedule", conversationId, schedule.schedule_id, schedule.paused ? "resume" : "pause", schedule.runs);
    actions.append(button(schedule.paused ? "恢复后续触发" : "暂停后续触发", "quiet", () => run(null, async () => {
      try {
        await changeSchedule(conversationId, schedule.schedule_id, { paused: !schedule.paused }, stableRequest(pendingControl, key));
        releaseRequest(pendingControl, key);
        notify(schedule.paused ? "提醒已恢复" : "提醒已暂停；已发生的触发记录保留");
        renderPluginPage();
      } catch (error) {
        if (error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT") releaseRequest(pendingControl, key);
        throw error;
      }
    })));
    actions.append(button("修改标题/说明", "quiet", () => openScheduleEditor(conversationId, schedule)));
    actions.append(button("删除后续派发", "quiet danger", () => run(null, async () => {
      const deleteKey = requestKey("schedule-delete", conversationId, schedule.schedule_id);
      try {
        await removeSchedule(conversationId, schedule.schedule_id, stableRequest(pendingControl, deleteKey));
        releaseRequest(pendingControl, deleteKey);
        notify("已停止后续派发；触发记录仍保留");
        renderPluginPage();
      } catch (error) {
        if (error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT") releaseRequest(pendingControl, deleteKey);
        throw error;
      }
    })));
    box.append(actions);
    schedules.append(box);
  }
  body.append(schedules);

  const events = el("section", "settings-section plugin-section");
  events.append(el("h2", "", "触发事件"));
  const eventRows = Array.isArray(value.events) ? value.events : [];
  if (!eventRows.length) events.append(el("p", "hint", "当前没有触发事件。"));
  const eventList = el("ul", "plugin-events");
  for (const event of eventRows.slice(0, 50)) {
    const item = el("li");
    item.textContent = `${event.title || "提醒"} · ${event.status === "missed" ? "已错过" : "已到时间"} · 计划 ${formatShanghai(event.due_at)} · 记录 ${formatShanghai(event.created_at)} · 原始 ${originalTime(event.due_at)}`;
    eventList.append(item);
  }
  if (eventRows.length) events.append(eventList);
  events.append(el("p", "hint", "事件来自 Core 的持久记录，不依赖页面计时。"));
  body.append(events);
}

function openScheduleEditor(conversationId, schedule) {
  const body = el("div");
  const title = el("input");
  title.maxLength = 100;
  title.value = schedule.title || "";
  const prompt = el("textarea");
  prompt.rows = 3;
  prompt.maxLength = 2000;
  prompt.value = schedule.prompt || "";
  body.append(field("标题", title), field("提醒说明", prompt));
  body.append(el("p", "hint", "修改后仍只在原范围内触发；恢复时若范围、预算或期限不再匹配，Core 会返回真实错误，页面不会自动创建替代目标。"));
  openSheet({
    title: "修改提醒",
    body,
    actions: [
      { label: "取消", className: "quiet", onClick: closeSheet },
      { label: "保存到 Core", className: "primary", onClick: () => run(null, async () => {
        const changes = {};
        if (title.value !== schedule.title) changes.title = title.value;
        if (prompt.value !== schedule.prompt) changes.prompt = prompt.value;
        if (!Object.keys(changes).length) {
          notify("没有需要保存的修改");
          return;
        }
        const key = requestKey("schedule-edit", conversationId, schedule.schedule_id, JSON.stringify(changes));
        try {
          await changeSchedule(conversationId, schedule.schedule_id, changes, stableRequest(pendingControl, key));
          releaseRequest(pendingControl, key);
          closeSheet();
          notify("提醒修改已由 Core 确认");
          renderPluginPage();
        } catch (error) {
          if (error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT") releaseRequest(pendingControl, key);
          throw error;
        }
      }) },
    ],
  });
}
