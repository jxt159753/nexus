"use strict";

/*
 * Chat-side rendering for native plugin calls. Data is untrusted text; no
 * plugin-supplied HTML, script or host path is rendered or executed here.
 */

import {
  ApiError,
  answerWorkStep,
  api,
  decideApproval,
  state,
  taskDetail,
  uuid,
} from "./api.js";

import {
  loadPlugins,
  loadConversationPermission,
  pluginById,
  pluginState,
  readDeliverable,
  safeTextPreview,
  saveBytes,
  TEST_LABELS,
} from "./plugins.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, className, onClick) {
  const node = el("button", className || "", label);
  node.type = "button";
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

function notify(message, error = false) {
  const box = document.getElementById("notice");
  if (!box) return;
  box.textContent = message;
  box.className = "notice" + (error ? " error" : "");
  box.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { box.hidden = true; }, 7000);
}

function errorText(error) {
  const code = error instanceof ApiError ? error.code : error?.message;
  const labels = {
    DELIVERABLE_UNAVAILABLE: "交付物不可下载；只使用服务器登记的 download_url",
    NODE_OFFLINE: "设备离线，停止操作未派发",
    PLUGIN_VERSION_UNAVAILABLE: "插件版本不可用，停止控制不能提交",
    WORK_PERMISSION_EXPIRED: "本次工作授权已失效",
    AUTH_REQUIRED: "登录会话已过期",
  };
  return labels[code] || error?.message || "操作失败";
}

function badge(text, kind = "muted") {
  return el("span", `badge ${kind}`, text);
}

function fact(label, value) {
  const row = el("div", "query-fact");
  row.append(el("span", "", label), el("strong", "", String(value)));
  return row;
}

function isQuestionStep(step) {
  const args = step?.proposal?.arguments || {};
  if (args.plugin_id === "builtin.interaction" && args.tool === "ask_user_question") return true;
  return step?.result?.kind === "question" || step?.result?.kind === "plan";
}

function questionOf(step) {
  const result = step?.result || {};
  const input = step?.proposal?.arguments?.input || {};
  return {
    question: result.question || input.question || step?.request_text || "请回答问题",
    options: Array.isArray(result.options) ? result.options : (Array.isArray(input.options) ? input.options : []),
    kind: result.kind || "question",
  };
}

export function renderStructuredQuestion(box, turn, step) {
  if (!isQuestionStep(step) || step.status !== "permission") return false;
  const value = questionOf(step);
  box.append(el("p", "scope-label", value.kind === "plan" ? "计划决定" : "业务问题"));
  const prose = el("div", "prose");
  prose.textContent = value.question;
  box.append(prose);
  box.append(el("p", "hint", "这是业务问题，回答只绑定当前 step_id 和本轮 revision；不是权限审批，也不会修改普通/完全模式。"));
  const actions = el("div", "actions plugin-question-options");
  const input = el("input");
  input.maxLength = 2000;
  input.placeholder = "输入回答";
  input.setAttribute("aria-label", "业务问题回答");
  const submit = button("提交回答", "primary", () => send(input.value));
  const send = (text) => {
    const valueText = String(text || "").trim();
    if (!valueText) {
      notify("请输入回答或选择一个选项", true);
      return;
    }
    submit.disabled = true;
    answerWorkStep(turn.turn_id, step.step_id, turn.revision, valueText)
      .then(() => notify("回答已提交；业务问题不会改变权限模式"))
      .catch((error) => notify(errorText(error), true))
      .finally(() => { if (submit.isConnected) submit.disabled = false; });
  };
  for (const option of value.options.slice(0, 8)) {
    if (typeof option !== "string") continue;
    actions.append(button(option, "quiet", () => send(option)));
  }
  box.append(actions);
  const free = el("div", "plugin-answer");
  free.append(input, submit);
  box.append(free);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send(input.value);
    }
  });
  return true;
}

