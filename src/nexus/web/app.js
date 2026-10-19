"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  csrf: null, socket: null, generation: 0, cursor: null, retry: 0, timer: null,
  logged: false, tasks: new Map(), pending: new Map(), projects: [],
  before: null, recordCursor: {memories: "0", routes: "0"}, controlRevision: -1,
};
const statuses = {queued:"排队中", awaiting_approval:"等待审批", ready:"待派发", dispatching:"派发中", running:"执行中", recovering:"正在对账", verifying:"正在验证", succeeded:"已成功", failed:"已失败", cancelled:"已取消", timed_out:"已超时"};
const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
const kinds = {fact:"事实", inference:"推测", plan:"计划"};
let memoryRetry = null;
let languageRetry = null;
let languageBefore = null;
let languagePolling = true;
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = "notice" + (error ? " error" : "");
  $("notice").hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { $("notice").hidden = true; }, 7000);
}
function connection(text, live = false) {
  $("connection").textContent = text;
  $("connection").className = "connection" + (live ? " live" : "");
  document.querySelectorAll("#system-info, #list-directory, #pause, #resume, #cancel-all").forEach(b => {b.disabled = !live;});
}
function loggedOut() {
  state.logged = false; state.csrf = null; state.cursor = null;
  state.generation++; clearTimeout(state.timer);
  if (state.socket) state.socket.close();
  state.socket = null; state.pending.clear(); state.tasks.clear(); state.projects = [];
  memoryRetry = null;
  languageRetry = null; languageBefore = null;
  chatReset();
  if (typeof extensionReset === "function") extensionReset();
  $("language-form").reset(); $("language-requests").replaceChildren();
  state.controlRevision = -1;
  ["tasks", "memories", "routes", "projects", "capabilities"].forEach(id => $(id).replaceChildren());
  ["task-project", "memory-project"].forEach(id => $(id).replaceChildren(new Option("不关联项目", "")));
  $("memory-form").reset(); $("project-form").reset();
  $("console").hidden = true; $("login-panel").hidden = false; $("logout").hidden = true;
  connection("尚未登录");
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000), ...options,
    headers: {"Content-Type":"application/json", ...(state.csrf ? {"X-CSRF-Token":state.csrf} : {}), ...options.headers},
  });
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401) loggedOut();
    throw new Error(typeof value.detail === "string" ? value.detail : "输入无效，请检查后重试");
  }
  return value;
}
const action = (fn) => async (event) => {
  if (event) event.preventDefault();
  const submitter = event && event.submitter;
  if (submitter && submitter.disabled) return;
  if (submitter) submitter.disabled = true;
  try { await fn(event); } catch (error) { notify(error.message || "连接失败", true); }
  finally {if (submitter) submitter.disabled = false;}
};
function envelope(type, payload) {
  return {protocol_version:1, type, message_id:crypto.randomUUID(), correlation_id:null, sent_at:new Date().toISOString(), payload};
}
function send(type, payload) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) throw new Error("连接尚未就绪，请等待重连");
  const message = envelope(type, payload);
  state.socket.send(JSON.stringify(message));
  return message.message_id;
}
function setControl(value) {
  if (value.revision < state.controlRevision) return;
  state.controlRevision = value.revision;
  $("control-state").textContent = value.paused ? "已暂停派发" : "派发已开启";
  $("control-state").className = "badge " + (value.paused ? "pending" : "success");
}
async function nodes() {
  const value = await api("/api/v1/nodes");
  if (!state.logged) return;
  const selected = $("node").value;
  $("node").replaceChildren();
  for (const node of value.nodes) $("node").add(new Option(node.node_id, node.node_id));
  if (!value.nodes.length) $("node").add(new Option("暂无配置节点", ""));
  if (value.nodes.some(n => n.node_id === selected)) $("node").value = selected;
  const node = value.nodes.find(n => n.node_id === $("node").value);
  $("node-status").textContent = node ? (node.online ? "在线 · 已认证" : "离线 · 任务会等待节点") : "请先在 Core 配置节点";
  $("capabilities").replaceChildren(...(node ? node.tools : []).map(t => el("span", t)));
  chatNodes(value.nodes);
}
function putTask(task, detail) {
  const previous = state.tasks.get(task.task_id);
  if (previous && previous.task.revision > task.revision) return;
  state.tasks.set(task.task_id, {task, detail});
  if (state.tasks.size > 100) {
    const oldest = [...state.tasks.values()].sort((a,b) => a.task.created_at.localeCompare(b.task.created_at))[0];
    state.tasks.delete(oldest.task.task_id);
  }
  renderTasks();
  chatRender();
}
function taskCard({task, detail}) {
    const card = el("article", undefined, "task"); card.dataset.taskId = task.task_id;
    const heading = el("div", undefined, "section-heading");
    heading.append(el("h3", task.request.intent === "get_system_info" ? "读取系统信息" : "列出 workspace 目录"));
    heading.append(el("span", statuses[task.status] || task.status, "badge " + (task.status === "succeeded" ? "success" : task.status === "awaiting_approval" ? "pending" : ["failed", "timed_out"].includes(task.status) ? "failed" : "")));
    card.append(heading, el("p", `${new Date(task.created_at).toLocaleString()} · ${task.task_id}`, "meta"));
    const project = state.projects.find(p => p.project_id === task.project_id);
    if (task.project_id) card.append(el("small", `项目：${project ? project.name : task.project_id}`));
    if (task.cancel_requested) card.append(el("p", terminal.has(task.status) ? "已记录取消请求；以最终执行证据为准。" : "已请求取消，等待节点确认实际停止。", "hint"));
    if (task.execution_outcome === "unknown") card.append(el("p", "执行结果未知 · Core 正在保留证据并对账", "hint"));
    if (task.result) card.append(el("pre", JSON.stringify(task.result, null, 2)));
    if (task.error) card.append(el("p", `${task.error.code} · ${task.error.message}`, "hint"));
    if (task.verification) card.append(el("p", `验证级别：${task.verification}`, "hint"));
    if (task.result && task.result.truncated) card.append(el("p", "结果已截断，以上不是完整目录。", "hint"));
    const buttons = el("div", undefined, "button-row");
    if (task.status === "awaiting_approval" && detail && detail.approvals.length) {
      const approval = detail.approvals[0];
      card.append(el("pre", JSON.stringify({node_id:task.node_id, action:task.action, expires_at:approval.expires_at}, null, 2)));
      if (!approval.decision && new Date(approval.expires_at) > new Date()) {
        for (const [decision, label] of [["reject","拒绝"],["approve","批准本次任务"]]) {
          const button = el("button", label, decision === "approve" ? "primary" : "quiet");
          button.onclick = action(async () => {send("approval.decide", {approval_id:approval.approval_id, decision}); button.disabled = true;});
          buttons.append(button);
        }
      } else card.append(el("p", "审批已决定或已到期，等待 Core 更新状态。", "hint"));
    }
    if (!terminal.has(task.status)) {
      const cancel = el("button", "取消任务", "quiet");
      cancel.onclick = action(async () => {send("task.cancel", {task_id:task.task_id}); cancel.disabled = true;});
      buttons.append(cancel);
    }
    const more = el("button", "执行记录", "quiet");
    more.onclick = action(async () => {
      const value = await api("/api/v1/tasks/" + task.task_id);
      let block = card.querySelector(".evidence");
      if (block) {block.remove(); return;}
      block = el("pre", JSON.stringify({attempts:value.attempts, approvals:value.approvals, events:value.events}, null, 2), "evidence");
      card.append(block);
    });
    buttons.append(more); card.append(buttons); return card;
}
function renderTasks() {
  const cards = [...state.tasks.values()].sort((a,b) => b.task.created_at.localeCompare(a.task.created_at)).map(taskCard);
  $("tasks").replaceChildren(...cards); $("empty-tasks").hidden = cards.length > 0;
}
async function updateTask(id) {
  const value = await api("/api/v1/tasks/" + id);
  if (state.logged) putTask(value.task, value);
}
async function taskPage(reset = false) {
  const value = await api("/api/v1/client/snapshot" + (!reset && state.before ? "?before=" + state.before : ""));
  if (!state.logged) return;
  if (reset) state.tasks.clear();
  for (const task of value.tasks) {
    state.tasks.set(task.task_id, {task});
    if (task.status === "awaiting_approval") await updateTask(task.task_id);
  }
  while (state.tasks.size > 100) state.tasks.delete(state.tasks.keys().next().value);
  state.before = value.next_before;
  $("older-tasks").hidden = !value.has_more;
  setControl(value.control); renderTasks();
}
function connect() {
  const generation = ++state.generation;
  clearTimeout(state.timer);
  if (state.socket) state.socket.close();
  const url = new URL("/ws/v1/client", location.href); url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  if ($("node").value) url.searchParams.set("node_id", $("node").value);
  const socket = new WebSocket(url); state.socket = socket;
  let chain = Promise.resolve(), backlog = 0, aborted = false;
  connection("正在连接");
  socket.onopen = () => {if (generation === state.generation) send("client.sync", {event_id:state.cursor});};
  socket.onmessage = (event) => {
    if (aborted || generation !== state.generation) return;
    if (++backlog > 64) {aborted = true; state.cursor = null; socket.close(); return;}
    chain = chain.then(async () => {
      if (aborted || generation !== state.generation) return;
      const message = JSON.parse(event.data), p = message.payload;
      if (message.type === "client.synced") {
        if (p.full_sync_required) await taskPage(true);
        if (aborted || generation !== state.generation) return;
        state.cursor = p.event_id; state.retry = 0; connection("已连接 · 实时同步", true);
        for (const request of state.pending.values()) {
          if (request.node_id === $("node").value) request.message_id = send("task.submit", request.payload);
        }
      } else if (message.type === "task.accepted") {
        await updateTask(p.task_id);
        const item = state.tasks.get(p.task_id);
        if (item) state.pending.delete(item.task.request.request_id);
      } else if (["task.updated", "approval.required", "control.updated"].includes(message.type)) {
        if (state.cursor !== null && BigInt(p.event_id) <= BigInt(state.cursor)) return;
        if (message.type === "control.updated") setControl(p);
        else await updateTask(p.task_id);
        if (!aborted && generation === state.generation) state.cursor = p.event_id;
      } else if (message.type === "error") {
        notify(`${p.code} · ${p.message}`, true);
        // Rejected submissions are not blindly retried on reconnect.
        for (const [id, request] of state.pending) if (request.message_id === message.correlation_id) state.pending.delete(id);
      }
    }).catch(error => {if (generation === state.generation) {aborted = true; notify(error.message, true); state.cursor = null; socket.close();}}).finally(() => {backlog--;});
  };
  socket.onclose = async (event) => {
    if (generation !== state.generation || !state.logged) return;
    connection("连接中断 · 任务继续由 Core 管理");
    if (event.code === 1008) {loggedOut(); notify("会话或连接权限已失效，请重新登录", true); return;}
    state.timer = setTimeout(() => {if (state.logged) connect();}, Math.min(30000, 1000 * 2 ** Math.min(state.retry++, 5)) + Math.random()*300);
  };
}
// Keep only the request payload in the bounded retry map. Message IDs identify
// individual sends; request_id remains stable across connection loss.
function submit(intent) {
  if (!$("node").value) throw new Error("请先配置执行节点");
  if (state.pending.size >= 10) throw new Error("待确认任务已达上限，请等待同步");
  const limit = Number($("limit").value);
  if (intent === "list_directory" && (!Number.isInteger(limit) || limit < 1 || limit > 200)) throw new Error("条目数须为 1–200 的整数");
  const payload = {request_id:crypto.randomUUID(), intent, arguments:intent === "list_directory" ? {root_id:"workspace", limit} : {}, project_id:$("task-project").value || null};
  const message_id = send("task.submit", payload);
  state.pending.set(payload.request_id, {payload, message_id, node_id:$("node").value});
  notify("任务已提交，等待 Core 确认");
}
async function loadProjects() {
  const projects = []; let cursor = "0";
  for (let page = 0; page < 10; page++) {
    const value = await api("/api/v1/records/projects?limit=100&after=" + cursor);
    projects.push(...value.projects); cursor = value.next_after;
    if (value.projects.length < 100) break;
  }
  if (!state.logged) return;
  state.projects = projects;
  for (const id of ["task-project", "memory-project"]) {
    const selected = $(id).value; $(id).replaceChildren(new Option("不关联项目", ""));
    for (const project of projects) $(id).add(new Option(project.name, project.project_id));
    if (projects.some(p => p.project_id === selected)) $(id).value = selected;
  }
  $("projects").replaceChildren(...projects.map(p => el("span", p.name)));
}
async function records(kind, reset = true) {
  const cursor = reset ? "0" : state.recordCursor[kind];
  const history = kind === "memories" && $("memory-history").checked ? "&history=true" : "";
  const value = await api(`/api/v1/records/${kind}?limit=20&after=${cursor}${history}`);
  if (!state.logged) return;
  if (reset) $(kind).replaceChildren();
  for (const item of value[kind]) {
    const card = el("article", undefined, "record");
    if (kind === "memories") {
      card.append(el("h3", `${kinds[item.kind]} · ${item.verification === "verified" ? "有验证依据" : "未经验证"}`), el("p", item.content));
      const inactive = item.retracted_at ? "已撤销" : item.superseded_at ? "已被替代" : item.valid_until && new Date(item.valid_until) <= new Date() ? "已过期" : "当前有效";
      card.append(el("p", `${inactive} · 来源：${item.source} · 有效至：${item.valid_until ? new Date(item.valid_until).toLocaleString() : "未设期限"}`, "meta"));
      card.append(el("pre", JSON.stringify({memory_id:item.memory_id, project_id:item.project_id, consent_event_id:item.consent_event_id, evidence_ref:item.evidence_ref, verified_at:item.verified_at, supersedes_id:item.supersedes_id}, null, 2)));
      if (!item.retracted_at) {const retract = el("button", "撤销本条记忆", "quiet"); retract.onclick = action(async () => {await api(`/api/v1/memories/${item.memory_id}/retract`, {method:"POST", body:"{}"}); await records("memories");}); card.append(retract);}
    } else {
      card.append(el("h3", `${item.mode} · ${item.reason_code}`), el("p", `${item.tool}@${item.tool_version} · ${statuses[item.status]} · 策略 ${item.policy_version}`, "meta"));
      card.append(el("pre", JSON.stringify({task_id:item.task_id, project_id:item.project_id, verification:item.verification, elapsed_ms:item.elapsed_ms, origin:item.origin, recorded_at:item.recorded_at}, null, 2)));
    }
    $(kind).append(card);
  }
  if (!$(kind).children.length) $(kind).append(el("p", kind === "memories" ? "没有符合条件的记忆。" : "还没有路由记录。", "empty"));
  while ($(kind).children.length > 100) $(kind).firstChild.remove();
  state.recordCursor[kind] = value.next_after;
  $("more-" + kind).hidden = value[kind].length < 20;
}
async function usage() {
  const value = await api("/api/v1/models/usage");
  if (!state.logged) return;
  $("model-calls").replaceChildren(document.createTextNode(value.calls + " "), el("small", "次调用"));
  $("model-cost").textContent = `费用 ${value.cost_microunits === null ? "未知（保留预留额度）" : "$" + value.cost_microunits / 1000000 + "（公开峰值单价上界）"} · 模型成功不等于任务成功`;
  document.querySelector(".model-badge").textContent = value.enabled ? "规则优先 · DeepSeek 按次同意" : "规则路由 · 模型已禁用";
  chatAvailability(value.chat_enabled);
}
async function languagePage(older = false) {
  const value = await api("/api/v1/language" + (older && languageBefore ? "?before=" + languageBefore : ""));
  if (!state.logged) return;
  languagePolling = !older;
  if (!older) $("language-requests").replaceChildren();
  const labels = {queued:"等待解释", interpreting:"模型解释中", proposed:"请确认动作", submitted:"已创建执行任务", rejected:"不支持的任务", failed:"解释失败", cancelled:"已取消", expired:"已过期"};
  for (const item of value.requests) {
    const card = el("article", undefined, "record"); card.dataset.languageId = item.language_id;
    card.append(el("h3", labels[item.status] || item.status), el("p", item.text), el("p", `${item.mode || "等待路由"} · ${item.language_id}`, "meta"));
    if (item.error_code) card.append(el("p", item.error_code, "hint"));
    if (item.proposal) card.append(el("pre", JSON.stringify({node_id:item.node_id, action:item.proposal, expires_at:item.expires_at}, null, 2)));
    const buttons = el("div", undefined, "button-row");
    if (item.status === "proposed") {
      const confirm = el("button", "确认此动作并创建任务", "primary");
      confirm.onclick = action(async () => {confirm.disabled = true; try {const result = await api(`/api/v1/language/${item.language_id}/confirm`, {method:"POST", body:"{}"}); await updateTask(result.task.task_id); await languagePage();} finally {confirm.disabled = false;}});
      buttons.append(confirm);
    }
    if (["queued", "interpreting", "proposed"].includes(item.status)) {
      const cancel = el("button", "取消解释请求", "quiet");
      cancel.onclick = action(async () => {await api(`/api/v1/language/${item.language_id}/cancel`, {method:"POST", body:"{}"}); await languagePage();});
      buttons.append(cancel);
    }
    if (item.task_id) {
      const task = el("button", "查看执行任务", "quiet");
      task.onclick = action(async () => {await updateTask(item.task_id); document.querySelector(`[data-task-id="${item.task_id}"]`)?.scrollIntoView({block:"center"});});
      buttons.append(task);
    }
    card.append(buttons); $("language-requests").append(card);
  }
  while ($("language-requests").children.length > 100) $("language-requests").firstChild.remove();
  languageBefore = value.requests.at(-1)?.cursor || null;
  $("older-language").hidden = value.requests.length < 20;
}
async function enter(session) {
  state.csrf = session.csrf_token; state.logged = true;
  $("login-panel").hidden = true; $("console").hidden = false; $("logout").hidden = false;
  try {await Promise.all([nodes(), loadProjects(), usage(), languagePage(), chatList(), typeof extensionStatus === "function" ? extensionStatus() : Promise.resolve()]);}
  catch (error) {loggedOut(); throw error;}
  connect();
}
$("login-form").onsubmit = action(async () => {
  const token = $("token").value; $("token").value = "";
  const session = await api("/api/v1/session", {method:"POST", body:JSON.stringify({token})});
  await enter(session);
});
$("logout").onclick = action(async () => {await api("/api/v1/session", {method:"DELETE"}); loggedOut();});
$("node").onchange = action(async () => {await nodes(); connect();});
$("system-info").onclick = action(async () => submit("get_system_info"));
$("list-directory").onclick = action(async () => submit("list_directory"));
$("language-form").onsubmit = action(async () => {
  if (!$("node").value) throw new Error("请先选择节点");
  const value = {request_id:crypto.randomUUID(), node_id:$("node").value, project_id:$("task-project").value || null, text:$("language-text").value, allow_model:$("language-consent").checked};
  const signature = JSON.stringify({...value, request_id:null});
  if (languageRetry?.signature === signature) value.request_id = languageRetry.request_id;
  languageRetry = {signature, request_id:value.request_id};
  await api("/api/v1/language", {method:"POST", body:JSON.stringify(value)});
  languageRetry = null; $("language-consent").checked = false; await languagePage(); notify("解释请求已保存，请检查动作后确认");
});
$("language-text").oninput = () => {$("language-consent").checked = false;};
$("refresh-language").onclick = action(async () => {await languagePage(); await usage();});
$("older-language").onclick = action(async () => languagePage(true));
for (const kind of ["pause", "resume", "cancel_all"]) $(kind.replace("_", "-")).onclick = action(async () => {send("control." + kind, {request_id:crypto.randomUUID()}); notify("控制请求已发送，等待 Core 更新");});
$("refresh-tasks").onclick = action(async () => taskPage(true));
$("older-tasks").onclick = action(async () => taskPage());
$("project-form").onsubmit = action(async () => {await api("/api/v1/projects", {method:"POST", body:JSON.stringify({name:$("project-name").value, description:$("project-description").value})}); $("project-form").reset(); await loadProjects(); notify("项目已创建");});
$("memory-form").onsubmit = action(async () => {
  const verified = $("verification").value === "verified";
  if (verified && (!$("evidence").value.trim() || !$("verified-at").value)) throw new Error("请填写验证依据和验证时间");
  const value = {request_id:crypto.randomUUID(), consent:$("consent").checked, content:$("memory-content").value, kind:$("memory-kind").value, source:$("memory-source").value, project_id:$("memory-project").value || null, verification:$("verification").value, evidence_ref:$("evidence").value || null, verified_at:verified ? new Date($("verified-at").value).toISOString() : null, valid_until:$("valid-until").value ? new Date($("valid-until").value).toISOString() : null, supersedes_id:$("supersedes").value || null};
  const signature = JSON.stringify({...value, request_id:null});
  if (memoryRetry && memoryRetry.signature === signature) value.request_id = memoryRetry.request_id;
  memoryRetry = {signature, request_id:value.request_id};
  await api("/api/v1/memories", {method:"POST", body:JSON.stringify(value)});
  memoryRetry = null; $("memory-form").reset(); await records("memories"); notify("记忆及明确同意记录已保存");
});
$("memory-history").onchange = action(async () => records("memories"));
$("more-memories").onclick = action(async () => records("memories", false));
$("more-routes").onclick = action(async () => records("routes", false));
$("refresh-routes").onclick = action(async () => {await Promise.all([records("routes"), usage()]);});
document.querySelectorAll("[data-tab]").forEach(button => {button.onclick = action(async () => {
  for (const tab of ["chat", "tasks", "extensions", "memories", "routes"]) $(tab + "-view").hidden = tab !== button.dataset.tab;
  document.querySelector(".workspace").classList.toggle("chat-mode",button.dataset.tab==="chat");
  document.querySelectorAll("[data-tab]").forEach(b => {b.classList.toggle("active", b === button); b.setAttribute("aria-pressed", String(b === button));});
  if (button.dataset.tab === "chat") await Promise.all([chatList(),usage()]);
  else if (button.dataset.tab === "extensions") { if (typeof extensionPage === "function") await extensionPage(); }
  else if (button.dataset.tab !== "tasks") await records(button.dataset.tab);
});});
setInterval(() => {if (state.logged) nodes().catch(error => notify(error.message, true));}, 5000);
setInterval(() => {if (state.logged && languagePolling && !$("tasks-view").hidden) Promise.all([languagePage(), usage()]).catch(error => notify(error.message, true));}, 3000);
window.addEventListener("pagehide", () => {state.generation++; clearTimeout(state.timer); if (state.socket) state.socket.close();});
window.addEventListener("pageshow", event => {if (event.persisted && state.logged) connect();});
initChat();
api("/api/v1/session").then(enter).catch(error => {loggedOut(); if (error.message !== "AUTH_REQUIRED") notify(error.message, true);});
