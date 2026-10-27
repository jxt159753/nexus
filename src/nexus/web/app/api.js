"use strict";

export const STATUS_LABELS = {
  queued: "排队中",
  generating: "正在生成",
  stopping: "正在停止",
  completed: "已完成",
  stopped: "已停止",
  failed: "未完成",
  interrupted: "已中断",
  awaiting_approval: "等待审批",
  ready: "待派发",
  dispatching: "派发中",
  running: "执行中",
  recovering: "对账中",
  verifying: "验证中",
  succeeded: "已成功",
  cancelled: "已取消",
  timed_out: "已超时",
};

export const TASK_TERMINAL = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
export const TOOL_LABELS = {
  read_file: "读取文件",
  find_files: "查找文件",
  write_file: "写入文件",
  preview_file_write: "预览写入",
  read: "按行读取",
  glob: "匹配路径",
  grep: "搜索内容",
  edit: "精确修改",
  web_search: "搜索网页",
  web_fetch: "抓取网页",
  run_command: "固定命令",
  get_system_info: "读取系统信息",
  list_directory: "列出工作区",
  read_image: "读取图片",
  get_current_time: "查询时间",
  job_list: "查询任务列表",
  job_output: "查询任务输出",
  get_node_status: "查询节点状态",
  run_skill: "运行流程",
  run_tool: "运行工具",
  inspect_capability: "查看能力",
  plugin_execute: "原生插件",
  plugin_inspect: "插件目录",
  plugin_install: "自写插件",
  inspect_ui_extension: "检查界面插件",
  submit_ui_extension: "开发与验证界面插件",
};

const PREFS_KEY = "nexus.chat.prefs.v1";

export const state = {
  csrf: null,
  session: null,
  ready: false,
  view: "home",
  nodes: [],
  nodesFresh: false,
  activity: null,
  activityFresh: false,
  projects: [],
  usage: null,
  control: { paused: false, revision: 0 },
  conversations: [],
  archived: false,
  conversation: null,
  turns: new Map(),
  turnOrder: [],
  activeTurn: null,
  workGrant: null,
  tasks: new Map(),
  taskFetchAt: new Map(),
  previews: new Map(),
  prefs: {
    projectId: "",
    tools: true,
    resultFeedback: false,
    context: true,
    lastConversationId: "",
  },
  loading: false,
  signature: "",
  lastSent: null,
};

let unauthorizedHandler = () => {};