function shellFacts(data) {
  const facts = el("div", "query-facts");
  const status = data.status === "running" ? "运行中"
    : data.status === "cancelled" ? "已停止"
      : data.status === "succeeded" ? "已结束（退出码 0）"
        : data.status === "failed" ? "已结束（非零退出码）"
          : data.status === "unknown" ? "未知" : data.status || "未知";
  facts.append(fact("状态", status));
  if (data.exit_code !== undefined && data.exit_code !== null) facts.append(fact("退出码", data.exit_code));
  else facts.append(fact("退出码", "尚无（仍在运行）"));
  if (data.cwd) facts.append(fact("工作目录", data.cwd));
  if (data.truncated || data.dropped) facts.append(fact("截断", data.dropped ? "环形缓冲已丢弃较早输出" : "是"));
  if (data.execution_stopped === true) facts.append(fact("停止确认", "真实进程已停止"));
  return facts;
}

function stopTarget(step) {
  const args = step?.proposal?.arguments || {};
  const data = step?.result?.data || {};
  const pluginId = args.plugin_id;
  if (pluginId === "builtin.shell" && data.job_id) {
    return { pluginId: "builtin.jobs", tool: "job_kill", input: { job_id: data.job_id }, label: "停止后台任务" };
  }
  if (pluginId === "builtin.terminal" && (data.session_id || data.id)) {
    return { pluginId: "builtin.terminal", tool: "terminal_signal", input: { session_id: data.session_id || data.id, signal: "terminate" }, label: "停止终端" };
  }
  if (pluginId === "builtin.jobs" && step?.proposal?.tool === "job_output" && args.input?.job_id) {
    return { pluginId: "builtin.jobs", tool: "job_kill", input: { job_id: args.input.job_id }, label: "停止后台任务" };
  }
  if (pluginId === "builtin.terminal" && ["terminal_read", "terminal_send", "terminal_list"].includes(step?.proposal?.tool) && args.input?.session_id) {
    return { pluginId: "builtin.terminal", tool: "terminal_signal", input: { session_id: args.input.session_id, signal: "terminate" }, label: "停止终端" };
  }
  return null;
}

async function waitTask(taskId, { timeoutMs = 45000 } = {}) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const detail = await taskDetail(taskId);
    if (detail?.task && ["succeeded", "failed", "cancelled", "timed_out", "awaiting_approval"].includes(detail.task.status)) return detail;
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
  return taskDetail(taskId);
}

async function approveIfAllowed(detail, conversationId) {
  const task = detail?.task;
  if (!task || task.status !== "awaiting_approval") return detail;
  const approval = (detail.approvals || []).find((item) => !item.decision && new Date(item.expires_at) > new Date());
  if (!approval) return detail;
  let mode = null;
  try {
    mode = await loadConversationPermission(conversationId);
  } catch {
    /* normal behavior remains explicit approval */
  }
  if (mode?.effective_mode !== "full") return detail;
  await decideApproval(approval.approval_id, "approve");
  return waitTask(task.task_id);
}

/*
 * Explicit stop uses the same persisted Harness task path as a normal plugin
 * call. It waits for the terminal task record and only reports a stop when the
 * result says the real process stopped. A normal-mode approval remains a real
 * Core approval; the page never fabricates a stopped state.
 */
