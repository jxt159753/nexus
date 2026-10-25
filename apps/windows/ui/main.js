"use strict";

import {
  ApiError,
  api,
  TOOL_LABELS,
  activeTurnOfConversation,
  archiveConversation,
  clearPrefs,
  createConversation,
  decideApproval,
  loadControl,
  loadConversations,
  loadCore,
  loadHistory,
  loadNodes,
  loadProjects,
  loadSnapshot,
  loadUsage,
  loadPrefs,
  login,
  logout,
  nodeName,
  pendingApprovals,
  pendingPermissions,
  prepareSend,
  projectName,
  renameConversation,
  resetState,
  sendPayload,
  selectConversation,
  setPref,
  setUnauthorizedHandler,
  state,
  stopTurn,
  taskDetail,
} from "./api.js";

import {
  bindUI,
  button,
  closeSheet,
  el,
  errorText,
  formatTime,
  notify,
  openSheet,
  refreshComposerState,
  renderChat,
  renderDock,
  renderSettings,
  renderDevices,
  renderConversations,
  runAction,
  ui,
} from "./render.js";

import { initVisualization } from "./visualization.js";
import { initUIExtensions } from "./ui-extensions.js";
import { initVoice, stopVoice, renderVoiceSettings } from "./voice.js";
import {
  closePluginPage,
  openConversationPermission,
  openPluginPage,
} from "./plugin-page.js";
import { loadConversationPermission, pluginState } from "./plugins.js";
import {
  attachmentBlockReason,
  attachmentBlocksSend,
  attachmentState,
  bindAttachmentUI,
  draftAttachments,
  clearAttachments,
  ensureAttachments,
  initAttachmentComposer,
  leaveConversation,
  openAttachmentConsentSheet,
  previewAttachments,
  resetAttachments,
  withAttachmentConsent,
} from "./attachments.js";

const $ = (id) => document.getElementById(id);
const native = window.__NEXUS_NATIVE__ || null;
const CORE_URL_KEY = "nexus.coreUrl";
let pollTimer = null;
let activityTimer = null;
let activityRefreshing = false;
let deviceTimer = null;
let deviceLoading = false;
let lastSignature = "";
let refreshing = false;
let booting = true;
let visualization = null;
let uiExtensions = null;
const conversationDrafts = new Map();
const consumedNavigation = new Set();
let switchingConversation = false;

function saveConversationDraft() {
  conversationDrafts.set(state.conversation?.conversation_id || "", [$("chat-text").value, $("dock-text").value]);
}

async function openConversationSource(id, turnId = null) {
  if (switchingConversation) return;
  switchingConversation = true;
  try {
    if (id !== state.conversation?.conversation_id) {
      if (draftAttachments().length) throw new Error("当前有未发送附件，请先发送或移除，再打开来源会话。文字草稿会保留。");
      saveConversationDraft();
      await stopVoice();
      const selected = await selectConversation(id);
      if (!selected) return;
      leaveConversation();
      const draft = conversationDrafts.get(id) || ["", ""];
      [$("chat-text"), $("dock-text")].forEach((input, index) => { input.value = draft[index]; input.style.height = ""; });
    }
    if (turnId && !state.turns.has(turnId)) {
      const value = await api(`/api/v1/chat/turns/${turnId}`);
      const turn = value.turn || value;
      if (turn.conversation_id === id) {
        state.turns.set(turn.turn_id, turn);
        state.turnOrder = [...state.turns.values()].sort((a,b) => a.cursor - b.cursor);
      }
    }
    await syncConversationPermission();
    openChat();
    if (turnId) scrollToTurn(turnId);
  } finally { switchingConversation = false; }
}

async function followConversationNavigation() {
  const turn = state.turnOrder.find(item => item.request_id === state.lastSent?.request_id && item.presentation?.phase === "completed" && item.presentation?.navigation);
  const navigation = turn?.presentation.navigation;
  if (!navigation || consumedNavigation.has(navigation.step_id) || switchingConversation) return;
  consumedNavigation.add(navigation.step_id);
  try { await openConversationSource(navigation.conversation_id); }
  catch (error) { notify(errorText(error), true); }
}
let nodeAutoStarted = false;

function normalizeCoreUrl(value) {
  const url = new URL(value.trim());
  if (url.username || url.password || url.search || url.hash) throw new Error("Core 地址不能包含凭证、查询或片段");
  const loopback = ["127.0.0.1", "localhost", "::1", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) {
    throw new Error("Core 必须使用 HTTPS；本机调试允许 http://127.0.0.1");
  }
  if (url.pathname !== "/" && url.pathname !== "") throw new Error("Core 地址只填写站点根地址");
  return url.origin;
}

