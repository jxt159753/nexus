"use strict";

import { api, createConversation, state } from "./api.js";
import { button, closeSheet, el, notify, openSheet } from "./render.js";

const LABEL = { stopped: "语音已结束", activating: "正在连接语音", standby: "本地唤醒待机",
  listening: "正在听你说", thinking: "正在思考", speaking: "正在回答", interrupted: "已打断", error: "语音不可用" };
let active = null;
let starting = false;
let latest = "stopped";
let native = null;
let hooks = {};
let timer = null;

function failure(error) {
  const text = error?.message || String(error || "");
  const messages = {
    VOICE_BUSY: "另一个语音会话仍在运行，请先结束它；连接失败的待启动会话会在 30 秒内释放。",
    PAUSED: "Core 已暂停，请恢复运行后再开始语音。",
    AUTH_REQUIRED: "登录已失效，请重新登录后开始语音。",
    VOICE_DISABLED: "这台设备尚未启用语音，请检查 Core 和设备配置。",
    BUSY: "当前会话还有回复或任务，请等待结束后再开始语音。",
    MODEL_DISABLED: "聊天模型尚未启用，请先完成模型配置。",
    MODEL_CONSENT_REQUIRED: "开始语音需要同意本轮音频和聊天内容外发。",
    POLICY_DENIED: "当前设备或会话权限不允许这项语音操作。",
  };
  for (const [code, message] of Object.entries(messages)) if (text.includes(code)) return message;
  return /[\u4e00-\u9fff]/.test(text) ? text.slice(0, 300) : "语音连接失败，请检查网络和音频设备后重新开始。";
}

function renderStatus(event = {}) {
  if (event.event === "state") latest = event.state;
  const bar = document.getElementById("voice-status");
  if (!bar) return;
  bar.hidden = !active && !starting;
  bar.dataset.state = latest;
  bar.querySelector("strong").textContent = LABEL[latest] || "语音连接中";
  const detail = bar.querySelector(".voice-detail");
  if (event.event === "partial" || event.event === "final") detail.textContent = event.text || "";
  else if (event.event === "devices") detail.textContent = `${event.input || "默认麦克风"} · ${event.aec_active ? "回声消除已启用，可插话" : "半双工；请用打断按钮"}`;
  else if (event.event === "state" && event.detail) detail.textContent = event.detail;
  const expiry = bar.querySelector(".voice-expiry");
  expiry.textContent = active?.expires_at ? `麦克风已开启 · ${new Date(active.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} 到期自动关闭` : "";
  if (event.event === "state" && event.state === "standby") detail.textContent = event.detail || "说“你好奈克斯”或“Hey NEXUS”，听到提示音后说话。待机音频仅在本机处理。";
  for (const id of ["chat-voice", "dock-voice"]) {
    const control = document.getElementById(id);
    if (control) control.textContent = active ? "语音设置" : "语音";
  }
}

async function onEvent(event) {
  if (!event || typeof event.event !== "string") return;
  renderStatus(event);
  if (event.event === "error") {
    notify(event.message || "语音不可用，请检查连接和设备", true);
    await stopVoice();
  } else if (event.event === "final" || event.event === "session") {
    await hooks.refresh?.(true);
  } else if (event.event === "state" && event.state === "stopped" && active) {
    const old = active;
    active = null;
    renderStatus();
    // Always reconcile the original session; never recreate a paid request.
    api(`/api/v1/voice/sessions/${old.voice_session_id}/stop`, { method: "POST", body: "{}" }).catch(() => {});
    await hooks.refresh?.(true);
  }
}

export async function stopVoice() {
  const old = active;
  active = null;
  latest = "stopped";
  try {
    await native?.voiceStop?.();
  } finally {
    renderStatus();
    if (old && state.ready) {
      await api(`/api/v1/voice/sessions/${old.voice_session_id}/stop`, { method: "POST", body: "{}" }).catch(() => {});
      await hooks.refresh?.(true);
    }
  }
}

function checkbox(text, checked = false) {
  const label = el("label", "voice-option");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  label.append(input, document.createTextNode(text));
  return { label, input };
}

function deviceSelect(title, devices, direction) {
  const label = el("label", "voice-device", title);
  const select = document.createElement("select");
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "跟随系统默认设备（变化后需重新开始）";
  select.append(defaultOption);
  const seen = new Set();
  for (const device of devices.filter((value) => value[direction])) {
    if (seen.has(device.name)) continue;
    seen.add(device.name);
    const option = document.createElement("option");
    option.value = device.name;
    option.textContent = device.name;
    select.append(option);
  }
  label.append(select);
  return { label, select };
}

