"use strict";
const { chromium } = require(process.env.NEXUS_PLAYWRIGHT || "playwright-core");
const assert = require("node:assert/strict");
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (data) => { raw += data; });
process.stdin.on("end", async () => {
  const input = JSON.parse(raw);
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(({ nodeId }) => {
      window.__voiceCalls = [];
      window.__NEXUS_NATIVE__ = {
        voiceStatus: async () => ({ available: true, running: !!window.__voiceRunning, node_id: nodeId }),
        voiceDevices: async () => [{ name: "模拟麦克风", input: true }, { name: "模拟耳机", output: true }],
        voiceStart: async (config) => {
          window.__voiceCalls.push(config);
          window.__voiceRunning = true;
          window.__voiceEvent({ event: "state", state: "listening" });
        },
        voiceStop: async () => { window.__voiceRunning = false; },
        voiceInterrupt: async () => window.__voiceEvent({ event: "state", state: "interrupted" }),
        onVoice: (listener) => { window.__voiceEvent = listener; },
      };
    }, { nodeId: input.node_id });
    await page.goto(input.origin + "/app");
    await page.locator("#token").fill(input.owner_token);
    await page.locator("#login-form button[type=submit]").click();
    await page.locator("#dock-voice").waitFor({ state: "visible" });
    await page.locator("#dock-voice").click();
    await page.getByRole("heading", { name: "开始语音", exact: true }).waitFor();
    assert.match(await page.locator("#sheet-body").innerText(), /阿里云百炼/);
    const proactive = page.getByLabel(/允许 Core 在唤醒待机/);
    const listen = page.getByLabel(/另行允许主动播报后收听/);
    assert.equal(await proactive.isChecked(), false);
    assert.equal(await proactive.isDisabled(), true);
    assert.equal(await listen.isDisabled(), true);
    await page.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(await page.evaluate(() => window.__voiceCalls.length), 0);
    await page.locator("#chat-voice").click();
    await page.getByRole("button", { name: "同意外发并开始语音", exact: true }).click();
    await page.getByText("正在听你说", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.__voiceCalls.length), 1);
    assert.equal(await page.evaluate(() => window.__voiceCalls[0].wake), false);
    await page.locator("#voice-status").getByRole("button", { name: "打断", exact: true }).click();
    await page.getByText("已打断", { exact: true }).waitFor();
    await page.locator("#voice-status").getByRole("button", { name: "结束语音", exact: true }).click();
    await page.locator("#voice-status").waitFor({ state: "hidden" });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#chat-voice").click();
    await page.getByRole("heading", { name: "开始语音", exact: true }).waitFor();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    const usage = await page.evaluate(async () => (await (await fetch("/api/v1/models/usage")).json()).calls);
    assert.equal(usage, 0);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: input.screenshot, fullPage: true });
    console.log(JSON.stringify({ passed: true, native_audio: "simulated", real_model_calls: 0,
      consent_cancel_does_not_start: true, independent_proactive_consent: true, interrupt_and_stop: true,
      mobile_390: true, browser: await browser.version() }));
  } finally { await browser.close(); }
});