function showCoreField() {
  if (!native) return;
  $("core-field").hidden = false;
  $("remember-field").hidden = false;
  $("core-url").value = localStorage.getItem(CORE_URL_KEY) || "";
}

async function prepareNativeCore() {
  if (!native) return true;
  showCoreField();
  const saved = localStorage.getItem(CORE_URL_KEY);
  if (!saved) return false;
  await native.setCoreUrl(saved);
  return true;
}

function setConnection(text, kind = "") {
  const node = $("connection");
  if (!node) return;
  node.textContent = text;
  node.className = "connection" + (kind ? " " + kind : "");
}

function updateConnection() {
  if (!state.ready) {
    setConnection("连接中");
    return;
  }
  if (!state.nodesFresh) {
    setConnection("正在同步设备", "offline");
    return;
  }
  const online = state.nodes.filter((node) => node.online).length;
  const suffix = state.control?.paused ? " · 已暂停派发" : "";
  setConnection(`${online} 台设备在线${suffix}`, "live");
}

function showLogin(message = "") {
  conversationDrafts.clear(); consumedNavigation.clear();
  clearTimeout(pollTimer);
  pollTimer = null;
  clearTimeout(activityTimer);
  activityTimer = null;
  clearTimeout(deviceTimer);
  resetState();
  resetAttachments();
  uiExtensions?.stop();
  visualization?.visible(false);
  uiExtensions?.sync();
  $("shell").hidden = true;
  $("login").hidden = false;
  $("login-form").reset();
  showCoreField();
  const error = $("login-error");
  error.hidden = !message;
  error.textContent = message;
  if (native && !localStorage.getItem(CORE_URL_KEY)) $("core-url").focus();
  else $("token").focus();
}

async function startNodeIfConfigured() {
  if (!native || nodeAutoStarted) return;
  nodeAutoStarted = true;
  try {
    const status = await native.nodeStatus();
    if (status.running) return;
    const saved = await native.loadNode();
    if (!saved?.token) return;
    if (state.nodes.some((node) => node.node_id === saved.node_id && node.online)) {
      notify("检测到同一 Node 身份已在线（可能是独立启动器）；App 未重复启动，避免身份重复或能力降级", true);
      return;
    }
    const value = await native.nodeStart({
      core_ws_url: saved.core_ws_url,
      node_id: saved.node_id,
      node_token: saved.token,
      workspace_root: saved.workspace_root,
    });
    if (!value.running) {
      notify("本机 Node 自动启动失败，请在设置中检查配置", true);
      return;
    }
    for (let attempt = 0; attempt < 4; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      try {
        await loadNodes();
      } catch {
        /* Core may still be waking up. */
      }
      if (state.nodes.some((node) => node.online)) break;
    }
    updateConnection();
    renderDock();
    syncVisualization(true);
    if (!$("settings").hidden) renderSettings();
  } catch (error) {
    notify(`本机 Node 自动启动失败：${errorText(error)}`, true);
  }
}

function syncVisualization(visible = state.view === "home") {
  const canvas = $("visual-canvas");
  if (!canvas) return;
  if (!visualization) visualization = initVisualization(canvas);
  if (!visualization) return;
  const tasks = new Map();
  for (const [id, value] of state.tasks) tasks.set(id, value?.task || value);
  visualization.update({
    nodes: state.nodes,
    tasks,
    activeTurn: state.activeTurn,
    activeTurns: state.activity?.turns,
    nodesFresh: state.nodesFresh,
    activityFresh: state.activityFresh,
    nodeId: state.lastSent?.client_node_id || "",
    messageKey: state.lastSent?.request_id || "",
    paused: !!state.control?.paused,
  });
  visualization.visible(!!visible);
  uiExtensions?.sync();
}

function enterApp() {
  $("login").hidden = true;
  $("shell").hidden = false;
  state.view = "home";
  $("home-view").hidden = false;
  $("chat-view").hidden = true;
  updateConnection();
  renderDock();
  if (state.conversation) {
    lastSignature = state.signature;
  }
  startPolling();
  syncVisualization(true);
  startNodeIfConfigured();
}

async function bootstrap() {
  loadPrefs();
  if (native) {
    let ready = false;
    try {
      ready = await prepareNativeCore();
    } catch (error) {
      booting = false;
      showLogin(error.message);
      return;
    }
    if (!ready) {
      booting = false;
      showLogin();
      return;
    }
    try {
      if (await native.loadSession()) {
        try {
          await loadCore();
          await ensurePendingDetails();
          booting = false;
          enterApp();
          return;
        } catch (error) {
          if (!(error instanceof ApiError) || (error.status !== 401 && error.status !== 403)) {
            booting = false;
            showLogin(errorText(error));
            return;
          }
          await native.forgetSession().catch(() => {});
        }
      }
      const token = await native.loadOwner(localStorage.getItem(CORE_URL_KEY));
      if (token) {
        try {
          await login(token);
          await native.rememberSession().catch(() => {});
        } catch (error) {
          if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
            await native.forgetOwner().catch(() => {});
          }
          throw error;
        }
      }
    } catch (error) {
      booting = false;
      showLogin(errorText(error));
      return;
    }
  }
  try {
    await loadCore();
    await ensurePendingDetails();
    await syncConversationPermission();
    booting = false;
    enterApp();
  } catch (error) {
    booting = false;
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      showLogin();
    } else {
      showLogin(errorText(error));
    }
  }
}

