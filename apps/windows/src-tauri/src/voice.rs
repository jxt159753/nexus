use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, State};
use url::Url;
use uuid::Uuid;

use crate::{auth, bridge::BridgeState};

pub struct VoiceManager {
    inner: Arc<Mutex<VoiceInner>>,
}

struct VoiceInner {
    child: Option<Child>,
    input: Option<ChildStdin>,
    event: Value,
    failed: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VoiceLaunch {
    pub(crate) request_id: Uuid,
    pub(crate) conversation_id: Uuid,
    #[serde(default)]
    pub(crate) wake: bool,
    #[serde(default)]
    pub(crate) input_device: String,
    #[serde(default)]
    pub(crate) output_device: String,
}

#[derive(Serialize)]
pub struct VoiceStatus {
    available: bool,
    running: bool,
    node_id: Option<String>,
    event: Value,
    resident: crate::wake::WakeStatus,
}

fn executable(app: &AppHandle) -> Result<PathBuf, String> {
    let path = app.path().resource_dir().map_err(|_| "无法定位语音资源")?
        .join("binaries").join("nexus-voice.exe");
    if path.is_file() { return Ok(path); }
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("binaries/nexus-voice.exe");
    if path.is_file() { return Ok(path); }
    Err("安装包缺少语音运行时，请更新安装包".into())
}

fn hidden_command(path: &std::path::Path) -> Command {
    let mut command = Command::new(path);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command.env_clear();
    for name in ["SystemRoot", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE", "APPDATA", "LOCALAPPDATA"] {
        if let Some(value) = std::env::var_os(name) { command.env(name, value); }
    }
    command.env("PYTHONIOENCODING", "utf-8");
    command
}

impl VoiceManager {
    pub fn new() -> Self {
        Self { inner: Arc::new(Mutex::new(VoiceInner {
            child: None, input: None, event: json!({"event":"state","state":"stopped"}),
            failed: false,
        })) }
    }

    pub fn stop(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            if let Some(input) = inner.input.as_mut() {
                let _ = input.write_all(b"{\"command\":\"stop\"}\n");
                let _ = input.flush();
            }
            inner.input.take();
            if let Some(mut child) = inner.child.take() {
                let deadline = Instant::now() + Duration::from_millis(1000);
                while Instant::now() < deadline {
                    if matches!(child.try_wait(), Ok(Some(_))) { break; }
                    std::thread::sleep(Duration::from_millis(20));
                }
                if !matches!(child.try_wait(), Ok(Some(_))) {
                    crate::node::terminate_tree(&mut child);
                }
            }
            inner.event = json!({"event":"state","state":"stopped","media_stopped":true});
        }
    }

    pub(crate) fn running(&self) -> bool {
        let mut inner = self.inner.lock().expect("voice state");
        let outcome = inner.child.as_mut().map(|child| child.try_wait());
        let running = matches!(outcome, Some(Ok(None)));
        if matches!(outcome, Some(Ok(Some(status))) if !status.success()) { inner.failed = true; }
        if !running { inner.child.take(); inner.input.take(); }
        running
    }

    pub(crate) fn take_failure(&self) -> bool {
        std::mem::take(&mut self.inner.lock().expect("voice state").failed)
    }
}

#[tauri::command]
pub fn voice_status(app: AppHandle, state: State<'_, VoiceManager>) -> Result<VoiceStatus, String> {
    let resident = app.state::<crate::wake::WakeManager>().status();
    let running = state.running();
    let inner = state.inner.lock().map_err(|_| "语音状态不可用")?;
    let saved = auth::load_node()?;
    Ok(VoiceStatus { available: executable(&app).is_ok(), running,
        node_id: saved.map(|value| value.node_id), event: inner.event.clone(), resident })
}

