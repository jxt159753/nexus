"use strict";

import {
  ApiError,
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
  renderChat,
  renderDock,
  renderSettings,
  renderConversations,
  runAction,
  ui,
} from "./render.js";

import { initVisualization } from "./visualization.js";
import { initVoice, stopVoice } from "./voice.js";

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let lastSignature = "";
let lastSnapshotAt = 0;
let refreshing = false;
let booting = true;
let visualization = null;

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
  const online = state.nodes.filter((node) => node.online).length;
  const suffix = state.control?.paused ? " · 已暂停派发" : "";
  setConnection(`Core 已连接 · ${online}/${state.nodes.length} 设备在线${suffix}`, "live");
}

function showLogin(message = "") {
  clearTimeout(pollTimer);
  pollTimer = null;
  resetState();
  visualization?.visible(false);
  $("shell").hidden = true;
  $("login").hidden = false;
  $("login-form").reset();
  const error = $("login-error");
  error.hidden = !message;
  error.textContent = message;
  $("token").focus();
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
    nodeId: state.lastSent?.node_id || state.prefs.nodeId || "",
    messageKey: state.lastSent?.request_id || "",
    paused: !!state.control?.paused,
  });
  visualization.visible(!!visible);
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
}

async function bootstrap() {
  loadPrefs();
  try {
    await loadCore();
    await ensurePendingDetails();
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
    if (Date.now() - lastSnapshotAt > 4000) {
      await loadSnapshot();
      lastSnapshotAt = Date.now();
      await ensurePendingDetails();
    }
    updateConnection();
    syncViews(force);
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

function startPolling() {
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

function openChat() {
  state.view = "chat";
  $("home-view").hidden = true;
  $("chat-view").hidden = false;
  $("chat-view").classList.remove("drawer-open");
  visualization?.visible(false);
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
  $("settings").hidden = false;
  renderSettings();
  Promise.all([loadNodes(), loadProjects(), loadUsage(), loadControl()])
    .then(() => { renderSettings(); updateConnection(); })
    .catch((error) => notify(errorText(error), true));
}

function closeSettings() {
  $("settings").hidden = true;
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
    notify("正在连接 Core", true);
    return;
  }
  if (state.activeTurn) {
    notify("请等待当前回复结束，或先停止回复", true);
    return;
  }
  const textarea = composerText(view);
  const text = textarea.value;
  if (!text.trim()) {
    notify("请先输入内容", true);
    textarea.focus();
    return;
  }
  const control = composerSend(view);
  control.disabled = true;
  try {
    const prepared = await prepareSend(text);
    if (prepared.kind === "consent") {
      openConsentSheet(prepared, view, text);
      return;
    }
    await sendPayload(prepared.payload);
    clearComposer(view);
    await refresh(true);
    notify("已发送；输出会显示在输入框上方");
  } catch (error) {
    notify(errorText(error), true);
  } finally {
    if (control.isConnected) control.disabled = !state.ready || !!state.activeTurn;
    syncViews();
  }
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
  body.append(el("p", "", `${nodeName(state.prefs.nodeId)} · 工作区内已配置的读取/查找/写入/固定命令${imageAvailable ? "，以及受控图片读取" : ""}。写入仍会展示差异并读回校验。`));
  body.append(el("p", "hint", "费用受 Core 的本轮上限约束；费用未知时不会按 0 计算。批准执行与同意结果回传是两件事；执行前若需要审批，会在消息里的任务卡再次询问。不开启工作工具或不同意结果外发时，本轮不会触发图片工具。"));

  const onlyChat = button("仅普通聊天", "quiet", () => runAction(async () => {
    closeSheet();
    const plain = await prepareSend(text, { toolsOverride: false });
    if (plain.kind !== "send") throw new Error("范围已变化，请重试");
    await sendPayload(plain.payload);
    clearComposer(view);
    await refresh(true);
    notify("已按普通聊天发送");
  }, onlyChat));
  const allow = button("同意并发送", "primary", () => runAction(async () => {
    closeSheet();
    const allowed = await prepareSend(text, { allowWork: true });
    if (allowed.kind !== "send") throw new Error("范围已变化，请重试");
    await sendPayload(allowed.payload);
    setPref("resultFeedback", true);
    clearComposer(view);
    await refresh(true);
    notify("已发送；本轮允许工具结果回传");
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

  body.append(el("p", "scope-label", "目标设备"));
  const nodeList = el("div", "choice-row");
  if (!state.nodes.length) {
    body.append(el("p", "hint", "Core 尚未配置 Node；仍可进行普通聊天。"));
  }
  for (const node of state.nodes) {
    const label = el("label", "choice" + (node.node_id === state.prefs.nodeId ? " on" : ""));
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "scope-node";
    input.value = node.node_id;
    input.checked = node.node_id === state.prefs.nodeId;
    label.append(input, document.createTextNode(`${nodeName(node.node_id)}${node.online ? "" : " · 离线"}`));
    input.addEventListener("change", () => {
      for (const item of nodeList.querySelectorAll(".choice")) item.classList.remove("on");
      label.classList.add("on");
    });
    nodeList.append(label);
  }
  body.append(nodeList);

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
    const checkedNode = $("sheet-body").querySelector('input[name="scope-node"]:checked');
    if (checkedNode) setPref("nodeId", checkedNode.value);
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
  facts.append(el("li", "", `工具：${turn.allow_tools ? "允许" : "未允许"} · 设备：${turn.node_id ? nodeName(turn.node_id) : "未指定"}`));
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
    await login($("token").value);
    await loadCore();
    await ensurePendingDetails();
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
        showLogin();
      }) },
    ],
  });
}

function bindEvents() {
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
    const projectId = state.prefs.projectId || state.conversation?.project_id || null;
    await createConversation(projectId);
    await loadHistory(state.conversation.conversation_id);
    openChat();
    renderDock();
    notify("已新建会话");
  }));
  $("dock-new").addEventListener("click", () => runAction(async () => {
    const projectId = state.prefs.projectId || state.conversation?.project_id || null;
    await createConversation(projectId);
    await loadHistory(state.conversation.conversation_id);
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
      if (!$("sheet").hidden) closeSheet();
      else if (!$("settings").hidden) closeSettings();
      else if ($("chat-view").classList.contains("drawer-open")) $("chat-view").classList.remove("drawer-open");
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.ready) refresh(true);
  });
  window.addEventListener("online", () => { if (state.ready) refresh(true); });
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
  openChat,
  closeChat,
  openSettings,
  openScope,
  openPending,
  newConversation: () => $("new-conversation").click(),
  sendFrom,
  refresh,
  scrollToTurn,
  stopTurn: stopActive,
  logout: confirmLogout,
  openTurnDetails,
});

state.ready = false;
bindEvents();
initVoice({ refresh, openChat });
setUnauthorizedHandler(() => {
  if (!booting) showLogin("会话已过期，请重新登录");
});
bootstrap();
