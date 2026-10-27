"use strict";

/*
 * Conversation attachments: explicit local selection, bounded upload, stable
 * request ids, server reconciliation and a separate per-turn model consent.
 *
 * This module never treats an uploaded file as model input. A file is sent to
 * the model only after `model-preview` has shown the actual text/image/metadata
 * range and the user confirms that exact selection and digest.
 */

import { ApiError, api, createConversation, loadHistory, state, uuid } from "./api.js";
import {
  createAttachmentEvidence,
  inspectAttachmentSnapshot,
  openImagePreview,
} from "./image.js";

const MAX_DISPLAY_NAME = 200;
const HEX64 = /^[a-f0-9]{64}$/;
const UUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const UPLOAD_TIMEOUT_MS = 75000;

const ui = {
  notify: () => {},
  openSheet: () => {},
  closeSheet: () => {},
  refresh: () => {},
  openChat: () => {},
  clearComposer: () => {},
  runAction: async (fn) => { await fn(); },
};

export function bindAttachmentUI(handlers) {
  Object.assign(ui, handlers);
}

const uploadQueue = [];
let activeUploads = 0;

export const attachmentState = {
  conversationId: "",
  supported: null,
  capabilities: null,
  items: [],
  preview: null,
  previewKey: "",
  loading: false,
  notice: "",
  noticeError: false,
};

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

function freshDraft(conversationId) {
  // Never let a queued upload from the previous conversation start later.
  uploadQueue.length = 0;
  attachmentState.conversationId = conversationId || "";
  attachmentState.supported = null;
  attachmentState.capabilities = null;
  attachmentState.items = [];
  attachmentState.preview = null;
  attachmentState.previewKey = "";
  attachmentState.loading = false;
  attachmentState.notice = "";
  attachmentState.noticeError = false;
}

export function resetAttachments() {
  leaveConversation();
}

/*
 * Called before switching or creating a conversation. The next draft never
 * inherits attachment ids from the previous conversation; uploads that are
 * still in flight are aborted, and only uploads created by this draft are
 * removed from the Core (historical turn attachments are never deleted).
 */
export function leaveConversation() {
  const items = attachmentState.items.slice();
  freshDraft("");
  renderAttachmentComposers();
  for (const item of items) {
    if (item.status === "uploading") item.controller?.abort();
    cleanupDraftItem(item).catch(() => {});
  }
}

async function cleanupDraftItem(item) {
  if (item.status === "uploading") await item.promise?.catch(() => {});
  if (!item.ownedByDraft) return;
  const attachment = item.attachment || await findExisting(item);
  if (!attachment) return;
  try {
    await api(`${basePath(item.conversationId)}/${safeIdentifier(attachment.attachment_id)}`, {
      method: "DELETE",
      body: "{}",
    });
  } catch {
    /* best-effort cleanup; ATTACHMENT_IN_USE and network errors keep history */
  }
}

export function clearAttachments() {
  attachmentState.items = [];
  attachmentState.preview = null;
  attachmentState.previewKey = "";
  attachmentState.notice = "";
  attachmentState.noticeError = false;
  renderAttachmentComposers();
}

export function draftAttachments() {
  return attachmentState.items.slice();
}

export function attachmentConversationId() {
  return attachmentState.conversationId;
}

function currentConversationId() {
  return state.conversation?.conversation_id || "";
}

function basePath(conversationId) {
  return `/api/v1/chat/conversations/${conversationId}/attachments`;
}

function safeIdentifier(value) {
  return typeof value === "string" && UUID.test(value) ? value : "";
}

function itemBasePath(item) {
  return `${basePath(item.conversationId)}/${safeIdentifier(item.attachment?.attachment_id || "")}`;
}

