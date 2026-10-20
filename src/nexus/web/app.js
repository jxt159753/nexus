"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  csrf: null, socket: null, generation: 0, cursor: null, retry: 0, timer: null,
  logged: false, tasks: new Map(), pending: new Map(), projects: [],
  before: null, recordCursor: {memories: "0", routes: "0"}, controlRevision: -1,
};
const statuses = {queued:"�Ŷ���", awaiting_approval:"�ȴ�����", ready:"���ɷ�", dispatching:"�ɷ���", running:"ִ����", recovering:"���ڶ���", verifying:"������֤", succeeded:"�ѳɹ�", failed:"��ʧ��", cancelled:"��ȡ��", timed_out:"�ѳ�ʱ"};
const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
const kinds = {fact:"��ʵ", inference:"�Ʋ�", plan:"�ƻ�"};
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
  projectReset(); basicReset();
  if (typeof extensionReset === "function") extensionReset();
  $("language-form").reset(); $("language-requests").replaceChildren();
  state.controlRevision = -1;
  ["tasks", "memories", "routes", "projects", "capabilities"].forEach(id => $(id).replaceChildren());
  ["task-project", "memory-project"].forEach(id => $(id).replaceChildren(new Option("��������Ŀ", "")));
  $("memory-form").reset(); $("project-form").reset();
  $("console").hidden = true; $("login-panel").hidden = false; $("logout").hidden = true;
  connection("��δ��¼");
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000), ...options,
    headers: {"Content-Type":"application/json", ...(state.csrf ? {"X-CSRF-Token":state.csrf} : {}), ...options.headers},
  });
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401) loggedOut();
    throw new Error(typeof value.detail === "string" ? value.detail : "������Ч�����������");
  }
  return value;
}
const action = (fn) => async (event) => {
  if (event) event.preventDefault();
  const submitter = event && event.submitter;
  if (submitter && submitter.disabled) return;
  if (submitter) submitter.disabled = true;
  try { await fn(event); } catch (error) { notify(error.message || "����ʧ��", true); }
  finally {if (submitter) submitter.disabled = false;}
};
function envelope(type, payload) {
  return {protocol_version:1, type, message_id:crypto.randomUUID(), correlation_id:null, sent_at:new Date().toISOString(), payload};
}
function send(type, payload) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) throw new Error("������δ��������ȴ�����");
  const message = envelope(type, payload);
  state.socket.send(JSON.stringify(message));
  return message.message_id;
}
function setControl(value) {
  if (value.revision < state.controlRevision) return;
  state.controlRevision = value.revision;
  $("control-state").textContent = value.paused ? "����ͣ�ɷ�" : "�ɷ��ѿ���";
  $("control-state").className = "badge " + (value.paused ? "pending" : "success");
}
async function nodes() {
  const value = await api("/api/v1/nodes");
  if (!state.logged) return;
  const selected = $("node").value;
  $("node").replaceChildren();
  for (const node of value.nodes) $("node").add(new Option(node.node_id, node.node_id));
  if (!value.nodes.length) $("node").add(new Option("�������ýڵ�", ""));
  if (value.nodes.some(n => n.node_id === selected)) $("node").value = selected;
  const node = value.nodes.find(n => n.node_id === $("node").value);
  $("node-status").textContent = node ? (node.online ? "���� �� ����֤" : "���� �� �����ȴ��ڵ�") : "������ Core ���ýڵ�";
  $("capabilities").replaceChildren(...(node ? node.tools : []).map(t => el("span", t)));
  chatNodes(value.nodes);
  basicNodes(value.nodes);
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
  basicUpdatePreview();
}
function taskCard({task, detail}) {
    const card = el("article", undefined, "task"); card.dataset.taskId = task.task_id;
    const heading = el("div", undefined, "section-heading");
    heading.append(el("h3", toolLabels[task.request.intent] || task.request.intent));
    heading.append(el("span", statuses[task.status] || task.status, "badge " + (task.status === "succeeded" ? "success" : task.status === "awaiting_approval" ? "pending" : ["failed", "timed_out"].includes(task.status) ? "failed" : "")));
    card.append(heading, el("p", `${new Date(task.created_at).toLocaleString()} �� ${task.task_id}`, "meta"));
    const project = state.projects.find(p => p.project_id === task.project_id);
    if (task.project_id) card.append(el("small", `��Ŀ��${project ? project.name : task.project_id}`));
    if (task.cancel_requested) card.append(el("p", terminal.has(task.status) ? "�Ѽ�¼ȡ������������ִ��֤��Ϊ׼��" : "������ȡ�����ȴ��ڵ�ȷ��ʵ��ֹͣ��", "hint"));
    if (task.execution_outcome === "unknown") card.append(el("p", "ִ�н��δ֪ �� Core ���ڱ���֤�ݲ�����", "hint"));
    if (task.result) card.append(el("pre", JSON.stringify(task.result, null, 2)));
    if (task.error) card.append(el("p", `${task.error.code} �� ${task.error.message}`, "hint"));
    if (task.verification) card.append(el("p", `��֤����${task.verification}`, "hint"));
    if (task.result && task.result.truncated) card.append(el("p", "����ѽضϣ����ϲ�������Ŀ¼��", "hint"));
    const buttons = el("div", undefined, "button-row");
    if (task.status === "awaiting_approval" && detail && detail.approvals.length) {
      const approval = detail.approvals[0];
      card.append(el("pre", JSON.stringify({node_id:task.node_id, action:task.action, expires_at:approval.expires_at}, null, 2)));
      if (!approval.decision && new Date(approval.expires_at) > new Date()) {
        for (const [decision, label] of [["reject","�ܾ�"],["approve","��׼��������"]]) {
          const button = el("button", label, decision === "approve" ? "primary" : "quiet");
          button.onclick = action(async () => {send("approval.decide", {approval_id:approval.approval_id, decision}); button.disabled = true;});
          buttons.append(button);
        }
      } else card.append(el("p", "�����Ѿ������ѵ��ڣ��ȴ� Core ����״̬��", "hint"));
    }
    if (!terminal.has(task.status)) {
      const cancel = el("button", "ȡ������", "quiet");
      cancel.onclick = action(async () => {send("task.cancel", {task_id:task.task_id}); cancel.disabled = true;});
      buttons.append(cancel);
    }
    const more = el("button", "ִ�м�¼", "quiet");
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
  connection("��������");
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
        state.cursor = p.event_id; state.retry = 0; connection("������ �� ʵʱͬ��", true);
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
        notify(`${p.code} �� ${p.message}`, true);
        // Rejected submissions are not blindly retried on reconnect.
        for (const [id, request] of state.pending) if (request.message_id === message.correlation_id) state.pending.delete(id);
      }
    }).catch(error => {if (generation === state.generation) {aborted = true; notify(error.message, true); state.cursor = null; socket.close();}}).finally(() => {backlog--;});
  };
  socket.onclose = async (event) => {
    if (generation !== state.generation || !state.logged) return;
    connection("�����ж� �� ��������� Core ����");
    if (event.code === 1008) {loggedOut(); notify("�Ự������Ȩ����ʧЧ�������µ�¼", true); return;}
    state.timer = setTimeout(() => {if (state.logged) connect();}, Math.min(30000, 1000 * 2 ** Math.min(state.retry++, 5)) + Math.random()*300);
  };
}
// Keep only the request payload in the bounded retry map. Message IDs identify
// individual sends; request_id remains stable across connection loss.
function submit(intent) {
  if (!$("node").value) throw new Error("��������ִ�нڵ�");
  if (state.pending.size >= 10) throw new Error("��ȷ�������Ѵ����ޣ���ȴ�ͬ��");
  const limit = Number($("limit").value);
  if (intent === "list_directory" && (!Number.isInteger(limit) || limit < 1 || limit > 200)) throw new Error("��Ŀ����Ϊ 1�C200 ������");
  const payload = {request_id:crypto.randomUUID(), intent, arguments:intent === "list_directory" ? {root_id:"workspace", limit} : {}, project_id:$("task-project").value || null};
  const message_id = send("task.submit", payload);
  state.pending.set(payload.request_id, {payload, message_id, node_id:$("node").value});
  notify("�������ύ���ȴ� Core ȷ��");
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
  for (const id of ["task-project", "memory-project", "chat-project", "project-workspace"]) {
    const selected = $(id).value; $(id).replaceChildren(new Option("��������Ŀ", ""));
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
      card.append(el("h3", `${kinds[item.kind]} �� ${item.verification === "verified" ? "����֤����" : "δ����֤"}`), el("p", item.content));
      const inactive = item.retracted_at ? "�ѳ���" : item.superseded_at ? "�ѱ����" : item.valid_until && new Date(item.valid_until) <= new Date() ? "�ѹ���" : "��ǰ��Ч";
      card.append(el("p", `${inactive} �� ��Դ��${item.source} �� ��Ч����${item.valid_until ? new Date(item.valid_until).toLocaleString() : "δ������"}`, "meta"));
      card.append(el("pre", JSON.stringify({memory_id:item.memory_id, project_id:item.project_id, consent_event_id:item.consent_event_id, evidence_ref:item.evidence_ref, verified_at:item.verified_at, supersedes_id:item.supersedes_id}, null, 2)));
      if (!item.retracted_at) {const retract = el("button", "������������", "quiet"); retract.onclick = action(async () => {await api(`/api/v1/memories/${item.memory_id}/retract`, {method:"POST", body:"{}"}); await records("memories");}); card.append(retract);}
    } else {
      card.append(el("h3", `${item.mode} �� ${item.reason_code}`), el("p", `${item.tool}@${item.tool_version} �� ${statuses[item.status]} �� ���� ${item.policy_version}`, "meta"));
      card.append(el("pre", JSON.stringify({task_id:item.task_id, project_id:item.project_id, verification:item.verification, elapsed_ms:item.elapsed_ms, origin:item.origin, recorded_at:item.recorded_at}, null, 2)));
    }
    $(kind).append(card);
  }
  if (!$(kind).children.length) $(kind).append(el("p", kind === "memories" ? "û�з��������ļ��䡣" : "��û��·�ɼ�¼��", "empty"));
  while ($(kind).children.length > 100) $(kind).firstChild.remove();
  state.recordCursor[kind] = value.next_after;
  $("more-" + kind).hidden = value[kind].length < 20;
}
async function usage() {
  const value = await api("/api/v1/models/usage");
  if (!state.logged) return;
  $("model-calls").replaceChildren(document.createTextNode(value.calls + " "), el("small", "�� API ���ã�����ҳ��"));
  $("model-cost").textContent = `���� ${value.cost_microunits === null ? "δ֪������Ԥ����ȣ�" : "$" + value.cost_microunits / 1000000 + "��������ֵ�����Ͻ磩"} �� ģ�ͳɹ�����������ɹ�`;
  document.querySelector(".model-badge").textContent = value.enabled ? "�������� �� DeepSeek ����ͬ��" : "����·�� �� ģ���ѽ���";
  chatAvailability(value.chat_enabled,value.web_enabled);
}
async function languagePage(older = false) {
  const value = await api("/api/v1/language" + (older && languageBefore ? "?before=" + languageBefore : ""));
  if (!state.logged) return;
  languagePolling = !older;
  if (!older) $("language-requests").replaceChildren();
  const labels = {queued:"�ȴ�����", interpreting:"ģ�ͽ�����", proposed:"��ȷ�϶���", submitted:"�Ѵ���ִ������", rejected:"��֧�ֵ�����", failed:"����ʧ��", cancelled:"��ȡ��", expired:"�ѹ���"};
  for (const item of value.requests) {
    const card = el("article", undefined, "record"); card.dataset.languageId = item.language_id;
    card.append(el("h3", labels[item.status] || item.status), el("p", item.text), el("p", `${item.mode || "�ȴ�·��"} �� ${item.language_id}`, "meta"));
    if (item.error_code) card.append(el("p", item.error_code, "hint"));
    if (item.proposal) card.append(el("pre", JSON.stringify({node_id:item.node_id, action:item.proposal, expires_at:item.expires_at}, null, 2)));
    const buttons = el("div", undefined, "button-row");
    if (item.status === "proposed") {
      const confirm = el("button", "ȷ�ϴ˶�������������", "primary");
      confirm.onclick = action(async () => {confirm.disabled = true; try {const result = await api(`/api/v1/language/${item.language_id}/confirm`, {method:"POST", body:"{}"}); await updateTask(result.task.task_id); await languagePage();} finally {confirm.disabled = false;}});
      buttons.append(confirm);
    }
    if (["queued", "interpreting", "proposed"].includes(item.status)) {
      const cancel = el("button", "ȡ����������", "quiet");
      cancel.onclick = action(async () => {await api(`/api/v1/language/${item.language_id}/cancel`, {method:"POST", body:"{}"}); await languagePage();});
      buttons.append(cancel);
    }
    if (item.task_id) {
      const task = el("button", "�鿴ִ������", "quiet");
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
  if (!$("node").value) throw new Error("����ѡ��ڵ�");
  const value = {request_id:crypto.randomUUID(), node_id:$("node").value, project_id:$("task-project").value || null, text:$("language-text").value, allow_model:$("language-consent").checked};
  const signature = JSON.stringify({...value, request_id:null});
  if (languageRetry?.signature === signature) value.request_id = languageRetry.request_id;
  languageRetry = {signature, request_id:value.request_id};
  await api("/api/v1/language", {method:"POST", body:JSON.stringify(value)});
  languageRetry = null; $("language-consent").checked = false; await languagePage(); notify("���������ѱ��棬���鶯����ȷ��");
});
$("language-text").oninput = () => {$("language-consent").checked = false;};
$("refresh-language").onclick = action(async () => {await languagePage(); await usage();});
$("older-language").onclick = action(async () => languagePage(true));
for (const kind of ["pause", "resume", "cancel_all"]) $(kind.replace("_", "-")).onclick = action(async () => {send("control." + kind, {request_id:crypto.randomUUID()}); notify("���������ѷ��ͣ��ȴ� Core ����");});
$("refresh-tasks").onclick = action(async () => taskPage(true));
$("older-tasks").onclick = action(async () => taskPage());
$("project-form").onsubmit = action(async () => {await api("/api/v1/projects", {method:"POST", body:JSON.stringify({name:$("project-name").value, description:$("project-description").value})}); $("project-form").reset(); await loadProjects(); notify("��Ŀ�Ѵ���");});
$("memory-form").onsubmit = action(async () => {
  const verified = $("verification").value === "verified";
  if (verified && (!$("evidence").value.trim() || !$("verified-at").value)) throw new Error("����д��֤���ݺ���֤ʱ��");
  const value = {request_id:crypto.randomUUID(), consent:$("consent").checked, content:$("memory-content").value, kind:$("memory-kind").value, source:$("memory-source").value, project_id:$("memory-project").value || null, verification:$("verification").value, evidence_ref:$("evidence").value || null, verified_at:verified ? new Date($("verified-at").value).toISOString() : null, valid_until:$("valid-until").value ? new Date($("valid-until").value).toISOString() : null, supersedes_id:$("supersedes").value || null};
  const signature = JSON.stringify({...value, request_id:null});
  if (memoryRetry && memoryRetry.signature === signature) value.request_id = memoryRetry.request_id;
  memoryRetry = {signature, request_id:value.request_id};
  await api("/api/v1/memories", {method:"POST", body:JSON.stringify(value)});
  memoryRetry = null; $("memory-form").reset(); await records("memories"); notify("���估��ȷͬ���¼�ѱ���");
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
initProjects();
initBasicTools();
api("/api/v1/session").then(enter).catch(error => {loggedOut(); if (error.message !== "AUTH_REQUIRED") notify(error.message, true);});