async function refresh(force = false) {
  if (!state.ready || refreshing) return;
  refreshing = true;
  try {
    if (state.conversation) await loadHistory(state.conversation.conversation_id);
    await syncConversationPermission();
    await ensureAttachments().catch((error) => {
      if (error instanceof ApiError && error.status === 401) throw error;
    });
    await ensurePendingDetails();
    updateConnection();
    syncViews(force);
    await followConversationNavigation();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    setConnection("连接中断，正在重试…", "offline");
  } finally {
    refreshing = false;
  }
}

function syncViews(force = false) {
  renderDock();
  syncVisualization(state.view === "home");
  if (state.view === "chat" && (force || state.signature !== lastSignature)) {
    renderChat(force);
    lastSignature = state.signature;
  }
}

// Device presence must not wait for history, model output or task details.
async function syncDevices() {
  clearTimeout(deviceTimer);
  if (!state.ready || deviceLoading) return;
  if (document.hidden) return;
  deviceLoading = true;
  try {
    await loadNodes();
  } catch {
    state.nodesFresh = false;
  } finally {
    deviceLoading = false;
    if (state.ready) {
      updateConnection();
      renderDevices();
      syncVisualization();
      deviceTimer = setTimeout(syncDevices, state.nodesFresh ? 2000 : 5000);
    }
  }
}

async function syncActivity() {
  if (!state.ready || document.hidden || activityRefreshing) return;
  clearTimeout(activityTimer);
  activityRefreshing = true;
  try {
    await loadSnapshot();
  } catch (error) {
    state.activityFresh = false;
  } finally {
    activityRefreshing = false;
    if (state.ready) {
      syncVisualization();
      activityTimer = setTimeout(syncActivity, 2000);
    }
  }
}

function startPolling() {
  syncDevices();
  syncActivity();
  clearTimeout(pollTimer);
  const tick = async () => {
    if (!state.ready) {
      pollTimer = setTimeout(tick, 1500);
      return;
    }
    if (document.hidden) {
      pollTimer = setTimeout(tick, 2500);
      return;
    }
    await refresh(false);
    pollTimer = setTimeout(tick, state.activeTurn ? 700 : 3000);
  };
  pollTimer = setTimeout(tick, 300);
}

async function ensurePendingDetails() {
  const pending = [...state.tasks.values()]
    .filter((value) => value.task?.status === "awaiting_approval" && !value.approvals)
    .map((value) => value.task.task_id)
    .slice(0, 4);
  await Promise.all(pending.map((id) => taskDetail(id).catch(() => {})));
}

async function syncConversationPermission() {
  if (!state.conversation) return;
  if (pluginState.conversationPermissionId !== state.conversation.conversation_id) {
    pluginState.conversationPermission = null;
  }
  try {
    await loadConversationPermission(state.conversation.conversation_id);
  } catch {
    /* the chat keeps working; the chip reports an explicit error on demand */
  }
}

function openChat() {
  state.view = "chat";
  $("home-view").hidden = true;
  $("chat-view").hidden = false;
  $("chat-view").classList.remove("drawer-open");
  visualization?.visible(false);
  uiExtensions?.sync();
  renderChat(true);
  lastSignature = state.signature;
}

function closeChat() {
  state.view = "home";
  $("chat-view").hidden = true;
  $("home-view").hidden = false;
  renderDock();
  syncVisualization(true);
}

function openSettings() {
  closePluginPage();
  $("settings").hidden = false;
  renderSettings();
  Promise.all([loadNodes(), loadProjects(), loadUsage(), loadControl()])
    .then(() => { renderSettings(); updateConnection(); })
    .catch((error) => notify(errorText(error), true));
}

function renameDevice(node) {
  const name = el("input");
  name.value = nodeName(node.node_id);
  name.maxLength = 40;
  const body = el("div");
  body.append(el("p", "hint", "例如“书房电脑”“随身手机”。保存后，所有客户端都可以用这个名称指代设备。"), name);
  openSheet({ title: "设备名称", body, actions: [
    { label: "取消", className: "quiet", onClick: closeSheet },
    { label: "保存", className: "primary", onClick: () => runAction(async () => {
      await api(`/api/v1/nodes/${node.node_id}/name`, { method: "PATCH", body: JSON.stringify({ name: name.value.trim() }) });
      closeSheet();
      await syncDevices();
    }) },
  ] });
  name.focus();
}

