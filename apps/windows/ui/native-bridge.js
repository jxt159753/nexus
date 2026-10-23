(() => {
  const tauri = window.__TAURI__;
  if (!tauri?.core?.invoke) return;
  const invoke = tauri.core.invoke;
  const originalFetch = window.fetch.bind(window);

  const native = {
    isNative: true,
    setCoreUrl: (url) => invoke("set_core_url", { url }),
    request: (method, path, body, headers) => invoke("api_request", { method, path, body, headers }),
    readDeliverable: (path) => invoke("read_deliverable", { path }),
    attachmentRequest: (id, method, path, body, headers) => invoke("attachment_request", { id, method, path, body, headers }),
    cancelAttachment: (id) => invoke("cancel_attachment_request", { id }),
    rememberOwner: (coreUrl, token) => invoke("remember_owner", { coreUrl, token }),
    loadOwner: (coreUrl) => invoke("load_owner", { coreUrl }),
    forgetOwner: () => invoke("forget_owner"),
    loadSession: () => invoke("load_session"),
    rememberSession: () => invoke("remember_session"),
    forgetSession: () => invoke("forget_session"),
    rememberNode: (config) => invoke("remember_node", { config }),
    loadNode: () => invoke("load_node"),
    forgetNode: () => invoke("forget_node"),
    nodeStatus: () => invoke("node_status"),
    nodeStart: (config) => invoke("node_start", { config }),
    nodeStop: () => invoke("node_stop"),
    voiceStatus: () => invoke("voice_status"),
    voiceDevices: () => invoke("voice_devices"),
    voiceStart: (config) => invoke("voice_start", { config }),
    voiceStop: () => invoke("voice_stop"),
    voiceInterrupt: () => invoke("voice_interrupt"),
    onVoice: (listener) => tauri.event.listen("nexus-voice", (event) => listener(event.payload)),
  };
  window.__NEXUS_NATIVE__ = native;

  function apiPath(raw) {
    try {
      const url = new URL(raw, location.href);
      if (url.origin === location.origin && url.pathname.startsWith("/api/")) {
        return url.pathname + url.search;
      }
    } catch {
      /* not a URL */
    }
    return raw.startsWith("/api/") ? raw : null;
  }

  window.fetch = async (input, init = {}) => {
    const raw = typeof input === "string" ? input : input?.url;
    const path = raw ? apiPath(raw) : null;
    if (!path) return originalFetch(input, init);
    const headers = {};
    new Headers(init.headers || {}).forEach((value, key) => { headers[key] = value; });
    const result = await native.request(init.method || "GET", path, init.body ?? null, headers);
    return new Response(result.status === 204 ? null : result.body, {
      status: result.status,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  };
})();
