import { marked } from "./vendor/markdown/marked.esm.js";
import DOMPurify from "./vendor/markdown/purify.es.mjs";

const ALLOWED_TAGS = [
  "p", "br", "strong", "em", "del", "blockquote", "ul", "ol", "li",
  "pre", "code", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
  "table", "thead", "tbody", "tr", "th", "td", "a", "span",
];
const ALLOWED_ATTR = ["href", "title", "target", "rel", "class", "align", "colspan", "rowspan"];

marked.setOptions({ gfm: true, breaks: false, headerIds: false, mangle: false });

function safe(html) {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ["img", "video", "audio", "iframe", "object", "embed", "form", "input", "button", "style", "script"],
    FORBID_ATTR: ["style", "onerror", "onload", "onclick"],
  });
}

function linkify(container) {
  for (const link of container.querySelectorAll("a")) {
    const href = link.getAttribute("href") || "";
    if (!/^(https?:|mailto:)/i.test(href)) {
      link.removeAttribute("href");
      continue;
    }
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
}

export function renderMarkdown(container, text) {
  if (!container) return;
  const value = String(text ?? "");
  if (!value.trim()) {
    container.replaceChildren();
    return;
  }
  const html = marked.parse(value, { gfm: true, breaks: false });
  container.innerHTML = safe(html);
  linkify(container);
}

export function markdownPreview(text, limit = 220) {
  const value = String(text ?? "");
  if (!value.trim()) return "";
  const box = document.createElement("div");
  renderMarkdown(box, value);
  const plain = (box.textContent || "").replace(/\s+/g, " ").trim();
  return plain.length > limit ? plain.slice(0, limit) + "…" : plain;
}