function closeSettings() {
  $("settings").hidden = true;
}

function openPlugins(tab = "permissions") {
  closeSettings();
  openPluginPage(tab);
}

function openAutomation() {
  if (!state.conversation) {
    notify("请先打开一个会话，再查看目标与提醒", true);
    return;
  }
  openPlugins("automation");
}

function composerText(view) {
  return view === "chat" ? $("chat-text") : $("dock-text");
}

function composerSend(view) {
  return view === "chat" ? $("chat-send") : $("dock-send");
}

function clearComposer(view) {
  const textarea = composerText(view);
  textarea.value = "";
  textarea.style.height = "";
}

async function sendFrom(view) {
  if (!state.ready) {
    notify("正在连接", true);
    return;
  }
  if (state.activeTurn) {
    notify("请等待当前回复结束，或先停止回复", true);
    return;
  }
  const textarea = composerText(view);
  const text = textarea.value;
  const selected = draftAttachments().filter((item) => item.status === "uploaded" && item.attachment);
  if (!text.trim() && !selected.length) {
    notify("请先输入内容或选择附件", true);
    textarea.focus();
    return;
  }
  if (attachmentBlocksSend()) {
    notify(attachmentBlockReason() || "附件还没有准备好，请稍后再发送", true);
    return;
  }
  const control = composerSend(view);
  control.disabled = true;
  try {
    const prepared = await prepareSend(text, {
      hasAttachments: selected.length > 0,
      attachmentFields: attachmentState.supported !== false,
    });
    if (prepared.kind === "consent") {
      openConsentSheet(prepared, view, text);
      return;
    }
    await finishSend(prepared, view, text);
  } catch (error) {
    notify(errorText(error), true);
  } finally {
    if (control.isConnected) control.disabled = !state.ready || !!state.activeTurn || attachmentBlocksSend();
    syncViews();
  }
}

/*
 * The base payload still has no attachment selection. Only this function,
 * after a fresh model-preview and the user's explicit confirmation, attaches
 * the digest. A failed send keeps both the uploaded originals and the draft.
 */
async function finishSend(prepared, view, text, after = null) {
  const selected = draftAttachments().filter((item) => item.status === "uploaded" && item.attachment);
  if (!selected.length) {
    await sendPayload(prepared.payload);
    clearAttachments();
    after?.();
    clearComposer(view);
    await refresh(true);
    notify("已发送；输出会显示在输入框上方");
    return;
  }
  const preview = await previewAttachments();
  let sending = false;
  openAttachmentConsentSheet({
    preview,
    capabilityImageEnabled: attachmentState.capabilities?.image_model_enabled !== false,
    onCancel: () => notify("已取消发送；附件与草稿仍然保留"),
    onConfirm: async () => {
      if (sending) return;
      sending = true;
      try {
        const payload = {
          ...withAttachmentConsent(prepared.payload, preview),
          expected_revision: state.conversation?.revision ?? prepared.payload.expected_revision,
        };
        await sendPayload(payload);
        closeSheet();
        clearAttachments();
        after?.();
        clearComposer(view);
        await refresh(true);
        notify("已发送；附件范围按本次预览确认");
      } finally {
        sending = false;
      }
    },
  });
}

function hasDraftAttachments() {
  return draftAttachments().some((item) => item.status === "uploaded" && item.attachment);
}