async function stopProcess(turn, step, target, control) {
  control.disabled = true;
  const status = el("p", "hint stop-confirmation", "停止任务：已提交，等待真实确认…");
  control.closest(".actions")?.after(status);
  try {
    await loadPlugins().catch(() => {});
    const version = pluginById(pluginState.list, target.pluginId);
    if (!version?.digest) throw new ApiError(403, "PLUGIN_VERSION_UNAVAILABLE");
    const conversationId = state.conversation?.conversation_id;
    if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
    const nodeId = step.node_id || turn.node_id;
    if (!nodeId) throw new ApiError(400, "NO_NODE");
    const request = {
      request_id: uuid(),
      project_id: state.conversation?.project_id || null,
      intent: "execute_plugin",
      arguments: {
        plugin_id: target.pluginId,
        tool: target.tool,
        input: target.input,
        root_id: "workspace",
        release_digest: version.digest,
        context_id: conversationId,
        invocation_id: uuid(),
        capability_digests: {},
        max_subcalls: 0,
      },
    };
    const reply = await api("/api/v1/tasks", {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId, request }),
    });
    const taskId = reply?.task?.task_id;
    if (!taskId) throw new ApiError(500, "STOP_TASK_UNAVAILABLE");
    notify("停止请求已写入 Core，等待真实执行确认");
    let detail = await waitTask(taskId);
    if (detail?.task?.status === "awaiting_approval") {
      notify("Core 要求批准停止操作；这不是权限模式切换");
      const approval = (detail.approvals || []).find((item) => !item.decision && new Date(item.expires_at) > new Date());
      if (approval) {
        const approve = button("批准停止操作", "primary", async () => {
          approve.disabled = true;
          try {
            await decideApproval(approval.approval_id, "approve");
            detail = await waitTask(taskId);
            reportStop(detail, status);
          } catch (error) {
            notify(errorText(error), true);
          } finally {
            if (approve.isConnected) approve.disabled = false;
          }
        });
        control.replaceWith(approve);
        detail = await approveIfAllowed(detail, conversationId);
        if (detail?.task?.status === "awaiting_approval") {
          status.textContent = "停止任务：等待你批准这次停止操作";
          return;
        }
      }
    }
    reportStop(detail, status);
  } catch (error) {
    status.textContent = `停止任务失败：${errorText(error)}`;
    notify(errorText(error), true);
  } finally {
    if (control.isConnected) control.disabled = false;
  }
}

function reportStop(detail, status) {
  const task = detail?.task;
  const say = (text, error = false) => {
    if (status?.isConnected) status.textContent = text;
    notify(text, error);
  };
  if (!task) {
    say("停止任务：未读取到记录，状态未知", true);
    return;
  }
  if (["cancelled", "timed_out"].includes(task.status)) {
    say(`停止任务：${task.status}；未确认进程停止前不显示已停止`, true);
    return;
  }
  if (task.status !== "succeeded") {
    say(`停止任务：${task.status}${task.error?.code ? " · " + task.error.code : ""}`, true);
    return;
  }
  const output = task.result?.data || task.result || {};
  const stopped = output.execution_stopped === true || output.status === "cancelled";
  if (stopped) say(`停止任务：Core 已收到真实停止确认（status=${output.status || "unknown"} · execution_stopped=true）`);
  else if (output.status === "running") say("停止任务：执行端仍报告运行中，尚未停止", true);
  else say("停止任务：结果未知；保留原执行证据，未按已停止处理", true);
}

function renderShell(box, turn, step, data) {
  box.append(shellFacts(data));
  if (data.output) {
    const pre = el("pre");
    pre.textContent = data.output;
    box.append(pre);
  }
  box.append(el("p", "hint", "宿主 Shell/后台任务按 Node 当前 Windows 用户权限运行；工作目录不是文件隔离。刷新或重连不会重新执行命令。"));
  const target = stopTarget(step);
  const running = data.status === "running" || (data.status !== "cancelled" && data.execution_stopped !== true && data.status !== "succeeded" && data.status !== "failed");
  if (target && running) {
    const actions = el("div", "actions");
    const stop = button(target.label, "quiet danger", () => stopProcess(turn, step, target, stop));
    actions.append(stop);
    const pending = el("p", "hint", "停止按钮会创建一条真实的停止任务并等待 Core/Node 确认；不会在页面本地假称已停止。");
    box.append(actions, pending);
  }
}

function renderJobs(box, data) {
  if (!Array.isArray(data.jobs)) {
    box.append(el("p", "hint", "任务列表字段不完整。"));
    return;
  }
  const list = el("div", "query-jobs");
  for (const job of data.jobs.slice(0, 20)) {
    const row = el("div", "query-job");
    const head = el("div", "query-job-head");
    head.append(el("strong", "", job.id || job.job_id || "任务"), badge(job.status || "未知", job.status === "succeeded" ? "success" : job.status === "running" ? "pending" : job.status === "unknown" ? "unknown" : "muted"));
    row.append(head);
    list.append(row);
  }
  box.append(list);
}