export class ApiError extends Error {
  constructor(status, code) {
    super(code || `HTTP_${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code || `HTTP_${status}`;
  }
}

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler;
}

export function uuid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) {
      const value = JSON.parse(raw);
      if (value && typeof value === "object") { state.prefs = { ...state.prefs, ...value }; delete state.prefs.nodeId; }
    }
  } catch { /* display preference only */ }
}

export function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(state.prefs)); } catch { /* ignore */ }
}

export function setPref(key, value) {
  state.prefs[key] = value;
  savePrefs();
}

export function clearPrefs() {
  try { localStorage.removeItem(PREFS_KEY); } catch { /* ignore */ }
  state.prefs = {
    projectId: "", tools: true, resultFeedback: false,
    context: true, lastConversationId: "",
  };
}

export async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  const headers = {
    "Content-Type": "application/json",
    ...(state.csrf ? { "X-CSRF-Token": state.csrf } : {}),
    ...(options.headers || {}),
  };
  try {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers,
      signal: controller.signal,
    });
    const text = await response.text();
    let value = null;
    try { value = text ? JSON.parse(text) : null; } catch { value = { detail: "INVALID_RESPONSE" }; }
    if (!response.ok) {
      if (response.status === 401) unauthorizedHandler();
      const code = typeof value?.detail === "string" ? value.detail : `HTTP_${response.status}`;
      throw new ApiError(response.status, code);
    }
    return value;
  } finally {
    clearTimeout(timer);
  }
}

export async function getSession() {
  const value = await api("/api/v1/session");
  state.session = value;
  state.csrf = value.csrf_token;
  return value;
}

export async function login(token) {
  const value = await api("/api/v1/session", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
  state.csrf = value.csrf_token;
  state.session = { expires_at: value.expires_at };
  return getSession();
}

export async function logout() {
  try {
    await api("/api/v1/session", { method: "DELETE", body: "{}" });
  } finally {
    resetState();
  }
}

export function resetState() {
  state.csrf = null;
  state.session = null;
  state.ready = false;
  state.nodes = [];
  state.nodesFresh = false;
  state.activity = null;
  state.activityFresh = false;
  state.projects = [];
  state.usage = null;
  state.control = { paused: false, revision: 0 };
  state.conversations = [];
  state.archived = false;
  state.conversation = null;
  state.turns.clear();
  state.turnOrder = [];
  state.activeTurn = null;
  state.workGrant = null;
  state.tasks.clear();
  state.taskFetchAt.clear();
  state.previews.clear();
  state.signature = "";
  state.pending = [];
  state.prefs.lastConversationId = "";
  savePrefs();
}

function pickConversation() {
  const saved = state.conversations.find((item) => item.conversation_id === state.prefs.lastConversationId);
  return saved || state.conversations[0] || null;
}

export async function loadCore() {
  await getSession();
  const [nodes, projects, usage, control, conversations, snapshot] = await Promise.all([
    api("/api/v1/nodes"),
    api("/api/v1/records/projects?limit=100&after=0"),
    api("/api/v1/models/usage"),
    api("/api/v1/control"),
    api("/api/v1/chat/conversations?archived=false"),
    api("/api/v1/client/snapshot"),
  ]);
  state.nodes = nodes.nodes || [];
  state.projects = projects.projects || [];
  state.usage = usage;
  state.control = control;
  state.conversations = conversations.conversations || [];
  state.tasks.clear();
  for (const task of [...(snapshot.tasks || []), ...(snapshot.active_tasks || [])]) state.tasks.set(task.task_id, { task });
  state.activity = snapshot.activity || null;
  state.activityFresh = true;
  state.nodesFresh = true;
  const conversation = pickConversation();
  state.ready = true;
  if (conversation) {
    state.conversation = conversation;
    await loadHistory(conversation.conversation_id);
  }
  return state;
}

export async function loadNodes() {
  const value = await api("/api/v1/nodes");
  state.nodes = value.nodes || [];
  state.nodesFresh = true;
  return state.nodes;
}

export async function loadProjects() {
  const value = await api("/api/v1/records/projects?limit=100&after=0");
  state.projects = value.projects || [];
  return state.projects;
}

export async function loadUsage() {
  state.usage = await api("/api/v1/models/usage");
  return state.usage;
}

export async function loadControl() {
  state.control = await api("/api/v1/control");
  return state.control;
}

export async function loadConversations(archived = state.archived) {
  const value = await api(`/api/v1/chat/conversations?archived=${archived ? "true" : "false"}`);
  state.archived = archived;
  state.conversations = value.conversations || [];
  return state.conversations;
}

export async function createConversation(projectId = null) {
  const value = await api("/api/v1/chat/conversations", {
    method: "POST",
    body: JSON.stringify({ request_id: uuid(), project_id: projectId || null }),
  });
  state.conversation = value.conversation;
  state.turns.clear();
  state.turnOrder = [];
  state.activeTurn = null;
  state.taskFetchAt.clear();
  await loadConversations(false);
  state.prefs.lastConversationId = value.conversation.conversation_id;
  savePrefs();
  return value.conversation;
}

let selectionEpoch = 0;
export async function selectConversation(conversationId) {
  const epoch = ++selectionEpoch;
  const value = await api(`/api/v1/chat/conversations/${conversationId}/turns`);
  if (epoch !== selectionEpoch) return null;
  state.turns.clear();
  state.turnOrder = [];
  state.taskFetchAt.clear();
  state.conversation = value.conversation;
  for (const turn of value.turns || []) state.turns.set(turn.turn_id, turn);
  state.turnOrder = [...state.turns.values()].sort((a, b) => (a.cursor ?? 0) - (b.cursor ?? 0));
  state.activeTurn = value.active_turn_id;
  state.workGrant = value.work_grant || null;
  const known = state.conversations.find((item) => item.conversation_id === conversationId);
  if (known) Object.assign(known, state.conversation);
  if (state.prefs.lastConversationId !== conversationId) {
    state.prefs.lastConversationId = conversationId;
    savePrefs();
  }
  await syncTasksForTurns();
  state.signature = turnSignature();
  return value;
}

export async function loadHistory(conversationId = state.conversation?.conversation_id, before = null) {
  if (!conversationId) return null;
  const epoch = selectionEpoch;
  const query = before ? `?before=${before}` : "";
  const value = await api(`/api/v1/chat/conversations/${conversationId}/turns${query}`);
  if (epoch !== selectionEpoch || state.conversation?.conversation_id !== conversationId) return null;
  if (!before) {
    state.turns.clear();
    state.turnOrder = [];
  }
  state.conversation = value.conversation;
  for (const turn of value.turns || []) state.turns.set(turn.turn_id, turn);
  state.turnOrder = [...state.turns.values()].sort((a, b) => (a.cursor ?? 0) - (b.cursor ?? 0));
  state.activeTurn = value.active_turn_id;
  state.workGrant = value.work_grant || null;
  const known = state.conversations.find((item) => item.conversation_id === conversationId);
  if (known) Object.assign(known, state.conversation);
  if (state.prefs.lastConversationId !== conversationId) {
    state.prefs.lastConversationId = conversationId;
    savePrefs();
  }
  await syncTasksForTurns();
  state.signature = turnSignature();
  return value;
}

function turnSignature() {
  return JSON.stringify([
    state.conversation?.conversation_id,
    state.conversation?.revision,
    state.conversation?.title,
    state.conversation?.archived,
    ...[...state.turnOrder].map((turn) => [
      turn.turn_id,
      turn.revision,
      turn.status,
      turn.phase,
      turn.work_status,
      turn.response_text?.length || 0,
      turn.error_code || "",
      (turn.work_steps || []).map((step) => [
        step.step_id,
        step.status,
        step.permission_decision || "",
        step.result ? 1 : 0,
        step.result?.status || "",
        step.result?.execution_outcome || "",
        step.result?.verification || "",
        step.result?.error_code || "",
      ]),
      turn.presentation ? [
        turn.presentation.version,
        turn.presentation.phase,
        turn.presentation.label,
        turn.presentation.process_truncated,
        turn.presentation.live_text,
        turn.presentation.final_text,
        turn.presentation.collapse_process,
        turn.presentation.conversations,
        turn.presentation.navigation,
        (turn.presentation.process || []).map((item) => [item.id, item.text, item.truncated]),
      ] : null,
    ]),
  ]);
}

export async function syncTasksForTurns() {
  const ids = new Set();
  for (const turn of state.turns.values()) {
    for (const step of turn.work_steps || []) {
      if (step.task_id) ids.add(step.task_id);
      if (step.preview_task_id) ids.add(step.preview_task_id);
    }
  }
  const now = Date.now();
  await Promise.all([...ids].slice(0, 8).map(async (id) => {
    const cached = state.tasks.get(id);
    if (cached?.task && TASK_TERMINAL.has(cached.task.status)) return;
    const at = state.taskFetchAt.get(id) || 0;
    if (now - at < 1500) return;
    state.taskFetchAt.set(id, now);
    try {
      state.tasks.set(id, await api(`/api/v1/tasks/${id}`));
    } catch { /* a transient task read must not break chat */ }
  }));
}

export async function loadSnapshot() {
  const value = await api("/api/v1/client/snapshot");
  const current = new Map([...(value.tasks || []), ...(value.active_tasks || [])].map(task => [task.task_id, task]));
  // Older active tasks can finish outside the latest history page. Read their
  // terminal evidence before retiring their visual marker.
  const ended = [...state.tasks.values()].map(value => value.task || value)
    .filter(task => !TASK_TERMINAL.has(task.status) && !current.has(task.task_id));
  await Promise.all(ended.map(async task => {
    const detail = await api(`/api/v1/tasks/${task.task_id}`);
    current.set(task.task_id, detail.task);
  }));
  for (const task of current.values()) {
    const cached = state.tasks.get(task.task_id);
    if (!cached?.task || cached.task.revision <= task.revision) {
      state.tasks.set(task.task_id, { ...(cached || {}), task });
    }
  }
  state.control = value.control || state.control;
  state.activity = value.activity || null;
  state.activityFresh = true;
  return value;
}

export async function refreshAfterAction() {
  if (state.conversation) await loadHistory(state.conversation.conversation_id);
  await loadSnapshot();
  await loadUsage().catch(() => {});
}

export async function taskDetail(taskId) {
  const value = await api(`/api/v1/tasks/${taskId}`);
  state.tasks.set(taskId, value);
  state.taskFetchAt.set(taskId, Date.now());
  return value;
}

export async function prepareSend(text, { allowWork = false, toolsOverride = null, hasAttachments = false, attachmentFields = true } = {}) {
  const trimmed = String(text || "").trim();
  if (!trimmed && !hasAttachments) throw new ApiError(400, "EMPTY_MESSAGE");
  if (!state.conversation) {
    await createConversation(state.prefs.projectId || null);
    await loadHistory(state.conversation.conversation_id);
  }
  const tools = toolsOverride === null ? !!state.prefs.tools : !!toolsOverride;
  let catalog = null;
  if (tools) catalog = await api("/api/v1/chat/work-catalog");
  const entries = catalog?.catalog || [];
  const workAvailable = entries.length > 0;
  const imageAvailable = entries.some((item) => (item.route_intent || item.name) === "read_image");
  // Image snapshots are always a fresh, per-round outbound choice. A remembered
  // "result feedback" preference only covers text results and never consents to
  // sending an image snapshot in a later round.
  const workConsented = allowWork || (!imageAvailable && state.prefs.resultFeedback);
  if (workAvailable && !workConsented) {
    return { kind: "consent", text: trimmed, catalog, imageAvailable };
  }
  const useWork = workAvailable && workConsented;
  const payload = {
    request_id: uuid(),
    expected_revision: state.conversation.revision,
    text: trimmed,
    include_context: !!state.prefs.context,
    allow_model: true,
    allow_tools: !!useWork,
    allow_skills: false,
    // Attachments stay unselected here. Only the explicit model-preview
    // consent sheet fills these fields together with its digest. An older Core
    // without the attachment contract rejects unknown fields, so in that case
    // the message omits them entirely and the UI explains the interface is
    // unavailable.
    ...(attachmentFields ? {
      attachment_ids: [],
      attachments_digest: null,
      allow_attachment_model: false,
    } : {}),
    ...(useWork ? {
      workspace_work: true,
      all_nodes: true,
      work_digest: catalog.work_digest,
      allow_work_results: true,
    } : {}),
  };
  if (useWork && window.__NEXUS_NATIVE__) {
    const local = await window.__NEXUS_NATIVE__.loadNode().catch(() => null);
    if (state.nodes.some((node) => node.node_id === local?.node_id)) payload.client_node_id = local.node_id;
  }
  return { kind: "send", payload, workAvailable, imageAvailable };
}

export async function sendPayload(payload) {
  if (!state.conversation) throw new ApiError(400, "NO_CONVERSATION");
  await api(`/api/v1/chat/conversations/${state.conversation.conversation_id}/turns`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.lastSent = payload;
  await loadHistory(state.conversation.conversation_id);
  return payload;
}

export async function stopTurn(turnId) {
  await api(`/api/v1/chat/turns/${turnId}/stop`, { method: "POST", body: "{}" });
  await refreshAfterAction();
}

export async function decideApproval(approvalId, decision) {
  await api(`/api/v1/approvals/${approvalId}`, {
    method: "POST",
    body: JSON.stringify({ decision }),
  });
  await refreshAfterAction();
}

export async function cancelTask(taskId) {
  await api(`/api/v1/tasks/${taskId}/cancel`, { method: "POST", body: "{}" });
  await refreshAfterAction();
}

export async function answerWorkStep(turnId, stepId, revision, text) {
  const value = String(text || "").trim();
  if (!value) throw new ApiError(400, "EMPTY_MESSAGE");
  await api(`/api/v1/chat/turns/${turnId}/permission`, {
    method: "POST",
    body: JSON.stringify({
      request_id: uuid(),
      step_id: stepId,
      expected_revision: revision,
      text: value,
    }),
  });
  await refreshAfterAction();
}

export async function answerPermission(turnId, stepId, revision, agree) {
  await answerWorkStep(turnId, stepId, revision, agree ? "同意" : "不同意");
}

export async function decideAction(turnId, digest, decision, allowResult = false) {
  await api(`/api/v1/chat/turns/${turnId}/action`, {
    method: "POST",
    body: JSON.stringify({
      proposal_digest: digest,
      decision,
      allow_result_model: decision === "confirm" && !!allowResult,
    }),
  });
  await refreshAfterAction();
}

export async function previewResult(turnId) {
  const value = await api(`/api/v1/chat/turns/${turnId}/result-preview`, {
    method: "POST",
    body: JSON.stringify({ fields: null }),
  });
  state.previews.set(turnId, value.preview);
  await loadHistory(state.conversation.conversation_id);
  return value.preview;
}

export async function consentResult(turnId, preview) {
  await api(`/api/v1/chat/turns/${turnId}/result-consent`, {
    method: "POST",
    body: JSON.stringify({
      result_digest: preview.result_digest,
      expected_revision: preview.expected_revision,
      allow_model: true,
    }),
  });
  state.previews.delete(turnId);
  await refreshAfterAction();
}

export async function renameConversation(conversationId, title) {
  const value = await api(`/api/v1/chat/conversations/${conversationId}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
  await loadConversations(state.archived);
  return value.conversation;
}

export async function archiveConversation(conversationId, archived) {
  const value = await api(`/api/v1/chat/conversations/${conversationId}`, {
    method: "PATCH",
    body: JSON.stringify({ archived: !!archived }),
  });
  await loadConversations(state.archived);
  return value.conversation;
}

export async function createProject(name, description = "") {
  const value = await api("/api/v1/projects", {
    method: "POST",
    body: JSON.stringify({ name, description }),
  });
  await loadProjects();
  return value.project;
}

export function conversationTitle(conversation = state.conversation) {
  return conversation?.title?.trim() || "新对话";
}

export function projectName(projectId) {
  return state.projects.find((item) => item.project_id === projectId)?.name || "";
}

export function nodeName(nodeId) {
  const node = state.nodes.find((item) => item.node_id === nodeId);
  if (!node) return nodeId ? "设备" : "Core 自动选择设备";
  return node.label || `设备 ${state.nodes.indexOf(node) + 1}`;
}

export function nodeOnline(nodeId) {
  return !!state.nodes.find((item) => item.node_id === nodeId)?.online;
}

export function activeTurnOfConversation() {
  if (!state.activeTurn) return null;
  return state.turns.get(state.activeTurn) || null;
}

export function pendingApprovals() {
  const items = [];
  for (const value of state.tasks.values()) {
    const task = value?.task;
    if (!task || task.status !== "awaiting_approval") continue;
    const approval = (value.approvals || []).find((item) => !item.decision);
    if (approval && new Date(approval.expires_at) > new Date()) {
      items.push({ task, approval });
    }
  }
  return items.sort((a, b) => a.approval.expires_at.localeCompare(b.approval.expires_at));
}

export function pendingPermissions() {
  const turn = activeTurnOfConversation();
  if (!turn || turn.work_status !== "permission") return [];
  return (turn.work_steps || []).filter((step) => step.status === "permission");
}