function openConsentSheet(prepared, view, text) {
  const entries = prepared.catalog?.catalog || [];
  const imageAvailable = !!(prepared.imageAvailable || entries.some((item) => item.name === "read_image"));
  const body = document.createDocumentFragment();
  const intro = el("p", "", imageAvailable
    ? "使用工作区工具时，本轮实际执行结果和可用的图片快照会发送给 DeepSeek，用于生成回答。"
    : "使用工作区工具时，本轮实际执行结果会发送给 DeepSeek，用于生成回答。");
  body.append(intro);

  body.append(el("p", "scope-label", "将发送"));
  const list = el("ul");
  list.append(el("li", "", state.prefs.context ? "本轮输入和所选上下文" : "本轮输入"));
  list.append(el("li", "", "实际工具结果，可能包含读取到的文件文本、目录条目或命令输出摘要"));
  list.append(el("li", "", "按你的问题查到的本人其他会话摘录、项目待办与任务状态"));
  if (imageAvailable) list.append(el("li", "", "配置工作区内图片的有界快照"));
  body.append(list);

  if (imageAvailable) {
    body.append(el("p", "scope-label", "图片快照限制"));
    const images = el("ul");
    images.append(el("li", "", "仅限配置工作区内的图片；最多 1024px / 8 KiB，可能继续缩小或使用有损 JPEG。"));
    images.append(el("li", "", "GIF/WebP 只取首帧；透明区域显示白底。"));
    images.append(el("li", "", "这是本轮的一次性同意；以前允许过结果回传，不代表同意本轮的图片快照。"));
    body.append(images);
  }

  body.append(el("p", "scope-label", "不额外发送"));
  const notSent = el("ul");
  notSent.append(el("li", "", "绝对路径、模型密钥、Node 令牌"));
  notSent.append(el("li", "", "未单独选择的长期记忆"));
  body.append(notSent);

  body.append(el("p", "scope-label", "将在设备上使用"));
  body.append(el("p", "", `Core 根据你的话自动选择已授权设备 · 已配置的读取/查找/写入/固定命令${imageAvailable ? "，以及受控图片读取" : ""}。写入仍会展示差异并读回校验。`));
  body.append(el("p", "hint", "费用受 Core 的本轮上限约束；费用未知时不会按 0 计算。批准执行与同意结果回传是两件事；执行前若需要审批，会在消息里的任务卡再次询问。不开启工作工具或不同意结果外发时，本轮不会触发图片工具。"));

  const onlyChat = button("仅普通聊天", "quiet", () => runAction(async () => {
    closeSheet();
    const plain = await prepareSend(text, {
      toolsOverride: false,
      hasAttachments: hasDraftAttachments(),
      attachmentFields: attachmentState.supported !== false,
    });
    if (plain.kind !== "send") throw new Error("范围已变化，请重试");
    await finishSend(plain, view, text);
  }, onlyChat));
  const allow = button("同意并发送", "primary", () => runAction(async () => {
    closeSheet();
    const allowed = await prepareSend(text, {
      allowWork: true,
      hasAttachments: hasDraftAttachments(),
      attachmentFields: attachmentState.supported !== false,
    });
    if (allowed.kind !== "send") throw new Error("范围已变化，请重试");
    await finishSend(allowed, view, text, () => setPref("resultFeedback", true));
  }, allow));
  openSheet({
    title: "本次需要你决定",
    body,
    actions: [
      { label: "仅普通聊天", className: "quiet", onClick: () => onlyChat.click() },
      { label: "同意并发送", className: "primary", onClick: () => allow.click() },
    ],
  });
}

function openScope(focus = "") {
  const body = document.createDocumentFragment();
  const conversation = state.conversation;

  body.append(el("p", "scope-label", "项目"));
  if (conversation?.project_id) {
    body.append(el("p", "hint", `当前会话固定属于“${projectName(conversation.project_id) || "已关联项目"}”。这里的选择用于新会话。`));
  }
  const projectSelect = el("select");
  projectSelect.append(new Option("不关联项目", ""));
  for (const project of state.projects) {
    projectSelect.append(new Option(project.name, project.project_id));
  }
  projectSelect.value = conversation?.project_id || state.prefs.projectId || "";
  body.append(projectSelect);

  body.append(el("p", "scope-label", "自动选择设备"));
  body.append(el("p", "hint", "NEXUS 会按你的话选择可用设备。可以说“我的电脑”“我的手机”或你设置的设备名称；目标有歧义时再询问。"));
  for (const node of state.nodes) body.append(el("p", "", `${nodeName(node.node_id)} · ${state.nodesFresh ? (node.online ? "在线" : "离线") : "状态同步中"}`));

  body.append(el("p", "scope-label", "本轮工具与外发"));
  const toolsLabel = el("label", "switch");
  const tools = document.createElement("input");
  tools.type = "checkbox";
  tools.checked = !!state.prefs.tools;
  toolsLabel.append(tools, document.createTextNode("允许本轮使用已配置的工作区工具"));
  body.append(toolsLabel);

  const feedbackLabel = el("label", "switch");
  const feedback = document.createElement("input");
  feedback.type = "checkbox";
  feedback.checked = !!state.prefs.resultFeedback;
  feedbackLabel.append(feedback, document.createTextNode("允许把本轮实际工具结果发送给 DeepSeek"));
  body.append(feedbackLabel);

  const contextLabel = el("label", "switch");
  const context = document.createElement("input");
  context.type = "checkbox";
  context.checked = !!state.prefs.context;
  contextLabel.append(context, document.createTextNode("附带本会话最近已完成对话"));
  body.append(contextLabel);

  body.append(el("p", "hint", "更改只影响下一轮；当前已发送的请求不会被改写。工具结果回传默认关闭；打开后，纯文字工具结果可在后续轮次沿用这项显示偏好。若服务器返回图片工具能力，每一轮仍会单独确认图片快照外发，历史选择不代替本轮同意。"));

  const save = button("保存范围", "primary", () => runAction(async () => {
    setPref("projectId", projectSelect.value || "");
    setPref("tools", tools.checked);
    setPref("resultFeedback", feedback.checked);
    setPref("context", context.checked);
    closeSheet();
    renderDock();
    renderChat(true);
    notify(conversation?.project_id && projectSelect.value !== conversation.project_id
      ? "已保存；项目选择用于新会话，当前会话不变"
      : "本次范围已更新");
  }, save));
  openSheet({
    title: "本次范围",
    body,
    actions: [
      { label: "取消", className: "quiet", onClick: closeSheet },
      { label: "保存范围", className: "primary", onClick: () => save.click() },
    ],
  });
}

