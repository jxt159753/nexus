"use strict";

/*
 * Bounded, verified rendering for filesystem.read_image results.
 * The module never turns arbitrary strings into image sources: only a
 * canonical base64 PNG/JPEG whose dimensions, byte length and metadata agree
 * with the original tool result is accepted.
 */

export const IMAGE_LIMITS = {
  maxDimension: 1024,
  maxBytes: 8192,
  maxBase64: 10924,
  maxSourceBytes: 4 * 1024 * 1024,
  maxSourceDimension: 8192,
  maxSourcePixels: 16_000_000,
};

const MEDIA_TYPES = new Set(["image/png", "image/jpeg"]);
const SOURCE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const HEX64 = /^[a-f0-9]{64}$/;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
const IMAGE_DATA = /^data:image\/(?:png|jpeg);base64,/i;

function integer(value, min, max) {
  return Number.isInteger(value) && value >= min && value <= max;
}

function bytesOf(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  for (const key of ["path", "source_sha256", "source_size_bytes", "source_media_type", "original_width",
    "original_height", "image", "downscaled", "first_frame_only"]) {
    if (!(key in data)) return null;
  }
  if (typeof data.path !== "string" || !data.path || data.path.length > 240) return null;
  if (!HEX64.test(data.source_sha256) || !SOURCE_TYPES.has(data.source_media_type)) return null;
  if (!integer(data.source_size_bytes, 1, IMAGE_LIMITS.maxSourceBytes)) return null;
  if (!integer(data.original_width, 1, IMAGE_LIMITS.maxSourceDimension)
    || !integer(data.original_height, 1, IMAGE_LIMITS.maxSourceDimension)
    || data.original_width * data.original_height > IMAGE_LIMITS.maxSourcePixels) return null;
  if (typeof data.downscaled !== "boolean" || typeof data.first_frame_only !== "boolean") return null;

  const image = data.image;
  if (!image || typeof image !== "object" || Array.isArray(image)) return null;
  for (const key of ["media_type", "width", "height", "size_bytes", "sha256", "data_base64"]) {
    if (!(key in image)) return null;
  }
  if (!MEDIA_TYPES.has(image.media_type)) return null;
  if (!integer(image.width, 1, IMAGE_LIMITS.maxDimension)
    || !integer(image.height, 1, IMAGE_LIMITS.maxDimension)) return null;
  if (!integer(image.size_bytes, 1, IMAGE_LIMITS.maxBytes)) return null;
  if (!HEX64.test(image.sha256)) return null;
  if (image.width > data.original_width || image.height > data.original_height) return null;

  const expectedDownscaled = image.width !== data.original_width || image.height !== data.original_height;
  if (data.downscaled !== expectedDownscaled) return null;
  if (["image/gif", "image/webp"].includes(data.source_media_type) && !data.first_frame_only) return null;
  if (data.source_media_type === "image/jpeg" && data.first_frame_only) return null;

  const encoded = image.data_base64;
  if (typeof encoded !== "string" || encoded.length < 4 || encoded.length > IMAGE_LIMITS.maxBase64
    || encoded.length % 4 !== 0 || !BASE64.test(encoded)) return null;
  let raw;
  try {
    raw = atob(encoded);
  } catch {
    return null;
  }
  if (!raw || raw.length !== image.size_bytes || btoa(raw) !== encoded) return null;
  const decoded = rasterDimensions(raw, image.media_type);
  if (!decoded || decoded[0] !== image.width || decoded[1] !== image.height) return null;

  return {
    root_id: data.root_id === "workspace" ? "workspace" : "workspace",
    path: data.path,
    source_sha256: data.source_sha256,
    source_size_bytes: data.source_size_bytes,
    source_media_type: data.source_media_type,
    original_width: data.original_width,
    original_height: data.original_height,
    image: {
      media_type: image.media_type,
      width: image.width,
      height: image.height,
      size_bytes: image.size_bytes,
      sha256: image.sha256,
      data_base64: encoded,
    },
    downscaled: data.downscaled,
    first_frame_only: data.first_frame_only,
  };
}

function rasterDimensions(raw, mediaType) {
  const at = (offset) => raw.charCodeAt(offset);
  if (mediaType === "image/png") {
    if (raw.length < 45
      || [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].some((value, index) => at(index) !== value)
      || [0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52].some((value, index) => at(8 + index) !== value)
      || [0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82].some((value, index) => at(raw.length - 12 + index) !== value)) return null;
    return [readUint32(raw, 16), readUint32(raw, 20)];
  }
  if (mediaType !== "image/jpeg" || at(0) !== 0xff || at(1) !== 0xd8
    || at(raw.length - 2) !== 0xff || at(raw.length - 1) !== 0xd9) return null;
  let offset = 2;
  while (offset + 4 <= raw.length) {
    if (at(offset) !== 255) break;
    while (offset < raw.length && at(offset) === 255) offset += 1;
    if (offset >= raw.length) break;
    const marker = at(offset);
    offset += 1;
    const length = (at(offset) << 8) | at(offset + 1);
    if (length < 2 || offset + length > raw.length) break;
    if ((marker === 0xc0 || marker === 0xc2) && length >= 8) {
      return [(at(offset + 5) << 8) | at(offset + 6), (at(offset + 3) << 8) | at(offset + 4)];
    }
    if (marker === 0xda) break;
    offset += length;
  }
  return null;
}

