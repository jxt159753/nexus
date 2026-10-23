"use strict";

import {
  ApiError,
  STATUS_LABELS,
  TASK_TERMINAL,
  TOOL_LABELS,
  cancelTask,
  conversationTitle,
  decideAction,
  decideApproval,
  answerPermission,
  consentResult,
  createProject,
  nodeName,
  pendingApprovals,
  pendingPermissions,
  previewResult,
  projectName,
  selectConversation,
  setPref,
  state,
  taskDetail,
  clearPrefs,
} from "./api.js";

import { markdownPreview, renderMarkdown } from "./markdown.js";
import {
  createImageEvidence,
  createImageMetadata,
  imageSummary,
  openImagePreview,
  safeJson,
} from "./image.js";

const $ = (id) => document.getElementById(id);
const presentationUi = new Map();

export const ui = {
  openChat: () => {},
  closeChat: () => {},
  openSettings: () => {},
  openScope: () => {},
  openPending: () => {},
  newConversation: () => {},
  sendFrom: () => {},
  refresh: () => {},
  scrollToTurn: () => {},
  logout: () => {},
  openTurnDetails: () => {},
};

export function bindUI(handlers) {
  Object.assign(ui, handlers);
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function button(label, className, onClick) {
  const node = el("button", className || "", label);
  node.type = "button";
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

export function notify(message, error = false) {
  const box = $("notice");
  if (!box) return;
  box.textContent = message;
  box.className = "notice" + (error ? " error" : "");
  box.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { box.hidden = true; }, 6500);
}

export function errorText(error) {
  const code = error instanceof ApiError ? error.code : error?.message;
  const labels = {
    AUTH_REQUIRED: "会话已过期，请重新登录",
    BUSY: "当前繁忙，请稍后再试",
    IDEMPOTENCY_CONFLICT: "状态已经变化，请刷新后重试",
    UNSUPPORTED_TASK: "这个请求暂不支持",
    POLICY_DENIED: "当前权限或范围不允许这个操作",
    APPROVAL_EXPIRED: "审批已过期，请重新提出",
    NODE_OFFLINE: "设备离线，任务会等待节点",
    EXECUTION_UNKNOWN: "执行结果未知，不能自动重跑",
    MODEL_DISABLED: "模型未启用",
    MODEL_TIMEOUT: "模型响应超时",
    MODEL_REQUEST_FAILED: "模型连接失败",
    MODEL_RESPONSE_INVALID: "模型返回无法校验",
    BUDGET_EXCEEDED: "本轮或每日预算不足",
    RESULT_INVALID: "结果未通过校验",
    RESULT_UNAVAILABLE: "当前没有可回传的结果",
    WORK_PERMISSION_EXPIRED: "本次工作授权已失效",
    EMPTY_MESSAGE: "请先输入内容",
    NO_CONVERSATION: "请先选择会话",
    INVALID_MESSAGE: "输入无法校验，请检查后重试",
    INVALID_RESPONSE: "服务返回了无法解析的内容",
    FORBIDDEN: "操作被拒绝",
    HTTP_503: "Core 暂时不可用",
  };
  return labels[code] || error?.message || "操作失败";
}

export async function runAction(fn, control) {
  if (control) control.disabled = true;
  try {
    await fn();
  } catch (error) {
    notify(errorText(error), true);
  } finally {
    if (control?.isConnected) control.disabled = false;
  }
}

export function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return "今天";
  return date.toLocaleDateString([], { month: "numeric", day: "numeric" });
}

export function money(microunits) {
  if (microunits === null || microunits === undefined) return "费用未知";
  const value = microunits / 1000000;
  return "$" + (value < 0.01 ? value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "") : value.toFixed(3));
}

function short(text, limit = 120) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > limit ? value.slice(0, limit) + "…" : value;
}

function statusBadge(status, label) {
  const kind = status === "succeeded" || status === "completed" ? "success"
    : status === "awaiting_approval" || status === "permission" ? "pending"
      : ["failed", "timed_out", "interrupted"].includes(status) ? "failed"
        : status === "unknown" ? "unknown" : "muted";
  const fallback = { permission: "需要答复", awaiting_approval: "等待审批", proposed: "等待决定", succeeded: "已完成", cancelled: "已取消" };
  return el("span", `badge ${kind}`, label || fallback[status] || STATUS_LABELS[status] || status || "未知");
}

function renderProse(container, text) {
  renderMarkdown(container, text);
}

function card(title, status, className = "") {
  const box = el("section", `card ${className}`.trim());
  const head = el("div", "card-head");
  head.append(el("strong", "", title));
  if (status) head.append(status);
  box.append(head);
  return box;
}