function renderPresent(box, data) {
  const files = Array.isArray(data.files) ? data.files : [];
  if (!files.length) {
    box.append(el("p", "hint", "没有服务器登记的交付物。"));
    return;
  }
  const list = el("div", "deliverable-list");
  for (const file of files) {
    const row = el("article", "deliverable");
    const head = el("div", "card-head");
    head.append(el("strong", "", file.name || "交付物"));
    if (file.size_bytes !== undefined) head.append(badge(`${file.size_bytes} 字节`, "muted"));
    row.append(head);
    if (file.description) row.append(el("p", "hint", String(file.description)));
    row.append(el("p", "hint", `SHA-256 ${file.sha256 || "未提供"} · 只通过认证 download_url 读取`));
    const actions = el("div", "actions");
    const download = button("下载原件", "quiet", async () => {
      download.disabled = true;
      try {
        const dataValue = await readDeliverable(file.download_url);
        saveBytes(dataValue.bytes, file.name || dataValue.filename);
        notify("已通过认证接口读取交付物并开始下载");
      } catch (error) {
        notify(errorText(error), true);
      } finally {
        if (download.isConnected) download.disabled = false;
      }
    });
    const preview = button("预览文本", "quiet", async () => {
      preview.disabled = true;
      try {
        const dataValue = await readDeliverable(file.download_url);
        const text = safeTextPreview(dataValue.bytes);
        if (text.binary) {
          notify("该交付物不是 UTF-8 文本，只提供认证下载", true);
          return;
        }
        let boxNode = row.querySelector(".deliverable-preview");
        if (boxNode) { boxNode.remove(); return; }
        boxNode = el("pre", "deliverable-preview");
        boxNode.textContent = text.text + (text.truncated ? "\n…（预览已截断）" : "");
        row.append(boxNode);
      } catch (error) {
        notify(errorText(error), true);
      } finally {
        if (preview.isConnected) preview.disabled = false;
      }
    });
    actions.append(download, preview);
    row.append(actions);
    list.append(row);
  }
  box.append(list);
  box.append(el("p", "hint", "文件文本只按纯文本显示；不执行 HTML，也不运行插件提供的前端脚本。"));
}

function renderInspect(box, data) {
  if (Array.isArray(data.plugins)) {
    const list = el("div", "plugin-list");
    for (const item of data.plugins.slice(0, 64)) {
      const row = el("div", "plugin-row");
      const head = el("div", "plugin-row-head");
      head.append(el("strong", "", item.name || item.plugin_id));
      head.append(badge(TEST_LABELS[item.test_status] || item.test_status || "未知", item.test_status === "passed" ? "success" : "muted"));
      if (item.enabled) head.append(badge("已启用", "success"));
      row.append(head);
      row.append(el("p", "hint", `${item.plugin_id} · ${item.version} · ${item.digest?.slice(0, 16)}…`));
      list.append(row);
    }
    box.append(list);
    return;
  }
  const facts = el("div", "query-facts");
  facts.append(fact("插件", data.plugin_id || "—"), fact("版本", data.version || "—"));
  facts.append(fact("固定摘要", data.digest || "—"));
  facts.append(fact("测试", TEST_LABELS[data.test_status] || data.test_status || "未知"));
  facts.append(fact("批准", data.approved ? "是" : "否"));
  facts.append(fact("启用", data.enabled ? "是" : "否"));
  if (data.execution) {
    facts.append(fact("执行配置", data.execution.status === "unconfigured" ? "unconfigured（未配置）" : data.execution.status === "node_offline" ? "node_offline（节点离线）" : data.execution.status === "available" ? "available（可用）" : String(data.execution.status)));
    if (Array.isArray(data.execution.configured_tools)) facts.append(fact("已配置工具", data.execution.configured_tools.join(", ") || "无"));
  }
  box.append(facts);
  if (data.release) {
    const details = el("details");
    details.append(el("summary", "", "只读插件契约"));
    const pre = el("pre");
    pre.textContent = JSON.stringify(data.release, null, 2).slice(0, 12000);
    details.append(pre);
    box.append(details);
  }
}

