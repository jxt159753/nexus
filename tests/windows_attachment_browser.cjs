"use strict";
const { chromium } = require(process.env.NEXUS_PLAYWRIGHT || "playwright-core");
const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const input = JSON.parse(fs.readFileSync(process.env.NEXUS_WINDOWS_INPUT, "utf8"));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (buffer) => crypto.createHash("sha256").update(buffer).digest("hex");

function startCore() {
  const log = fs.openSync(input.coreLog, "a");
  return spawn(input.python, ["-B", input.serverScript, input.config], {
    cwd: input.repo,
    env: { ...process.env, DEEPSEEK_API_KEY: "synthetic-windows-attachment-only" },
    stdio: ["ignore", log, log],
  });
}

async function stopProcess(child) {
  if (!child) return;
  try { child.kill(); } catch { /* ignore */ }
  await wait(800);
  try { execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" }); } catch { /* ignore */ }
}

async function waitHttp(url) {
  for (let index = 0; index < 120; index += 1) {
    try { if ((await fetch(url)).status === 200) return; } catch { /* not ready */ }
    await wait(250);
  }
  throw new Error("Core did not start");
}

async function findAppPage(browser) {
  for (let index = 0; index < 120; index += 1) {
    const pages = browser.contexts().flatMap((context) => context.pages());
    const page = pages.find((item) => item.url().includes("app.html"));
    if (page) return page;
    await wait(500);
  }
  throw new Error("installed app page did not open");
}

async function launchApp(port, appDir, webviewDir) {
  fs.mkdirSync(appDir, { recursive: true });
  fs.mkdirSync(webviewDir, { recursive: true });
  const child = spawn(input.app, [], {
    env: {
      ...process.env,
      APPDATA: appDir,
      WEBVIEW2_USER_DATA_FOLDER: webviewDir,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port}`,
    },
    stdio: "ignore",
  });
  for (let index = 0; index < 120; index += 1) {
    try {
      const value = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (value.ok) {
        const version = await value.json();
        const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
        const page = await findAppPage(browser);
        await page.waitForLoadState("domcontentloaded");
        return { child, browser, page, version: version.Browser };
      }
    } catch { /* not ready */ }
    await wait(500);
  }
  await stopProcess(child);
  throw new Error("WebView2 CDP endpoint did not open");
}

async function login(page, origin, token, remember = true) {
  await page.waitForSelector("#login:not([hidden])", { timeout: 30000 });
  if (await page.locator("#core-field").isVisible()) {
    await page.locator("#core-url").fill(origin);
  }
  await page.locator("#token").fill(token);
  if (remember) await page.locator("#remember").check();
  await page.locator("#login-form button").click();
  await page.waitForSelector("#shell:not([hidden])", { timeout: 45000 });
  await page.waitForFunction(() => document.querySelector("#connection").textContent.includes("Core 已连接"), null, { timeout: 45000 });
}

async function attachmentPage(page, conversationId) {
  return page.evaluate(async (id) => (await (await fetch(`/api/v1/chat/conversations/${id}/attachments`)).json()), conversationId);
}

async function currentConversation(page) {
  return page.evaluate(() => {
    try { return JSON.parse(localStorage.getItem("nexus.chat.prefs.v1") || "{}").lastConversationId || null; }
    catch { return null; }
  });
}

async function waitUploaded(page, count) {
  await page.waitForFunction((value) => {
    const items = [...document.querySelectorAll("#chat-attachments .attachment-item")];
    return items.length === value && items.every((item) => item.dataset.status === "uploaded");
  }, count, { timeout: 180000 });
}

(async () => {
  const evidence = { passed: false, installer_sha256: input.installerSha256, core_version: "local source, attachment migration 022; test schema 026" };
  let core = null;
  let app = null;
  let cdp = null;
  try {
    core = startCore();
    await waitHttp(input.origin + "/");
    app = await launchApp(input.cdpPort, input.appData, input.webviewData);
    evidence.browser = app.version;
    const page = app.page;
    cdp = await page.context().newCDPSession(page);
    await login(page, input.origin, input.token);

    // More than the old 2 MiB string bridge limit.
    const bigBytes = crypto.randomBytes(5 * 1024 * 1024 + 321);
    const bigPath = path.join(input.workDir, "win-big.bin");
    fs.writeFileSync(bigPath, bigBytes);
    const bigHash = sha256(bigBytes);
    await page.setInputFiles("#attachment-input", [bigPath]);
    await page.waitForSelector("#chat-view:not([hidden])", { timeout: 30000 });
    await waitUploaded(page, 1);
    const conversation = await currentConversation(page);
    const pageInfo = await attachmentPage(page, conversation);
    assert(pageInfo.attachments.length === 1);
    const attachment = pageInfo.attachments[0];
    evidence.upload_over_2mib = attachment.size_bytes === bigBytes.length;
    evidence.ui_has_no_fake_progress = await page.evaluate(() => !document.querySelector(".attachment-progress"));

    // Same bytes through the dedicated native Base64 download path.
    const bridgeHash = await page.evaluate(async ({ attachmentId, conversationId }) => {
      const native = window.__NEXUS_NATIVE__;
      const id = crypto.randomUUID();
      const response = await native.attachmentRequest(
        id, "GET",
        `/api/v1/chat/conversations/${conversationId}/attachments/${attachmentId}/content?encoding=base64`,
        null, null,
      );
      const value = JSON.parse(response.body);
      const raw = atob(value.data_base64);
      const bytes = new Uint8Array(raw.length);
      for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    }, { attachmentId: attachment.attachment_id, conversationId: conversation });
    evidence.bridge_download_sha256 = bridgeHash === bigHash;

    // UI download through the same bridge and a browser download target.
    const downloads = path.join(input.workDir, "downloads");
    fs.mkdirSync(downloads, { recursive: true });
    await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads, eventsEnabled: true });
    await page.getByRole("button", { name: "下载原件" }).first().click();
    let downloaded = null;
    for (let index = 0; index < 120; index += 1) {
      const files = fs.readdirSync(downloads).filter((name) => name.endsWith(".bin"));
      if (files.length) {
        const candidate = path.join(downloads, files[0]);
        const size = fs.statSync(candidate).size;
        if (size === bigBytes.length) { downloaded = candidate; break; }
      }
      await wait(250);
    }
    if (downloaded) {
      evidence.ui_download_sha256 = sha256(fs.readFileSync(downloaded)) === bigHash;
    } else {
      evidence.ui_download_sha256 = false;
    }

    // Send with the explicit per-turn consent, then restore after an app restart.
    await page.locator("#chat-text").fill("Windows 安装版大文件附件");
    await page.click("#chat-send");
    await page.waitForSelector("#sheet:not([hidden])", { timeout: 30000 });
    await page.locator("#sheet .switch input").check();
    await page.getByRole("button", { name: "同意并发送" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".chat-turn").length === 1, null, { timeout: 60000 });
    await page.waitForFunction(() => document.querySelector("#chat-send").disabled === false, null, { timeout: 60000 });
    evidence.sent_with_consent = true;
    await page.screenshot({ path: input.screenshot, fullPage: true });

    // Cancel a large transfer if the bridge is still busy; record the real case.
    const cancelPath = path.join(input.workDir, "win-cancel.bin");
    fs.writeFileSync(cancelPath, crypto.randomBytes(16 * 1024 * 1024));
    await page.evaluate(() => {
      window.__cancelClicked = false;
      window.__cancelPoll = setInterval(() => {
        const item = document.querySelector("#chat-attachments .attachment-item");
        const button = [...document.querySelectorAll("#chat-attachments button")]
          .find((node) => node.textContent.trim() === "取消上传");
        if (button) {
          button.click();
          window.__cancelClicked = true;
          clearInterval(window.__cancelPoll);
        } else if (item?.dataset.status === "uploaded") {
          clearInterval(window.__cancelPoll);
        }
      }, 10);
    });
    await page.setInputFiles("#attachment-input", [cancelPath]);
    await page.waitForFunction(() => window.__cancelClicked
      || document.querySelector("#chat-attachments .attachment-item")?.dataset.status === "uploaded", null, { timeout: 60000 });
    await page.evaluate(() => clearInterval(window.__cancelPoll));
    if (await page.evaluate(() => window.__cancelClicked)) {
      evidence.cancel_observed = true;
      await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length === 0, null, { timeout: 60000 });
    } else {
      evidence.cancel_observed = false;
      await page.getByRole("button", { name: "移除" }).first().click();
      await page.waitForFunction(() => document.querySelectorAll("#chat-attachments .attachment-item").length === 0, null, { timeout: 60000 });
    }
    await wait(1500);
    const afterCancel = await attachmentPage(page, conversation);
    evidence.cancel_no_orphan = !afterCancel.attachments.some((item) => item.filename === "win-cancel.bin");

    // Disconnect, failed upload keeps its request id, restart the same Core.
    await stopProcess(core);
    core = null;
    await page.waitForFunction(() => document.querySelector("#connection").textContent.includes("连接中断"), null, { timeout: 30000 });
    const retryPath = path.join(input.workDir, "win-retry.bin");
    fs.writeFileSync(retryPath, Buffer.from("retry after disconnect"));
    await page.setInputFiles("#attachment-input", [retryPath]);
    await page.waitForFunction(() => document.querySelector("#chat-attachments .attachment-item")?.dataset.status === "failed", null, { timeout: 60000 });
    evidence.disconnect_failure_shown = true;
    core = startCore();
    await waitHttp(input.origin + "/");
    await page.waitForFunction(() => document.querySelector("#connection").textContent.includes("Core 已连接"), null, { timeout: 60000 });
    await page.getByRole("button", { name: "用原编号重试" }).first().click();
    await waitUploaded(page, 1);
    const afterRetry = await attachmentPage(page, conversation);
    evidence.retry_once = afterRetry.attachments.filter((item) => item.filename === "win-retry.bin").length === 1;

    // Restart the installed app: same WebView profile restores session and history.
    await app.browser.close().catch(() => {});
    await stopProcess(app.child);
    app = await launchApp(input.cdpPort, input.appData, input.webviewData);
    const restarted = app.page;
    if (await restarted.locator("#login").isVisible()) {
      await login(restarted, input.origin, input.token);
    } else {
      await restarted.waitForSelector("#shell:not([hidden])", { timeout: 45000 });
    }
    await restarted.click("#dock-expand");
    await restarted.waitForSelector("#chat-view:not([hidden])", { timeout: 30000 });
    await restarted.waitForFunction(() => document.querySelectorAll(".turn-attachments .turn-attachment").length === 1, null, { timeout: 30000 });
    evidence.restart_keeps_history = true;

    // Expire the persisted session; the app must return to the login view.
    execFileSync(input.python, ["-B", "-c", [
      "import sqlite3,sys",
      "db=sqlite3.connect(sys.argv[1])",
      "db.execute(\"UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE revoked_at IS NULL\")",
      "db.commit()",
    ].join(";"), input.db], { stdio: "ignore" });
    await restarted.waitForSelector("#login:not([hidden])", { timeout: 30000 });
    evidence.expired_login_returns_to_login = true;
    // Do not leave the temporary test Core credential in the user's vault.
    await restarted.evaluate(async () => {
      try { await window.__NEXUS_NATIVE__?.forgetOwner?.(); } catch { /* ignore */ }
      try { await window.__NEXUS_NATIVE__?.forgetSession?.(); } catch { /* ignore */ }
    });
    evidence.credentials_cleared = true;

    evidence.passed = true;
    process.stdout.write(JSON.stringify(evidence));
  } finally {
    try { await app?.browser?.close(); } catch { /* ignore */ }
    await stopProcess(app?.child);
    await stopProcess(core);
  }
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});

function assert(value) {
  if (!value) throw new Error("assertion failed");
}
