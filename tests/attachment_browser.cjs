"use strict";
const { chromium } = require(process.env.NEXUS_PLAYWRIGHT || "playwright-core");
const assert = require("node:assert/strict");

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const VALID_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
);

function bigBinary() {
  const buffer = Buffer.alloc(3 * 1024 * 1024 + 137);
  for (let index = 0; index < buffer.length; index += 1) buffer[index] = (index * 31 + 7) & 0xff;
  return buffer;
}

function textFile() {
  return Buffer.from("你".repeat(5000), "utf8");
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (part) => { raw += part; });
process.stdin.on("end", async () => {
  const input = JSON.parse(raw);
  const browser = await chromium.launch({ headless: true, channel: process.env.NEXUS_TEST_BROWSER_CHANNEL || "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    // Intentional 401/403/404/409/413 probes are asserted separately.
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console:${message.text()}`);
    }
  });
  const evidence = {
    passed: false,
    browser: `msedge/${(await browser.version?.()) || "system"}`,
    progress_observed: false,
    text_truncation: false,
    metadata_only: false,
    image_snapshot: false,
    image_source_limit: false,
    corrupt_image_kept: false,
    refresh_no_reupload: false,
    no_consent_no_send: false,
    attachment_only_message: false,
    drag_and_drop: false,
    paste_image: false,
    cross_conversation_404: false,
    attachment_in_use_403: false,
    duplicate_key_409: false,
    oversized_413: false,
    unauthorized_401: false,
    cancel_upload: false,
    legacy_console: false,
    real_model_calls: 0,
    screenshots: ["desktop", "mobile"],
    home_390: true,
  };

  async function usageCalls() {
    return page.evaluate(async () => (await (await fetch("/api/v1/models/usage")).json()).calls);
  }
  async function attachmentPage(conversationId) {
    return page.evaluate(async (id) => (await (await fetch(`/api/v1/chat/conversations/${id}/attachments`)).json()), conversationId);
  }
  async function currentConversation() {
    return page.evaluate(() => {
      try { return JSON.parse(localStorage.getItem("nexus.chat.prefs.v1") || "{}").lastConversationId || null; }
      catch { return null; }
    });
  }
  async function startNewConversation() {
    const previous = await currentConversation();
    await page.click("#new-conversation");
    await page.waitForFunction((oldId) => {
      try { return JSON.parse(localStorage.getItem("nexus.chat.prefs.v1") || "{}").lastConversationId !== oldId; }
      catch { return false; }
    }, previous, { timeout: 30000 });
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 0, null, { timeout: 30000 });
  }
  async function openChatView() {
    if (await page.locator("#chat-view").isHidden()) await page.click("#dock-expand");
    await page.waitForSelector("#chat-view:not([hidden])", { timeout: 30000 });
  }
  async function waitUploaded(host, count) {
    try {
      await page.waitForFunction(({ host, count }) => {
        const items = [...document.querySelectorAll(`${host} .attachment-item`)];
        return items.length === count && items.every((item) => item.dataset.status === "uploaded");
      }, { host, count }, { timeout: 90000 });
    } catch (error) {
      const diagnostics = await page.evaluate((target) => ({
        text: document.querySelector(target)?.innerText,
        statuses: [...document.querySelectorAll(`${target} .attachment-item`)].map((item) => item.dataset.status),
        notice: document.querySelector(`${target} .attachment-notice`)?.textContent,
      }), host);
      console.error("UPLOAD-DIAGNOSTIC:" + JSON.stringify(diagnostics));
      throw error;
    }
  }
  async function sendAndConsent({ expectModes, expectImageNotice }) {
    const before = await usageCalls();
    await page.click("#chat-send");
    await page.waitForSelector("#sheet:not([hidden])");
    assert.equal(await page.locator("#sheet-title").innerText(), "本次附件外发范围");
    assert.equal(await page.locator(".chat-turn").count(), 0);
    const sheetText = await page.locator("#sheet-body").innerText();
    if (!expectModes.every((mode) => sheetText.includes(mode)) || (expectImageNotice && !sheetText.includes(expectImageNotice))) {
      console.error("SHEET:" + JSON.stringify(sheetText));
    }
    for (const mode of expectModes) assert.ok(sheetText.includes(mode), `missing ${mode}`);
    if (expectImageNotice) assert.ok(sheetText.includes(expectImageNotice), sheetText);
    assert.equal(await usageCalls(), before, "model-preview must not call the model");
    await page.locator("#sheet .switch input").check();
    await page.getByRole("button", { name: "同意并发送" }).click();
    await page.waitForFunction(() => document.querySelector("#sheet").hidden === true, null, { timeout: 30000 });
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 1, null, { timeout: 30000 });
    await page.waitForFunction(() => document.querySelector("#chat-send").disabled === false, null, { timeout: 30000 });
    const after = await usageCalls();
    assert.equal(after, before + 1, "one consented chat call");
    return after;
  }

  try {
    await page.goto(`${input.origin}/app`);
    await page.locator("#token").fill(input.token);
    await page.locator("#login-form button").click();
    await page.waitForSelector("#shell:not([hidden])", { timeout: 30000 });
    await page.waitForFunction(() => document.querySelector("#connection").textContent.includes("Core 已连接"));

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(200);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth <= 1), "home dock overflows at 390px");
    await page.setViewportSize({ width: 1440, height: 1000 });

    // Slow the link so real XHR upload progress can be observed rather than
    // inferred from a completed request.
    const cdp = await context.newCDPSession(page);
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false, latency: 10, downloadThroughput: 8 * 1024 * 1024, uploadThroughput: 2 * 1024 * 1024,
    });
    await page.evaluate(() => {
      window.__attachmentProgress = [];
      new MutationObserver(() => {
        for (const bar of document.querySelectorAll(".attachment-progress")) {
          const value = Number(bar.getAttribute("aria-valuenow"));
          if (Number.isInteger(value)) window.__attachmentProgress.push(value);
        }
      }).observe(document.body, { subtree: true, childList: true, attributes: true });
    });

    // Real Edge file chooser opened by the dock's attachment button.
    const chooserPromise = page.waitForEvent("filechooser");
    await page.click("#dock-attach");
    const chooser = await chooserPromise;
    await chooser.setFiles([
      { name: "任意二进制.bin", mimeType: "application/octet-stream", buffer: bigBinary() },
      { name: "empty.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(0) },
      { name: "中文名称.txt", mimeType: "text/plain", buffer: textFile() },
      { name: "paper.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7 not parsed") },
    ]);
    await page.waitForSelector("#chat-view:not([hidden])", { timeout: 30000 });
    await waitUploaded("#chat-attachments", 4);
    evidence.progress_observed = await page.evaluate(() => window.__attachmentProgress.length > 0
      && window.__attachmentProgress.every((value) => value >= 0 && value <= 100));
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    const text = await page.locator("#chat-attachments").innerText();
    assert.ok(text.includes("任意二进制.bin") && text.includes("中文名称.txt") && text.includes("paper.pdf"), text);
    assert.ok(text.includes("可读取文本"), text);
    assert.ok(text.includes("已上传，尚未解析"), text);
    assert.ok(text.includes("3.0 MiB") || text.includes("3 MiB") || text.includes("2.9 MiB"), text);
    evidence.text_truncation = text.includes("发送时最多 8 KiB");
    evidence.metadata_only = text.includes("模型只会收到名称、大小、SHA-256 和未解析说明");
    const conversationA = await currentConversation();
    assert.ok(conversationA);

    const callsBefore = await usageCalls();
    await page.locator("#chat-text").fill("请只根据附件资料回答");
    const sheetPromise = page.waitForSelector("#sheet:not([hidden])");
    await page.click("#chat-send");
    await sheetPromise;
    const consentText = await page.locator("#sheet-body").innerText();
    assert.ok(consentText.includes("文本正文") && consentText.includes("仅文件信息"), consentText);
    assert.ok(consentText.includes("文本已截断"), "long text must be visibly truncated");
    assert.equal(await usageCalls(), callsBefore);
    assert.equal(await page.locator(".chat-turn").count(), 0);
    evidence.no_consent_no_send = true;
    await page.locator("#sheet .switch input").check();
    await page.getByRole("button", { name: "同意并发送" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 1, null, { timeout: 30000 });
    await page.waitForFunction(() => document.querySelector("#chat-send").disabled === false, null, { timeout: 30000 });
    assert.equal(await usageCalls(), callsBefore + 1);
    evidence.real_model_calls = 1;
    assert.equal(await page.locator(".turn-attachments .turn-attachment").count(), 4);
    const turnText = await page.locator(".chat-turn").first().innerText();
    assert.ok(turnText.includes("中文名称.txt") && turnText.includes("paper.pdf"), turnText);

    // Refresh restores the manifest only: no new upload, no new message.
    await page.reload();
    await page.waitForSelector("#shell:not([hidden])", { timeout: 30000 });
    await openChatView();
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 1, null, { timeout: 30000 });
    assert.equal(await page.locator(".turn-attachments .turn-attachment").count(), 4);
    assert.equal(await usageCalls(), callsBefore + 1);
    const pageAfterRefresh = await attachmentPage(conversationA);
    assert.equal(pageAfterRefresh.attachments.length, 4);
    evidence.refresh_no_reupload = true;

    // Re-select a historical attachment without re-uploading or deleting it.
    const firstAttachment = pageAfterRefresh.attachments[0];
    await page.locator(".turn-attachments .turn-attachment").first()
      .getByRole("button", { name: "重新选择用于下一轮" }).click();
    await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length === 1);
    assert.ok((await page.locator("#chat-attachments").innerText()).includes("沿用本会话已上传原件"));
    await page.locator("#chat-attachments .attachment-item").getByRole("button", { name: "移除" }).click();
    await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length === 0);
    assert.equal((await page.evaluate(async (url) => (await fetch(url)).status, firstAttachment.download_url)), 200);

    // Image round: verified snapshot, corrupt image and source-limit image.
    await startNewConversation();
    await page.setInputFiles("#attachment-input", [
      { name: "小图.png", mimeType: "image/png", buffer: VALID_PNG },
      { name: "损坏.png", mimeType: "image/png", buffer: Buffer.concat([PNG_SIGNATURE, Buffer.from("not a real png")]) },
      { name: "超大图.png", mimeType: "image/png", buffer: Buffer.concat([PNG_SIGNATURE, Buffer.alloc(4 * 1024 * 1024 + 1)]) },
    ]);
    await waitUploaded("#chat-attachments", 3);
    const imageText = await page.locator("#chat-attachments").innerText();
    evidence.corrupt_image_kept = imageText.includes("图片无法预览，原件已保留");
    evidence.image_source_limit = imageText.includes("超过图片源限制");
    assert.ok(imageText.includes("图片快照已生成"), imageText);
    assert.ok(evidence.corrupt_image_kept && evidence.image_source_limit, imageText);
    await page.getByRole("button", { name: "预览图片" }).first().click();
    await page.waitForSelector("#sheet:not([hidden])");
    await page.waitForSelector(".attachment-image img");
    const imageSrc = await page.locator(".attachment-image img").getAttribute("src");
    assert.ok(imageSrc.startsWith("data:image/png;base64,"), imageSrc);
    const frameBackground = await page.locator(".attachment-image .image-frame").evaluate((node) => getComputedStyle(node).backgroundColor);
    assert.equal(frameBackground, "rgb(255, 255, 255)");
    evidence.image_snapshot = true;
    await page.locator(".attachment-image .image-thumb").focus();
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector("#image-preview-dialog")?.open === true);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.querySelector("#image-preview-dialog")?.open !== true);
    await page.click("#sheet-close");
    await page.locator("#chat-text").fill("");
    const imageCalls = await sendAndConsent({ expectModes: ["图片快照", "仅文件信息"], expectImageNotice: "图片输入已启用" });
    evidence.real_model_calls = 2;
    const turn = await page.locator(".chat-turn").first().innerText();
    assert.ok(turn.includes("小图.png") && turn.includes("损坏.png") && turn.includes("超大图.png"), turn);
    assert.equal(await page.locator(".chat-turn .msg.user .prose").count(), 0);
    assert.equal(await page.locator(".turn-attachments .turn-attachment").count(), 3);
    evidence.attachment_only_message = true;
    assert.equal(imageCalls, callsBefore + 2);

    // Drag and drop plus clipboard image paste.
    await startNewConversation();
    await page.evaluate(() => {
      const data = new DataTransfer();
      data.items.add(new File([new Uint8Array([1, 2, 3, 4])], "拖入.dat", { type: "application/octet-stream" }));
      const form = document.getElementById("chat-form");
      form.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: data }));
      form.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: data }));
    });
    await waitUploaded("#chat-attachments", 1);
    evidence.drag_and_drop = true;
    await page.evaluate(() => {
      const data = new DataTransfer();
      data.items.add(new File([Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="), (c) => c.charCodeAt(0))], "粘贴图片.png", { type: "image/png" }));
      const event = new Event("paste", { bubbles: true, cancelable: true });
      Object.defineProperty(event, "clipboardData", { value: data });
      document.getElementById("chat-text").dispatchEvent(event);
    });
    await waitUploaded("#chat-attachments", 2);
    evidence.paste_image = true;
    const draftConversation = await currentConversation();
    const draftItems = await attachmentPage(draftConversation);
    const draftIds = draftItems.attachments.map((item) => item.attachment_id);
    for (const id of draftIds) {
      const path = `/api/v1/chat/conversations/${draftConversation}/attachments/${id}`;
      await page.evaluate(async (target) => {
        const session = await (await fetch("/api/v1/session")).json();
        await fetch(target, { method: "DELETE", headers: { "X-CSRF-Token": session.csrf_token, "Content-Type": "application/json" }, body: "{}" });
      }, path);
    }
    await page.locator("#chat-attachments .attachment-item").first().getByRole("button", { name: "移除" }).click();
    await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length <= 1);
    await page.reload();
    await page.waitForSelector("#shell:not([hidden])", { timeout: 30000 });
    await openChatView();
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 0);

    // Cancel an in-flight transfer and a queued one; no orphan blob remains.
    await startNewConversation();
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false, latency: 10, downloadThroughput: 4 * 1024 * 1024, uploadThroughput: 512 * 1024,
    });
    await page.setInputFiles("#attachment-input", [
      { name: "cancel-big.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(3 * 1024 * 1024, 7) },
      { name: "cancel-queued.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(2 * 1024 * 1024, 9) },
    ]);
    await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length === 2);
    await page.waitForFunction(() => [...document.querySelectorAll("#chat-attachments .attachment-item")]
      .some((item) => ["uploading", "queued"].includes(item.dataset.status)));
    const cancelButtons = page.getByRole("button", { name: "取消上传" });
    await cancelButtons.last().click();
    try {
      await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length === 1);
    } catch (error) {
      console.error("CANCEL-DIAGNOSTIC:" + JSON.stringify(await page.evaluate(() => ({
        statuses: [...document.querySelectorAll("#chat-attachments .attachment-item")].map((item) => [item.dataset.status, item.innerText]),
        notice: document.querySelector("#chat-attachments .attachment-notice")?.textContent,
      }))));
      throw error;
    }
    await page.locator("#chat-attachments .attachment-item").getByRole("button", { name: "取消上传" }).click();
    await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length === 0, null, { timeout: 30000 });
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    await page.waitForTimeout(1500);
    const cancelConversation = await currentConversation();
    const cancelPage = await attachmentPage(cancelConversation);
    assert.equal(cancelPage.attachments.length, 0, "cancelled uploads must not leave an orphan blob");
    evidence.cancel_upload = true;

    // Client-side oversized file, cross-conversation, duplicate key and in-use.
    await page.setInputFiles("#attachment-input", [
      { name: "over.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(16 * 1024 * 1024 + 1) },
    ]);
    await page.waitForFunction(() => document.querySelector("#chat-attachments .attachment-item")?.dataset.status === "failed");
    assert.ok((await page.locator("#chat-attachments").innerText()).includes("超过服务器上限"));
    evidence.oversized_413 = true;
    await page.locator("#chat-attachments .attachment-item").getByRole("button", { name: "移除" }).click();

    const attached = pageAfterRefresh.attachments.find((item) => item.attachment_id === firstAttachment.attachment_id) || firstAttachment;
    const cross = await page.evaluate(async ({ conversation, attachment }) => {
      const content = await fetch(`/api/v1/chat/conversations/${conversation}/attachments/${attachment}/content`);
      return { status: content.status };
    }, { conversation: await currentConversation(), attachment: attached.attachment_id });
    assert.equal(cross.status, 404);
    evidence.cross_conversation_404 = true;
    const inUse = await page.evaluate(async ({ conversation, attachment }) => {
      const session = await (await fetch("/api/v1/session")).json();
      const response = await fetch(`/api/v1/chat/conversations/${conversation}/attachments/${attachment}`, {
        method: "DELETE", headers: { "X-CSRF-Token": session.csrf_token, "Content-Type": "application/json" }, body: "{}",
      });
      return { status: response.status, detail: (await response.json()).detail };
    }, { conversation: conversationA, attachment: attached.attachment_id });
    assert.equal(inUse.status, 403);
    assert.equal(inUse.detail, "ATTACHMENT_IN_USE");
    evidence.attachment_in_use_403 = true;

    const duplicate = await page.evaluate(async (conversationId) => {
      const session = await (await fetch("/api/v1/session")).json();
      const base = `/api/v1/chat/conversations/${conversationId}/attachments`;
      const requestId = crypto.randomUUID();
      const headers = { "X-CSRF-Token": session.csrf_token, "Content-Type": "application/octet-stream" };
      const first = await fetch(`${base}?request_id=${requestId}&filename=dup.bin`, { method: "POST", headers, body: new Uint8Array([1, 2, 3]) });
      const second = await fetch(`${base}?request_id=${requestId}&filename=dup.bin`, { method: "POST", headers, body: new Uint8Array([4, 5, 6]) });
      const secondBody = await second.json();
      const created = await first.json();
      const remove = await fetch(`${base}/${created.attachment.attachment_id}`, {
        method: "DELETE", headers: { "X-CSRF-Token": session.csrf_token, "Content-Type": "application/json" }, body: "{}",
      });
      return { first: first.status, second: second.status, detail: secondBody.detail, removed: remove.status };
    }, await currentConversation());
    assert.equal(duplicate.first, 200);
    assert.equal(duplicate.second, 409);
    assert.equal(duplicate.detail, "IDEMPOTENCY_CONFLICT");
    evidence.duplicate_key_409 = true;

    const serverTooLarge = await page.evaluate(async (conversationId) => {
      const session = await (await fetch("/api/v1/session")).json();
      const response = await fetch(`/api/v1/chat/conversations/${conversationId}/attachments?request_id=${crypto.randomUUID()}&filename=too.bin`, {
        method: "POST", headers: { "X-CSRF-Token": session.csrf_token, "Content-Type": "application/octet-stream" }, body: new Uint8Array(16 * 1024 * 1024 + 1),
      });
      return { status: response.status, detail: (await response.json()).detail };
    }, await currentConversation());
    assert.equal(serverTooLarge.status, 413);
    evidence.oversized_413 = true;

    // 390px layout with a selected attachment.
    await page.setInputFiles("#attachment-input", [
      { name: "手机宽度.txt", mimeType: "text/plain", buffer: Buffer.from("mobile layout") },
    ]);
    await waitUploaded("#chat-attachments", 1);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    assert.ok(overflow <= 1, `horizontal overflow ${overflow}`);
    assert.equal(await page.locator("#chat-attach").isVisible(), true);
    assert.equal(await page.locator("#chat-send").isVisible(), true);
    await page.locator("#chat-attach").focus();
    assert.equal(await page.evaluate(() => document.activeElement.id), "chat-attach");
    await page.screenshot({ path: input.mobile, fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({ path: input.desktop, fullPage: true });

    // Legacy console uses the same selection and per-turn consent contract.
    await page.goto(`${input.origin}/`);
    await page.waitForSelector("#console:not([hidden])", { timeout: 30000 });
    await page.click("#chat-new");
    await page.waitForFunction(() => !document.querySelector("#chat-send").disabled, null, { timeout: 30000 });
    await page.setInputFiles("#chat-files", [
      { name: "旧控制台附件.txt", mimeType: "text/plain", buffer: Buffer.from("legacy console attachment") },
    ]);
    try {
      await page.waitForFunction(() => [...document.querySelectorAll("#chat-attachments .attachment-item")]
        .some((item) => item.dataset.status === "uploaded"), null, { timeout: 60000 });
    } catch (error) {
      console.error("LEGACY-DIAGNOSTIC:" + JSON.stringify(await page.evaluate(() => ({
        panel: document.querySelector("#chat-attachments")?.innerText,
        hidden: document.querySelector("#chat-attachments")?.hidden,
        notice: document.querySelector("#notice")?.textContent,
        supported: typeof chatAttachmentState === "undefined" ? "missing" : chatAttachmentState.supported,
        items: typeof chatAttachmentState === "undefined" ? [] : chatAttachmentState.items.map((item) => [item.status, item.error]),
      }))));
      console.error("PAGE-ERRORS:" + JSON.stringify(errors));
      throw error;
    }
    await page.getByRole("button", { name: "预览", exact: true }).first().click();
    await page.waitForSelector(".attachment-text-preview");
    assert.equal(await page.locator(".chat-turn").count(), 0);
    await page.locator("#chat-text").fill("旧控制台附件资料");
    await page.click("#chat-send");
    await page.waitForSelector("#chat-attachment-consent-check", { timeout: 30000 });
    assert.ok((await page.locator("#chat-attachment-consent").innerText()).includes("文本正文"));
    assert.equal(await page.locator(".chat-turn").count(), 0);
    await page.locator("#chat-attachment-consent-check").check();
    await page.getByRole("button", { name: "同意并发送" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 1, null, { timeout: 30000 });
    assert.equal(await page.locator(".turn-attachments .turn-attachment").count(), 1);
    await page.reload();
    await page.waitForSelector("#console:not([hidden])", { timeout: 30000 });
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 1, null, { timeout: 30000 });
    assert.equal(await page.locator(".turn-attachments .turn-attachment").count(), 1);
    evidence.legacy_console = true;
    evidence.real_model_calls = 3;

    // Expired login is reported as 401; this is the last step because it clears cookies.
    await context.clearCookies();
    const unauthorized = await page.evaluate(async () => (await fetch("/api/v1/session")).status);
    assert.equal(unauthorized, 401);
    evidence.unauthorized_401 = true;

    assert.deepEqual(errors, []);
    evidence.passed = true;
    process.stdout.write(JSON.stringify(evidence));
  } finally {
    await browser.close();
  }
}).on("error", (error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
process.on("unhandledRejection", (error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
