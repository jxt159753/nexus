import { api, state } from "./api.js";
import { el, button, openSheet, closeSheet, errorText, notify, runAction } from "./render.js";
import { loadExtension, evaluateExtension, applyCommands } from "./extension-runtime.js";
import { gestureInput } from "./gesture-input.js";

export function initUIExtensions(getVisualization) {
  const key = "nexus.ui-extension.client.v1";
  const clientId = localStorage.getItem(key) || crypto.randomUUID();
  localStorage.setItem(key, clientId);
  const cameraKey = "nexus.gesture.camera.v1";
  let cameraId = localStorage.getItem(cameraKey) || "";
  const client = { client_id: clientId, runtime: "visualization-worker-v1", platform: window.__NEXUS_NATIVE__ ? "windows" : "web" };
  let conversation = null, policy = null, active = null, runtime = null, pendingReceipt = null;
  let polling = false, epoch = 0, status = "尚未授权", cameraStarting = false, panel = null, lastPoll = 0;
  const endpoint = (id = conversation) => `/api/v1/ui-extensions/conversations/${id}`;
  const send = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
  const report = (id, version, outcome, error = "") => send(endpoint(id) + "/runtime-report", {
    client_id: clientId, version_id: version.version_id, bundle_digest: version.bundle_digest,
    deployment_revision: version.deployment_revision, status: outcome, error: error.slice(0, 300),
  });
  const camera = gestureInput(async frame => {
    const current = active, instance = runtime, id = conversation;
    if (!instance || !state.ready || state.conversation?.conversation_id !== id || state.control?.paused || state.view !== "home" || document.hidden || Date.now() - lastPoll > 6000) { stopCamera(); return; }
    try {
      const commands = await instance.update(frame);
      if (runtime === instance && active === current && conversation === id) applyCommands(getVisualization()?.input, document.getElementById("visual-canvas"), commands);
    } catch (error) {
      if (runtime !== instance) return;
      stopCamera(); instance.close(); runtime = null;
      pendingReceipt = { kind: "runtime", conversation: id, version: current, error: error.message };
      status = "插件运行失败，正在回退"; draw();
    }
  }, error => { status = cameraError(error); draw(); });

  function cameraError(error) {
    return ({ NotAllowedError: "摄像头权限未开启", NotFoundError: "没有找到摄像头", NotReadableError: "摄像头被占用或无法读取", OverconstrainedError: "所选摄像头不可用，请重新选择" })[error.name] || error.message;
  }
  const cameraStatus = () => `手势识别中 · ${camera.label || "摄像头"} · 画面仅在本机处理`;
  async function refreshCameras() {
    const select = panel?.querySelector("[data-camera-device]");
    if (!select || !navigator.mediaDevices?.enumerateDevices) return;
    try {
      const devices = (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === "videoinput");
      if (!select.isConnected) return;
      select.replaceChildren(new Option("系统默认摄像头", ""));
      devices.forEach((device, index) => select.add(new Option(device.label || `摄像头 ${index + 1}（开启权限后显示名称）`, device.deviceId)));
      if (cameraId && !devices.some(device => device.deviceId === cameraId)) select.add(new Option("上次选择的摄像头不可用，请重新选择", cameraId));
      select.value = cameraId;
    } catch (error) { notify(cameraError(error), true); }
  }
  function stopCamera() { camera.stop(); cameraStarting = false; draw(); }
  function clear() {
    epoch++; stopCamera(); runtime?.close(); runtime = null; active = null; policy = null;
    pendingReceipt = null; conversation = null; status = "尚未授权";
  }
  function draw() {
    const entry = document.getElementById("gestures-button");
    if (entry) { entry.textContent = camera.running ? "停止手势" : "手势"; entry.title = status; }
    if (!panel?.isConnected) return;
    panel.querySelector("[data-status]").textContent = status;
    panel.querySelector("[data-version]").textContent = active ? `当前版本 ${active.version}` : "尚无可用插件";
    const toggle = panel.querySelector("[data-policy]");
    toggle.textContent = policy?.enabled && policy.client_id === clientId ? "停用本会话插件" : "允许 NEXUS 为本会话开发和升级";
    toggle.disabled = !conversation;
    const start = panel.querySelector("[data-camera]");
    start.textContent = camera.running || cameraStarting ? "停止摄像头" : "开启摄像头手势";
    start.disabled = !runtime || active?.status !== "accepted" || state.view !== "home";
    panel.querySelector("[data-camera-device]").disabled = cameraStarting;
  }
  async function poll() {
    if (polling) return;
    const id = state.ready && state.conversation?.conversation_id;
    if (id !== conversation) { clear(); conversation = id || null; }
    if (!id || document.hidden) { stopCamera(); return; }
    if (state.view !== "home" || state.control?.paused) stopCamera();
    polling = true; const token = epoch;
    try {
      if (pendingReceipt?.conversation === id) {
        const receipt = pendingReceipt;
        if (receipt.kind === "test") await send(endpoint(id) + `/versions/${receipt.test.version_id}/test-report`, receipt.body);
        else await report(id, receipt.version, receipt.kind === "ready" ? "ready" : "failed", receipt.error);
        if (pendingReceipt === receipt) pendingReceipt = null;
      }
      const response = await send(endpoint(id) + "/poll", client);
      if (token !== epoch || state.conversation?.conversation_id !== id) return;
      if (policy && response.policy?.revision !== policy.revision) stopCamera();
      lastPoll = Date.now(); policy = response.policy;
      if (!response.active) {
        runtime?.close(); runtime = null; active = null; stopCamera();
        status = policy?.enabled ? (policy.client_id === clientId ? "等待 NEXUS 开发插件" : "本会话已在另一客户端授权") : "尚未授权";
      } else if (!runtime || !active || active.version_id !== response.active.version_id || active.deployment_revision !== response.active.deployment_revision) {
        runtime?.close(); runtime = null; active = response.active;
        const candidate = active;
        try {
          const loaded = await loadExtension(candidate.bundle, candidate.bundle_digest);
          if (token !== epoch) { loaded.close(); return; }
          const commands = await loaded.update({ time_ms: 0, hands: [] });
          if (commands.length || !getVisualization()?.input) { loaded.close(); throw Error("插件空闲检查或可视化接口未就绪"); }
          runtime = loaded;
          pendingReceipt = { kind: "ready", conversation: id, version: candidate, error: "" };
          status = camera.running ? cameraStatus() : "插件已加载，等待确认";
        } catch (error) {
          if (token !== epoch) return;
          runtime?.close(); runtime = null; stopCamera();
          pendingReceipt = { kind: "runtime", conversation: id, version: candidate, error: error.message };
          status = "插件加载失败，正在回退";
        }
      } else {
        active = response.active;
        status = camera.running ? cameraStatus() : "插件就绪；摄像头默认关闭";
      }
      if (!camera.running && response.latest?.status === "rolled_back") status = active ? `升级失败，已回退到 ${active.version}` : "插件运行失败，已停用";
      if (!camera.running && response.latest?.status === "failed") status = active ? `新版本验证失败，保留 ${active.version}` : "新版本验证失败，等待 NEXUS 修正";
      if (response.test && token === epoch) {
        status = "正在验证新插件"; draw();
        const evidence = await evaluateExtension(response.test);
        if (token !== epoch) return;
        pendingReceipt = { kind: "test", conversation: id, test: response.test,
          body: { lease_id: response.test.lease_id, client_id: clientId, bundle_digest: response.test.bundle_digest, ...evidence } };
        await send(endpoint(id) + `/versions/${response.test.version_id}/test-report`, pendingReceipt.body);
        pendingReceipt = null;
        status = evidence.error ? `插件测试失败：${evidence.error}` : "测试输出已提交，等待 Core 核验";
      }
    } catch (error) {
      if (token === epoch) {
        stopCamera(); runtime?.close(); runtime = null;
        // Keep the identical receipt for reconciliation; never rerun an unknown test.
        if (error.status >= 400 && error.status < 500) pendingReceipt = null;
        active = null; status = errorText(error);
      }
    } finally { polling = false; draw(); }
  }
  function open() {
    panel = el("div");
    const label = el("p", "hint"); label.dataset.status = "";
    const version = el("p"); version.dataset.version = "";
    const toggle = button("", "quiet", () => runAction(async () => {
      await poll();
      if (!conversation) throw Error("请先创建或选择会话");
      const enabled = !(policy?.enabled && policy.client_id === clientId);
      await api(endpoint() + "/policy", { method: "PUT", body: JSON.stringify({ expected_revision: policy?.revision || 0,
        enabled, client_id: clientId, plugin_id: "user.gesture_control", profile: "gesture_navigation_v1" }) });
      runtime?.close(); runtime = null; active = null; stopCamera();
      await poll();
      notify(enabled ? "已授权；可以让 NEXUS 开发或升级手势插件" : "已停用插件");
    })); toggle.dataset.policy = "";
    const cameraLabel = el("label", "field", "摄像头");
    const cameraSelect = el("select"); cameraSelect.dataset.cameraDevice = "";
    cameraSelect.setAttribute("aria-label", "摄像头");
    cameraSelect.add(new Option("系统默认摄像头", ""));
    cameraSelect.onchange = () => {
      stopCamera(); cameraId = cameraSelect.value;
      if (cameraId) localStorage.setItem(cameraKey, cameraId); else localStorage.removeItem(cameraKey);
      status = "已选择摄像头，点击开启后生效"; draw();
    };
    cameraLabel.append(cameraSelect);
    const start = button("", "primary", () => runAction(async () => {
      if (camera.running || cameraStarting) { stopCamera(); return; }
      if (!runtime || active?.status !== "accepted" || state.view !== "home") throw Error("请回到动态视图并等待插件就绪");
      cameraStarting = true; status = "正在启动本地识别"; draw();
      try { await camera.start(cameraId); if (camera.running) { status = cameraStatus(); closeSheet(); } }
      catch (error) { status = cameraError(error); notify(status, true); }
      finally { cameraStarting = false; draw(); }
    })); start.dataset.camera = "";
    panel.append(el("p", "hint", "允许 NEXUS 为当前会话生成、测试和更新手势插件，失败自动回退。插件只能旋转、缩放、选择和复位此界面。"), label, version, toggle,
      el("p", "hint", "先选择摄像头，再点击开启，画面不上传。单手捏合移动旋转，双手捏合拉开缩放，轻捏松开选择，双手张开复位。切换会话、离开动态视图或失去连接时停止。"), cameraLabel, start);
    openSheet({ title: "手势控制", body: panel, actions: [{ label: "关闭", className: "quiet", onClick: closeSheet }] });
    draw(); refreshCameras(); poll();
  }
  const entry = document.createElement("button"); entry.id = "gestures-button"; entry.className = "quiet"; entry.type = "button"; entry.textContent = "手势";
  entry.onclick = () => { if (camera.running || cameraStarting) { stopCamera(); status = "摄像头已停止"; draw(); } else open(); };
  document.getElementById("settings-button").before(entry);
  const interval = setInterval(poll, 2000);
  navigator.mediaDevices?.addEventListener("devicechange", refreshCameras);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopCamera(); else poll(); });
  window.addEventListener("pagehide", () => { clearInterval(interval); navigator.mediaDevices?.removeEventListener("devicechange", refreshCameras); clear(); });
  return { sync() {
    if (!state.ready || state.conversation?.conversation_id !== conversation) clear();
    if (state.view !== "home" || document.hidden || state.control?.paused) stopCamera();
    poll();
  }, stop: clear };
}
