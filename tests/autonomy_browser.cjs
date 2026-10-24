"use strict";

/*
 * Real-browser checks for the autonomy UI on the Web and Windows bundled
 * surfaces. The Core API is a deterministic fixture; no model, device or
 * production database is involved.
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.NEXUS_PLAYWRIGHT || "../var/attachment-browser/node_modules/playwright-core");

const root = path.resolve(__dirname, "..");
const CONVERSATION = "11111111-1111-4111-8111-111111111111";
const GOAL = "22222222-2222-4222-8222-222222222222";
const NODE = "33333333-3333-4333-8333-333333333333";
const TASK = "44444444-4444-4444-8444-444444444444";
const CREATED_WATCH = "55555555-5555-4555-8555-555555555555";
const NOW = "2026-10-23T10:00:00Z";
const LATER = "2026-10-30T10:00:00Z";

function makeApi() {
  const goal = {
    goal_id: GOAL,
    conversation_id: CONVERSATION,
    owner_id: "owner-1",
    objective: "核对报告并保留原始输入",
    status: "active",
    max_rounds: 10,
    rounds: 3,
    created_at: NOW,
    expires_at: LATER,
    completion_verified: true,
    verification: "declared_task_assertions",
    plan_revision: 2,
    plan: {
      expected_revision: 2,
      constraints: ["只操作本任务 workspace", "保留原始输入"],
      criteria: [{
        id: "report",
        description: "报告内容与预期一致且读取未截断",
        category: "artifact",
        tool: "filesystem.read",
        arguments: [{ path: "/root_id", equals: "workspace" }],
        assertions: [{ path: "/text", equals: "检查通过" }, { path: "/truncated", equals: false }],
      }],
      steps: [
        { id: "readback", instruction: "执行文件读回并核对报告", depends_on: [], criteria: ["report"] },
        { id: "finish", instruction: "汇总最终结论", depends_on: ["readback"], criteria: ["report"] },
      ],
    },
    policy: { resume_after_restart: false, priority: 1, policy_revision: 4, replans: 0, last_reason: "environment_changed" },
    steps: [
      { step_id: "readback", status: "succeeded", turn_id: "turn-1" },
      { step_id: "finish", status: "pending", turn_id: null },
    ],
    evidence: [{
      goal_id: GOAL, criterion_id: "report", task_id: TASK, attempt_id: "66666666-6666-4666-8666-666666666666",
      digest: "a".repeat(64), created_at: NOW,
    }],
    checkpoints: [{
      expected_revision: 2, step_id: "readback", status: "succeeded", evidence: [],
      note: "读回成功", remaining: ["汇总最终结论"],
    }],
    watches: [{ watch_id: "watch-goal-1", kind: "file", target: "reports/status.txt", priority: 2, paused: false, expires_at: LATER }],
  };
  const watches = [{
    watch_id: "watch-goal-1", owner_id: "owner-1", conversation_id: CONVERSATION, kind: "file",
    node_id: NODE, target: "reports/status.txt", goal_id: GOAL, priority: 2,
    interval_seconds: 60, max_polls: 100, polls: 4, expires_at: LATER, paused: 0, revision: 1,
  }, {
    watch_id: "watch-stopped", owner_id: "owner-1", conversation_id: CONVERSATION, kind: "external",
    node_id: null, target: "构建系统", goal_id: null, priority: 0,
    interval_seconds: 60, max_polls: 10, polls: 1, expires_at: LATER, paused: 0, revision: 2,
  }];
  return {
    goal, watches,
    events: [{
      event_id: 1, watch_id: "watch-goal-1", source_key: "sha-a",
      value_json: JSON.stringify({ kind: "file", path: "reports/status.txt", sha256: "b".repeat(64) }),
      created_at: NOW, consumed: 0, acknowledged: 0, kind: "file", priority: 2,
    }, {
      event_id: 2, watch_id: "watch-stopped", source_key: "build-1",
      value_json: JSON.stringify({ kind: "external", value: "<img src=x onerror=\"window.__xss=1\">构建完成", verification: "owner_supplied_observation" }),
      created_at: NOW, consumed: 0, acknowledged: 0, kind: "external", priority: 0,
    }, {
      event_id: 3, watch_id: "watch-created", source_key: "blocked",
      value_json: JSON.stringify({ kind: "sensor_blocked", reason: "EXECUTION_UNKNOWN", message: "观察条件已失效或执行尚未核实，已暂停订阅。" }),
      created_at: NOW, consumed: 0, acknowledged: 1, kind: "file", priority: 1,
    }],
    acked: new Set(),
    archived: [
      { conversation_id: "77777777-7777-4777-8777-777777777777", title: "热归档会话", revision: 5, archived: 1, cold_archived: 0 },
      { conversation_id: "88888888-8888-4888-8888-888888888888", title: "冷归档会话", revision: 6, archived: 1, cold_archived: 1 },
    ],
    suite: {
      revision: 2,
      digest: "c".repeat(64),
      cases_json: JSON.stringify([{ tool: "add", input: { a: 1, b: 2 }, expected: { sum: 3 } }]),
      rollback_on_failure: 0,
    },
    rollouts: [{
      version_id: "v2", baseline_id: "v1", status: "observing", observations: 12, failures: 0,
      decision_revision: 3, suite_revision: 2, created_at: NOW,
    }],
    policyConflicts: 1,
    suiteConflicts: 1,
    authFail: false,
    loginFail: false,
    created: null,
    requests: [],
  };
}

const taskDetail = {
  task: {
    task_id: TASK, owner_id: "owner-1", node_id: NODE,
    request: { intent: "read_file", arguments: { root_id: "workspace", path: "reports/status.txt" } },
    status: "succeeded", execution_outcome: "known", verification: "node_report_schema_checked",
    created_at: NOW, revision: 3,
    result: { status: "succeeded", text: "检查通过", truncated: false, sha256: "d".repeat(64) },
  },
  attempts: [], approvals: [], events: [],
};

const pluginDetail = {
  version_id: "99999999-9999-4999-8999-999999999999",
  plugin_id: "user.add",
  name: "加法插件",
  version: "2.0.0",
  digest: "e".repeat(64),
  builtin: false,
  approved: false,
  enabled: false,
  test_status: "passed",
  test: {
    verification: "isolated_business_cases",
    business_verified: true,
    host_safety_verified: false,
    cases: [
      { case: 0, kind: "contract", matched: true, actual: { sum: 3 } },
      { case: 1, kind: "business", matched: false, actual: { sum: 4 } },
      { case: 2, kind: "baseline", matched: true, actual: { sum: 3 } },
    ],
    comparison: {
      baseline_id: "v1", baseline_digest: "f".repeat(64), baseline_passed: 1,
      candidate_passed: 0, case_count: 1,
    },
  },
};

function response(route, status, value) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: { "Cache-Control": "no-store" },
    body: JSON.stringify(value),
  });
}

function hasCsrf(request) {
  return Boolean(request.headers()["x-csrf-token"]);
}

async function main() {
  const server = http.createServer((req, res) => {
    const name = new URL(req.url, "http://localhost").pathname;
    let base;
    let relative;
    if (name.startsWith("/windows/")) { base = path.join(root, "apps/windows/ui"); relative = name.slice(9); }
    else if (name.startsWith("/static/")) { base = path.join(root, "src/nexus/web"); relative = name.slice(8); }
    else { base = path.join(root, "src/nexus/web"); relative = name === "/" ? "index.html" : "app/app.html"; }
    const file = path.resolve(base, relative);
    if (!file.startsWith(base + path.sep) || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
    const mime = { ".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html", ".css": "text/css", ".svg": "image/svg+xml" };
    res.writeHead(200, { "Content-Type": mime[path.extname(file)] || "application/octet-stream" });
    res.end(fs.readFileSync(file));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({ channel: "msedge", headless: true });
    const results = [];
    for (const surface of ["web", "windows"]) results.push(await runSurface(browser, origin, surface));
    process.stdout.write(JSON.stringify({ passed: true, surfaces: results, real_model_calls: 0 }));
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

async function runSurface(browser, origin, surface) {
  const api = makeApi();
  const base = surface === "web" ? "/static/app/" : "/windows/";
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    // 4xx responses are deliberate in this fixture (CSRF, session and revision
    // conflicts). They must be visible in the UI, not silenced as page errors.
    if (/Failed to load resource: the server responded with a status of 4\d\d/.test(message.text())) return;
    errors.push(`console: ${message.text()}`);
  });
  await page.route("**/main.js*", (route) => route.fulfill({ contentType: "text/javascript", body: "" }));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const pathname = url.pathname;
    const body = request.postDataJSON?.() ?? null;
    api.requests.push({ method, path: pathname + url.search, csrf: request.headers()["x-csrf-token"] || "", body });
    try {
      if (method === "POST" && pathname === "/api/v1/session") {
        if (api.loginFail) return await response(route, 401, { detail: "AUTH_REQUIRED" });
        return await response(route, 200, { csrf_token: "csrf-fixture", expires_at: LATER, owner_id: "owner-1" });
      }
      if (method === "GET" && pathname === "/api/v1/session") {
        if (api.authFail) return await response(route, 401, { detail: "AUTH_REQUIRED" });
        return await response(route, 200, { csrf_token: "csrf-fixture", expires_at: LATER, owner_id: "owner-1" });
      }
      if (pathname.startsWith("/api/v1/") && method !== "GET" && method !== "HEAD" && !hasCsrf(request)) {
        return await response(route, 403, { detail: "FORBIDDEN" });
      }
      if (method === "GET" && pathname === "/api/v1/autonomy/health") {
        if (api.authFail) return await response(route, 401, { detail: "AUTH_REQUIRED" });
        return await response(route, 200, {
          status: "degraded",
          observed_at: NOW,
          workers: { health: "running" },
          storage: {
            database_bytes: 10 * 1024 * 1024, reusable_bytes: 1024 * 1024,
            disk_free_bytes: 50 * 1024 * 1024 * 1024, hot_conversations: 3,
            hot_turns: 120, cold_bytes: 2 * 1024 * 1024, events: 3,
            schedule_events: 1, plugin_tests: 2,
          },
          unknown_tasks: 0,
          alerts: [{ alert_key: "capacity", severity: "warning", message: "运行记录接近容量上限；请归档已结束会话或扩充受控存储。", active: 1, revision: 2, updated_at: NOW }],
        });
      }
      if (method === "GET" && pathname === "/api/v1/autonomy/events") {
        const after = Number(url.searchParams.get("after") || 0);
        const rows = api.events.filter((row) => row.event_id > after).slice(0, 100)
          .map((row) => ({ ...row, acknowledged: row.acknowledged || (api.acked.has(row.event_id) ? 1 : 0) }));
        return await response(route, 200, { events: rows, next_after: rows.length ? rows.at(-1).event_id : after });
      }
      const ack = pathname.match(/^\/api\/v1\/autonomy\/events\/(\d+)\/ack$/);
      if (method === "POST" && ack) {
        api.acked.add(Number(ack[1]));
        return await response(route, 200, { acknowledged: true });
      }
      if (method === "GET" && pathname === "/api/v1/plugins") {
        return await response(route, 200, { enabled: true, plugins: [] });
      }
      if (method === "GET" && pathname === "/api/v1/permissions/default") {
        return await response(route, 200, { mode: "normal", revision: 1 });
      }
      if (method === "GET" && pathname === "/api/v1/autonomy/watches") {
        return await response(route, 200, { watches: api.watches });
      }
      if (method === "POST" && pathname === "/api/v1/autonomy/watches") {
        api.created = body;
        api.watches.unshift({
          watch_id: CREATED_WATCH, owner_id: "owner-1", conversation_id: body.conversation_id,
          kind: body.kind, node_id: body.node_id || null, target: body.target,
          goal_id: body.goal_id || null, priority: body.priority, interval_seconds: body.interval_seconds,
          max_polls: body.max_polls, polls: 0, expires_at: LATER, paused: 0, revision: 1,
        });
        return await response(route, 200, api.watches[0]);
      }
      const stop = pathname.match(/^\/api\/v1\/autonomy\/watches\/([^/]+)$/);
      if (method === "DELETE" && stop) {
        if (body.expected_revision !== 1) return await response(route, 409, { detail: "watch changed" });
        const found = api.watches.find((item) => item.watch_id === stop[1]);
        if (found) { found.paused = 1; found.revision = 2; }
        return await response(route, 200, { paused: true, revision: 2 });
      }
      const external = pathname.match(/^\/api\/v1\/autonomy\/watches\/([^/]+)\/events$/);
      if (method === "POST" && external) {
        return await response(route, 403, { detail: "WATCH_ENDED" });
      }
      if (method === "GET" && pathname === `/api/v1/plugins/conversations/${CONVERSATION}`) {
        return await response(route, 200, { goals: [api.goal], plan_mode: false, revision: 1, todos: [], schedules: [], events: [] });
      }
      if (method === "GET" && pathname === `/api/v1/autonomy/conversations/${CONVERSATION}/goals/${GOAL}`) {
        return await response(route, 200, api.goal);
      }
      if (method === "PUT" && pathname.endsWith(`/goals/${GOAL}/policy`)) {
        if (api.policyConflicts > 0) {
          api.policyConflicts -= 1;
          return await response(route, 409, { detail: "goal policy changed" });
        }
        api.goal.policy = { ...api.goal.policy, resume_after_restart: body.resume_after_restart, priority: body.priority, policy_revision: body.expected_revision + 1 };
        return await response(route, 200, api.goal);
      }
      if (method === "GET" && pathname === "/api/v1/autonomy/plugins/user.add/business-suite") {
        return await response(route, 200, { suite: api.suite, rollouts: api.rollouts });
      }
      if (method === "PUT" && pathname === "/api/v1/autonomy/plugins/user.add/business-suite") {
        if (api.suiteConflicts > 0) {
          api.suiteConflicts -= 1;
          return await response(route, 409, { detail: "business suite changed" });
        }
        api.suite = { ...api.suite, revision: body.expected_revision + 1, cases_json: JSON.stringify(body.cases), rollback_on_failure: body.rollback_on_failure ? 1 : 0 };
        return await response(route, 200, { revision: api.suite.revision, digest: api.suite.digest, host_safety_verified: false });
      }
      const preview = pathname.match(/^\/api\/v1\/autonomy\/conversations\/([^/]+)\/archive-preview$/);
      if (method === "GET" && preview) {
        return await response(route, 200, { digest: "1".repeat(64), rows: 4, original_bytes: 4096, already_archived: false });
      }
      const archive = pathname.match(/^\/api\/v1\/autonomy\/conversations\/([^/]+)\/archive$/);
      if (method === "POST" && archive) {
        if (body.digest !== "1".repeat(64)) return await response(route, 409, { detail: "archive preview changed" });
        for (const row of api.archived) if (row.conversation_id === archive[1]) row.cold_archived = 1;
        return await response(route, 200, { archived: true, cold_bytes: 3 * 1024 * 1024 });
      }
      if (method === "GET" && pathname === "/api/v1/chat/conversations") {
        if (url.searchParams.get("archived") === "true") return await response(route, 200, { conversations: api.archived });
        return await response(route, 200, { conversations: [{ conversation_id: CONVERSATION, title: "测试会话" }] });
      }
      if (method === "PATCH" && pathname.startsWith("/api/v1/chat/conversations/")) {
        return await response(route, 403, { detail: "ARCHIVE_INVALID" });
      }
      if (method === "GET" && pathname === `/api/v1/tasks/${TASK}`) {
        return await response(route, 200, taskDetail);
      }
      return await response(route, 404, { detail: "NOT_FOUND" });
    } catch (error) {
      return await response(route, 500, { detail: "FIXTURE_ERROR", message: error.message });
    }
  });
  await page.goto(`${origin}${surface === "web" ? "/app" : "/windows/app.html"}`);
  await page.evaluate(async ({ base, detail }) => {
    const api = await import(base + "api.js");
    const auto = await import(base + "autonomy-page.js");
    const render = await import(base + "render.js");
    api.state.csrf = "csrf-fixture";
    api.state.session = { owner_id: "owner-1", expires_at: "2026-10-30T10:00:00Z" };
    api.state.nodes = [{ node_id: "33333333-3333-4333-8333-333333333333", online: true, tools: [] }];
    api.state.conversations = [{ conversation_id: "11111111-1111-4111-8111-111111111111", title: "测试会话" }];
    api.state.conversation = api.state.conversations[0];
    api.state.ready = true;
    document.getElementById("shell").hidden = false;
    const host = document.createElement("div");
    host.id = "test-host";
    document.body.append(host);
    window.__mods = { api, auto, render, host };
    window.__pluginDetail = detail;
    window.__pluginModule = await import(base + "plugin-page.js");
  }, { base, detail: pluginDetail });

  const host = page.locator("#test-host");
  const closeSheetIfOpen = async () => {
    await page.evaluate(() => window.__mods.render.closeSheet());
  };
  const resetHost = async () => {
    await closeSheetIfOpen();
    await page.evaluate(() => {
      window.__mods.auto.stopAutonomyTimers();
      window.__mods.host.replaceChildren();
    });
  };

  // Login and CSRF behavior of the shared API client.
  await page.evaluate(async () => {
    const { state, login, setUnauthorizedHandler } = window.__mods.api;
    state.csrf = null;
    await login("owner-token");
    window.__afterLogin = state.csrf;
    setUnauthorizedHandler(() => { window.__unauthorized = true; });
  });
  assert.equal(await page.evaluate(() => window.__afterLogin), "csrf-fixture", `${surface}: login did not store CSRF`);
  api.loginFail = true;
  await page.evaluate(async () => {
    const { state, login } = window.__mods.api;
    state.csrf = null;
    try {
      await login("wrong-token");
      window.__loginFail = "unexpected-success";
    } catch (error) {
      window.__loginFail = `${error.status}:${error.code}`;
    }
    state.csrf = "csrf-fixture";
  });
  assert.equal(await page.evaluate(() => window.__loginFail), "401:AUTH_REQUIRED", `${surface}: failed login was not reported`);
  api.loginFail = false;
  await page.evaluate(async () => {
    const { state, api } = window.__mods.api;
    state.csrf = null;
    try {
      await api("/api/v1/autonomy/watches", { method: "POST", body: "{}" });
      window.__csrfResult = "unexpected-success";
    } catch (error) {
      window.__csrfResult = `${error.status}:${error.code}`;
    }
    state.csrf = "csrf-fixture";
  });
  assert.equal(await page.evaluate(() => window.__csrfResult), "403:FORBIDDEN", `${surface}: missing CSRF was not visible`);
  api.authFail = true;
  await page.evaluate(async () => {
    try { await window.__mods.api.api("/api/v1/autonomy/health"); } catch { /* session expiry is reported by the handler */ }
  });
  assert.equal(await page.evaluate(() => window.__unauthorized), true, `${surface}: 401 did not reach the login handler`);
  api.authFail = false;

  // Goal detail: machine assertions vs user acceptance, policy conflict.
  await page.evaluate(() => window.__mods.auto.openGoalDetail("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", {
    onOpenWatches: () => {},
  }));
  await page.waitForSelector(".autonomy-verification");
  const goalText = await page.locator(".autonomy-detail").innerText();
  for (const marker of ["机器断言通过", "用户验收：未记录", "剩余 7 轮", "固定验收条件", "依赖步骤", "检查点与未完成事项", "watch-goal-1", "environment_changed"]) {
    assert(goalText.includes(marker), `${surface}: goal detail missing ${marker}`);
  }
  const policyToggle = page.locator(".autonomy-block input[type=checkbox]").first();
  assert.equal(await policyToggle.isChecked(), false, `${surface}: resume_after_restart no longer defaults off`);
  await policyToggle.check();
  await page.getByRole("button", { name: /保存策略/ }).click();
  await page.waitForSelector(".autonomy-block .error:not([hidden])");
  assert(await policyToggle.isChecked(), `${surface}: stale policy conflict dropped the user's choice`);
  const conflictText = await page.locator(".autonomy-block .error:not([hidden])").innerText();
  assert(conflictText.includes("你的选择仍保留"), `${surface}: conflict did not preserve input: ${conflictText}`);
  await page.getByRole("button", { name: /保存策略/ }).click();
  await page.waitForFunction(() => document.querySelector(".autonomy-detail")?.textContent.includes("策略 revision 5"));
  const policyRequest = [...api.requests].reverse().find((item) => item.method === "PUT" && item.path.includes("/policy"));
  assert.equal(policyRequest.body.expected_revision, 4, `${surface}: policy request lost the independent revision`);
  assert.equal(policyRequest.csrf, "csrf-fixture", `${surface}: policy request lacked CSRF`);

  // Evidence task link opens the real task card.
  await page.getByRole("button", { name: "查看任务执行证据" }).click();
  await page.waitForSelector(".sheet-body .card");
  assert((await page.locator(".sheet-body").innerText()).includes("读取文件"), `${surface}: task evidence did not open`);
  await resetHost();

  // Watch form, watch_id association and stopped-source error.
  await page.evaluate(() => {
    const { auto } = window.__mods;
    auto.renderWatchesTab(window.__mods.host, {
      conversationId: "11111111-1111-4111-8111-111111111111",
      conversations: [{ conversation_id: "11111111-1111-4111-8111-111111111111", title: "测试会话" }],
      nodes: [{ node_id: "33333333-3333-4333-8333-333333333333", online: true }],
    });
  });
  await page.waitForSelector("#test-host .autonomy-form");
  await page.waitForFunction(() => document.getElementById("test-host")?.textContent.includes("watch-goal-1"));
  const form = page.locator("#test-host .autonomy-form");
  await form.locator("select").nth(1).selectOption("file");
  await form.locator("input").first().fill("reports/status.txt");
  await form.getByRole("button", { name: "创建观察订阅" }).click();
  await page.waitForFunction(() => document.getElementById("test-host")?.textContent.includes("55555555-5555-4555-8555-555555555555"));
  assert.equal(api.created.kind, "file", `${surface}: watch kind not sent`);
  assert.equal(api.created.node_id, NODE, `${surface}: watch node not sent`);
  assert.equal(api.created.interval_seconds, 60, `${surface}: watch interval not sent`);
  assert.equal(api.created.priority, 0, `${surface}: watch priority not sent`);
  const createdText = await host.innerText();
  assert(createdText.includes("已关联目标") || createdText.includes("未关联目标"), `${surface}: watch goal association missing`);
  const createdRow = page.locator("#test-host .plugin-row", { hasText: CREATED_WATCH });
  await createdRow.getByRole("button", { name: "停止订阅" }).click();
  await page.locator("#sheet-actions").getByRole("button", { name: "确认停止" }).click();
  await page.waitForFunction(() => document.getElementById("test-host")?.textContent.includes("已暂停"));
  assert(api.requests.some((item) => item.method === "DELETE" && item.path.includes(CREATED_WATCH)), `${surface}: stop did not reach Core`);
  const stoppedRow = page.locator("#test-host .plugin-row", { hasText: "构建系统" });
  await stoppedRow.locator("textarea").fill("保留的输入值");
  await stoppedRow.getByRole("button", { name: "提交外部事件" }).click();
  await page.waitForFunction(() => document.getElementById("test-host")?.textContent.includes("订阅已停止或已到期"));
  assert.equal(await stoppedRow.locator("textarea").inputValue(), "保留的输入值", `${surface}: failed external event dropped input`);

  // Notifications: event_id dedupe, text-only rendering, ack.
  await resetHost();
  await page.evaluate(() => window.__mods.auto.renderEventsTab(window.__mods.host));
  await page.waitForFunction(() => document.querySelectorAll("#test-host .autonomy-event").length === 3);
  assert.equal(await page.evaluate(() => window.__xss), undefined, `${surface}: event value became executable`);
  assert.equal(await page.locator("#test-host .autonomy-event img").count(), 0, `${surface}: event HTML was rendered`);
  const eventText = await host.innerText();
  assert(eventText.includes("EXECUTION_UNKNOWN"), `${surface}: sensor blocked reason missing`);
  await page.getByRole("button", { name: "立即刷新" }).click();
  await page.waitForTimeout(400);
  assert.equal(await page.locator("#test-host .autonomy-event").count(), 3, `${surface}: refresh duplicated events`);
  await page.locator("#test-host .autonomy-event").first().getByRole("button", { name: "确认这条通知" }).click();
  await page.waitForFunction(() => document.querySelector("#test-host .autonomy-event")?.textContent.includes("已确认"));
  assert(api.acked.has(1), `${surface}: ack did not reach Core`);

  // Simulated reload: a new module instance must not repeat stored events or alerts.
  await page.evaluate(async ({ base }) => {
    window.__mods.auto.stopAutonomyTimers();
    const fresh = await import(`${base}autonomy-page.js?restart=1`);
    const host = document.createElement("div");
    host.id = "restart-host";
    document.body.append(host);
    window.__restart = fresh;
    fresh.renderEventsTab(host);
  }, { base });
  await page.waitForFunction(() => document.querySelectorAll("#restart-host .autonomy-event").length === 3);
  assert.equal(await page.locator("#restart-host .autonomy-event").count(), 3, `${surface}: reload duplicated notifications`);
  assert((await page.locator("#restart-host .autonomy-event").first().innerText()).includes("已确认"), `${surface}: acknowledged event lost its state`);
  assert.equal(await page.locator("#restart-host .autonomy-alert .badge").filter({ hasText: "新" }).count(), 0, `${surface}: unchanged alert revision notified twice`);
  await page.evaluate(() => { window.__restart.stopAutonomyTimers(); document.getElementById("restart-host").remove(); });
  api.events = [];
  await page.evaluate(async ({ base }) => {
    window.__emptyEvents = await import(`${base}autonomy-page.js?empty=1`);
    const host = document.createElement("div");
    host.id = "empty-events";
    document.body.append(host);
    window.__emptyEvents.renderEventsTab(host);
  }, { base });
  await page.waitForFunction(() => document.getElementById("empty-events")?.textContent.includes("当前没有事件"));
  await page.evaluate(() => { window.__emptyEvents.stopAutonomyTimers(); document.getElementById("empty-events").remove(); });

  // Plugin page tab wiring (not just the standalone module).
  await page.evaluate(() => window.__pluginModule.openPluginPage("events"));
  await page.waitForFunction(() => document.querySelectorAll("#plugin-body .autonomy-event").length === 3);
  assert(await page.locator(".plugin-tab.active").innerText() === "通知中心", `${surface}: notification tab not active`);
  const eventRequest = api.requests.find((item) => item.method === "GET" && item.path.startsWith("/api/v1/autonomy/events"));
  assert(eventRequest, `${surface}: plugin page did not read events`);
  await page.evaluate(() => window.__pluginModule.openPluginPage("automation"));
  await page.waitForFunction(() => document.querySelector("#plugin-body")?.textContent.includes("核对报告并保留原始输入"));
  await page.getByRole("button", { name: "查看目标详情" }).click();
  await page.waitForSelector(".autonomy-verification");
  assert((await page.locator(".sheet-body").innerText()).includes("剩余 7 轮"), `${surface}: automation goal detail did not open`);
  await page.evaluate(() => { window.__mods.render.closeSheet(); window.__pluginModule.closePluginPage(); window.__mods.auto.stopAutonomyTimers(); document.getElementById("test-host").replaceChildren(); });

  // Storage: health facts, digest-bound archive, corrupted restore.
  await resetHost();
  await page.evaluate(() => window.__mods.auto.renderStorageTab(window.__mods.host));
  await page.waitForFunction(() => document.getElementById("test-host")?.textContent.includes("热数据（近似）"));
  const storageText = await host.innerText();
  for (const marker of ["冷数据", "磁盘剩余", "capacity", "revision 2", "运行记录接近容量上限"]) {
    assert(storageText.includes(marker), `${surface}: storage page missing ${marker}`);
  }
  const hotRow = page.locator("#test-host .plugin-row", { hasText: "热归档会话" });
  await hotRow.getByRole("button", { name: "预览压缩归档" }).click();
  await hotRow.getByRole("button", { name: /确认压缩归档/ }).click();
  await page.waitForFunction(() => document.getElementById("test-host")?.textContent.includes("已压缩归档"));
  const archiveRequest = [...api.requests].reverse().find((item) => item.method === "POST" && item.path.includes("/archive"));
  assert.equal(archiveRequest.body.digest, "1".repeat(64), `${surface}: archive confirmation was not digest-bound`);
  const coldRow = page.locator("#test-host .plugin-row", { hasText: "冷归档会话" });
  await coldRow.getByRole("button", { name: /取消归档/ }).click();
  await page.locator("#sheet-actions").getByRole("button", { name: "确认恢复" }).click();
  await page.waitForFunction(() => document.getElementById("notice")?.textContent.includes("归档数据校验失败"));
  assert((await page.locator("#test-host").innerText()).includes("历史正文仍由原会话 API 读取"), `${surface}: storage scope note missing`);

  // Business suite editor: explicit save, conflict keeps draft, safety stays false.
  await resetHost();
  await page.evaluate(() => window.__mods.auto.openBusinessSuite({ plugin_id: "user.add", name: "加法插件" }, window.__pluginDetail));
  await page.waitForFunction(() => document.querySelector(".sheet-body")?.textContent.includes("宿主安全未验证"));
  const suiteText = await page.locator(".sheet-body").innerText();
  for (const marker of ["原样例", "候选业务用例实际结果", "基线比较", "真实观察与回退", "观察中", "候选通过 0/1"]) {
    assert(suiteText.includes(marker), `${surface}: business suite missing ${marker}`);
  }
  const editor = page.locator(".sheet-body textarea").first();
  const draft = JSON.stringify([
    { tool: "add", input: { a: 1, b: 2 }, expected: { sum: 3 } },
    { tool: "add", input: { a: 2, b: 3 }, expected: { sum: 5 } },
  ], null, 2);
  await editor.fill(draft);
  await page.getByRole("button", { name: /明确保存业务用例/ }).click();
  await page.waitForFunction(() => document.querySelector(".sheet-body .error")?.textContent.includes("你的草稿仍保留"));
  assert.equal(await editor.inputValue(), draft, `${surface}: suite conflict dropped the draft`);
  await page.getByRole("button", { name: /明确保存业务用例/ }).click();
  await page.waitForFunction(() => document.querySelector(".sheet-body")?.textContent.includes("已保存"));
  const suiteRequest = [...api.requests].reverse().find((item) => item.method === "PUT" && item.path.includes("/business-suite"));
  assert.equal(suiteRequest.body.expected_revision, 2, `${surface}: suite save lost revision`);
  assert.equal(suiteRequest.body.cases.length, 2, `${surface}: suite save lost cases`);
  assert((await page.locator(".sheet-body").innerText()).includes("宿主安全仍未验证"), `${surface}: safety claim changed`);

  // Narrow layout must remain usable and free of horizontal page overflow.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(150);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert(overflow <= 1, `${surface}: narrow layout overflows by ${overflow}px`);
  assert(await page.getByRole("button", { name: /明确保存业务用例/ }).isVisible(), `${surface}: save button not usable at 390px`);
  await page.evaluate(() => {
    window.__mods.render.closeSheet();
    window.__mods.auto.openGoalDetail("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", { onOpenWatches: () => {} });
  });
  await page.waitForSelector(".autonomy-verification");
  const goalOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert(goalOverflow <= 1, `${surface}: goal sheet overflows by ${goalOverflow}px at 390px`);
  await page.evaluate(() => { window.__mods.render.closeSheet(); window.__pluginModule.openPluginPage("watches"); });
  await page.waitForFunction(() => document.querySelector("#plugin-body .autonomy-form"));
  const pluginOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert(pluginOverflow <= 1, `${surface}: plugin page overflows by ${pluginOverflow}px at 390px`);
  assert(await page.getByRole("button", { name: "创建观察订阅" }).isVisible(), `${surface}: watch form not usable at 390px`);
  await page.evaluate(() => window.__pluginModule.closePluginPage());
  await page.evaluate(() => {
    document.getElementById("settings").hidden = false;
    window.__mods.render.renderSettings();
  });
  await page.waitForTimeout(120);
  const settingsOverflow = await page.evaluate(() => {
    const panel = document.getElementById("settings");
    return panel.scrollWidth - panel.clientWidth;
  });
  assert(settingsOverflow <= 1, `${surface}: settings overflows by ${settingsOverflow}px at 390px`);
  assert(await page.getByRole("button", { name: "存储与健康" }).isVisible(), `${surface}: storage entry missing in settings`);
  await page.evaluate(() => { document.getElementById("settings").hidden = true; });

  await page.close();
  assert.deepEqual(errors, [], `${surface}: page errors`);
  return { surface, flows: 9, widths: [1280, 390] };
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