export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "大小未知";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(value < 10 * 1024 * 1024 ? 1 : 0)} MiB`;
}

function sanitizeName(raw) {
  const text = String(raw || "").replace(/\\/g, "/").split("/").pop() || "";
  const cleaned = text.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  const value = cleaned || "附件";
  return value.length > MAX_DISPLAY_NAME ? value.slice(0, MAX_DISPLAY_NAME) : value;
}

export function safeDownloadName(raw) {
  const value = sanitizeName(raw).replace(/[<>:"|?*]/g, "_").replace(/^\.+$/, "附件");
  return value || "附件";
}

function previewReason(kind, reason) {
  if (kind === "image") return "图片快照已生成";
  if (kind === "text") return "可读取文本（发送时最多 8 KiB）";
  if (reason === "IMAGE_SOURCE_LIMIT") return "超过图片源限制，原件已保留";
  if (reason === "IMAGE_DECODER_UNAVAILABLE") return "Core 未安装图片解码器，原件已保留";
  if (reason === "IMAGE_PREVIEW_UNAVAILABLE") return "图片无法预览，原件已保留";
  if (reason === "FILE_TYPE_NOT_PARSED") return "已上传，尚未解析";
  return "已上传，尚未解析";
}

export function attachmentStatus(item) {
  if (item.status === "queued") return { label: "等待上传", kind: "pending" };
  if (item.status === "uploading") return {
    label: item.progress === null ? "上传中" : `上传中 ${Math.round(item.progress * 100)}%`,
    kind: "pending",
  };
  if (item.status === "reconciling") return { label: "结果未知，正在核对服务器", kind: "pending" };
  if (item.status === "failed") return { label: "上传失败", kind: "failed" };
  if (item.status === "cancelled") return { label: "已取消", kind: "muted" };
  if (item.attachment) {
    return { label: previewReason(item.attachment.preview_kind, item.attachment.reason), kind: "success" };
  }
  return { label: "等待上传", kind: "pending" };
}

export function attachmentBlockReason() {
  if (!attachmentState.items.length) return null;
  if (attachmentState.items.some((item) => ["queued", "uploading", "reconciling"].includes(item.status))) {
    return "附件尚未完成上传；请等待上传完成后再发送。";
  }
  if (attachmentState.items.some((item) => item.status === "failed")) {
    return "有附件上传失败；请重试或移除后再发送。";
  }
  return null;
}

export function attachmentBlocksSend() {
  return !!attachmentBlockReason();
}

export async function ensureAttachments() {
  const conversationId = currentConversationId();
  if (!conversationId) {
    if (attachmentState.items.length || attachmentState.conversationId) {
      freshDraft("");
      renderAttachmentComposers();
    }
    return attachmentState;
  }
  if (attachmentState.conversationId !== conversationId) {
    freshDraft(conversationId);
    renderAttachmentComposers();
  }
  if (attachmentState.supported === null && !attachmentState.loading) {
    attachmentState.loading = true;
    try {
      const page = await api(`${basePath(conversationId)}`);
      attachmentState.supported = true;
      attachmentState.capabilities = page?.capabilities || null;
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 405 || error.status === 501)) {
        attachmentState.supported = false;
        attachmentState.notice = "当前 Core 不支持会话附件接口；本页不会上传或发送附件。";
        attachmentState.noticeError = true;
      } else if (error instanceof ApiError && error.status === 401) {
        attachmentState.supported = null;
        throw error;
      } else {
        attachmentState.supported = false;
        attachmentState.notice = "读取附件能力失败；暂时不能上传附件。";
        attachmentState.noticeError = true;
      }
    } finally {
      attachmentState.loading = false;
    }
    renderAttachmentComposers();
  }
  return attachmentState;
}

function maxFiles() {
  const value = Number(attachmentState.capabilities?.max_files_per_turn);
  return Number.isInteger(value) && value > 0 && value <= 4 ? value : 4;
}

function maxFileBytes() {
  const value = Number(attachmentState.capabilities?.max_file_bytes);
  return Number.isInteger(value) && value > 0 ? value : 16 * 1024 * 1024;
}

function setNotice(message, error = false) {
  attachmentState.notice = message;
  attachmentState.noticeError = error;
}

function invalidatePreview() {
  attachmentState.preview = null;
  attachmentState.previewKey = "";
  // A stale consent sheet must not survive a selection change, but unrelated
  // sheets (settings, approvals) stay open.
  const title = document.getElementById("sheet-title");
  const sheet = document.getElementById("sheet");
  if (title && sheet && !sheet.hidden && title.textContent.startsWith("本次附件外发范围")) {
    ui.closeSheet?.();
  }
}

function selectionKey() {
  return attachmentState.items
    .filter((item) => item.status === "uploaded" && item.attachment)
    .map((item) => item.attachment.attachment_id)
    .join(",");
}

function renderAttachmentComposers() {
  for (const [id, view] of [["chat-attachments", "chat"], ["dock-attachments", "home"]]) {
    const host = document.getElementById(id);
    if (!host) continue;
    const fragment = document.createDocumentFragment();
    const items = attachmentState.items;
    if (attachmentState.notice) {
      fragment.append(el("p", "attachment-notice" + (attachmentState.noticeError ? " error" : ""), attachmentState.notice));
    }
    if (items.length) {
      const list = el("div", "attachment-list");
      for (const item of items) list.append(renderDraftItem(item));
      fragment.append(list);
    }
    if (items.length && attachmentState.supported === true) {
      const caps = attachmentState.capabilities || {};
      const hint = el("p", "attachment-limits",
        `单个最多 ${formatBytes(caps.max_file_bytes)} · 每次最多 ${maxFiles()} 个文件`);
      fragment.append(hint);
      if (items.some((item) => item.attachment?.preview_kind === "image")) {
        const enabled = caps.image_model_enabled !== false;
        fragment.append(el("p", "hint attachment-image-switch", enabled
          ? "图片输入已启用：发送前会展示实际快照并单独征求同意。"
          : "图片输入未启用：本轮仅发送文件信息，不会发送图片内容。"));
      }
    }
    host.replaceChildren(fragment);
    host.dataset.view = view;
    host.hidden = !items.length && !attachmentState.notice;
  }
  for (const id of ["chat-attach", "dock-attach"]) {
    const node = document.getElementById(id);
    if (!node) continue;
    node.disabled = attachmentState.supported === false;
    node.title = attachmentState.supported === false ? "当前 Core 不支持附件接口" : "添加文件";
  }
  ui.refreshComposerState?.();
}

function renderDraftItem(item) {
  const status = attachmentStatus(item);
  const box = el("article", "attachment-item");
  box.dataset.status = item.status;
  const main = el("div", "attachment-main");
  const head = el("div", "attachment-head");
  head.append(el("strong", "attachment-name", item.name));
  head.append(el("span", `badge ${status.kind}`, status.label));
  main.append(head);

  const facts = [formatBytes(item.size)];
  if (item.sha256) facts.push(`SHA-256 ${item.sha256.slice(0, 12)}…`);
  if (item.fromHistory) facts.push("沿用本会话已上传原件");
  main.append(el("p", "attachment-meta", facts.join(" · ")));

  if (item.status === "uploading" && item.progress !== null) {
    const progress = el("div", "attachment-progress");
    progress.setAttribute("role", "progressbar");
    progress.setAttribute("aria-valuemin", "0");
    progress.setAttribute("aria-valuemax", "100");
    const percent = Math.max(0, Math.min(100, Math.round(item.progress * 100)));
    progress.setAttribute("aria-valuenow", String(percent));
    progress.setAttribute("aria-label", `${item.name} 上传进度`);
    const bar = el("i");
    bar.style.width = `${percent}%`;
    progress.append(bar);
    main.append(progress);
  }
  if (item.status === "failed") {
    main.append(el("p", "attachment-error", item.error || "上传失败，可重试或移除。"));
  }
  if (item.status === "uploading") {
    main.append(el("p", "hint", "上传中；完成前不会发送消息。取消会核对服务器，避免留下重复原件。"));
  }
  if (item.status === "uploaded" && item.attachment) {
    const attachment = item.attachment;
    if (attachment.preview_kind === "unparsed") {
      main.append(el("p", "hint", "已上传，尚未解析。模型只会收到名称、大小、SHA-256 和未解析说明，不能声称读过正文。"));
    } else if (attachment.preview_kind === "text") {
      main.append(el("p", "hint", "文本按不受信资料处理；发送时最多 8 KiB，超出会明确截断。"));
    } else if (attachment.preview_kind === "image") {
      main.append(el("p", "hint", "图片使用 Core 验证过的 PNG/JPEG 快照；可能缩小、有损，GIF/WebP 只取首帧，透明区域白底。"));
    }
  }
  box.append(main);

  const actions = el("div", "attachment-actions");
  if (item.status === "uploaded" && item.attachment) {
    if (item.attachment.preview_kind === "text" || item.attachment.preview_kind === "image") {
      actions.append(button(item.attachment.preview_kind === "image" ? "预览图片" : "预览文本", "quiet", () => runPreview(item)));
    }
    actions.append(button("下载原件", "quiet", () => downloadAttachment(item.attachment)));
  }
  if (item.status === "failed") {
    actions.append(button("用原编号重试", "quiet", () => retryItem(item)));
  }
  if (["uploading", "queued", "reconciling"].includes(item.status)) {
    actions.append(button("取消上传", "quiet danger", () => cancelItem(item)));
  } else if (item.status === "uploaded" || item.status === "failed") {
    actions.append(button("移除", "quiet", () => removeItem(item)));
  }
  box.append(actions);
  return box;
}

async function sha256Hex(file) {
  if (!globalThis.crypto?.subtle) return null;
  try {
    const digest = await globalThis.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}

function bytesToBase64(bytes) {
  let text = "";
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    text += String.fromCharCode.apply(null, bytes.subarray(offset, offset + chunk));
  }
  return btoa(text);
}

function base64ToBytes(value) {
  const raw = atob(value);
  const bytes = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  return bytes;
}

function nativeBridge() {
  const native = globalThis.__NEXUS_NATIVE__;
  return native && typeof native.attachmentRequest === "function" ? native : null;
}

class UploadError extends Error {
  constructor(status, code) {
    super(code || `HTTP_${status}`);
    this.name = "UploadError";
    this.status = status;
    this.code = code || `HTTP_${status}`;
  }
}

function parseXhr(xhr) {
  let value = null;
  try { value = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch { value = null; }
  if (xhr.status < 200 || xhr.status >= 300) {
    const code = typeof value?.detail === "string" ? value.detail : `HTTP_${xhr.status}`;
    throw new UploadError(xhr.status, code);
  }
  return value;
}

function rawUpload(item, signal, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const conversationId = encodeURIComponent(item.conversationId);
    const query = `?request_id=${encodeURIComponent(item.requestId)}`
      + `&filename=${encodeURIComponent(item.name)}`
      + `&media_type=${encodeURIComponent(item.mediaType || "application/octet-stream")}`;
    xhr.open("POST", `/api/v1/chat/conversations/${conversationId}/attachments${query}`, true);
    xhr.withCredentials = true;
    xhr.timeout = UPLOAD_TIMEOUT_MS;
    if (state.csrf) xhr.setRequestHeader("X-CSRF-Token", state.csrf);
    xhr.setRequestHeader("Content-Type", item.mediaType || "application/octet-stream");
    let done = false;
    const finish = (fn, value) => { if (done) return; done = true; signal?.removeEventListener("abort", abort); fn(value); };
    const abort = () => { try { xhr.abort(); } catch { /* already closed */ } };
    signal?.addEventListener("abort", abort, { once: true });
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      try { finish(resolve, parseXhr(xhr)); } catch (error) { finish(reject, error); }
    };
    xhr.onerror = () => finish(reject, new UploadError(0, "NETWORK_ERROR"));
    xhr.ontimeout = () => finish(reject, new UploadError(0, "UPLOAD_TIMEOUT"));
    xhr.onabort = () => finish(reject, new UploadError(0, "ABORTED"));
    xhr.send(item.file);
  });
}

async function nativeUpload(item, signal) {
  const native = nativeBridge();
  const bytes = new Uint8Array(await item.file.arrayBuffer());
  const payload = JSON.stringify({
    request_id: item.requestId,
    filename: item.name,
    media_type: item.mediaType || "application/octet-stream",
    data_base64: bytesToBase64(bytes),
  });
  const path = `${basePath(item.conversationId)}?encoding=base64`;
  const transferId = item.transferId = uuid();
  const onAbort = () => { native.cancelAttachment?.(transferId).catch?.(() => {}); };
  signal?.addEventListener("abort", onAbort, { once: true });
  try {
    const response = await native.attachmentRequest(transferId, "POST", path, payload,
      state.csrf ? { "x-csrf-token": state.csrf } : null);
    let value = null;
    try { value = response.body ? JSON.parse(response.body) : null; } catch { value = null; }
    if (response.status < 200 || response.status >= 300) {
      const code = typeof value?.detail === "string" ? value.detail : `HTTP_${response.status}`;
      throw new UploadError(response.status, code);
    }
    return value;
  } catch (error) {
    if (String(error?.message || "").includes("取消")) throw new UploadError(0, "ABORTED");
    if (error instanceof UploadError) throw error;
    throw new UploadError(0, "NETWORK_ERROR");
  } finally {
    signal?.removeEventListener("abort", onAbort);
    item.transferId = "";
  }
}

async function performUpload(item, signal) {
  const onProgress = (value) => { item.progress = value; renderAttachmentComposers(); };
  if (nativeBridge()) return nativeUpload(item, signal);
  return rawUpload(item, signal, onProgress);
}

function uploadErrorMessage(item, error) {
  const code = error?.code || "UPLOAD_FAILED";
  const status = error?.status ?? 0;
  const labels = {
    UPLOAD_TOO_LARGE: `文件超过服务器上限（${formatBytes(maxFileBytes())}）`,
    UPLOAD_TIMEOUT: "上传超时；结果未知，会在重试前核对服务器",
    NETWORK_ERROR: "网络中断；结果未知，可在恢复后核对并重试",
    ABORTED: "已取消上传",
    INVALID_UPLOAD: "文件名或内容不符合服务器要求",
    UPLOAD_ENCODING_UNSUPPORTED: "服务器拒绝该传输编码",
    UPLOAD_BUSY: "服务器上传通道繁忙，请稍后重试",
    BUSY: "已达容量、数量或速率限制，请稍后重试",
    IDEMPOTENCY_CONFLICT: "同一上传编号已绑定其他内容；不会重复存储",
    CONVERSATION_ARCHIVED: "会话已归档，不能上传附件",
    ATTACHMENT_IN_USE: "附件已关联聊天轮次，不能通过移除按钮删除",
    NOT_FOUND: "附件或会话不存在，可能已被删除",
    FORBIDDEN: "当前身份不能上传到这个会话",
    MODEL_INPUT_LIMIT: "本轮附件与上下文超过模型请求上限，请减少本轮选择",
  };
  if (labels[code]) return labels[code];
  if (status === 401) return "登录已过期，请重新登录后再上传";
  if (status === 403) return "上传被拒绝（403）";
  if (status === 404) return "附件或会话不存在（404）";
  if (status === 409) return "上传冲突（409）；同一编号不能保存不同内容";
  if (status === 413) return `文件过大（413）；单个最多 ${formatBytes(maxFileBytes())}`;
  if (status === 429) return "容量、数量、速率或传输通道受限（429）；请稍后重试";
  if (status >= 500) return "Core 暂时不可用（5xx）；结果未知，可核对后重试";
  return `上传失败：${code}`;
}

async function findExisting(item) {
  if (!item.sha256) return null;
  try {
    const page = await api(basePath(item.conversationId));
    const match = (page?.attachments || []).find((attachment) => attachment.filename === item.name
      && attachment.size_bytes === item.size && attachment.sha256 === item.sha256);
    return match || null;
  } catch {
    return null;
  }
}

async function startUpload(item) {
  item.status = "uploading";
  item.error = "";
  item.progress = null;
  item.controller = new AbortController();
  item.promise = (async () => {
    try {
      const value = await performUpload(item, item.controller.signal);
      const attachment = value?.attachment;
      if (!attachment || safeIdentifier(attachment.attachment_id) === "") {
        throw new UploadError(0, "INVALID_RESPONSE");
      }
      item.status = "uploaded";
      item.attachment = attachment;
      item.progress = null;
      item.error = "";
      item.ownedByDraft = true;
      setNotice(`已上传：${attachment.filename}`);
      invalidatePreview();
    } catch (error) {
      if (item.status === "cancelled") return;
      const unknown = error instanceof UploadError
        && (error.status === 0 || error.status === 408 || error.status >= 500);
      if (unknown) {
        item.status = "reconciling";
        renderAttachmentComposers();
        const existing = await findExisting(item);
        if (existing) {
          item.status = "uploaded";
          item.attachment = existing;
          item.ownedByDraft = true;
          item.error = "";
          setNotice(`服务器已有该原件（${existing.filename}），已沿用原记录，没有重复存储。`);
        } else {
          item.status = "failed";
          item.error = `${uploadErrorMessage(item, error)}；未在服务器找到该文件，可用原编号和原件重试。`;
          setNotice(item.error, true);
        }
      } else {
        item.status = "failed";
        item.error = uploadErrorMessage(item, error);
        setNotice(item.error, true);
      }
      invalidatePreview();
    } finally {
      item.controller = null;
      item.progress = null;
      renderAttachmentComposers();
    }
  })();
  renderAttachmentComposers();
  return item.promise;
}

export async function addFiles(fileList, view = "chat") {
  const files = [...(fileList || [])].filter((file) => file && typeof file === "object");
  if (!files.length) return;
  if (!currentConversationId()) {
    // A new chat creates its conversation first, then uploads into it. This
    // keeps every attachment scoped to one server-side conversation.
    await createConversation(state.prefs.projectId || null);
    await loadHistory(state.conversation.conversation_id);
    freshDraft(state.conversation.conversation_id);
  }
  await ensureAttachments();
  if (attachmentState.supported !== true) {
    setNotice("当前 Core 不支持会话附件接口，不能上传。", true);
    renderAttachmentComposers();
    return;
  }
  const room = maxFiles() - attachmentState.items.length;
  if (room <= 0) {
    setNotice(`本轮最多选择 ${maxFiles()} 个附件；请先移除一些文件。`, true);
    renderAttachmentComposers();
    return;
  }
  const accepted = files.slice(0, room);
  if (files.length > room) {
    setNotice(`本轮最多选择 ${maxFiles()} 个附件；多出的 ${files.length - room} 个没有加入。`, true);
  } else {
    setNotice("");
  }
  for (const file of accepted) {
    const name = sanitizeName(file.name);
    const size = Number(file.size);
    if (!Number.isInteger(size) || size < 0) continue;
    if (size > maxFileBytes()) {
      const item = {
        localId: uuid(), conversationId: currentConversationId(), requestId: uuid(),
        file, name, size, mediaType: file.type || "application/octet-stream",
        status: "failed", unknown: false, progress: null, attachment: null,
        error: `文件超过服务器上限（${formatBytes(maxFileBytes())}），没有上传。`,
        sha256: null, ownedByDraft: false, fromHistory: false,
      };
      attachmentState.items.push(item);
      setNotice(item.error, true);
      continue;
    }
    const item = {
      localId: uuid(), conversationId: currentConversationId(), requestId: uuid(),
      file, name, size, mediaType: file.type || "application/octet-stream",
      status: "queued", unknown: false, progress: null, attachment: null,
      error: "", sha256: null, ownedByDraft: false, fromHistory: false,
    };
    attachmentState.items.push(item);
    // Uploading starts immediately (at most two transfers) so the composer can
    // show real progress without exceeding Core's transfer slots.
    item.sha256 = await sha256Hex(file);
    scheduleUpload(item);
  }
  invalidatePreview();
  renderAttachmentComposers();
  if (view === "home" && attachmentState.items.length) ui.openChat?.();
}

export function addExistingAttachment(attachment) {
  if (!attachment || !currentConversationId()) return false;
  const conversationId = currentConversationId();
  if (attachmentState.conversationId !== conversationId) freshDraft(conversationId);
  if (attachment.conversation_id && attachment.conversation_id !== conversationId) {
    setNotice("该附件属于其他会话，不能带到当前会话。", true);
    renderAttachmentComposers();
    return false;
  }
  if (attachmentState.items.some((item) => item.attachment?.attachment_id === attachment.attachment_id)) {
    setNotice("该附件已在本轮选择中。");
    renderAttachmentComposers();
    return false;
  }
  if (attachmentState.items.length >= maxFiles()) {
    setNotice(`本轮最多选择 ${maxFiles()} 个附件。`, true);
    renderAttachmentComposers();
    return false;
  }
  attachmentState.items.push({
    localId: uuid(), conversationId, requestId: null, file: null,
    name: sanitizeName(attachment.filename), size: Number(attachment.size_bytes),
    mediaType: attachment.media_type || "application/octet-stream",
    status: "uploaded", unknown: false, progress: null, attachment,
    error: "", sha256: attachment.sha256 || null, ownedByDraft: false, fromHistory: true,
  });
  invalidatePreview();
  setNotice("已加入下一轮选择；发送前会重新预览实际外发范围并单独征求同意。");
  renderAttachmentComposers();
  ui.openChat?.();
  return true;
}

export async function cancelItem(item) {
  if (!item) return;
  let deleted = true;
  if (item.status === "uploading") {
    item.status = "cancelled";
    item.controller?.abort();
    renderAttachmentComposers();
    await item.promise?.catch(() => {});
    // The transfer may have committed before the abort; reconcile and delete
    // only the upload this draft owns, never a historical turn attachment.
    const existing = item.attachment || await findExisting(item);
    if (existing) {
      item.attachment = existing;
      deleted = await deleteUploaded(item, { silent: true });
    }
  } else if (item.status === "uploaded") {
    deleted = await deleteUploaded(item);
  } else if (item.status === "queued") {
    const index = uploadQueue.indexOf(item);
    if (index >= 0) uploadQueue.splice(index, 1);
    item.status = "cancelled";
    removeLocalItem(item);
    renderAttachmentComposers();
    return;
  } else if (item.status === "failed" || item.status === "cancelled") {
    const existing = item.attachment || await findExisting(item);
    if (existing) {
      item.attachment = existing;
      deleted = await deleteUploaded(item, { silent: true });
    }
  }
  if (!deleted) return;
  removeLocalItem(item);
  renderAttachmentComposers();
}

async function deleteUploaded(item, { silent = false } = {}) {
  if (!item.ownedByDraft && item.fromHistory) return true;
  if (!item.attachment) return true;
  const path = `${basePath(item.conversationId)}/${safeIdentifier(item.attachment.attachment_id)}`;
  try {
    await api(path, { method: "DELETE", body: "{}" });
    return true;
  } catch (error) {
    if (error instanceof ApiError && error.status === 403 && error.code === "ATTACHMENT_IN_USE") {
      setNotice("该附件已关联聊天轮次，不能通过移除按钮删除；历史记录保留。", true);
      return false;
    }
    if (error instanceof ApiError && error.status === 404) return true;
    if (!silent) setNotice(`删除上传件失败：${errorTextFor(error)}`, true);
    return false;
  }
}

function errorTextFor(error) {
  if (error instanceof ApiError) {
    const labels = {
      ATTACHMENT_IN_USE: "附件已关联聊天轮次，不能删除",
      NOT_FOUND: "附件已不存在",
      FORBIDDEN: "当前身份不能删除这个附件",
      AUTH_REQUIRED: "登录已过期",
      BUSY: "服务器繁忙",
    };
    return labels[error.code] || error.code;
  }
  return error?.message || "操作失败";
}

function removeLocalItem(item) {
  const index = attachmentState.items.indexOf(item);
  if (index >= 0) attachmentState.items.splice(index, 1);
  invalidatePreview();
}

export async function removeItem(item) {
  if (!item) return;
  const deleted = await deleteUploaded(item);
  if (!deleted) return;
  removeLocalItem(item);
  setNotice(`已从本轮选择移除：${item.name}`);
  renderAttachmentComposers();
}

export async function retryItem(item) {
  if (!item || item.status !== "failed" || !item.file) return;
  // A stale failed item may already exist on the server; look first so the
  // stable request id and original bytes are reused, never a new key.
  const existing = await findExisting(item);
  if (existing) {
    item.attachment = existing;
    item.status = "uploaded";
    item.ownedByDraft = true;
    item.error = "";
    setNotice(`服务器已有该原件（${existing.filename}），已沿用，没有重复存储。`);
    invalidatePreview();
    renderAttachmentComposers();
    return;
  }
  await scheduleUpload(item);
}

function scheduleUpload(item) {
  uploadQueue.push(item);
  pumpUploads();
  return item.promise || Promise.resolve();
}

function pumpUploads() {
  // Core exposes two transfer slots shared with downloads; the composer sends
  // one upload at a time so a slot race cannot surface as a spurious 429.
  while (activeUploads < 1 && uploadQueue.length) {
    const next = uploadQueue.shift();
    if (!next || next.status === "cancelled") continue;
    activeUploads += 1;
    startUpload(next).finally(() => {
      activeUploads -= 1;
      pumpUploads();
    });
  }
}

async function runPreview(item) {
  if (!item.attachment) return;
  const host = el("div", "attachment-preview-panel");
  host.append(el("p", "hint", "正在读取服务器预览…"));
  ui.openSheet?.({
    title: `附件预览 · ${item.name}`,
    body: host,
    actions: [{ label: "关闭", className: "quiet", onClick: () => ui.closeSheet?.() }],
  });
  try {
    const value = await api(itemBasePath(item) + "/preview");
    host.replaceChildren();
    renderPreviewBody(host, value.attachment || item.attachment, value.preview);
  } catch (error) {
    host.replaceChildren(el("p", "attachment-error", `读取预览失败：${errorTextFor(error)}`));
  }
}

export function renderPreviewBody(host, attachment, preview) {
  const kind = preview?.kind || attachment?.preview_kind;
  if (kind === "text") {
    host.append(el("p", "scope-label", "文本预览（不受信资料）"));
    const pre = el("pre", "attachment-text-preview");
    pre.textContent = typeof preview?.text === "string" ? preview.text : "";
    host.append(pre);
    if (preview?.truncated) {
      host.append(el("p", "attachment-warning", "文本已截断：这里只显示前 8 KiB；发送给模型时也只包含这部分，并会明确标记截断。"));
    }
    host.append(el("p", "hint", "只作为普通文本显示，不执行 HTML、SVG 或脚本；预览不等于模型已读取。"));
    return true;
  }
  if (kind === "image") {
    host.append(el("p", "scope-label", "图片快照（Core 验证）"));
    const figure = createAttachmentEvidence(preview?.snapshot, {
      title: attachment?.filename || "图片快照",
      onOpen: ({ url, alt, caption, trigger }) => openImagePreview({ url, alt, caption, trigger }),
    });
    if (figure) host.append(figure);
    else host.append(el("p", "attachment-warning", "图片预览未通过严格白名单校验；原件仍已保存，可下载。"));
    return true;
  }
  host.append(el("p", "scope-label", "当前不可预览"));
  host.append(el("p", "attachment-warning", previewReason(kind, preview?.reason || attachment?.reason)));
  host.append(el("p", "hint", "原件已保存并可下载；预览不可用不等于上传失败。PDF、Word、压缩包、程序和音视频不会被解析或执行。"));
  return false;
}

function downloadFileBrowser(attachment) {
  const conversationId = safeIdentifier(attachment.conversation_id);
  const attachmentId = safeIdentifier(attachment.attachment_id);
  if (!conversationId || !attachmentId) throw new Error("附件地址无效");
  const anchor = document.createElement("a");
  anchor.href = `${basePath(conversationId)}/${attachmentId}/content`;
  anchor.download = safeDownloadName(attachment.filename);
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = safeDownloadName(filename);
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

export async function downloadAttachment(attachment) {
  if (!attachment) return;
  const native = nativeBridge();
  if (!native) {
    downloadFileBrowser(attachment);
    setNotice(`已请求下载原件：${attachment.filename}`);
    return;
  }
  const conversationId = safeIdentifier(attachment.conversation_id);
  const attachmentId = safeIdentifier(attachment.attachment_id);
  if (!conversationId || !attachmentId) {
    setNotice("附件地址无效，未下载。", true);
    return;
  }
  const transferId = uuid();
  const path = `${basePath(conversationId)}/${attachmentId}/content?encoding=base64`;
  setNotice(`正在通过原生桥下载 ${attachment.filename}…`);
  try {
    const response = await native.attachmentRequest(transferId, "GET", path, null, null);
    let value = null;
    try { value = response.body ? JSON.parse(response.body) : null; } catch { value = null; }
    if (response.status < 200 || response.status >= 300) {
      const code = typeof value?.detail === "string" ? value.detail : `HTTP_${response.status}`;
      throw new UploadError(response.status, code);
    }
    if (typeof value?.data_base64 !== "string" || value.data_base64.length > 23 * 1024 * 1024) {
      throw new UploadError(0, "INVALID_RESPONSE");
    }
    const blob = new Blob([base64ToBytes(value.data_base64)], { type: "application/octet-stream" });
    saveBlob(blob, attachment.filename);
    setNotice(`已保存原件：${attachment.filename}`);
  } catch (error) {
    setNotice(`下载失败：${error instanceof UploadError ? uploadErrorMessage(attachment, error) : error?.message || "未知错误"}`, true);
  }
}

export async function previewAttachments() {
  const items = attachmentState.items.filter((item) => item.status === "uploaded" && item.attachment);
  if (!items.length) throw new ApiError(400, "EMPTY_ATTACHMENT_SELECTION");
  const ids = items.map((item) => item.attachment.attachment_id);
  const value = await api(`${basePath(attachmentState.conversationId)}/model-preview`, {
    method: "POST",
    body: JSON.stringify({ attachment_ids: ids }),
  });
  if (!value || !Array.isArray(value.attachments) || !Array.isArray(value.payloads)
    || value.attachments.length !== ids.length || !HEX64.test(value.attachments_digest || "")) {
    throw new ApiError(502, "INVALID_RESPONSE");
  }
  attachmentState.preview = value;
  attachmentState.previewKey = ids.join(",");
  return value;
}

export function withAttachmentConsent(payload, preview) {
  return {
    ...payload,
    attachment_ids: preview.attachments.map((attachment) => attachment.attachment_id),
    attachments_digest: preview.attachments_digest,
    allow_attachment_model: true,
  };
}

export function emptyAttachmentFields() {
  return { attachment_ids: [], attachments_digest: null, allow_attachment_model: false };
}

function modeLabel(mode) {
  if (mode === "text") return "文本正文";
  if (mode === "image") return "图片快照";
  return "仅文件信息";
}

export function openAttachmentConsentSheet({ preview, capabilityImageEnabled, onConfirm, onCancel }) {
  const body = document.createDocumentFragment();
  const imageEnabled = capabilityImageEnabled !== false;
  body.append(el("p", "", "以下文件将连同本轮文字一起发送给 DeepSeek。上传本身没有发送；这里展示的是实际外发范围。"));
  body.append(el("p", "scope-label", "本轮附件"));
  const list = el("div", "attachment-consent-list");
  (preview.attachments || []).forEach((attachment, index) => {
    const payload = preview.payloads?.[index] || {};
    const box = el("section", "attachment-consent");
    const head = el("div", "attachment-head");
    head.append(el("strong", "", attachment.filename));
    head.append(el("span", "badge", modeLabel(attachment.model_mode)));
    box.append(head, el("p", "attachment-meta", `${formatBytes(attachment.size_bytes)} · SHA-256 ${String(attachment.sha256 || "").slice(0, 16)}…`));
    if (attachment.model_mode === "text") {
      const pre = el("pre", "attachment-text-preview");
      pre.textContent = typeof payload.text === "string" ? payload.text : "";
      box.append(pre);
      if (payload.truncated) {
        box.append(el("p", "attachment-warning", "文本已截断：只发送前 8 KiB，并明确标记不完整。"));
      }
    } else if (attachment.model_mode === "image") {
      const figure = createAttachmentEvidence(payload.snapshot, {
        title: attachment.filename,
        onOpen: ({ url, alt, caption, trigger }) => openImagePreview({ url, alt, caption, trigger }),
      });
      if (figure) box.append(figure);
      else box.append(el("p", "attachment-warning", "图片快照未通过严格校验；不会把它当作图片发送。"));
      box.append(el("p", "hint", "模型收到的是这个快照，可能缩小或有损；透明区域白底，GIF/WebP 仅首帧。"));
    } else {
      box.append(el("p", "attachment-warning", attachment.model_notice
        || "本轮仅发送文件信息（名称、大小、SHA-256 与未解析说明）；模型不会收到正文。"));
    }
    if (attachment.model_notice && attachment.model_mode !== "metadata") {
      box.append(el("p", "hint", attachment.model_notice));
    }
    list.append(box);
  });
  body.append(list);

  body.append(el("p", "scope-label", "图片开关"));
  const imageCount = (preview.attachments || []).filter((attachment) => attachment.preview_kind === "image").length;
  body.append(el("p", "hint", imageCount === 0
    ? "本轮没有图片附件。"
    : imageEnabled
      ? "图片输入已启用；上方的 PNG/JPEG 快照会作为图片发送。"
      : "图片输入未启用：本轮仅发送文件信息，不会发送图片内容。"));

  body.append(el("p", "scope-label", "不额外发送"));
  const notSent = el("ul");
  notSent.append(el("li", "", "未选择的文件、其他会话附件或未重新选择的历史附件"));
  notSent.append(el("li", "", "原文件的绝对路径、模型密钥或 Node 令牌"));
  body.append(notSent);

  body.append(el("p", "hint", "原请求上限（完整请求 32 KiB 与单轮费用）不因附件提高；超出会拒绝，不会截断图片 Base64、自动拆分调用或声称读过未解析内容。选择或摘要改变后需要重新预览和同意。"));

  const consentLabel = el("label", "switch");
  const consent = document.createElement("input");
  consent.type = "checkbox";
  consent.checked = false;
  consentLabel.append(consent, document.createTextNode("本次同意将以上附件范围发送给 DeepSeek"));
  body.append(consentLabel);

  const actions = [
    { label: "取消，保留草稿", className: "quiet", onClick: () => { ui.closeSheet?.(); onCancel?.(); } },
    {
      label: "同意并发送",
      className: "primary",
      onClick: () => {
        if (!consent.checked) {
          ui.notify?.("请先勾选本次附件外发同意", true);
          return;
        }
        Promise.resolve(onConfirm?.()).catch((error) => {
          ui.notify?.(error?.message || "发送失败", true);
        });
      },
    },
  ];
  ui.openSheet?.({ title: "本次附件外发范围", body, actions });
}

export function renderTurnAttachments(turn) {
  const attachments = Array.isArray(turn?.attachments) ? turn.attachments : [];
  if (!attachments.length) return null;
  const box = el("section", "turn-attachments");
  box.append(el("p", "turn-attachments-title", `本轮附件 · ${attachments.length} 个`));
  for (const attachment of attachments) {
    const row = el("div", "turn-attachment");
    const main = el("div", "attachment-main");
    const head = el("div", "attachment-head");
    head.append(el("strong", "attachment-name", sanitizeName(attachment.filename)));
    head.append(el("span", "badge", modeLabel(attachment.model_mode)));
    main.append(head);
    const facts = [formatBytes(attachment.size_bytes)];
    if (attachment.preview_kind) facts.push(previewReason(attachment.preview_kind, attachment.reason));
    main.append(el("p", "attachment-meta", facts.join(" · ")));
    row.append(main);
    const actions = el("div", "attachment-actions");
    const view = button("预览", "quiet", () => {
      const host = el("div", "attachment-preview-panel");
      host.append(el("p", "hint", "正在读取服务器预览…"));
      ui.openSheet?.({
        title: `附件预览 · ${attachment.filename}`,
        body: host,
        actions: [{ label: "关闭", className: "quiet", onClick: () => ui.closeSheet?.() }],
      });
      api(`${basePath(attachment.conversation_id)}/${safeIdentifier(attachment.attachment_id)}/preview`)
        .then((value) => {
          host.replaceChildren();
          renderPreviewBody(host, value.attachment || attachment, value.preview);
        })
        .catch((error) => host.replaceChildren(el("p", "attachment-error", `读取预览失败：${errorTextFor(error)}`)));
    });
    actions.append(view);
    actions.append(button("下载原件", "quiet", () => downloadAttachment(attachment)));
    if (!state.conversation?.archived) {
      actions.append(button("重新选择用于下一轮", "quiet", () => addExistingAttachment(attachment)));
    }
    row.append(actions);
    box.append(row);
  }
  box.append(el("p", "hint", "历史附件不会自动再次发送给模型，也不会自动上传或重建消息；需要下一轮阅读时请重新选择并确认外发范围。"));
  return box;
}

function hasFiles(event) {
  return Array.from(event?.dataTransfer?.types || []).includes("Files");
}

function wireComposer(view) {
  const form = document.getElementById(view === "chat" ? "chat-form" : "dock-form");
  const textarea = document.getElementById(view === "chat" ? "chat-text" : "dock-text");
  if (!form || !textarea) return;
  const depth = () => form.classList.toggle("drop-active", false);
  form.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    form.classList.add("drop-active");
  });
  form.addEventListener("dragleave", (event) => {
    if (!form.contains(event.relatedTarget)) depth();
  });
  form.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    form.classList.remove("drop-active");
    addFiles(event.dataTransfer.files, view).catch((error) => ui.notify?.(error?.message || "添加附件失败", true));
  });
  textarea.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    addFiles(files, view).catch((error) => ui.notify?.(error?.message || "粘贴图片失败", true));
  });
}

export function initAttachmentComposer() {
  const input = document.getElementById("attachment-input");
  if (input) {
    input.addEventListener("change", () => {
      const files = [...(input.files || [])];
      input.value = "";
      const view = state.view === "home" ? "home" : "chat";
      if (files.length) addFiles(files, view).catch((error) => ui.notify?.(error?.message || "添加附件失败", true));
    });
  }
  for (const [id, view] of [["chat-attach", "chat"], ["dock-attach", "home"]]) {
    const node = document.getElementById(id);
    if (!node || !input) continue;
    node.addEventListener("click", async () => {
      try {
        await ensureAttachments();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return;
      }
      if (attachmentState.supported === false) {
        ui.notify?.("当前 Core 不支持会话附件接口", true);
        return;
      }
      input.click();
    });
  }
  wireComposer("chat");
  wireComposer("home");
}