function readUint32(raw, offset) {
  return ((raw.charCodeAt(offset) << 24) >>> 0)
    + ((raw.charCodeAt(offset + 1) << 16) | (raw.charCodeAt(offset + 2) << 8) | raw.charCodeAt(offset + 3));
}

export function inspectImageResult(data) {
  const checked = bytesOf(data);
  if (!checked) return { ok: false, reason: "图片结果未通过严格白名单校验" };
  return {
    ok: true,
    data: checked,
    url: `data:${checked.image.media_type};base64,${checked.image.data_base64}`,
    mediaType: checked.image.media_type,
    width: checked.image.width,
    height: checked.image.height,
    originalWidth: checked.original_width,
    originalHeight: checked.original_height,
    sizeBytes: checked.image.size_bytes,
    sourceSizeBytes: checked.source_size_bytes,
    sourceMediaType: checked.source_media_type,
    downscaled: checked.downscaled,
    firstFrameOnly: checked.first_frame_only,
    path: checked.path,
    sha256: checked.image.sha256,
  };
}

export function imageSummary(data) {
  if (data && typeof data === "object" && data.image && typeof data.image === "object"
    && data.image.data_base64 !== undefined && !inspectImageResult(data).ok) return null;
  const info = inspectImageResult(data);
  if (info.ok) {
    return {
      path: info.path,
      media_type: info.mediaType,
      width: info.width,
      height: info.height,
      original_width: info.originalWidth,
      original_height: info.originalHeight,
      size_bytes: info.sizeBytes,
      sha256: info.sha256,
      downscaled: info.downscaled,
      first_frame_only: info.firstFrameOnly,
    };
  }
  const image = data && typeof data === "object" ? data.image : null;
  if (!image || typeof image !== "object") return null;
  return {
    media_type: typeof image.media_type === "string" ? image.media_type : "",
    width: Number.isInteger(image.width) ? image.width : null,
    height: Number.isInteger(image.height) ? image.height : null,
    size_bytes: Number.isInteger(image.size_bytes) ? image.size_bytes : null,
    sha256: typeof image.sha256 === "string" ? image.sha256 : "",
    original_width: Number.isInteger(data?.original_width) ? data.original_width : null,
    original_height: Number.isInteger(data?.original_height) ? data.original_height : null,
    notice: typeof image.notice === "string" ? image.notice : "",
  };
}

function detailsLine(label, value) {
  const p = document.createElement("p");
  p.className = "hint image-detail";
  const strong = document.createElement("strong");
  strong.textContent = label + "：";
  p.append(strong, document.createTextNode(String(value)));
  return p;
}

function badge(text) {
  const node = document.createElement("span");
  node.className = "badge";
  node.textContent = text;
  return node;
}

export function createImageEvidence(data, { onOpen = null } = {}) {
  const info = inspectImageResult(data);
  if (!info.ok) return null;
  const figure = document.createElement("figure");
  figure.className = "image-evidence";

  const frame = document.createElement("div");
  frame.className = "image-frame";
  const thumb = document.createElement("button");
  thumb.type = "button";
  thumb.className = "image-thumb";
  thumb.setAttribute("aria-label", `展开图片预览：${info.path}，${info.width}×${info.height}`);
  thumb.setAttribute("aria-haspopup", "dialog");
  const image = document.createElement("img");
  image.src = info.url;
  image.alt = `workspace/${info.path} 的模型快照，${info.width}×${info.height}`;
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("error", () => {
    thumb.replaceWith(Object.assign(document.createElement("p"), {
      className: "hint image-broken",
      textContent: "图片数据通过校验，但浏览器无法解码显示；执行状态以任务记录为准。",
    }));
  }, { once: true });
  thumb.append(image);
  frame.append(thumb);
  figure.append(frame);

  const caption = document.createElement("figcaption");
  const title = document.createElement("strong");
  title.textContent = `workspace/${info.path}`;
  caption.append(title);

  const sizeLine = document.createElement("p");
  sizeLine.className = "image-dimensions";
  sizeLine.textContent = `原图 ${info.originalWidth}×${info.originalHeight} → 提供给模型的快照 ${info.width}×${info.height}`;
  caption.append(sizeLine);

  const badges = document.createElement("p");
  badges.className = "image-badges";
  badges.append(badge(info.downscaled ? "已缩小" : "未缩小"));
  badges.append(badge(info.mediaType === "image/jpeg" ? "有损 JPEG 快照" : "PNG 快照"));
  if (info.firstFrameOnly) badges.append(badge("仅首帧"));
  badges.append(badge(`≤${Math.round(IMAGE_LIMITS.maxBytes / 1024)} KiB`));
  caption.append(badges);

  caption.append(detailsLine("快照大小", `${info.sizeBytes} 字节`));
  caption.append(detailsLine("SHA-256", `${info.sha256.slice(0, 16)}…`));
  const sourceLabel = info.sourceMediaType.replace("image/", "").toUpperCase();
  caption.append(detailsLine("源文件", `${sourceLabel} · ${info.sourceSizeBytes} 字节`));
  const notes = [];
  if (info.downscaled) notes.push(`已从原图 ${info.originalWidth}×${info.originalHeight} 缩小`);
  if (info.mediaType === "image/jpeg") notes.push("快照使用有损 JPEG");
  if (info.firstFrameOnly) notes.push("GIF/WebP 只保留首帧");
  notes.push("透明区域按白底显示");
  const note = document.createElement("p");
  note.className = "hint image-note";
  note.textContent = `${notes.join("；")}。模型实际收到的是这个快照；如文字或细节不可辨识，不能声称模型看到了完整原图。`;
  caption.append(note);

  const hint = document.createElement("p");
  hint.className = "hint image-open-hint";
  hint.textContent = "选择缩略图或按 Enter 可展开预览；预览只使用已校验的结果数据，不会重新调用工具或模型。";
  caption.append(hint);
  figure.append(caption);

  thumb.addEventListener("click", () => {
    if (onOpen) onOpen({ url: info.url, alt: image.alt, caption: `workspace/${info.path} · ${info.width}×${info.height}`, trigger: thumb });
    else openImagePreview({ url: info.url, alt: image.alt, caption: `workspace/${info.path} · ${info.width}×${info.height}`, trigger: thumb });
  });
  return figure;
}

