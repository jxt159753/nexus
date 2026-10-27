// Trusted host for data-only scene commands. Generated code never runs here.
import { gameModeActive } from "./game-mode.js";
const fields = { rotate: ["dx", "dy"], zoom: ["delta"], select: ["x", "y"], reset: [] };

export function validateCommands(commands) {
  if (!Array.isArray(commands) || commands.length > 4) throw Error("插件指令数量无效");
  for (const command of commands) {
    const keys = command && fields[command.op];
    if (!keys || Object.keys(command).length !== keys.length + 1) throw Error("插件指令类型无效");
    for (const key of keys) {
      const value = command[key], normalized = key === "x" || key === "y";
      if (!Number.isFinite(value) || value < (normalized ? 0 : -200) || value > (normalized ? 1 : 200)) throw Error("插件指令超出范围");
    }
  }
  return commands;
}

export function applyCommands(bridge, canvas, commands) {
  validateCommands(commands);
  if (!bridge || !canvas) throw Error("可视化尚未就绪");
  for (const command of commands) {
    if (command.op === "rotate") bridge.rotate(command.dx, command.dy);
    if (command.op === "zoom") bridge.zoom(command.delta);
    if (command.op === "reset") bridge.reset();
    if (command.op === "select") {
      const rect = canvas.getBoundingClientRect();
      bridge.select(rect.left + rect.width * command.x, rect.top + rect.height * command.y);
    }
  }
}

function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object") return "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
  return JSON.stringify(value);
}

export async function verifyBundle(bundle, digest) {
  if (bundle?.runtime !== "visualization-worker-v1" || bundle.format !== 1 || typeof bundle.source !== "string") throw Error("插件版本格式不支持");
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(bundle)));
  if ([...new Uint8Array(hash)].map(x => x.toString(16).padStart(2, "0")).join("") !== digest) throw Error("插件内容校验失败");
}

export async function loadExtension(bundle, digest) {
  await verifyBundle(bundle, digest);
  if (gameModeActive()) throw Error("游戏模式：插件已暂停");
  const frame = document.createElement("iframe");
  frame.hidden = true;
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.src = new URL("./extension-sandbox.html", import.meta.url).href;
  const channel = new MessageChannel();
  let serial = 0, pending = null, dead = false;
  const close = () => {
    dead = true;
    window.removeEventListener("nexus-game-mode-change", onGameMode);
    if (pending) { clearTimeout(pending.timer); pending.reject(Error("插件已停止")); pending = null; }
    channel.port1.close(); frame.remove();
  };
  const onGameMode = () => { if (gameModeActive()) close(); };
  window.addEventListener("nexus-game-mode-change", onGameMode);
  const request = (kind, data) => new Promise((resolve, reject) => {
    if (dead || pending) return reject(Error("插件当前不可执行"));
    const id = ++serial;
    pending = { id, resolve, reject, timer: setTimeout(() => { close(); }, kind === "load" ? 5000 : 1000) };
    channel.port1.postMessage({ id, kind, ...data });
  });
  channel.port1.onmessage = ({ data }) => {
    if (!pending || data?.id !== pending.id) return;
    const call = pending; pending = null; clearTimeout(call.timer);
    if (data.error) { call.reject(Error(data.error)); close(); }
    else call.resolve(data.value);
  };
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error("插件隔离环境加载超时")), 5000);
      frame.onload = () => { clearTimeout(timer); frame.contentWindow.postMessage("nexus-extension-connect", "*", [channel.port2]); resolve(); };
      frame.onerror = () => { clearTimeout(timer); reject(Error("插件隔离环境加载失败")); };
      document.body.append(frame);
    });
    const result = await request("load", { source: bundle.source });
    if (result?.ready !== true) throw Error("插件未完成初始化");
    return {
      async update(input) {
        try {
          const result = await request("frame", { frame: input });
          return validateCommands(result?.commands);
        } catch (error) { close(); throw error; }
      },
      close,
    };
  } catch (error) { close(); throw error; }
}

export async function evaluateExtension(test) {
  const outputs = [];
  try {
    for (const item of test.cases) {
      const runtime = await loadExtension(test.bundle, test.bundle_digest);
      const actual = [];
      try { for (const frame of item.frames) actual.push(await runtime.update(frame)); }
      finally { runtime.close(); }
      outputs.push(actual);
    }
    return { outputs, error: "" };
  } catch (error) { return { outputs, error: String(error.message).slice(0, 300) }; }
}
