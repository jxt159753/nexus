"use strict";

/*
 * Native plugin, permission-mode and conversation-automation API client.
 *
 * This module only talks to the documented owner endpoints. It never infers
 * success from a local click: plugin lifecycle, permission revisions and
 * automation records are re-read from the server after every change.
 */

import { ApiError, api, uuid } from "./api.js";

export const SHANGHAI_ZONE = "Asia/Shanghai";

export const pluginState = {
  enabled: false,
  list: [],
  loadedAt: 0,
  detail: null,
  detailId: null,
  globalPermission: null,
  conversationPermission: null,
  conversationPermissionId: null,
  automation: null,
  automationId: null,
  conversations: [],
  conversationListAt: 0,
  loading: false,
};

export const TEST_LABELS = {
  bundled: "内置随应用发布",
  pending: "等待独立测试",
  running: "独立测试进行中",
  passed: "契约测试通过",
  failed: "测试失败",
  unavailable: "测试不可用",
  interrupted: "测试中断 / 结果未知",
};

export const TEST_KIND = {
  bundled: "muted",
  pending: "pending",
  running: "pending",
  passed: "success",
  failed: "failed",
  unavailable: "muted",
  interrupted: "unknown",
};

export const MODE_LABELS = { normal: "普通", full: "完全" };

export function modeLabel(mode) {
  return MODE_LABELS[mode] || mode || "未知";
}

export function sourceLabel(source) {
  if (source === "global") return "跟随全局";
  if (source === "conversation") return "会话覆盖";
  return source || "未知";
}

export function formatShanghai(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: SHANGHAI_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date) + "（Asia/Shanghai）";
  } catch {
    return date.toISOString();
  }
}

export function originalTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

export function formatRange(start, end) {
  const left = formatShanghai(start);
  const right = formatShanghai(end);
  return left && right ? `${left} → ${right}` : left || right || "未提供";
}

export function parseRule(row) {
  try {
    if (row?.rule && typeof row.rule === "object") return row.rule;
    if (typeof row?.rule_json === "string") return JSON.parse(row.rule_json);
  } catch {
    /* malformed server data stays visibly unknown */
  }
  return null;
}

export function ruleTimeZone(row) {
  const rule = parseRule(row);
  return rule?.time_zone || rule?.timeZone || "UTC";
}

export function permissionSummary(value) {
  if (!value) return "尚未读取";
  return `${modeLabel(value.effective_mode || value.mode)} · ${sourceLabel(value.source || "global")} · revision ${value.revision}`;
}

export async function loadGlobalPermission() {
  const value = await api("/api/v1/permissions/default");
  pluginState.globalPermission = { mode: value.mode, revision: value.revision };
  return pluginState.globalPermission;
}

export async function saveGlobalPermission(mode, expectedRevision, requestId) {
  if (mode !== "normal" && mode !== "full") throw new ApiError(400, "INVALID_PERMISSION_MODE");
  await api("/api/v1/permissions/default", {
    method: "PUT",
    body: JSON.stringify({
      request_id: requestId || uuid(),
      expected_revision: expectedRevision,
      mode,
    }),
  });
  return loadGlobalPermission();
}

export async function loadConversationPermission(conversationId) {
  if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
  const value = await api(`/api/v1/chat/conversations/${conversationId}/permission`);
  pluginState.conversationPermission = value;
  pluginState.conversationPermissionId = conversationId;
  return value;
}

export async function saveConversationPermission(conversationId, mode, expectedRevision, requestId) {
  if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
  if (![null, "normal", "full"].includes(mode)) throw new ApiError(400, "INVALID_PERMISSION_MODE");
  await api(`/api/v1/chat/conversations/${conversationId}/permission`, {
    method: "PUT",
    body: JSON.stringify({
      request_id: requestId || uuid(),
      expected_revision: expectedRevision,
      mode,
    }),
  });
  // The server is authoritative: always read back instead of trusting the
  // request we just sent.
  return loadConversationPermission(conversationId);
}

