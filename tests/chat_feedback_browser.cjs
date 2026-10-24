"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.NEXUS_PLAYWRIGHT || "../var/attachment-browser/node_modules/playwright-core");
const root = path.resolve(__dirname, "..");

const turn = {
  turn_id: "failure-fixture", conversation_id: "conversation", cursor: 1, revision: 1,
  text: "找校园图片", response_text: "我接着读取官网页面。", initial_response_text: "",
  created_at: "2026-10-23T06:44:00Z", status: "stopped", phase: "reply", work_status: "",
  work_grant_id: "grant", cost_status: "unknown", error_code: "WEB_TIMEOUT", web_call_count: 2,
  attachments: [], work_steps: [{step_id: "one", step_index: 1, status: "failed",
    proposal: {intent: "web_fetch", arguments: {url: "https://example.com/campus"}},
    result: {status: "failed", error_code: "WEB_TIMEOUT", message: "这个网页暂时没有响应，已有资料仍然保留。", cost_status: "unknown"}}],
  presentation: {version: 1, phase: "stopped", label: "已停止", process: [],
    live_text: "我接着读取官网页面。", final_text: "官网页面暂时没有响应，尚未取得图片。已找到[校园来源](https://example.com/campus)，可以先查看。",
    collapse_process: false},
};

async function main() {
  const server = http.createServer((req, res) => {
    const name = new URL(req.url, "http://localhost").pathname;
    if (name.startsWith("/api/")) {
      res.writeHead(401, {"Content-Type": "application/json"});
      return res.end(JSON.stringify({error: {code: "AUTH_REQUIRED"}}));
    }
    let base, relative;
    if (name.startsWith("/windows/")) { base = path.join(root, "apps/windows/ui"); relative = name.slice(9); }
    else if (name.startsWith("/static/")) { base = path.join(root, "src/nexus/web"); relative = name.slice(8); }
    else { base = path.join(root, "src/nexus/web"); relative = name === "/" ? "index.html" : "app/app.html"; }
    const file = path.resolve(base, relative);
    if (!file.startsWith(base + path.sep) || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
    const mime = {".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html", ".css": "text/css", ".svg": "image/svg+xml"};
    res.writeHead(200, {"Content-Type": mime[path.extname(file)] || "application/octet-stream"});
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({channel: "msedge", headless: true});
    for (const surface of ["web", "windows", "legacy"]) {
      const page = await browser.newPage({viewport: {width: 1280, height: 900}});
      const errors = [];
      page.on("pageerror", e => errors.push(e.message));
      await page.route("**/main.js*", route => route.fulfill({contentType: "text/javascript", body: ""}));
      await page.goto(origin + (surface === "web" ? "/app" : surface === "windows" ? "/windows/app.html" : "/"));
      await page.evaluate(async ({surface, turn}) => {
        if (surface === "legacy") {
          chatState.conversation = {conversation_id: "conversation", title: "失败反馈测试", revision: 1};
          chatState.turns = new Map([[turn.turn_id, turn]]);
          document.getElementById("login-panel").hidden = true;
          document.getElementById("console").hidden = false;
          document.getElementById("chat-view").hidden = false;
          chatRender();
        } else {
          const base = surface === "web" ? "/static/app/" : "/windows/";
          const {state} = await import(base + "api.js");
          const {renderChat} = await import(base + "render.js");
          Object.assign(state, {ready: true, conversation: {conversation_id: "conversation", title: "失败反馈测试"}, turnOrder: [turn]});
          document.getElementById("shell").hidden = false;
          document.getElementById("chat-view").hidden = false;
          document.getElementById("home-view").hidden = true;
          renderChat(true);
        }
      }, {surface, turn});
      const answer = page.locator(surface === "legacy" ? ".chat-assistant" : ".msg.assistant");
      const text = await answer.innerText();
      assert(text.includes("官网页面暂时没有响应，尚未取得图片"), surface);
      assert(!text.includes("WEB_TIMEOUT") && !text.includes("正在生成"), surface);
      assert(text.includes("已保留的回复片段"), surface);
      if (surface !== "legacy") assert.equal(await answer.locator('a[href="https://example.com/campus"]').count(), 1, surface);
      else assert(text.includes("https://example.com/campus"), surface);
      if (surface !== "legacy") {
        const final = await answer.locator(".final-answer").boundingBox();
        const process = await answer.locator(".process-panel").boundingBox();
        assert(final.y < process.y, surface);
        await answer.getByRole("button", {name: "收起处理过程"}).click();
        assert(await answer.locator(".final-answer").isVisible());
      }
      await page.setViewportSize({width: 390, height: 844});
      assert(await answer.getByText("官网页面暂时没有响应，尚未取得图片", {exact: false}).isVisible());
      assert.deepEqual(errors, [], surface);
      await page.close();
    }
    process.stdout.write(JSON.stringify({passed: true, surfaces: ["web", "windows-ui", "legacy"], widths: [1280, 390], real_model_calls: 0}));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
