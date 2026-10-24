"use strict";
// Real Three.js/WebGL objects with a deterministic RAF clock. The inspection
// bridge is injected only into the served test module, never the shipped UI.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.NEXUS_PLAYWRIGHT || "../var/attachment-browser/node_modules/playwright-core");
const root = path.resolve(__dirname, "..");
const before = process.argv.includes("--before");
const release = process.env.NEXUS_VISUALIZATION_RELEASE && path.resolve(process.env.NEXUS_VISUALIZATION_RELEASE);
const evidenceDir = release || path.join(root, "var/visualization-fix");
const nodes = [
  { node_id: "desktop", online: true, tools: ["system.info@1"], roots: ["workspace"] },
  { node_id: "phone", online: true, tools: ["android.device_status@1"], roots: [] },
];

function instrument(source) {
  const legacy = source.includes("const taskFlows = []");
  const anchor = "return {\n    update(data)";
  source = source.replaceAll("\r\n", "\n");
  assert(source.includes(anchor));
  return source.replace(anchor, `return {
    __test: { scene, camera, renderer, composer, nodeGroups, taskGroups, world,
      coreGroup, target, desiredTarget, selected, particleMaterials,
      selectObject, get time() { return ${legacy ? "window.__clock.now" : "animationTime"}; },
      get messages() { return ${legacy ? "messageFlow ? [messageFlow] : []" : "[...messageFlows.values()]"}; },
      flow(id) { return ${legacy ? "taskFlows[0]" : "taskGroups.get(id)?.flow"}; } },
    update(data)`);
}

async function startServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost").pathname;
    if (url.startsWith("/api/")) { res.writeHead(401); return res.end(); }
    const windows = url.startsWith("/windows/");
    const base = release ? path.join(release, windows ? "windows-src/ui" : "web")
      : path.join(root, windows ? "apps/windows/ui" : "src/nexus/web");
    const relative = windows ? url.slice(9) : url.startsWith("/static/") ? url.slice(8) : "app/app.html";
    const file = path.resolve(base, relative);
    if (!file.startsWith(base + path.sep) || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
    let body = fs.readFileSync(file);
    if (file.endsWith(`${path.sep}main.js`)) body = "";
    if (file.endsWith(`${path.sep}visualization.js`)) {
      body = instrument(before ? fs.readFileSync(path.join(evidenceDir, "visualization-before.js"), "utf8") : body.toString());
    }
    const mime = { ".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html", ".css": "text/css" };
    res.writeHead(200, { "Content-Type": mime[path.extname(file)] || "application/octet-stream" });
    res.end(body);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return server;
}

async function setup(page, origin, surface) {
  await page.addInitScript(() => {
    const callbacks = new Map();
    let id = 0;
    window.__clock = { now: 123456, frame(ms = 0) {
      this.now += ms;
      const pending = [...callbacks.values()]; callbacks.clear();
      pending.forEach(callback => callback(this.now));
    } };
    Object.defineProperty(performance, "now", { value: () => window.__clock.now });
    window.requestAnimationFrame = callback => { callbacks.set(++id, callback); return id; };
    window.cancelAnimationFrame = key => callbacks.delete(key);
  });
  await page.goto(origin + (surface === "web" ? "/app" : "/windows/app.html"));
  await page.evaluate(async ({ surface, nodes }) => {
    document.getElementById("login").hidden = true;
    document.getElementById("shell").hidden = false;
    document.getElementById("home-view").hidden = false;
    const module = await import((surface === "web" ? "/static/app/" : "/windows/") + "visualization.js");
    window.viz = module.initVisualization(document.getElementById("visual-canvas"));
    if (!window.viz) throw new Error("WebGL unavailable");
    window.data = { nodes, tasks: new Map(), messageKey: "", activeTurn: null };
    window.send = () => viz.update(data);
    window.task = (id, status = "running", nodeId = "desktop") => ({
      task_id: id, node_id: nodeId, status, created_at: new Date().toISOString(), request: { intent: "get_system_info" },
    });
    window.advance = (ms, render = false) => {
      // Render the sampled frames with the actual renderer; intermediate ticks
      // only integrate motion, to keep virtual-time regression runs inexpensive.
      const t = viz.__test;
      const draw = t.renderer.render, compose = t.composer?.value.render;
      if (!render) { t.renderer.render = () => {}; if (t.composer) t.composer.value.render = () => {}; }
      try { for (let left = ms; left > 0; left -= 50) __clock.frame(Math.min(50, left)); }
      finally { t.renderer.render = draw; if (t.composer) t.composer.value.render = compose; }
      t.scene.updateMatrixWorld(true); t.camera.updateMatrixWorld(true);
      __clock.frame(0);
    };
    send(); __clock.frame(0);
  }, { surface, nodes });
}