export function createImageMetadata(data) {
  // Metadata-only projections (job_output) omit data_base64. If inline image
  // bytes are present they must pass the full whitelist before any rendering.
  if (data && typeof data === "object" && data.image && typeof data.image === "object"
    && data.image.data_base64 !== undefined) return null;
  const summary = imageSummary(data);
  if (!summary || !MEDIA_TYPES.has(summary.media_type)) return null;
  const box = document.createElement("div");
  box.className = "image-metadata";
  const head = document.createElement("p");
  head.className = "hint";
  head.textContent = "图片元数据（不包含图片本体，也不重新读取图片）";
  box.append(head);
  const parts = [];
  if (summary.original_width && summary.original_height) parts.push(`原图 ${summary.original_width}×${summary.original_height}`);
  if (summary.width && summary.height) parts.push(`快照 ${summary.width}×${summary.height}`);
  if (summary.media_type) parts.push(summary.media_type === "image/jpeg" ? "有损 JPEG" : "PNG");
  if (summary.size_bytes) parts.push(`${summary.size_bytes} 字节`);
  if (summary.sha256) parts.push(`SHA-256 ${summary.sha256.slice(0, 16)}…`);
  if (parts.length) box.append(detailsLine("摘要", parts.join(" · ")));
  if (summary.notice) {
    const notice = document.createElement("p");
    notice.className = "hint";
    notice.textContent = summary.notice;
    box.append(notice);
  }
  return box;
}

export function openImagePreview({ url, alt = "", caption = "", trigger = null }) {
  if (typeof url !== "string" || !IMAGE_DATA.test(url)) return;
  let dialog = document.getElementById("image-preview-dialog");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "image-preview-dialog";
    dialog.className = "image-dialog";
    dialog.setAttribute("aria-label", "图片预览");
    const body = document.createElement("div");
    body.className = "image-dialog-body";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "icon-button image-dialog-close";
    close.setAttribute("aria-label", "关闭图片预览");
    close.textContent = "×";
    const image = document.createElement("img");
    image.className = "image-dialog-img";
    image.alt = "";
    const text = document.createElement("p");
    text.className = "hint image-dialog-caption";
    body.append(close, image, text);
    dialog.append(body);
    document.body.append(dialog);
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", () => {
      if (dialog._trigger?.isConnected) dialog._trigger.focus();
      dialog._trigger = null;
    });
  }
  const image = dialog.querySelector(".image-dialog-img");
  const text = dialog.querySelector(".image-dialog-caption");
  dialog._trigger = trigger;
  image.src = url;
  image.alt = alt;
  text.textContent = caption;
  if (!dialog.open) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.open = true;
  }
  requestAnimationFrame(() => dialog.querySelector(".image-dialog-close")?.focus());
}

export function safeJson(value, limit = 12000) {
  let raw;
  try {
    raw = JSON.stringify(value, (key, item) => {
      if (key === "data_base64") return "[base64 已省略]";
      if (typeof item === "string" && IMAGE_DATA.test(item)) return "[图片内联数据已省略]";
      return item;
    }, 2);
  } catch {
    return "[无法序列化的记录]";
  }
  if (!raw) return "";
  return raw.length > limit ? raw.slice(0, limit) + "…" : raw;
}

export function imageIntent(intent) {
  return intent === "read_image";
}