function renderInstall(box, data) {
  const facts = el("div", "query-facts");
  facts.append(fact("插件", data.plugin_id || "—"), fact("版本", data.version || "—"));
  facts.append(fact("摘要", data.digest || "—"));
  facts.append(fact("测试", TEST_LABELS[data.test_status] || data.test_status || "未知"));
  facts.append(fact("批准", data.approved ? "是" : "否"));
  facts.append(fact("启用", data.enabled ? "是" : "否"));
  if (data.approval_required) facts.append(fact("下一步", "需要 owner 批准具体版本"));
  box.append(facts);
  box.append(el("p", "hint", data.enabled
    ? "服务端记录该版本已启用。完全模式的自动启用只以这里的实际状态为准。"
    : "版本已登记但未启用；普通模式需要批准具体版本，完全模式也只在服务端确认测试通过并自动启用后可用。"));
}

export function renderPluginWorkStep(box, turn, step) {
  const intent = step?.proposal?.intent;
  const args = step?.proposal?.arguments || {};
  const result = step?.result || null;
  const data = result?.data;
  if (intent === "plugin_execute") {
    const pluginId = args.plugin_id;
    const tool = args.tool;
    if (pluginId === "builtin.interaction" && tool === "exit_plan_mode") {
      box.append(el("p", "hint", "计划决定只影响本会话的计划模式，不修改普通/完全权限。"));
      box.append(el("p", "hint", step.permission_text ? `你的答复：${step.permission_text}` : "等待你对计划作出决定。"));
      return true;
    }
    if (pluginId === "builtin.present") {
      if (data) renderPresent(box, data);
      else box.append(el("p", "hint", "交付物尚未返回。"));
      return true;
    }
    if (pluginId === "builtin.shell" || (pluginId === "builtin.jobs" && ["job_output"].includes(tool))) {
      if (data) renderShell(box, turn, step, data);
      else box.append(el("p", "hint", "命令尚未返回实际状态。"));
      return true;
    }
    if (pluginId === "builtin.jobs" && tool === "job_list") {
      if (data) renderJobs(box, data);
      return true;
    }
    if (pluginId === "builtin.terminal") {
      if (data) {
        if (Array.isArray(data.jobs)) {
          renderJobs(box, data);
          return true;
        }
        const facts = shellFacts(data);
        if (data.session_id) facts.append(fact("终端", data.session_id));
        if (data.signal) facts.append(fact("信号", data.signal));
        box.append(facts);
        if (data.output) {
          const pre = el("pre");
          pre.textContent = data.output;
          box.append(pre);
        }
        const target = stopTarget(step);
        if (target && data.status === "running" && tool !== "terminal_signal" && tool !== "terminal_close") {
          const actions = el("div", "actions");
          const stop = button(target.label, "quiet danger", () => stopProcess(turn, step, target, stop));
          actions.append(stop);
          box.append(actions, el("p", "hint", "停止终端会调用真实停止任务并等待 Core/Node 确认。"));
        }
      }
      return true;
    }
    if (pluginId === "builtin.goal" || pluginId === "builtin.schedule" || pluginId === "builtin.todo") {
      if (data) {
        const pre = el("pre");
        pre.textContent = JSON.stringify(data, null, 2).slice(0, 6000);
        box.append(pre);
        box.append(el("p", "hint", "这是 Core 记录的计划状态；可在“目标与提醒”页核对、暂停或修改。"));
      }
      return true;
    }
    if (pluginId === "builtin.manager") {
      if (data) renderInspect(box, data);
      return true;
    }
    if (pluginId === "builtin.workflow") {
      if (data) {
        const pre = el("pre");
        pre.textContent = JSON.stringify(data, null, 2).slice(0, 12000);
        box.append(pre);
      }
      return true;
    }
    if (data && typeof data === "object") {
      const pre = el("pre");
      pre.textContent = JSON.stringify(data, null, 2).slice(0, 8000);
      box.append(pre);
    }
    return true;
  }
  if (intent === "plugin_inspect" && data) {
    renderInspect(box, data);
    return true;
  }
  if (intent === "plugin_install" && data) {
    renderInstall(box, data);
    return true;
  }
  return false;
}
