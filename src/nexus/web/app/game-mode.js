// Local desktop resource settings. This never changes Core task permissions.
const native = window.__NEXUS_NATIVE__;
let current = { mode: "auto", active: !!native?.gameModeStatus, reason: "loading", error: "" };
let busy = false;
export const gameModeActive = () => current.active;

function summary() {
  if (current.error) return current.error;
  if (current.reason === "loading") return "正在读取游戏模式";
  if (current.active) return current.reason === "cooldown" ? "游戏模式开启 · 等待退出全屏" : "游戏模式开启 · 高耗能功能暂停";
  return current.mode === "off" ? "游戏模式已手动关闭" : "游戏模式自动检测中";
}

function draw() {
  const indicator = document.getElementById("game-mode-button");
  if (indicator) {
    indicator.textContent = current.active ? "游戏模式 · 已开启" : "游戏模式";
    indicator.title = summary(); indicator.disabled = busy;
    indicator.setAttribute("aria-pressed", String(current.active));
    document.querySelector("#tools-menu > summary")?.setAttribute("title", current.active ? "工具 · 游戏模式已开启" : "工具");
  }
  for (const control of document.querySelectorAll("[data-game-mode-select]")) {
    control.value = current.mode; control.disabled = busy;
  }
  for (const label of document.querySelectorAll("[data-game-mode-status]")) label.textContent = summary();
}

function apply(value) {
  if (!value || typeof value.active !== "boolean" || !["auto", "on", "off"].includes(value.mode)) return;
  const changed = current.active !== value.active;
  current = value;
  document.documentElement.classList.toggle("game-mode", current.active);
  if (current.active) {
    const notice = document.getElementById("notice"); if (notice) notice.hidden = true;
  }
  draw();
  if (changed) window.dispatchEvent(new CustomEvent("nexus-game-mode-change", { detail: current }));
}

async function select(mode) {
  if (busy) return;
  busy = true; draw();
  try { apply(await native.gameModeSet(mode)); }
  catch (error) { current = { ...current, error: error?.message || String(error) }; }
  finally { busy = false; draw(); }
}

export function renderGameModeSettings(body) {
  if (!native?.gameModeStatus) return;
  const section = document.createElement("section"); section.className = "settings-section";
  const heading = document.createElement("h2"); heading.textContent = "游戏模式";
  const label = document.createElement("label"); label.textContent = "运行方式 ";
  const control = document.createElement("select"); control.dataset.gameModeSelect = "";
  control.id = "game-mode-select"; label.htmlFor = control.id;
  for (const [value, text] of [["auto", "自动检测"], ["on", "手动开启"], ["off", "手动关闭"]]) control.add(new Option(text, value));
  control.addEventListener("change", () => select(control.value)); label.append(control);
  const status = document.createElement("p"); status.className = "hint"; status.dataset.gameModeStatus = "";
  const description = document.createElement("p"); description.className = "hint";
  description.textContent = "暂停摄像头识别、语音与常驻唤醒、虚拟手机和三维动画，提醒保留在收件箱，不主动弹窗。手动选择优先并记住；自动检测全屏游戏/应用，退出全屏15秒后恢复。窗口化游戏可手动开启。";
  const restore = document.createElement("p"); restore.className = "hint";
  restore.textContent = "退出后按原设置恢复语音待机；摄像头和虚拟手机需重新开启，应用及登录数据保留。可在会话中说“打开游戏模式”“关闭游戏模式”或“恢复自动检测游戏模式”。";
  const help = document.createElement("details"); help.className = "settings-help"; help.id = "game-mode-help";
  const summary = document.createElement("summary"); summary.textContent = "暂停与恢复说明";
  help.append(summary, description, restore);
  section.append(heading, label, status, help); body.append(section); draw();
}

export async function initGameMode() {
  if (!native?.gameModeStatus) return;
  const button = document.createElement("button"); button.type = "button"; button.className = "quiet";
  button.id = "game-mode-button";
  button.addEventListener("click", () => select(current.active ? "off" : "on"));
  document.getElementById("quick-tools")?.append(button);
  draw();
  const refresh = async () => {
    try { apply(await native.gameModeStatus()); }
    catch (error) { current.error = error?.message || String(error); draw(); }
  };
  await native.onGameMode?.(apply);
  await refresh();
  const timer = setInterval(refresh, 2000);
  window.addEventListener("pagehide", () => clearInterval(timer), { once: true });
}