function findTurnForTask(taskId) {
  for (const turn of state.turns.values()) {
    for (const step of turn.work_steps || []) {
      if (step.task_id === taskId || step.preview_task_id === taskId) return turn.turn_id;
    }
  }
  return null;
}

function openPending() {
  const approvals = pendingApprovals();
  const permissions = pendingPermissions();
  const body = document.createDocumentFragment();
  if (!approvals.length && !permissions.length) {
    body.append(el("p", "", "当前没有待处理事项。"));
  }
  for (const item of approvals) {
    const box = el("div", "card");
    const head = el("div", "card-head");
    head.append(el("strong", "", `审批：${TOOL_LABELS[item.task.request?.intent] || item.task.request?.intent || "任务"}`), el("span", "badge pending", "等待批准"));
    box.append(head);
    box.append(el("p", "", `${nodeName(item.task.node_id)} · 到期 ${formatTime(item.approval.expires_at)}`));
    if (item.task.request?.arguments && Object.keys(item.task.request.arguments).length) {
      const args = el("pre");
      args.textContent = JSON.stringify(item.task.request.arguments, null, 2).slice(0, 1600);
      box.append(args);
    }
    box.append(el("p", "hint", "批准只对这一动作、这一参数摘要和这一期限有效。"));
    const actions = el("div", "actions");
    const reject = button("拒绝", "quiet", () => runAction(async () => {
      await decideApproval(item.approval.approval_id, "reject");
      closeSheet();
      await refresh(true);
      notify("已拒绝该任务");
    }, reject));
    const approve = button("批准", "primary", () => runAction(async () => {
      await decideApproval(item.approval.approval_id, "approve");
      closeSheet();
      await refresh(true);
      notify("已批准该任务");
    }, approve));
    actions.append(reject, approve);
    box.append(actions);
    body.append(box);
  }
  for (const step of permissions) {
    const turn = activeTurnOfConversation();
    const box = el("div", "card");
    const head = el("div", "card-head");
    head.append(el("strong", "", `需要答复：${step.proposal?.intent || "步骤"}`), el("span", "badge pending", "等待你"));
    box.append(head, el("p", "", step.request_text || "请回到会话核对具体动作。"));
    const open = button("回到会话处理", "primary", () => {
      closeSheet();
      openChat();
      if (turn) scrollToTurn(turn.turn_id);
    });
    box.append(open);
    body.append(box);
  }
  openSheet({
    title: "待处理",
    body,
    actions: [{ label: "关闭", className: "quiet", onClick: closeSheet }],
  });
}

function scrollToTurn(turnId) {
  openChat();
  setTimeout(() => {
    const node = document.querySelector(`[data-turn-id="${turnId}"]`);
    if (!node) return;
    node.scrollIntoView({ block: "center", behavior: "smooth" });
    node.style.outline = "1px solid #f2f2f2";
    setTimeout(() => { node.style.outline = ""; }, 1800);
  }, 120);
}

function openChatMenu() {
  if (!state.conversation) {
    notify("还没有会话", true);
    return;
  }
  const conversation = state.conversation;
  const body = document.createDocumentFragment();
  const name = document.createElement("input");
  name.value = conversation.title || "";
  name.maxLength = 80;
  name.placeholder = "会话名称";
  body.append(name);
  body.append(el("p", "hint", "改名不会改变已有执行记录。归档只影响会话列表，不删除历史。"));
  const manage = el("div", "settings-row plugin-menu-actions");
  manage.append(
    button("会话权限模式", "quiet", () => { closeSheet(); openConversationPermission(); }),
    button("目标与提醒", "quiet", () => { closeSheet(); openAutomation(); }),
    button("权限与插件页", "quiet", () => { closeSheet(); openPlugins("permissions"); }),
  );
  body.append(manage);
  const save = button("保存名称", "primary", () => runAction(async () => {
    if (!name.value.trim()) throw new ApiError(400, "EMPTY_MESSAGE");
    await renameConversation(conversation.conversation_id, name.value.trim());
    closeSheet();
    await refresh(true);
    notify("会话名称已更新");
  }, save));
  const archiveLabel = conversation.archived ? "取消归档" : "归档会话";
  const archive = button(archiveLabel, "quiet", () => runAction(async () => {
    await archiveConversation(conversation.conversation_id, !conversation.archived);
    closeSheet();
    leaveConversation();
    state.conversation = null;
    state.turns.clear();
    state.turnOrder = [];
    state.activeTurn = null;
    await loadConversations(false);
    closeChat();
    renderDock();
    notify(conversation.archived ? "已取消归档" : "已归档；历史仍保留");
  }, archive));
  openSheet({
    title: "会话操作",
    body,
    actions: [
      { label: "取消", className: "quiet", onClick: closeSheet },
      { label: archiveLabel, className: "quiet", onClick: () => archive.click() },
      { label: "保存名称", className: "primary", onClick: () => save.click() },
    ],
  });
  name.focus();
}