function renderResultData(container, data) {
  if (!data) return;
  if (typeof data === "string") {
    const pre = el("pre");
    pre.textContent = data;
    container.append(pre);
    return;
  }
  if (data.image) {
    const evidence = createImageEvidence(data);
    if (evidence) {
      container.append(evidence);
      return;
    }
    const metadata = createImageMetadata(data);
    if (metadata) {
      container.append(metadata);
      return;
    }
    container.append(el("p", "hint", "图片结果未通过严格白名单校验，不显示预览或内联数据；执行状态以任务记录为准。"));
    return;
  }
  if (data.diff) {
    const pre = el("pre", "diff");
    pre.textContent = data.diff;
    container.append(pre);
    if (data.truncated) container.append(el("p", "hint", "差异已截断；请核对写入前预览。"));
    return;
  }
  if (Array.isArray(data.entries)) {
    const pre = el("pre");
    pre.textContent = data.entries.slice(0, 200).map((entry) => `${entry.kind === "directory" ? "▸" : "·"} ${entry.name}`).join("\n");
    container.append(pre);
    if (data.truncated) container.append(el("p", "hint", "条目已截断，以上不是完整目录。"));
    return;
  }
  if (Array.isArray(data.lines)) {
    const pre = el("pre");
    pre.textContent = data.lines.map((line) => `${line.number}: ${line.text}${line.truncated ? " […本行截断]" : ""}`).join("\n");
    container.append(pre);
    if (data.next_offset) container.append(el("p", "hint", `下一页从第 ${data.next_offset} 行开始。`));
    return;
  }
  if (Array.isArray(data.matches)) {
    const pre = el("pre");
    pre.textContent = data.matches.map((line) => `${line.path}:${line.number}: ${line.text}`).join("\n");
    container.append(pre);
    if (data.truncated) container.append(el("p", "hint", "搜索结果有省略。"));
    return;
  }
  if (Array.isArray(data.sources)) {
    const list = el("div", "sources");
    for (const source of data.sources.slice(0, 5)) {
      if (!/^https?:\/\//i.test(source.url || "")) continue;
      const link = el("a", "", source.title || source.url);
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      list.append(link);
      if (source.snippet) list.append(el("p", "hint", short(source.snippet, 240)));
    }
    container.append(list);
    if (data.truncated) container.append(el("p", "hint", "网页结果已截断。"));
    return;
  }
  if ("text" in data) {
    const pre = el("pre");
    pre.textContent = data.text;
    container.append(pre);
    if (data.truncated) container.append(el("p", "hint", "读取结果已截断。"));
    return;
  }
  if ("stdout" in data) {
    const pre = el("pre");
    pre.textContent = data.stdout || data.stderr || "命令没有文本输出。";
    container.append(pre);
    container.append(el("p", "hint", `退出码：${data.exit_code}`));
    return;
  }
  const pre = el("pre");
  const raw = safeJson(data, 4000);
  pre.textContent = raw.length >= 4000 ? raw + "\n…" : raw;
  container.append(pre);
}

function approvalOf(detail) {
  return (detail?.approvals || []).find((item) => !item.decision && new Date(item.expires_at) > new Date());
}

function renderTaskCard(detailOrTask, options = {}) {
  const value = detailOrTask?.task ? detailOrTask : { task: detailOrTask };
  const task = value.task;
  if (!task) return el("section", "card", "任务记录不可用");
  const omitResult = !!options.omitResult;
  const box = card(TOOL_LABELS[task.request?.intent] || task.request?.intent || "任务", statusBadge(task.status));
  const meta = el("p", "hint");
  meta.textContent = `${formatTime(task.created_at)} · ${nodeName(task.node_id)}${task.project_id ? " · 项目 " + (projectName(task.project_id) || "已关联") : ""}`;
  box.append(meta);

  if (task.cancel_requested) {
    box.append(el("p", "hint", TASK_TERMINAL.has(task.status)
      ? "已记录取消请求；以最终执行证据为准。"
      : "已请求取消，等待节点确认实际停止。"));
  }
  if (task.request?.arguments && Object.keys(task.request.arguments).length) {
    const args = el("pre");
    args.textContent = safeJson(task.request.arguments, 2000);
    box.append(args);
  }
  if (task.execution_outcome === "unknown") {
    box.append(el("p", "hint", "执行结果未知；Core 保留证据并对账，不能直接重跑。"));
  }
  if (task.result && !omitResult) {
    if (task.result.image && (task.status !== "succeeded" || task.execution_outcome === "unknown")) {
      box.append(el("p", "hint", "任务未成功或结果未知；不显示图片预览，仅保留真实执行状态。"));
    } else renderResultData(box, task.result);
  }
  if (task.error) box.append(el("p", "hint", `${task.error.code} · ${task.error.message}`));
  if (task.verification) box.append(el("p", "hint", `验证级别：${task.verification}`));

  const approval = approvalOf(value);
  const actions = el("div", "actions");
  if (task.status === "awaiting_approval" && approval) {
    box.append(el("p", "hint", `审批到期：${formatTime(approval.expires_at)} · 只批准这一个动作`));
    const reject = button("拒绝", "quiet", () => runAction(async () => {
      await decideApproval(approval.approval_id, "reject");
      await ui.refresh(true);
    }, reject));
    const approve = button("批准本次任务", "primary", () => runAction(async () => {
      await decideApproval(approval.approval_id, "approve");
      await ui.refresh(true);
    }, approve));
    actions.append(reject, approve);
  } else if (task.status === "awaiting_approval") {
    box.append(el("p", "hint", "审批已决定或已到期，等待 Core 更新状态。"));
  }
  if (!TASK_TERMINAL.has(task.status)) {
    const cancel = button("取消任务", "quiet", () => runAction(async () => {
      await cancelTask(task.task_id);
      await ui.refresh(true);
    }, cancel));
    actions.append(cancel);
  }
  if (value.attempts || value.approvals || value.events) {
    const details = el("details");
    details.append(el("summary", "", "技术证据"));
    const pre = el("pre");
    pre.textContent = safeJson({
      attempts: value.attempts || [],
      approvals: value.approvals || [],
      events: value.events || [],
    }, 12000);
    details.append(pre);
    box.append(details);
  } else if (options.allowEvidence !== false) {
    const details = el("details");
    details.append(el("summary", "", "查看完整记录"));
    const load = button("读取执行证据", "quiet", () => runAction(async () => {
      const detail = await taskDetail(task.task_id);
      details.replaceWith(renderTaskCard(detail));
      ui.refresh(true);
    }, load));
    details.append(load);
    box.append(details);
  }
  if (actions.childElementCount) box.append(actions);
  return box;
}

const requestedTasks = new Set();

function ensureTask(taskId) {
  if (!taskId || state.tasks.has(taskId) || requestedTasks.has(taskId)) return;
  requestedTasks.add(taskId);
  taskDetail(taskId)
    .then(() => ui.refresh(true))
    .catch(() => {})
    .finally(() => requestedTasks.delete(taskId));
}

function stepArguments(step) {
  const args = step?.proposal?.arguments || {};
  const intent = step?.proposal?.intent || "";
  if (intent === "get_current_time") {
    return Number.isInteger(args.utc_offset_minutes) ? `固定偏移：${offsetLabel(args.utc_offset_minutes)}` : "";
  }
  if (intent === "job_list") return `最多 ${args.limit ?? 10} 个任务`;
  if (intent === "job_output") {
    const id = typeof args.task_id === "string" ? args.task_id.slice(0, 8) : "?";
    return `任务 ${id} · 最多 ${args.max_bytes ?? 4096} 字节`;
  }
  if (intent === "read_image") {
    const path = args.path ? `workspace/${args.path}` : "工作区图片";
    return Number.isInteger(args.max_dimension) ? `${path} · 最长边 ${args.max_dimension}px` : path;
  }
  if (args.path) return `workspace/${args.path}`;
  if (args.pattern) return `匹配：${args.pattern}`;
  if (args.command_id) return `固定命令：${args.command_id}`;
  if (args.query) return `搜索：${args.query}`;
  if (args.root_id) return `范围：${args.root_id}`;
  if (args.version_id) return `能力版本：${String(args.version_id).slice(0, 8)}`;
  return "";
}

function offsetLabel(minutes) {
  if (!Number.isInteger(minutes)) return "";
  const absolute = Math.abs(minutes);
  return `UTC${minutes >= 0 ? "+" : "-"}${String(Math.floor(absolute / 60)).padStart(2, "0")}:${String(absolute % 60).padStart(2, "0")}`;
}

function queryFact(label, value) {
  const row = el("div", "query-fact");
  row.append(el("span", "", label), el("strong", "", String(value)));
  return row;
}

function renderCurrentTime(box, data) {
  const valid = data && typeof data.utc === "string" && typeof data.local === "string"
    && Number.isInteger(data.utc_offset_minutes)
    && Number.isInteger(data.elapsed_since_turn_seconds) && data.elapsed_since_turn_seconds >= 0;
  if (!valid) {
    box.append(el("p", "hint", "时间结果字段不完整或类型不符，没有可展示的时间快照。"));
    return true;
  }
  const facts = el("div", "query-facts");
  facts.append(queryFact("Core UTC 时间", data.utc));
  facts.append(queryFact("固定偏移当地时间", `${data.local} · ${offsetLabel(data.utc_offset_minutes)}`));
  facts.append(queryFact("本轮经过", `${data.elapsed_since_turn_seconds} 秒`));
  box.append(facts);
  box.append(el("p", "hint", "来自 Core 的只读时间快照；未按浏览器时区改写，不创建提醒，也不调用模型。"));
  return true;
}

function renderJobList(box, data) {
  if (!data || !Array.isArray(data.jobs)) {
    box.append(el("p", "hint", "任务列表结果字段不完整，没有可展示的任务摘要。"));
    return true;
  }
  const list = el("div", "query-jobs");
  for (const job of data.jobs.slice(0, 20)) {
    if (!job || typeof job !== "object") continue;
    const row = el("div", "query-job");
    const head = el("div", "query-job-head");
    head.append(el("strong", "", TOOL_LABELS[job.tool] || job.tool || "任务"));
    head.append(statusBadge(job.status));
    row.append(head);
    const facts = [];
    if (typeof job.task_id === "string") facts.push(`任务 ${job.task_id.slice(0, 8)}`);
    if (job.created_at) facts.push(`创建 ${formatTime(job.created_at)}`);
    if (job.execution_outcome) facts.push(`执行 ${job.execution_outcome === "unknown" ? "结果未知" : job.execution_outcome === "known" ? "结果已知" : "未开始"}`);
    if (job.verification) facts.push(`验证 ${job.verification}`);
    if (job.cancel_requested) facts.push("已请求取消");
    if (job.expires_at) facts.push(`到期 ${formatTime(job.expires_at)}`);
    row.append(el("p", "hint", facts.join(" · ")));
    list.append(row);
  }
  if (!list.childElementCount) list.append(el("p", "hint", "当前会话在该设备上没有可列出的已有任务。"));
  box.append(list);
  if (data.truncated) box.append(el("p", "hint", "结果达到返回上限，可能还有更早任务没有列出；这不是完整任务总数。"));
  box.append(el("p", "hint", "只读查询当前会话、所选设备在 NEXUS 中的原任务记录；不是常驻 Shell 或后台任务管理器，不会启动、停止或重跑任务。"));
  return true;
}

function renderJobOutput(box, data) {
  if (!data || !data.output || typeof data.output !== "object") {
    box.append(el("p", "hint", "任务输出结果字段不完整，没有可展示的输出。"));
    return true;
  }
  if (typeof data.task_id === "string") box.append(el("p", "hint", `任务 ${data.task_id.slice(0, 8)} · 观测于 ${formatTime(data.observed_at)}`));
  const output = data.output;
  const facts = el("div", "query-facts");
  if (output.status) facts.append(queryFact("状态", output.status));
  if (output.execution_outcome) facts.append(queryFact("执行结果", output.execution_outcome === "unknown" ? "未知，保留证据" : output.execution_outcome === "known" ? "已知" : "未开始"));
  if (output.verification) facts.append(queryFact("验证级别", output.verification));
  if (facts.childElementCount) box.append(facts);
  if (output.error_code) box.append(el("p", "hint", `错误码：${output.error_code}`));
  if (output.error && typeof output.error === "object") {
    const message = typeof output.error.message === "string" ? output.error.message : "";
    if (message) box.append(el("p", "hint", message));
  }
  if ("data" in output) renderResultData(box, output.data);
  else if (typeof output.data_excerpt === "string") {
    const pre = el("pre");
    pre.textContent = output.data_excerpt;
    box.append(pre, el("p", "hint", "这里只显示有界摘录，完整结果保留在原执行记录。"));
  }
  if (output.context_projection?.truncated) {
    box.append(el("p", "hint", output.context_projection.notice || "输出已截断，不能当作完整结果。"));
  }
  if (!("data" in output) && !("data_excerpt" in output) && !output.error_code && !output.context_projection) {
    const pre = el("pre");
    pre.textContent = safeJson(output, 4000);
    box.append(pre);
  }
  box.append(el("p", "hint", "只读快照；不等待、不重跑，也不会读取其他轮次的正文。"));
  return true;
}

function renderCapabilityDetail(box, data) {
  if (!data || typeof data !== "object") {
    box.append(el("p", "hint", "能力详情字段不完整。"));
    return true;
  }
  const facts = [];
  if (data.capability_id) facts.push(`能力 ${data.capability_id}`);
  if (data.name) facts.push(`类型 ${data.name}`);
  if (typeof data.artifact_digest === "string") facts.push(`摘要 ${data.artifact_digest.slice(0, 16)}…`);
  if (facts.length) box.append(el("p", "hint", facts.join(" · ")));
  if (typeof data.markdown === "string" && data.markdown.trim()) {
    const details = el("details", "readonly-capability");
    details.open = true;
    details.append(el("summary", "", "Skill 使用说明（只读）"));
    const pre = el("pre");
    pre.textContent = data.markdown;
    details.append(pre);
    box.append(details);
  }
  if (Array.isArray(data.steps)) {
    const details = el("details", "readonly-capability");
    details.open = true;
    details.append(el("summary", "", "固定步骤（只读）"));
    const list = el("ol", "capability-steps");
    for (const step of data.steps.slice(0, 20)) {
      if (!step || typeof step !== "object") continue;
      const item = el("li");
      const parts = [];
      if (step.id) parts.push(String(step.id));
      if (step.tool) parts.push(`${TOOL_LABELS[step.tool] || step.tool}@${step.tool_version ?? "?"}`);
      if (step.tool_version_id) parts.push(`Tool 版本 ${String(step.tool_version_id).slice(0, 8)}`);
      if (step.arguments && typeof step.arguments === "object") parts.push(JSON.stringify(step.arguments).slice(0, 240));
      item.textContent = parts.join(" · ");
      list.append(item);
    }
    details.append(list);
    box.append(details);
  }
  if (data.input_schema) {
    const details = el("details", "readonly-capability");
    details.append(el("summary", "", "输入契约（只读）"));
    const pre = el("pre");
    pre.textContent = safeJson(data.input_schema, 4000);
    details.append(pre);
    box.append(details);
  }
  box.append(el("p", "hint", "只读能力详情；Markdown、步骤和模型文字都是资料，不是 HTML 或权限指令，也不会执行或启用能力。"));
  return true;
}

function renderWorkStep(turn, step) {
  const intent = step?.proposal?.intent || "";
  const result = step?.result || null;
  const data = result?.data;
  const label = TOOL_LABELS[intent] || intent || "步骤";
  const box = card(`${step.step_index}. ${label}`, statusBadge(step.status));
  const args = stepArguments(step);
  if (args) box.append(el("p", "hint", args));

  if (step.status === "permission") {
    box.append(el("p", "hint", step.request_text || "这一步需要你的明确答复。"));
    const actions = el("div", "actions");
    const reject = button("不同意", "quiet", () => runAction(async () => {
      await answerPermission(turn.turn_id, step.step_id, turn.revision, false);
      await ui.refresh(true);
    }, reject));
    const approve = button("同意", "primary", () => runAction(async () => {
      await answerPermission(turn.turn_id, step.step_id, turn.revision, true);
      await ui.refresh(true);
    }, approve));
    actions.append(reject, approve);
    box.append(actions);
  }

  let handled = false;
  const resultKnown = result && result.execution_outcome !== "unknown"
    && (result.status === "succeeded" || result.status === undefined);
  const successful = step.status === "succeeded" && resultKnown;
  if (successful && data) {
    if (intent === "read_image") {
      const evidence = createImageEvidence(data);
      if (evidence) box.append(evidence);
      else if (data.image) box.append(el("p", "hint", "图片结果未通过严格白名单校验，不显示预览或内联数据；执行状态以任务记录为准。"));
      else box.append(el("p", "hint", "图片步骤没有返回可展示的图片字段；不生成占位成功图。"));
      handled = true;
    } else if (intent === "get_current_time") {
      handled = renderCurrentTime(box, data);
    } else if (intent === "job_list") {
      handled = renderJobList(box, data);
    } else if (intent === "job_output") {
      handled = renderJobOutput(box, data);
    } else if (intent === "inspect_capability") {
      handled = renderCapabilityDetail(box, data);
    }
  }

  for (const taskId of new Set([step.preview_task_id, step.task_id].filter(Boolean))) {
    const detail = state.tasks.get(taskId);
    const omitImageResult = intent === "read_image"
      && (handled || step.status !== "succeeded" || result?.execution_outcome === "unknown");
    if (detail?.task) box.append(renderTaskCard(detail, { allowEvidence: false, omitResult: omitImageResult }));
    else ensureTask(taskId);
  }

  const specialized = ["read_image", "get_current_time", "job_list", "job_output", "inspect_capability"].includes(intent);
  if (!handled && successful && result && !specialized && !step.task_id && !step.preview_task_id) {
    renderResultData(box, result.data || result);
  }
  if (result?.verification) box.append(el("p", "hint", `验证级别：${result.verification}`));
  if (result?.execution_outcome === "unknown") {
    box.append(el("p", "hint", "执行结果未知；Core 保留证据并核对，不能当作成功或直接重跑。"));
  }
  if (result?.status && result.status !== "succeeded" && !result.error_code) {
    box.append(el("p", "hint", `实际结果状态：${result.status}`));
  }
  if (result?.error_code) box.append(el("p", "hint", result.message || errorHint(result.error_code) || "这一步未能完成，具体原因保留在技术详情中。"));
  if (result?.error?.message) box.append(el("p", "hint", result.error.message));
  if (step.permission_text) box.append(el("p", "hint", `你的答复：${step.permission_text}`));
  if (step.proposal || result) {
    const details = el("details");
    details.append(el("summary", "", "技术详情"));
    const summary = imageSummary(data);
    if (summary) {
      const parts = [];
      if (summary.media_type) parts.push(summary.media_type === "image/jpeg" ? "有损 JPEG" : "PNG");
      if (summary.width && summary.height) parts.push(`${summary.width}×${summary.height}`);
      if (summary.size_bytes) parts.push(`${summary.size_bytes} 字节`);
      if (summary.sha256) parts.push(`SHA-256 ${summary.sha256.slice(0, 16)}…`);
      if (parts.length) details.append(el("p", "hint", `图片摘要：${parts.join(" · ")}`));
    }
    const pre = el("pre");
    pre.textContent = safeJson({ action: step.proposal, result: step.result }, 12000);
    details.append(pre);
    box.append(details);
  }
  return box;
}

function renderProposal(turn) {
  if (!turn.proposal || turn.task_id) return null;
  const box = card(TOOL_LABELS[turn.proposal.intent] || turn.proposal.intent, statusBadge(turn.action_status === "rejected" ? "cancelled" : "awaiting_approval"));
  const args = turn.proposal.arguments || {};
  const pre = el("pre");
  pre.textContent = JSON.stringify({
    node: nodeName(turn.node_id),
    action: turn.proposal,
    expires_at: turn.expires_at,
  }, null, 2);
  box.append(pre);
  if (turn.action_status !== "proposed" || new Date(turn.expires_at) <= new Date()) {
    box.append(el("p", "hint", turn.action_status === "rejected" ? "你已拒绝这个动作。" : "动作已过期或会话已归档，请重新提出请求。"));
    return box;
  }
  const consentLabel = el("label", "switch");
  const consent = document.createElement("input");
  consent.type = "checkbox";
  consent.checked = false;
  consent.disabled = !turn.result_supported;
  consentLabel.append(consent, document.createTextNode("允许本次白名单结果回传 DeepSeek，成功后自动回答"));
  box.append(consentLabel);
  box.append(el("p", "hint", "确认后仍检查权限；目录读取通常还需要一次任务审批。勾选回传会再调用一次模型，共用本轮费用上限。"));
  const actions = el("div", "actions");
  const reject = button("不执行", "quiet", () => runAction(async () => {
    await decideAction(turn.turn_id, turn.proposal_digest, "reject");
    await ui.refresh(true);
  }, reject));
  const confirm = button(consent.checked ? "确认执行并回传结果" : "确认执行", "primary", () => runAction(async () => {
    await decideAction(turn.turn_id, turn.proposal_digest, "confirm", consent.checked);
    await ui.refresh(true);
  }, confirm));
  consent.addEventListener("change", () => {
    confirm.textContent = consent.checked ? "确认执行并回传结果" : "确认执行";
  });
  actions.append(reject, confirm);
  box.append(actions);
  return box;
}

function renderPlan(turn) {
  const plan = turn.skill_plan;
  if (!plan) return null;
  const box = card("流程计划", statusBadge(turn.action_status === "rejected" ? "cancelled" : "awaiting_approval"));
  const list = el("ol");
  for (const step of plan.steps || []) {
    const item = el("li");
    item.textContent = `${TOOL_LABELS[step.tool] || step.tool}${step.id ? ` · ${step.id}` : ""}`;
    list.append(item);
  }
  box.append(list);
  if (turn.action_status === "proposed" && !turn.task_id && new Date(turn.expires_at || 0) > new Date()) {
    box.append(el("p", "hint", "需要参数的流程请先在原控制台补齐；这里可以确认不需要额外输入的固定步骤。"));
    const actions = el("div", "actions");
    const reject = button("不执行", "quiet", () => runAction(async () => {
      await decideAction(turn.turn_id, turn.proposal_digest, "reject");
      await ui.refresh(true);
    }, reject));
    const confirm = button("确认执行流程", "primary", () => runAction(async () => {
      await decideAction(turn.turn_id, turn.proposal_digest, "confirm", false);
      await ui.refresh(true);
    }, confirm));
    actions.append(reject, confirm);
    box.append(actions);
  }
  return box;
}

function renderResultConsent(turn) {
  const supported = turn.result_supported && turn.phase === "reply" && turn.status === "completed";
  if (!supported) return null;
  const box = card("执行结果回传", el("span", "badge pending", "需要你决定"));
  if (turn.auto_result_allowed && turn.auto_result_status === "waiting") {
    box.append(el("p", "hint", "已允许本次白名单结果回传，正在等待工具完成；完成后会自动生成回答。"));
    return box;
  }
  if (turn.auto_result_status === "stopped") {
    box.append(el("p", "hint", "已停止自动回传。工具结果已保留，尚未发送给模型。"));
  } else if (turn.auto_result_status === "unavailable") {
    box.append(el("p", "hint", "自动回传未完成，未增加模型调用。"));
  }
  const preview = state.previews.get(turn.turn_id);
  if (!preview) {
    box.append(el("p", "hint", "工具已经完成。可以把白名单字段和原上下文再发送给 DeepSeek，生成最终回答；最多再调用一次。"));
    const load = button("预览可回传结果", "quiet", () => runAction(async () => {
      await previewResult(turn.turn_id);
      ui.refresh(true);
    }, load));
    box.append(load);
    return box;
  }
  if (Array.isArray(preview.available_fields)) {
    const fields = el("p", "hint");
    fields.textContent = "本次字段：" + preview.available_fields.map((field) => field).join("、");
    box.append(fields);
  }
  renderResultData(box, preview.data);
  box.append(el("p", "hint", `以上是实际外发内容；有效至 ${formatTime(preview.expires_at)}。`));
  const actions = el("div", "actions");
  const send = button("发送给 DeepSeek 生成回答", "primary", () => runAction(async () => {
    await consentResult(turn.turn_id, preview);
    ui.refresh(true);
  }, send));
  actions.append(send);
  box.append(actions);
  return box;
}

function errorHint(code) {
  const hints = {
    MODEL_RESPONSE_INVALID: "模型返回的格式无法校验，本轮已停止。",
    MODEL_TOOL_CALL_LIMIT: "模型提出的动作超过本轮剩余额度，未执行这批动作。",
    MODEL_REQUEST_FAILED: "模型连接或响应解析失败，本轮已停止。",
    MODEL_TIMEOUT: "等待模型响应超时，本轮已停止。",
    MODEL_OUTCOME_UNKNOWN: "未收到完整模型响应或用量，实际费用无法确认；这不表示设备已经执行。",
  };
  return hints[code] || (String(code || "").startsWith("WEB_") ? "这次网页请求没有取得可用内容；已有资料仍然保留。" : "");
}

function presentationBadge(presentation) {
  const kind = presentation.phase === "completed" ? "success"
    : ["failed", "expired", "interrupted"].includes(presentation.phase) ? "failed"
      : presentation.phase === "unknown" ? "unknown"
        : presentation.phase === "awaiting_input" ? "pending" : "muted";
  return el("span", `badge ${kind}`, presentation.label || presentation.phase);
}

function presentationState(turn, presentation) {
  let value = presentationUi.get(turn.turn_id);
  if (!value) {
    value = {
      open: !(presentation.phase === "completed" && presentation.collapse_process && presentation.final_text !== null),
      manual: false,
      phase: presentation.phase,
    };
    presentationUi.set(turn.turn_id, value);
  } else if (value.phase !== presentation.phase) {
    if (presentation.phase === "completed" && !value.manual) {
      value.open = !(presentation.collapse_process && presentation.final_text !== null);
    }
    value.phase = presentation.phase;
  }
  if (presentation.phase === "awaiting_input") value.open = true;
  return value;
}

function renderPresentationBody(assistant, turn, presentation) {
  const status = el("div", "status-line");
  status.append(presentationBadge(presentation));
  if (turn.work_status && turn.work_status !== "permission") status.append(el("span", "hint", `工作：${turn.work_status}`));
  if (turn.cost_status === "unknown") status.append(el("span", "hint", "部分费用待核对"));
  else if (turn.cost_microunits !== null && turn.cost_microunits !== undefined) status.append(el("span", "hint", money(turn.cost_microunits)));
  if (turn.web_call_count) status.append(el("span", "hint", `网页请求 ${turn.web_call_count} 次`));
  if (turn.context_truncated) status.append(el("span", "hint", "部分历史或工具上下文已裁剪"));
  assistant.append(status);

  const hint = errorHint(turn.error_code);
  if (hint && !presentation.final_text) assistant.append(el("p", "hint", hint));

  if (presentation.final_text !== null && presentation.final_text !== undefined) {
    const final = el("section", "final-answer");
    final.append(el("p", "final-label", presentation.phase === "completed" ? "最终回答" : "本轮反馈"));
    const prose = el("div", "prose");
    renderProse(prose, presentation.final_text);
    final.append(prose);
    assistant.append(final);
  }

  const summaries = Array.isArray(presentation.process) ? presentation.process : [];
  const steps = Array.isArray(turn.work_steps) ? turn.work_steps : [];
  const live = typeof presentation.live_text === "string" ? presentation.live_text : "";
  const hasProcess = summaries.length > 0 || steps.length > 0 || live.length > 0;
  if (hasProcess) {
    const ui = presentationState(turn, presentation);
    const body = el("div", "process-body");
    body.hidden = !ui.open;
    const toggle = button(ui.open ? "收起处理过程" : "查看处理过程", "process-toggle");
    toggle.setAttribute("aria-expanded", String(ui.open));
    toggle.hidden = presentation.phase === "awaiting_input";
    toggle.disabled = presentation.phase === "awaiting_input";
    const head = el("div", "process-head");
    head.append(el("span", "process-title", "处理过程"), toggle);
    toggle.addEventListener("click", () => {
      ui.open = !ui.open;
      ui.manual = true;
      body.hidden = !ui.open;
      toggle.textContent = ui.open ? "收起处理过程" : "查看处理过程";
      toggle.setAttribute("aria-expanded", String(ui.open));
    });

    for (const item of summaries) {
      const text = el("div", "process-item prose");
      renderProse(text, item.text || "");
      if (item.truncated) text.append(el("p", "hint", "这条摘要已裁剪，完整内容保留在 Core 记录中。"));
      body.append(text);
    }
    if (presentation.process_truncated) {
      body.append(el("p", "hint", "较早的公开摘要已裁剪；完整记录仍在 Core 的执行日志中。"));
    }
    if (live) {
      const liveBox = el("section", "process-live");
      liveBox.append(el("p", "process-live-label", presentation.phase === "responding" ? "正在生成" : "已保留的回复片段"));
      const prose = el("div", "prose");
      renderProse(prose, live);
      liveBox.append(prose);
      body.append(liveBox);
    }
    for (const step of steps) body.append(renderWorkStep(turn, step));
    const panel = el("section", "process-panel");
    panel.append(head, body);
    assistant.append(panel);
  }

  const proposal = renderProposal(turn);
  if (proposal) assistant.append(proposal);
  const plan = renderPlan(turn);
  if (plan) assistant.append(plan);
  const consent = renderResultConsent(turn);
  if (consent) assistant.append(consent);
}

function renderLegacyBody(assistant, turn) {
  const body = el("div", "prose");
  if (turn.response_text) renderProse(body, turn.response_text);
  else if (turn.status === "queued") body.append(el("p", "hint", "已接收，等待开始…"));
  else if (turn.status === "generating") body.append(el("p", "hint", "正在生成回复…"));
  else if (turn.status === "stopped") body.append(el("p", "hint", "本轮已停止，已生成的部分保留。"));
  else if (turn.status === "failed" || turn.status === "interrupted") body.append(el("p", "hint", "本轮未完成。"));
  else body.append(el("p", "hint", "等待过程状态…"));
  assistant.append(body);

  const status = el("div", "status-line");
  const active = turn.turn_id === state.activeTurn;
  const label = turn.work_status === "permission" ? "需要你答复"
    : turn.auto_result_status === "waiting" ? "等待工具完成后自动回答"
      : active ? (STATUS_LABELS[turn.status] || turn.status) : (STATUS_LABELS[turn.status] || turn.status);
  status.append(statusBadge(turn.status === "completed" ? "succeeded" : turn.status, label));
  if (turn.work_status && turn.work_status !== "permission") status.append(el("span", "hint", `工作：${turn.work_status}`));
  if (turn.cost_status === "unknown") status.append(el("span", "hint", "费用未知，保留预留"));
  else if (turn.cost_microunits !== null && turn.cost_microunits !== undefined) status.append(el("span", "hint", money(turn.cost_microunits)));
  if (turn.web_call_count) status.append(el("span", "hint", `网页请求 ${turn.web_call_count} 次`));
  if (turn.context_truncated) status.append(el("span", "hint", "部分历史或工具上下文已裁剪"));
  if (turn.error_code) status.append(el("span", "hint", turn.error_code));
  assistant.append(status);

  const hint = errorHint(turn.error_code);
  if (hint) assistant.append(el("p", "hint", hint));

  for (const step of turn.work_steps || []) assistant.append(renderWorkStep(turn, step));
  const proposal = renderProposal(turn);
  if (proposal) assistant.append(proposal);
  const plan = renderPlan(turn);
  if (plan) assistant.append(plan);
  const consent = renderResultConsent(turn);
  if (consent) assistant.append(consent);
}

function renderTurn(turn) {
  const article = el("article", "chat-turn");
  article.dataset.turnId = turn.turn_id;

  const user = el("div", "msg user");
  const userMeta = el("div", "msg-meta");
  userMeta.append(el("strong", "", "你"), el("span", "", formatTime(turn.created_at)));
  user.append(userMeta, el("div", "prose", turn.text));
  article.append(user);

  const assistant = el("div", "msg assistant");
  const meta = el("div", "msg-meta");
  meta.append(el("strong", "", "NEXUS"), el("span", "", formatTime(turn.updated_at || turn.created_at)));
  const presentation = turn.presentation && turn.presentation.version === 1 ? turn.presentation : null;
  if (!presentation && turn.phase === "summary") meta.append(el("span", "badge success", "最终回答"));
  assistant.append(meta);

  if (presentation) renderPresentationBody(assistant, turn, presentation);
  else renderLegacyBody(assistant, turn);

  if (turn.memory_references?.length) {
    const details = el("details");
    details.append(el("summary", "", "本轮引用的记忆与来源"));
    const pre = el("pre");
    pre.textContent = safeJson(turn.memory_references, 5000);
    details.append(pre);
    assistant.append(details);
  }

  const actions = el("div", "actions");
  const inspect = button("技术详情", "quiet", () => ui.openTurnDetails(turn));
  actions.append(inspect);
  if (turn.turn_id === state.activeTurn && turn.status !== "stopped" && turn.status !== "failed") {
    const stop = button("停止回复", "quiet danger", () => ui.stopTurn?.());
    actions.append(stop);
  }
  if (actions.childElementCount) assistant.append(actions);

  article.append(assistant);
  return article;
}

function chips() {
  const wrap = document.createDocumentFragment();
  const conversation = state.conversation;
  const project = conversation?.project_id ? projectName(conversation.project_id) || "已关联项目" : (state.prefs.projectId ? projectName(state.prefs.projectId) || "新会话项目" : "不关联项目");
  const projectChip = button(`项目 · ${project}`, "chip", () => ui.openScope("project"));
  const node = state.nodes.find((item) => item.node_id === state.prefs.nodeId);
  const nodeChip = button(`${nodeName(state.prefs.nodeId)}${node && !node.online ? " · 离线" : ""}`, "chip" + (node && !node.online ? " off" : ""), () => ui.openScope("device"));
  const toolsChip = button(`工具 · ${state.prefs.tools ? "开" : "关"}`, "chip" + (state.prefs.tools ? " on" : " off"), () => ui.openScope("tools"));
  const feedbackChip = button(`结果回传 · ${state.prefs.resultFeedback ? "允许" : "询问"}`, "chip" + (state.prefs.resultFeedback ? " on" : ""), () => ui.openScope("tools"));
  const contextChip = button(`上下文 · ${state.prefs.context ? "开" : "关"}`, "chip" + (state.prefs.context ? " on" : " off"), () => ui.openScope("context"));
  wrap.append(projectChip, nodeChip, toolsChip, feedbackChip, contextChip);
  return wrap;
}

function renderChips(target) {
  if (!target) return;
  target.replaceChildren(chips());
}

export function renderDock() {
  if (!state.ready) return;
  $("dock-conversation").textContent = conversationTitle();
  const active = state.turns.get(state.activeTurn);
  const paused = state.control?.paused;
  const online = state.nodes.some((node) => node.online);
  $("dock-state").textContent = paused ? "已暂停派发"
    : active ? (active.presentation?.version === 1 ? active.presentation.label : (STATUS_LABELS[active.status] || "处理中"))
      : !state.nodes.length ? "未配置设备"
        : online ? "就绪" : "设备离线";
  $("dock-state").className = "hint" + (paused ? " error" : "");

  const thread = $("dock-thread");
  thread.replaceChildren();
  const last = state.turnOrder.at(-1);
  if (!last) {
    thread.hidden = true;
  } else {
    thread.hidden = false;
    const presentation = last.presentation && last.presentation.version === 1 ? last.presentation : null;
    let reply = "NEXUS：等待过程状态…";
    if (presentation) {
      const text = presentation.final_text !== null && presentation.final_text !== undefined
        ? presentation.final_text : presentation.live_text;
      reply = text ? `NEXUS：${markdownPreview(text, 220)}` : `NEXUS：${presentation.label}`;
    } else if (last.response_text) reply = `NEXUS：${markdownPreview(last.response_text, 220)}`;
    else if (last.work_status === "permission") reply = "NEXUS：需要你答复后才能继续";
    else if (last.status === "queued") reply = "NEXUS：等待开始…";
    else if (last.status === "generating") reply = "NEXUS：正在生成…";
    else if (last.status === "failed" || last.status === "interrupted") reply = `NEXUS：${STATUS_LABELS[last.status] || "未完成"}`;
    else if (last.status === "stopped") reply = "NEXUS：已停止";
    const line = el("div", "line assistant", reply);
    line.title = presentation
      ? (presentation.final_text ?? presentation.live_text ?? presentation.label)
      : (last.response_text || reply);
    thread.append(line);
  }

  const pending = $("dock-pending");
  const approvals = pendingApprovals();
  const permissions = pendingPermissions();
  const pendingCount = approvals.length + permissions.length;
  const topPending = $("pending-button");
  if (topPending) {
    topPending.hidden = pendingCount === 0;
    $("pending-count").textContent = String(pendingCount);
  }
  if (pendingCount) {
    pending.hidden = false;
    pending.replaceChildren();
    const label = approvals.length
      ? `需要你批准：${TOOL_LABELS[approvals[0].task.request?.intent] || approvals[0].task.request?.intent || "任务"}`
      : `需要你答复：${TOOL_LABELS[permissions[0].proposal?.intent] || permissions[0].proposal?.intent || "步骤"}`;
    pending.append(el("span", "grow", label));
    pending.append(button("查看", "", () => ui.openPending()));
  } else {
    pending.hidden = true;
    pending.replaceChildren();
  }

  renderChips($("dock-chips"));
  const activeTurn = state.activeTurn && state.turns.get(state.activeTurn);
  $("dock-stop").hidden = !activeTurn;
  $("dock-send").disabled = !state.ready || !!activeTurn;
  $("dock-send").textContent = "发送";
  $("dock-text").disabled = !state.ready;
}

export function renderConversations() {
  const list = $("conversation-list");
  list.replaceChildren();
  if (!state.conversations.length) {
    list.append(el("p", "hint", state.archived ? "没有已归档会话" : "还没有会话"));
  }
  for (const conversation of state.conversations) {
    const item = el("button", "conversation-item" + (conversation.conversation_id === state.conversation?.conversation_id ? " active" : ""));
    item.type = "button";
    item.append(el("strong", "", conversation.title || "新会话"));
    const project = conversation.project_id ? projectName(conversation.project_id) || "项目会话" : "不关联项目";
    item.append(el("small", "", `${formatDate(conversation.created_at)} · ${project}${conversation.archived ? " · 已归档" : ""}`));
    item.addEventListener("click", () => runAction(async () => {
      await selectConversation(conversation.conversation_id);
      ui.openChat();
      ui.refresh(true);
    }, item));
    list.append(item);
  }
  $("archive-toggle").textContent = state.archived ? "查看进行中" : "查看归档";
}

export function renderChat(force = false) {
  if (!state.ready) return;
  renderConversations();
  const title = conversationTitle();
  $("chat-title").textContent = title;
  const conversation = state.conversation;
  const parts = [];
  if (conversation?.project_id) parts.push(`项目 ${projectName(conversation.project_id) || "已关联"}`);
  else parts.push("不关联项目");
  if (conversation?.archived) parts.push("已归档");
  if (state.control?.paused) parts.push("派发已暂停");
  if (state.activeTurn) parts.push("本轮进行中");
  else if (conversation) parts.push("空闲");
  $("chat-sub").textContent = parts.join(" · ");

  const log = $("chat-messages");
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 120;
  let inner = log.querySelector(".messages-inner");
  if (!inner) {
    inner = el("div", "messages-inner");
    log.append(inner);
  }
  inner.replaceChildren();
  if (!state.turnOrder.length) {
    inner.append(el("div", "empty", conversation
      ? "这个会话还没有消息。输入一句话开始。"
      : "还没有会话。发送第一条消息会自动创建会话。"));
  }
  for (const turn of state.turnOrder) inner.append(renderTurn(turn));
  if (nearBottom || force) log.scrollTop = log.scrollHeight;
  const older = $("chat-older");
  if (older) older.hidden = state.turnOrder.length < 20;

  renderChips($("chat-chips"));
  const activeTurn = state.activeTurn && state.turns.get(state.activeTurn);
  $("chat-stop").hidden = !activeTurn;
  $("chat-send").disabled = !state.ready || !!activeTurn;
  const node = state.nodes.find((item) => item.node_id === state.prefs.nodeId);
  const workspaceReady = !!node && node.roots?.includes("workspace")
    && (node.tools || []).some((tool) => tool.startsWith("filesystem."));
  const imageCapable = !!node && (node.tools || []).includes("filesystem.read_image@1");
  // The server catalog decides whether read_image is actually exposed, but a
  // node-level image capability already means this round must ask again.
  const needsConsent = state.prefs.tools && workspaceReady && (!state.prefs.resultFeedback || imageCapable);
  $("chat-send").textContent = needsConsent ? "查看并发送" : "发送";
  $("chat-text").disabled = !state.ready;
}

export function renderSettings() {
  const body = $("settings-body");
  if (!body) return;
  body.replaceChildren();

  const connection = el("section", "settings-section");
  connection.append(el("h2", "", "连接与账户"));
  const core = el("div", "settings-row");
  const coreText = el("div", "grow");
  coreText.append(el("strong", "", "Core"));
  coreText.append(el("small", "", `${location.origin} · 会话有效至 ${state.session?.expires_at ? new Date(state.session.expires_at).toLocaleString() : "未知"}`));
  core.append(coreText);
  connection.append(core);
  const actions = el("div", "settings-row");
  actions.append(button("退出登录", "quiet", () => ui.logout()));
  actions.append(button("打开原控制台", "quiet", () => { location.href = "/"; }));
  connection.append(actions);
  body.append(connection);

  const devices = el("section", "settings-section");
  devices.append(el("h2", "", "设备"));
  if (!state.nodes.length) devices.append(el("p", "", "Core 尚未配置 Node。"));
  for (const node of state.nodes) {
    const row = el("div", "settings-row");
    const text = el("div", "grow");
    text.append(el("strong", "", nodeName(node.node_id)));
    text.append(el("small", "", `${node.online ? "在线" : "离线"} · ${node.online ? `${node.tools?.length || 0} 项工具` : "工具状态需连接后确认"}${node.roots?.length ? " · " + node.roots.join(", ") : ""}`));
    row.append(text);
    const choose = button(node.node_id === state.prefs.nodeId ? "当前目标" : "设为目标", "quiet", () => {
      setPref("nodeId", node.node_id);
      renderDock();
      renderChat(true);
      renderSettings();
    });
    choose.disabled = node.node_id === state.prefs.nodeId;
    row.append(choose);
    devices.append(row);
  }
  body.append(devices);

  const projects = el("section", "settings-section");
  projects.append(el("h2", "", "项目"));
  for (const project of state.projects) {
    const row = el("div", "settings-row");
    const text = el("div", "grow");
    text.append(el("strong", "", project.name));
    text.append(el("small", "", project.description || "无说明"));
    row.append(text);
    const choose = button("用于新会话", "quiet", () => {
      setPref("projectId", project.project_id);
      notify(`新会话将使用项目：${project.name}`);
      renderDock();
      renderChat(true);
    });
    row.append(choose);
    projects.append(row);
  }
  const form = el("form", "field");
  const name = el("input");
  name.placeholder = "新项目名称";
  name.maxLength = 100;
  name.required = true;
  const description = el("input");
  description.placeholder = "项目说明（可选）";
  description.maxLength = 500;
  const submit = el("button", "quiet", "创建项目");
  submit.type = "submit";
  submit.style.marginTop = "8px";
  form.append(name, description, submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runAction(async () => {
      if (!name.value.trim()) throw new ApiError(400, "EMPTY_MESSAGE");
      const project = await createProject(name.value.trim(), description.value.trim());
      notify(`已创建项目：${project.name}`);
      name.value = "";
      description.value = "";
      renderSettings();
    }, submit);
  });
  projects.append(form);
  body.append(projects);

  const imageNode = state.nodes.find((node) => (node.tools || []).includes("filesystem.read_image@1"));
  const imageSection = el("section", "settings-section");
  imageSection.append(el("h2", "", "图片读取"));
  if (imageNode) {
    imageSection.append(el("p", "", `服务器返回了 ${nodeName(imageNode.node_id)} 的图片工具能力。只有在工作工具开启、本轮同意结果外发，并且 Core 开启模型图片时才会使用。`));
    imageSection.append(el("p", "hint", "快照最多 1024px / 8 KiB，可能继续缩小或使用有损 JPEG；GIF/WebP 只取首帧，透明区域显示白底。每轮都会重新确认图片快照外发，历史选择不代替本轮同意。"));
    imageSection.append(el("p", "hint", "页面没有图片上传入口；图片只能来自配置工作区内的文件，并由设备工具读取。"));
  } else {
    imageSection.append(el("p", "", "服务器当前未返回可用的图片读取能力。"));
    imageSection.append(el("p", "hint", "可能未在 Core/Node 配置 filesystem.read_image@1、Node 未安装固定 Pillow 13.0.0，或模型图片开关未开启。此处不提供上传入口，也不显示可用的看图功能。"));
  }
  body.append(imageSection);

  const usage = el("section", "settings-section");
  usage.append(el("h2", "", "模型与网页"));
  const value = state.usage;
  if (!value) usage.append(el("p", "", "正在读取用量…"));
  else {
    usage.append(el("p", "", `${value.enabled ? "已启用" : "未启用"} · ${value.calls} 次 API 调用（含网页）`));
    usage.append(el("p", "", value.cost_microunits === null
      ? "费用未知；未知费用保留预留额度。"
      : `${money(value.cost_microunits)} · 公开峰值单价上界`));
    if (value.unknown_cost_calls) usage.append(el("p", "", `${value.unknown_cost_calls} 次调用费用未知`));
  }
  body.append(usage);

  const privacy = el("section", "settings-section");
  privacy.append(el("h2", "", "本地数据"));
  privacy.append(el("p", "", "App 不保存模型密钥、Node 令牌或 owner 入学凭证；这里只保留显示偏好与最后会话。"));
  privacy.append(button("清除本地显示偏好", "quiet", () => {
    clearPrefs();
    renderDock();
    renderChat(true);
    renderSettings();
    notify("已清除本地显示偏好");
  }));
  body.append(privacy);

  const about = el("section", "settings-section");
  about.append(el("h2", "", "关于"));
  about.append(el("p", "", "NEXUS 对话界面 · 可视化占位版本"));
  about.append(el("p", "", "过程视图、能力与记忆管理将在后续页面接入。"));
  body.append(about);
}

export function openSheet({ title, body, actions = [] }) {
  $("sheet-title").textContent = title || "本次需要你决定";
  const bodyNode = $("sheet-body");
  bodyNode.replaceChildren();
  if (body) bodyNode.append(body);
  const actionNode = $("sheet-actions");
  actionNode.replaceChildren();
  for (const item of actions) {
    actionNode.append(button(item.label, item.className || "quiet", item.onClick));
  }
  $("sheet").hidden = false;
  const first = actionNode.querySelector("button, input, select, textarea");
  first?.focus();
}

export function closeSheet() {
  $("sheet").hidden = true;
  $("sheet-body").replaceChildren();
  $("sheet-actions").replaceChildren();
}