export async function loadPlugins() {
  const value = await api("/api/v1/plugins");
  pluginState.enabled = !!value.enabled;
  pluginState.list = Array.isArray(value.plugins) ? value.plugins : [];
  pluginState.loadedAt = Date.now();
  return value;
}

export async function loadPluginDetail(versionId) {
  const value = await api(`/api/v1/plugins/versions/${versionId}`);
  pluginState.detail = value;
  pluginState.detailId = versionId;
  return value;
}

export async function startPluginTest(versionId, digest, conversationId) {
  if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
  return api(`/api/v1/plugins/versions/${versionId}/test`, {
    method: "POST",
    body: JSON.stringify({ conversation_id: conversationId, digest }),
  });
}

export async function decidePlugin(versionId, digest, action, conversationId) {
  if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
  if (!["approve", "enable", "disable"].includes(action)) throw new ApiError(400, "INVALID_ACTION");
  return api(`/api/v1/plugins/versions/${versionId}/decision`, {
    method: "POST",
    body: JSON.stringify({ conversation_id: conversationId, digest, action }),
  });
}

/*
 * Test execution is asynchronous on the server. Poll the authenticated detail
 * endpoint until it leaves pending/running; never turn a timeout into success.
 */
export async function waitPluginTest(versionId, { timeoutMs = 180000, intervalMs = 1500 } = {}) {
  const started = Date.now();
  let detail = await loadPluginDetail(versionId);
  while (["pending", "running"].includes(detail.test_status) && Date.now() - started < timeoutMs) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    detail = await loadPluginDetail(versionId);
  }
  return detail;
}

export async function loadPluginConversations() {
  const value = await api("/api/v1/chat/conversations?archived=false");
  pluginState.conversations = value.conversations || [];
  pluginState.conversationListAt = Date.now();
  return pluginState.conversations;
}

export async function loadAutomation(conversationId) {
  if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
  const value = await api(`/api/v1/plugins/conversations/${conversationId}`);
  pluginState.automation = value;
  pluginState.automationId = conversationId;
  return value;
}

export async function savePlanMode(conversationId, enabled, expectedRevision) {
  const value = await api(`/api/v1/plugins/conversations/${conversationId}/plan`, {
    method: "PUT",
    body: JSON.stringify({ expected_revision: expectedRevision, enabled: !!enabled }),
  });
  await loadAutomation(conversationId);
  return value;
}

export async function changeSchedule(conversationId, scheduleId, changes, requestId) {
  const value = await api(`/api/v1/plugins/conversations/${conversationId}/schedules/${scheduleId}`, {
    method: "PATCH",
    body: JSON.stringify({ request_id: requestId || uuid(), changes: changes || {} }),
  });
  await loadAutomation(conversationId);
  return value;
}

export async function removeSchedule(conversationId, scheduleId, requestId) {
  const value = await api(`/api/v1/plugins/conversations/${conversationId}/schedules/${scheduleId}`, {
    method: "DELETE",
    body: JSON.stringify({ request_id: requestId || uuid(), changes: {} }),
  });
  await loadAutomation(conversationId);
  return value;
}

export async function changeGoal(conversationId, goalId, status, requestId) {
  if (status !== "paused" && status !== "active") throw new ApiError(400, "INVALID_GOAL_STATUS");
  const value = await api(`/api/v1/plugins/conversations/${conversationId}/goals/${goalId}`, {
    method: "PATCH",
    body: JSON.stringify({ request_id: requestId || uuid(), changes: { status } }),
  });
  await loadAutomation(conversationId);
  return value;
}

/*
 * The generation entry reuses the normal chat work path. The caller must show
 * the exact outbound text and the per-round budget before POSTing; this helper
 * deliberately does not add tools, raise limits or choose a different model.
 */
