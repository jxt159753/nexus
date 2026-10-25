// Camera ownership stays in the trusted host and requires an explicit click.
export function gestureInput(onFrame, onError) {
  let worker = null, stream = null, video = null, timer = null, pending = null, epoch = 0, running = false;
  function stop() {
    epoch++; running = false; clearTimeout(timer);
    worker?.terminate(); worker = null;
    stream?.getTracks().forEach(track => track.stop()); stream = null;
    if (video) { video.pause(); video.srcObject = null; video.remove(); video = null; }
    if (pending) { clearTimeout(pending.timer); pending.reject(Error("摄像头已停止")); pending = null; }
  }
  function call(data, transfers = []) {
    return new Promise((resolve, reject) => {
      pending = { resolve, reject, timer: setTimeout(() => {
        pending = null; reject(Error("本地手势识别超时"));
      }, data.kind === "init" ? 30000 : 5000) };
      worker.postMessage(data, transfers);
    });
  }
  async function start(deviceId = "") {
    stop(); const token = epoch;
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw Error("此环境不支持摄像头，请使用受信任的 HTTPS 或 Windows 客户端");
      const acquired = await navigator.mediaDevices.getUserMedia({ video: { ...(deviceId ? { deviceId: { exact: deviceId } } : {}), width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 15, max: 20 } }, audio: false });
      if (token !== epoch) { acquired.getTracks().forEach(track => track.stop()); return; }
      stream = acquired;
      video = document.createElement("video"); video.muted = true; video.playsInline = true; video.srcObject = stream;
      await video.play();
      if (token !== epoch) return;
      worker = new Worker(new URL("./gesture-worker.js", import.meta.url));
      worker.onmessage = ({ data }) => {
        if (!pending) return;
        const call = pending; pending = null; clearTimeout(call.timer);
        if (data.error) call.reject(Error(data.error)); else call.resolve(data);
      };
      worker.onerror = () => {
        if (pending) { const call = pending; pending = null; clearTimeout(call.timer); call.reject(Error("本地识别组件加载失败")); }
      };
      await call({ kind: "init" });
      if (token !== epoch) return;
      running = true;
      const tick = async () => {
        if (token !== epoch) return;
        try {
          const bitmap = await createImageBitmap(video);
          if (token !== epoch) { bitmap.close(); return; }
          const result = await call({ kind: "frame", bitmap, time_ms: performance.now() }, [bitmap]);
          if (token !== epoch) return;
          await onFrame(result.frame);
          if (token === epoch) timer = setTimeout(tick, 67);
        } catch (error) { if (token === epoch) { stop(); onError(error); } }
      };
      tick();
    } catch (error) {
      if (token === epoch) { stop(); throw error; }
    }
  }
  return { start, stop, get running() { return running; }, get label() { return stream?.getVideoTracks()[0]?.label || ""; } };
}