function openTurnDetails(turn) {
  const body = document.createDocumentFragment();
  body.append(el("p", "scope-label", "本轮摘要"));
  const facts = el("ul");
  facts.append(el("li", "", `状态：${turn.status} · 阶段：${turn.phase || "reply"}${turn.work_status ? " · 工作：" + turn.work_status : ""}`));
  facts.append(el("li", "", `费用：${turn.cost_status === "unknown" ? "未知，保留预留" : formatMoney(turn.cost_microunits)}`));
  facts.append(el("li", "", `上下文：${turn.include_context ? "附带最近对话" : "仅本轮输入"}${turn.context_truncated ? "（已裁剪）" : ""}`));
  facts.append(el("li", "", `工具：${turn.allow_tools ? "允许" : "未允许"} · 设备：${turn.node_id ? nodeName(turn.node_id) : "Core 自动选择"}`));
  if (turn.memory_references?.length) facts.append(el("li", "", `引用记忆：${turn.memory_references.length} 条`));
  body.append(facts);
  body.append(el("p", "hint", "这里只显示过程摘要；完整请求正文和原始工具输出保留在 Core 的执行记录中，不在此处展开。"));
  openSheet({
    title: "技术详情",
    body,
    actions: [{ label: "关闭", className: "quiet", onClick: closeSheet }],
  });
}

function formatMoney(value) {
  if (value === null || value === undefined) return "费用未知";
  return `$${(value / 1000000).toFixed(6)}`;
}

async function handleLogin(event) {
  event.preventDefault();
  const error = $("login-error");
  error.hidden = true;
  const submit = event.submitter || $("login-form").querySelector("button");
  if (submit) submit.disabled = true;
  try {
    let url = null;
    if (native) {
      url = normalizeCoreUrl($("core-url").value);
      await native.setCoreUrl(url);
      localStorage.setItem(CORE_URL_KEY, url);
    }
    const token = $("token").value;
    await login(token);
    if (native) {
      if ($("remember").checked) {
        await native.rememberOwner(url, token).catch(() => {});
        await native.rememberSession().catch(() => {});
      } else {
        await native.forgetOwner().catch(() => {});
        await native.forgetSession().catch(() => {});
      }
    }
    await loadCore();
    await ensurePendingDetails();
    await syncConversationPermission();
    enterApp();
  } catch (exception) {
    error.textContent = errorText(exception);
    error.hidden = false;
  } finally {
    if (submit) submit.disabled = false;
  }
}

function confirmLogout() {
  const body = document.createDocumentFragment();
  body.append(el("p", "", "退出会撤销当前 Core 会话，并清除本页面的显示状态。"));
  openSheet({
    title: "退出登录",
    body,
    actions: [
      { label: "取消", className: "quiet", onClick: closeSheet },
      { label: "退出", className: "quiet danger", onClick: () => runAction(async () => {
        closeSheet();
        closeSettings();
        await stopVoice();
        await logout();
        if (native) {
          await native.forgetOwner().catch(() => {});
          await native.forgetSession().catch(() => {});
        }
        showLogin();
      }) },
    ],
  });
}