export async function submitPluginGeneration({ conversationId, nodeId, text, workspace = true }) {
  if (!conversationId) throw new ApiError(400, "NO_CONVERSATION");
  if (!nodeId) throw new ApiError(400, "NO_NODE");
  const history = await api(`/api/v1/chat/conversations/${conversationId}/turns`);
  const revision = history?.conversation?.revision ?? 0;
  const catalog = workspace ? await api(`/api/v1/chat/work-catalog?node_id=${nodeId}`) : null;
  const payload = {
    request_id: uuid(),
    expected_revision: revision,
    text: String(text || "").slice(0, 2000),
    include_context: false,
    allow_model: true,
    allow_tools: true,
    node_id: nodeId,
    allow_skills: false,
    ...(catalog?.work_digest ? {
      workspace_work: true,
      work_digest: catalog.work_digest,
      allow_work_results: true,
    } : {}),
  };
  await api(`/api/v1/chat/conversations/${conversationId}/turns`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return payload;
}

export async function latestGenerationTurn(conversationId, requestId) {
  const value = await api(`/api/v1/chat/conversations/${conversationId}/turns`);
  const turns = value.turns || [];
  return turns.find((turn) => turn.request_id === requestId) || turns.at(-1) || null;
}

export async function loadPluginControlTask(taskId) {
  return api(`/api/v1/tasks/${taskId}`);
}

export function highestVersion(list, pluginId) {
  const rows = (list || []).filter((item) => item.plugin_id === pluginId);
  return rows.sort((a, b) => String(b.version).localeCompare(String(a.version), undefined, { numeric: true }))[0] || null;
}

export function pluginById(list, pluginId) {
  return (list || []).find((item) => item.plugin_id === pluginId && item.enabled)
    || (list || []).find((item) => item.plugin_id === pluginId)
    || null;
}

function decodeBase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

/*
 * Deliverables are only read through their server-issued download_url. In the
 * Windows shell the native bridge performs the authenticated GET and returns
 * base64; a browser uses the same URL with the session cookie.
 */
export async function readDeliverable(url) {
  if (!url || !/^\/api\/v1\/plugins\/deliverables\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(url))) {
    throw new ApiError(400, "DELIVERABLE_URL_INVALID");
  }
  const native = window.__NEXUS_NATIVE__;
  if (native?.readDeliverable) {
    const value = await native.readDeliverable(url);
    if (!value || value.status < 200 || value.status >= 300) {
      throw new ApiError(value?.status || 0, "DELIVERABLE_UNAVAILABLE");
    }
    return { bytes: decodeBase64(value.body_base64 || ""), filename: value.filename || "download", sha256: value.sha256 || "" };
  }
  const response = await fetch(url, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/octet-stream" },
  });
  if (!response.ok) throw new ApiError(response.status, "DELIVERABLE_UNAVAILABLE");
  const buffer = new Uint8Array(await response.arrayBuffer());
  if (buffer.byteLength > 4 * 1024 * 1024) throw new ApiError(413, "DELIVERABLE_TOO_LARGE");
  return {
    bytes: buffer,
    filename: "download",
    sha256: response.headers.get("X-Content-SHA256") || "",
  };
}

export function safeTextPreview(bytes, limit = 262144) {
  if (!bytes || bytes.byteLength === 0) return { text: "", truncated: false, binary: false };
  const clipped = bytes.byteLength > limit;
  const slice = bytes.subarray(0, limit);
  if (slice.includes(0)) return { text: "", truncated: clipped, binary: true };
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(slice, { stream: clipped });
    return { text, truncated: clipped, binary: false };
  } catch {
    return { text: "", truncated: clipped, binary: true };
  }
}

export function saveBytes(bytes, filename) {
  const blob = new Blob([bytes], { type: "application/octet-stream" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = String(filename || "download").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").slice(0, 120) || "download";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 2000);
}

export function pluginVersionDigest(pluginId) {
  return pluginById(pluginState.list, pluginId)?.digest || null;
}