#[tauri::command]
pub fn voice_devices(app: AppHandle) -> Result<Value, String> {
    let mut child = hidden_command(&executable(&app)?).arg("--devices")
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null())
        .spawn().map_err(|_| "无法查询音频设备")?;
    let deadline = Instant::now() + Duration::from_secs(8);
    loop {
        if matches!(child.try_wait(), Ok(Some(_))) { break; }
        if Instant::now() >= deadline {
            crate::node::terminate_tree(&mut child);
            return Err("音频设备查询超时".into());
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    let mut output = Vec::new();
    child.stdout.take().ok_or("设备查询无结果")?.take(65537).read_to_end(&mut output)
        .map_err(|_| "设备查询失败")?;
    if output.len() > 65536 { return Err("音频设备列表超过限制".into()); }
    serde_json::from_slice(&output).map_err(|_| "无法读取音频设备列表".into())
}

#[tauri::command]
pub fn voice_start(app: AppHandle, state: State<'_, VoiceManager>, bridge: State<'_, BridgeState>,
                   config: VoiceLaunch) -> Result<(), String> {
    app.state::<crate::wake::WakeManager>().disable();
    state.stop();
    launch(&app, &state, &bridge, config)
}

pub(crate) fn launch(app: &AppHandle, state: &VoiceManager, bridge: &BridgeState,
                    config: VoiceLaunch) -> Result<(), String> {
    let core = bridge.authenticated_origin()?;
    let saved = auth::load_node()?.ok_or("请先配置本机 Node")?;
    let mut url = Url::parse(&saved.core_ws_url).map_err(|_| "本机 Node 地址无效")?;
    if url.path() != "/ws/v1/node" || url.query().is_some() || url.fragment().is_some()
        || !url.username().is_empty() || url.password().is_some() {
        return Err("本机 Node 地址无效".into());
    }
    let scheme = match url.scheme() { "wss" => "https", "ws" => "http", _ => return Err("语音需要可信连接".into()) };
    let mut check = url.clone();
    check.set_scheme(scheme).map_err(|_| "Core 地址无效")?;
    if check.origin().ascii_serialization() != core { return Err("语音 Node 必须连接当前登录的 Core".into()); }
    Uuid::parse_str(&saved.node_id).map_err(|_| "Node 标识无效")?;
    if config.input_device.len() > 200 || config.output_device.len() > 200 {
        return Err("音频设备名称过长".into());
    }
    url.set_path("");
    let exe = executable(&app)?;
    let resources = exe.parent().ok_or("语音资源无效")?.join("voice-models");
    let mut startup = json!({"core_url":url.as_str().trim_end_matches('/'),"node_id":saved.node_id,
        "request_id":config.request_id,"conversation_id":config.conversation_id,
        "origin":"https://tauri.localhost","start_on_launch":true,"stay_alive":false,
        "aec_enabled":true,"wake_enabled":config.wake,
        "input_device":if config.input_device.is_empty() { Value::Null } else { json!(config.input_device) },
        "output_device":if config.output_device.is_empty() { Value::Null } else { json!(config.output_device) }});
    if config.wake {
        let models = resources.join("kws");
        if !models.is_dir() { return Err("安装包缺少本地唤醒模型".into()); }
        startup["wake_model_dir"] = json!(models.to_string_lossy());
        startup["wake_keywords"] = json!(resources.join("keywords.txt").to_string_lossy());
    }
    let mut inner = state.inner.lock().map_err(|_| "语音状态不可用")?;
    if inner.child.as_mut().map(|child| matches!(child.try_wait(), Ok(None))).unwrap_or(false) {
        return Err("语音已在运行，请先结束当前会话".into());
    }
    let mut child = hidden_command(&exe).arg("--ipc").env("NEXUS_NODE_TOKEN", saved.token)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
        .spawn().map_err(|_| "无法启动语音运行时")?;
    let mut input = child.stdin.take().ok_or("无法连接语音进程")?;
    if writeln!(input, "{}", startup).and_then(|_| input.flush()).is_err() {
        crate::node::terminate_tree(&mut child);
        return Err("语音启动失败".into());
    }
    let output = child.stdout.take().ok_or("无法读取语音状态")?;
    let pid = child.id();
    inner.input = Some(input);
    inner.child = Some(child);
    inner.failed = false;
    inner.event = json!({"event":"state","state":"activating"});
    drop(inner);
    let shared = state.inner.clone();
    let app = app.clone();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(output);
        loop {
            let mut line = Vec::new();
            let count = reader.by_ref().take(16385).read_until(b'\n', &mut line).unwrap_or(0);
            if count == 0 || count > 16384 { break; }
            let Ok(value) = serde_json::from_slice::<Value>(&line) else { continue };
            if !matches!(value.get("event").and_then(Value::as_str),
                         Some("state" | "devices" | "session" | "partial" | "final" | "speak" | "metrics" | "error")) { continue; }
            if let Ok(mut inner) = shared.lock() {
                if inner.child.as_ref().map(Child::id) != Some(pid) { break; }
                if value["event"] == "error" { inner.failed = true; }
                inner.event = value.clone();
            }
            let _ = app.emit("nexus-voice", value);
        }
    });
    Ok(())
}

#[tauri::command]
pub fn voice_interrupt(state: State<'_, VoiceManager>) -> Result<(), String> {
    let mut inner = state.inner.lock().map_err(|_| "语音状态不可用")?;
    let input = inner.input.as_mut().ok_or("语音尚未开始")?;
    input.write_all(b"{\"command\":\"interrupt\"}\n").and_then(|_| input.flush())
        .map_err(|_| "语音进程已停止".into())
}

#[tauri::command]
pub fn voice_stop(app: AppHandle, state: State<'_, VoiceManager>) {
    app.state::<crate::wake::WakeManager>().disable();
    state.stop();
}