function bindEvents() {
  if (!native) {
    $("core-field").hidden = true;
    $("core-url").disabled = true;
    $("remember-field").hidden = true;
  }
  $("login-form").addEventListener("submit", handleLogin);
  $("login").hidden = false;
  $("home-button").addEventListener("click", () => {
    if (state.view === "chat") closeChat();
  });
  $("dock-open").addEventListener("click", openChat);
  $("dock-expand").addEventListener("click", openChat);
  $("chat-back").addEventListener("click", closeChat);
  $("chat-drawer").addEventListener("click", () => $("chat-view").classList.toggle("drawer-open"));
  $("settings-button").addEventListener("click", () => {
    if ($("settings").hidden) openSettings(); else closeSettings();
  });
  $("settings-close").addEventListener("click", closeSettings);
  $("plugins-button").addEventListener("click", () => {
    if ($("plugin-view").hidden) openPlugins("permissions"); else closePluginPage();
  });
  $("plugin-close").addEventListener("click", closePluginPage);
  $("sheet-close").addEventListener("click", closeSheet);
  $("sheet").addEventListener("click", (event) => {
    if (event.target?.hasAttribute?.("data-close-sheet")) closeSheet();
  });
  $("pending-button").addEventListener("click", openPending);
  $("dock-form").addEventListener("submit", (event) => { event.preventDefault(); sendFrom("home"); });
  $("chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendFrom("chat"); });
  $("dock-scope").addEventListener("click", () => openScope());
  $("chat-scope").addEventListener("click", () => openScope());
  $("dock-stop").addEventListener("click", stopActive);
  $("chat-stop").addEventListener("click", stopActive);
  $("new-conversation").addEventListener("click", () => runAction(async () => {
    saveConversationDraft();
    leaveConversation();
    const projectId = state.prefs.projectId || state.conversation?.project_id || null;
    await createConversation(projectId);
    clearComposer("chat"); clearComposer("dock");
    await loadHistory(state.conversation.conversation_id);
    await syncConversationPermission();
    await ensureAttachments();
    openChat();
    renderDock();
    notify("已新建会话");
  }));
  $("dock-new").addEventListener("click", () => runAction(async () => {
    saveConversationDraft();
    leaveConversation();
    const projectId = state.prefs.projectId || state.conversation?.project_id || null;
    await createConversation(projectId);
    clearComposer("chat"); clearComposer("dock");
    await loadHistory(state.conversation.conversation_id);
    await syncConversationPermission();
    await ensureAttachments();
    renderDock();
    syncVisualization(true);
    lastSignature = state.signature;
    notify("已新建会话");
    $("dock-text").focus();
  }, $("dock-new")));
  $("archive-toggle").addEventListener("click", () => runAction(async () => {
    await loadConversations(!state.archived);
    renderConversations();
    renderDock();
  }));
  $("chat-menu").addEventListener("click", openChatMenu);
  $("chat-older").addEventListener("click", () => runAction(async () => {
    const oldest = state.turnOrder[0];
    if (!oldest || !state.conversation) return;
    await loadHistory(state.conversation.conversation_id, oldest.cursor);
    renderChat(false);
    lastSignature = state.signature;
  }, $("chat-older")));
  for (const id of ["dock-text", "chat-text"]) {
    const textarea = $(id);
    if (id === "chat-text") {
      textarea.addEventListener("input", () => {
        textarea.style.height = "auto";
        textarea.style.height = Math.min(textarea.scrollHeight, 180) + "px";
      });
    }
    textarea.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      if (id === "dock-text" && !event.shiftKey) {
        event.preventDefault();
        sendFrom("home");
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        sendFrom(id === "chat-text" ? "chat" : "home");
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      // A native image dialog owns Escape while it is open; do not also close
      // the surrounding decision sheet in the same key press.
      if (document.querySelector("dialog[open]")) return;
      if (!$("sheet").hidden) closeSheet();
      else if (!$("plugin-view").hidden) closePluginPage();
      else if (!$("settings").hidden) closeSettings();
      else if ($("chat-view").classList.contains("drawer-open")) $("chat-view").classList.remove("drawer-open");
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.ready) { syncDevices(); syncActivity(); refresh(true); }
  });
  window.addEventListener("online", () => { if (state.ready) { syncDevices(); syncActivity(); refresh(true); } });
  window.addEventListener("focus", () => { if (state.ready) syncDevices(); });
  initAttachmentComposer();
}

async function stopActive() {
  const turn = state.activeTurn && state.turns.get(state.activeTurn);
  if (!turn) return;
  try {
    await stopTurn(turn.turn_id);
    await refresh(true);
    notify("已请求停止回复");
  } catch (error) {
    notify(errorText(error), true);
  }
}

bindUI({
  renderVoiceSettings,
  openConversationSource,
  renameDevice,
  openChat,
  closeChat,
  openSettings,
  openScope,
  openPending,
  newConversation: () => $("new-conversation").click(),
  sendFrom,
  refresh,
  updateConnection,
  scrollToTurn,
  stopTurn: stopActive,
  logout: confirmLogout,
  openTurnDetails,
  openPlugins,
  openConversationPermission,
  openAutomation,
});

bindAttachmentUI({
  notify,
  openSheet,
  closeSheet,
  refresh,
  openChat,
  clearComposer,
  refreshComposerState,
});

["chat-text", "dock-text"].forEach(id => $(id).addEventListener("input", saveConversationDraft));
state.ready = false;
bindEvents();
initVoice({ refresh, openChat, renderSettings });
uiExtensions = initUIExtensions(() => visualization);
setUnauthorizedHandler(() => {
  if (!booting) showLogin("会话已过期，请重新登录");
});
bootstrap();
