"use strict";
const { chromium } = require(process.env.NEXUS_PLAYWRIGHT || "playwright-core");
const assert = require("node:assert/strict");
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", value => { raw += value; });
process.stdin.on("end", async () => {
  const input = JSON.parse(raw);
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(({ nodeId }) => {
      window.__wakeCalls = [];
      window.__running = false;
      window.__resident = { enabled: false, session: null };
      window.__NEXUS_NATIVE__ = {
        setCoreUrl: async () => {}, loadSession: async () => true, loadOwner: async () => null,
        rememberOwner: async () => {}, rememberSession: async () => {},
        forgetOwner: async () => {}, forgetSession: async () => {},
        nodeStatus: async () => ({ running: true }), loadNode: async () => null,
        voiceStatus: async () => ({ available: true, running: window.__running, node_id: nodeId,
          resident: window.__resident, event: { event: "state", state: window.__running ? "standby" : "stopped" } }),
        voiceWake: async config => {
          window.__wakeCalls.push(config);
          window.__running = !!config;
          window.__resident = { enabled: !!config, session: config ? { resident: true,
            voice_session_id: crypto.randomUUID(), conversation_id: config.conversation_id } : null };
        },
        voiceStop: async () => { window.__running = false; window.__resident = { enabled: false, session: null }; },
        onVoice: callback => { window.__onVoice = callback; },
      };
    }, { nodeId: input.node_id });
    await page.goto(input.origin + "/app");
    await page.locator("#core-url").fill(input.origin);
    await page.locator("#token").fill(input.owner_token);
    await page.locator('#login-form button[type="submit"]').click();
    await page.waitForFunction(() => window.__wakeCalls.length === 1);
    const first = await page.evaluate(() => window.__wakeCalls[0]);
    assert.ok(first.owner_id && first.conversation_id);
    assert.equal(first.allow_tools, true);
    await page.locator("#settings-button").click();
    await page.getByRole("switch", { name: "常驻语音唤醒" }).waitFor();
    assert.equal(await page.locator("#voice-wake-enabled").isChecked(), true);
    await page.locator("#voice-wake-enabled").uncheck();
    await page.waitForFunction(() => !window.__running);
    await page.reload();
    await page.locator("#shell").waitFor({ state: "visible" });
    await page.locator("#settings-button").click();
    assert.equal(await page.locator("#voice-wake-enabled").isChecked(), false);
    assert.equal(await page.evaluate(() => window.__wakeCalls.length), 0);
    await page.getByText("隐私与高级设置", { exact: true }).click();
    await page.getByRole("button", { name: "清除本地显示偏好", exact: true }).click();
    assert.equal(await page.locator("#voice-wake-enabled").isChecked(), false);
    await page.locator("#voice-wake-enabled").check();
    await page.waitForFunction(() => window.__running);
    await page.locator("#settings-close").click();
    await page.locator("#dock-new").click();
    await page.waitForFunction(old => window.__wakeCalls.at(-1)?.conversation_id !== old, first.conversation_id);
    await page.evaluate(() => window.__onVoice({ event: "state", state: "stopped", media_stopped: true }));
    assert.equal(await page.evaluate(() => window.__resident.enabled), true, "renderer must not disarm native renewal");
    await page.locator("#voice-wake-indicator").click();
    await page.waitForFunction(() => !window.__running);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#settings-button").click();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    const calls = await page.evaluate(async () => (await (await fetch("/api/v1/models/usage")).json()).calls);
    assert.equal(calls, 0);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: input.screenshot, fullPage: true });
    console.log(JSON.stringify({ passed: true, native_audio: "simulated", real_model_calls: 0,
      default_enabled: true, off_survives_restart_and_clear_display_prefs: true,
      follows_conversation: true, native_renewal_not_cancelled_by_renderer: true, mobile_390: true }));
  } finally { await browser.close(); }
});