async function baseline(page) {
  return page.evaluate(() => {
    data.tasks.set("short", task("short")); send(); __clock.frame(0);
    const t = viz.__test, flow = t.flow("short");
    const startsAtCore = flow.head.position.distanceTo(flow.curve.getPointAt(0)) < 0.001;
    t.selectObject(t.nodeGroups.get("desktop").hit);
    const focusCentered = t.desiredTarget.distanceTo(t.nodeGroups.get("desktop").position) < 0.001;
    data.tasks.set("short", task("short", "succeeded")); send();
    return { startsAtCore, focusCentered, completionRetainsOrb: t.taskGroups.has("short") };
  });
}

async function regression(page, surface) {
  const result = await page.evaluate(() => {
    const check = (value, label) => { if (!value) throw new Error(label); };
    const t = viz.__test;
    data.tasks.set("short", task("short"));
    data.messageKey = "message-one"; data.activeTurn = "turn-one"; send();
    const item = t.taskGroups.get("short"), flow = item.flow, message = t.messages[0];
    check(flow.head.position.distanceTo(flow.curve.getPointAt(0)) < 1e-6, "pulse must start at Core");
    advance(300);
    const point = flow.head.position.clone();
    for (const status of ["ready", "dispatching", "running", "recovering", "verifying"]) {
      data.tasks.set("short", task("short", status)); send();
      check(t.taskGroups.get("short") === item && item.flow === flow, "status refresh recreated task");
      check(point.distanceTo(flow.head.position) < 1e-6, "status refresh moved pulse");
      check(t.messages[0] === message, "refresh recreated message pulse");
    }
    data.tasks.set("short", task("short", "succeeded")); data.activeTurn = null; send();
    check(t.taskGroups.get("short") === item && item.flow === flow, "completion removed orb");
    check(flow.head.material.color.getHex() === 0x63e6d2, "completed pulse must use actual status color");
    advance(2100);
    const endpointError = flow.head.position.distanceTo(t.nodeGroups.get("desktop").position);
    check(endpointError < 1e-6, "pulse disappeared before arriving at device");
    const finishAt = flow.fadeAt;
    for (let i = 0; i < 20; i += 1) send();
    check(flow.fadeAt === finishAt, "polling prolonged completion animation");
    advance(5900);
    check(flow.head.material.opacity > 0.89 && item.marker.material.opacity > 0.95, "afterglow did not hold");
    advance(1100);
    check(flow.head.material.opacity > 0.4 && flow.head.material.opacity < 0.5, "pulse did not fade smoothly");
    check(item.marker.material.opacity > 0.4 && item.marker.material.opacity < 0.6, "orb did not fade smoothly");
    advance(1100);
    check(!t.taskGroups.has("short") && t.messages.length === 0, "completed effects leaked");
    send(); check(!t.taskGroups.has("short") && t.messages.length === 0, "poll replayed completed effects");

    // Completing on the return leg must still reach the device before fading.
    data.tasks.set("return", task("return")); send(); advance(3000);
    const returning = t.flow("return");
    data.tasks.set("return", task("return", "cancelled")); send();
    advance(4200);
    check(returning.head.position.distanceTo(t.nodeGroups.get("desktop").position) < 1e-6, "return-leg completion failed to arrive");
    check(returning.head.material.color.getHex() === 0xff5f6d, "cancelled pulse presented as success");

    // Message changes drain the old flight; hidden views pause visual time.
    data.messageKey = "message-two"; data.activeTurn = "turn-two"; send(); advance(400);
    const oldMessage = t.messages[0];
    data.messageKey = "message-three"; data.activeTurn = "turn-three"; send();
    check(t.messages.includes(oldMessage) && t.messages.length === 2, "next message deleted in-flight pulse");
    const time = t.time, oldPosition = oldMessage.head.position.clone();
    viz.visible(false); __clock.frame(60000); viz.visible(true); __clock.frame(0);
    check(t.time === time && oldMessage.head.position.equals(oldPosition), "hidden view consumed flight time");
    data.activeTurn = null; send(); advance(13000);
    check(t.messages.length === 0, "message effect never expired without polling");

    // Click the rendered node, keep it as the exact pivot through refreshes.
    viz.input.reset(); advance(2000);
    const node = t.nodeGroups.get("desktop");
    const projected = node.group.getWorldPosition(node.position.clone()).project(t.camera);
    const rect = t.renderer.domElement.getBoundingClientRect();
    viz.input.select(rect.left + (projected.x + 1) * rect.width / 2, rect.top + (1 - projected.y) * rect.height / 2);
    check(t.selected.entity?.kind === "node", "node raycast failed");
    advance(4000);
    viz.input.rotate(150, -35); advance(600);
    check(t.target.distanceTo(node.position) < 0.001, "camera pivot differs from focused node");
    const centered = node.group.getWorldPosition(node.position.clone()).project(t.camera);
    check(Math.abs(centered.x) < 0.001 && Math.abs(centered.y) < 0.001, "focused node drifts during rotation");
    const nodeIdentity = node.group;
    data.nodes[0] = { ...data.nodes[0], online: false }; data.paused = true; send();
    check(t.nodeGroups.get("desktop").group === nodeIdentity, "online refresh rebuilt node");
    check(!document.getElementById("viz-drawer").hidden, "refresh closed selection");
    check(document.getElementById("viz-drawer-body").textContent.includes("离线"), "drawer state stale");
    advance(100);
    check(node.glow.material.opacity === 0.14, "offline brightness overwritten by animation");
    viz.close(); advance(4000);
    check(t.target.length() < 0.001, "close did not restore Core pivot");
    viz.input.rotate(80, 25); advance(300);
    const coreCenter = t.coreGroup.position.clone().project(t.camera);
    check(Math.abs(coreCenter.x) < 0.001 && Math.abs(coreCenter.y) < 0.001, "Core drifts during rotation");
    viz.input.reset(); __clock.frame(0);
    const resetPosition = t.camera.position.clone();
    advance(300);
    check(t.camera.position.distanceTo(resetPosition) < 1e-6, "reset retained old drag inertia");

    // Add/remove a device while a pulse and selection exist; resource count bounded.
    data.nodes[0].online = true; data.tasks.set("layout", task("layout")); send(); advance(500);
    const layoutFlow = t.flow("layout"), startedAt = layoutFlow.startedAt;
    data.nodes.push({ node_id: "tablet", online: true, tools: [], roots: [] }); send();
    check(t.flow("layout") === layoutFlow && layoutFlow.startedAt === startedAt, "topology reset pulse");
    advance(1900);
    check(layoutFlow.head.position.distanceTo(node.position) < 1e-6, "layout changed destination without updating path");
    t.selectObject(node.hit);
    data.nodes = data.nodes.filter(n => n.node_id !== "desktop"); send(); advance(4000);
    check(!t.taskGroups.has("layout") && t.target.length() < 0.001, "removed focused device left stale center");
    data.nodes = data.nodes.filter(n => n.node_id !== "tablet"); send();
    advance(100);
    const materials = t.particleMaterials.size;
    const geometries = t.renderer.info.memory.geometries;
    for (let i = 0; i < 50; i += 1) { data.nodes[0].online = i % 2 === 0; send(); }
    advance(100);
    check(t.particleMaterials.size === materials, "polling leaked particle materials");
    check(t.renderer.info.memory.geometries === geometries, `polling leaked geometry: ${geometries} -> ${t.renderer.info.memory.geometries}`);
    data.tasks.clear();
    for (let i = 0; i < 20; i += 1) data.tasks.set(`many-${i}`, task(`many-${i}`, "running", "phone"));
    send(); check(t.taskGroups.size === 12 && t.nodeGroups.get("phone").extra === 8, "marker cap/overflow failed");
    data.tasks.clear(); send(); advance(13000); send();
    check(t.taskGroups.size === 0 && t.nodeGroups.get("phone").badge === null, "snapshot removal leaked retained markers");
    data.tasks.set("failed", task("failed", "failed", "phone")); send();
    check(t.taskGroups.has("failed") && !t.flow("failed"), "historical failure must remain static");
    data.tasks.set("failed", { ...task("failed", "failed", "phone"), created_at: "2020-01-01T00:00:00Z" }); send(); advance(8100); send();
    check(!t.taskGroups.has("failed"), "old failure never expired");
    data.tasks.clear();
    for (let i = 0; i < 12; i += 1) data.tasks.set(`history-${i}`, task(`history-${i}`, "failed", "phone"));
    send(); data.tasks.set("new-active", task("new-active", "running", "phone")); send();
    check(t.taskGroups.has("new-active") && t.taskGroups.size === 12, "historical failures blocked active task");
    data.tasks.clear(); send(); advance(13000); send();
    for (let i = 0; i < 10; i += 1) { data.messageKey = `burst-${i}`; data.activeTurn = `burst-${i}`; send(); }
    check(t.messages.length === 4, "message effects exceeded bound");
    data.activeTurn = null; data.messageKey = ""; send(); advance(13000);
    check(t.messages.length === 0, "message burst did not drain");
    return { endpointError, particleMaterials: materials, allChecks: true };
  });
  await page.evaluate(({ nodes }) => {
    data.nodes = nodes; data.tasks.clear(); data.tasks.set("preview", task("preview"));
    data.messageKey = "preview-message"; data.activeTurn = "preview-turn"; send();
    viz.input.reset(); advance(1200);
  }, { nodes });
  // Native pointer listeners, including reset while drag inertia is still active.
  const canvas = page.locator("#visual-canvas canvas");
  const bounds = await canvas.boundingBox();
  await page.mouse.move(bounds.x + bounds.width * 0.4, bounds.y + bounds.height * 0.6);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.55, bounds.y + bounds.height * 0.65, { steps: 8 });
  await page.mouse.up();
  await page.evaluate(() => { advance(300); });
  await canvas.dblclick({ position: { x: bounds.width * 0.4, y: bounds.height * 0.6 } });
  await page.evaluate(() => {
    __clock.frame(0); const position = viz.__test.camera.position.clone(); advance(100);
    if (viz.__test.camera.position.distanceTo(position) > 1e-6) throw new Error("double click retained pointer inertia");
  });
  await page.screenshot({ path: path.join(evidenceDir, `${surface}-flight.png`) });
  await page.evaluate(() => { data.tasks.set("preview", task("preview", "succeeded")); data.activeTurn = null; send(); advance(2000); });
  await page.screenshot({ path: path.join(evidenceDir, `${surface}-afterglow.png`) });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => { viz.resize(); advance(100); });
  await page.evaluate(() => {
    const canvas = viz.__test.renderer.domElement;
    const emit = (type, id, x) => canvas.dispatchEvent(new PointerEvent(type, {
      pointerId: id, pointerType: "touch", clientX: x, clientY: 400, bubbles: true,
    }));
    // Synthetic capture is intentionally disabled: the test pointers do not
    // belong to the OS, but the real pinch listeners still process the events.
    canvas.setPointerCapture = () => {};
    viz.input.reset(); advance(100);
    emit("pointerdown", 1, 130); emit("pointerdown", 2, 230);
    emit("pointermove", 2, 280); emit("pointercancel", 1, 130); emit("pointercancel", 2, 280);
    const radius = viz.__test.camera.position.distanceTo(viz.__test.target);
    advance(1000);
    const nextRadius = viz.__test.camera.position.distanceTo(viz.__test.target);
    if (nextRadius >= 7 || Math.abs(nextRadius - radius) < 0.1) throw new Error("pinch zoom did not persist");
    if (viz.__test.selected.entity) throw new Error("cancelled pinch selected object");
    viz.input.reset(); advance(100);
  });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${surface} mobile overflow`);
  await page.screenshot({ path: path.join(evidenceDir, `${surface}-mobile.png`) });
  await page.evaluate(() => {
    const t = viz.__test; viz.destroy();
    if (t.particleMaterials.size !== 0 || document.querySelector("#visual-canvas canvas")) throw new Error("destroy leaked scene");
  });
  return result;
}

async function main() {
  fs.mkdirSync(evidenceDir, { recursive: true });
  const server = await startServer();
  let browser;
  try {
    browser = await chromium.launch({ channel: "msedge", headless: true });
    const evidence = [];
    for (const surface of ["web", "windows"]) {
      const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
      const errors = []; page.on("pageerror", error => errors.push(error.message));
      await setup(page, `http://127.0.0.1:${server.address().port}`, surface);
      const result = before ? await baseline(page) : await regression(page, surface);
      if (before) assert.deepEqual(result, { startsAtCore: false, focusCentered: false, completionRetainsOrb: false });
      assert.deepEqual(errors, [], surface);
      evidence.push({ surface, ...result });
      await page.close();
    }
    const result = { passed: true, baseline: before, browser: await browser.version(), evidence, real_model_calls: 0 };
    fs.writeFileSync(path.join(evidenceDir, before ? "baseline.json" : "browser-evidence.json"), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