async function openVoice(wakeByDefault = false) {
  if (starting) return;
  if (!state.ready) { notify("请先登录 Core", true); return; }
  if (!native?.voiceStart) { notify("Windows 实时语音请在更新后的 NEXUS Windows App 中使用", true); return; }
  try {
    const [local, server] = await Promise.all([native.voiceStatus(), api("/api/v1/voice")]);
    if (!local.available || !server.enabled) throw new Error("语音运行时或 Core 尚未启用，请完成更新配置");
    if (!local.node_id || !server.nodes.includes(local.node_id)) throw new Error("本机 Node 尚未获准使用语音");
    if (!active) active = server.sessions.find((session) => session.node_id === local.node_id) || null;
    if (active || local.running) {
      const body = el("div");
      body.append(el("p", "", LABEL[latest] || "语音正在运行"),
        el("p", "hint", "打断只停止当前回答；结束会关闭麦克风、播放及当前语音请求。"));
      openSheet({ title: "语音会话", body, actions: [
        { label: "返回", className: "quiet", onClick: closeSheet },
        { label: "打断回答", className: "quiet", onClick: () => native.voiceInterrupt().catch((e) => notify(failure(e), true)) },
        { label: "结束语音", className: "quiet danger", onClick: async () => { closeSheet(); await stopVoice(); } },
      ] });
      return;
    }
    if (state.activeTurn) throw new Error("请先等待当前回复或执行结束，再开始语音");
    if (!state.conversation) await createConversation(state.prefs.projectId || null);
    await hooks.refresh?.(true);
    hooks.openChat?.();
    const conversationId = state.conversation.conversation_id;
    const devices = await native.voiceDevices(); // Enumeration does not open a microphone.
    const body = el("div", "voice-consent");
    body.append(el("p", "", server.audio_disclosure),
      el("p", "hint", "本次同意仅用于这个会话和设备，最长 30 分钟。记忆及附件仍需各自选择；不自动加入。"));
    const input = deviceSelect("麦克风", devices, "input");
    const output = deviceSelect("播放设备", devices, "output");
    const context = checkbox("附带当前会话近期对话", !!state.prefs.context);
    const tools = checkbox("使用与文字聊天相同的工具，并外发本轮工具目录及结果", !!state.prefs.tools);
    const wake = checkbox("本地语音唤醒：说“你好奈克斯”或“Hey NEXUS”；麦克风持续开启，命中后才上传音频", wakeByDefault);
    const proactive = checkbox(`允许 Core 在唤醒待机时播报本会话的新提醒（北京时间 ${server.dnd_start_hour}:00–${server.dnd_end_hour}:00 免打扰，最多每日 ${server.proactive_daily_limit} 次）`);
    const listen = checkbox("另行允许主动播报后收听回答；8 秒无语音即回待机");
    proactive.input.disabled = !wake.input.checked;
    listen.input.disabled = true;
    wake.input.addEventListener("change", () => {
      proactive.input.disabled = !wake.input.checked;
      if (!wake.input.checked) proactive.input.checked = listen.input.checked = false;
      listen.input.disabled = !proactive.input.checked;
    });
    proactive.input.addEventListener("change", () => {
      listen.input.disabled = !proactive.input.checked;
      if (!proactive.input.checked) listen.input.checked = false;
    });
    body.append(input.label, output.label, context.label, tools.label, wake.label, proactive.label, listen.label,
      el("p", "hint", "开启后可把 App 最小化或切到后台。听到提示音后说话；静音30秒回到本地待机。普通/完全模式沿用当前会话设置；停止、断线、切换会话或退出 App 会关麦，不自动恢复。"));
    openSheet({ title: wakeByDefault ? "开启语音唤醒" : "开始语音", body, actions: [
      { label: "取消", className: "quiet", onClick: closeSheet },
      { label: "同意外发并开始语音", className: "primary", onClick: async () => {
        if (starting) return;
        starting = true;
        latest = "activating";
        closeSheet();
        renderStatus();
        try {
          const requestId = crypto.randomUUID();
          active = await api("/api/v1/voice/sessions", { method: "POST", body: JSON.stringify({
            request_id: requestId, node_id: local.node_id, conversation_id: conversationId,
            allow_audio_model: true, allow_model: true, include_context: context.input.checked,
            allow_tools: tools.input.checked, allow_work_results: tools.input.checked,
            trigger: wake.input.checked ? "wake" : "button",
            proactive_speak: proactive.input.checked, proactive_listen: listen.input.checked,
          }) });
          active.conversation_id = conversationId;
          await native.voiceStart({ request_id: requestId, conversation_id: conversationId,
            wake: wake.input.checked, input_device: input.select.value, output_device: output.select.value });
          renderStatus();
        } catch (error) {
          await stopVoice();
          notify(failure(error), true);
        } finally { starting = false; renderStatus(); }
      } },
    ] });
  } catch (error) { notify(failure(error), true); }
}

export function initVoice(value) {
  hooks = value;
  native = window.__NEXUS_NATIVE__;
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = new URL("./voice.css", import.meta.url).href;
  document.head.append(css);
  for (const id of ["chat", "dock"]) {
    const actions = document.querySelector(`#${id}-form .composer-actions`);
    if (actions) {
      const control = button("语音", "quiet", () => openVoice(false));
      control.id = `${id}-voice`;
      control.type = "button";
      if (native) actions.prepend(control);
    }
  }
  const bar = el("section", "voice-status");
  bar.id = "voice-status";
  bar.hidden = true;
  bar.setAttribute("aria-label", "语音状态");
  const text = el("div");
  const status = el("strong");
  status.setAttribute("role", "status");
  text.append(status, el("p", "voice-detail hint"), el("p", "voice-expiry hint"));
  bar.append(text,
    button("打断", "quiet", () => native?.voiceInterrupt?.().catch((e) => notify(failure(e), true))),
    button("结束语音", "quiet danger", () => stopVoice()));
  document.querySelector(".chat-head")?.after(bar);
  native?.onVoice?.(onEvent);
  if (native?.voiceStatus) {
    const poll = async () => {
      try {
        if (active) {
          if (!state.ready || state.conversation?.conversation_id !== active.conversation_id) await stopVoice();
          else {
            const local = await native.voiceStatus();
            if (!local.running && !starting) await stopVoice();
            else if (local.event) renderStatus(local.event);
          }
        }
      } finally { timer = setTimeout(poll, 1000); }
    };
    timer = setTimeout(poll, 1000);
  }
  window.addEventListener("pagehide", () => { clearTimeout(timer); native?.voiceStop?.(); });
}
