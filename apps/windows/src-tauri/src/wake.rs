//! Resident wake consent is scoped to the signed-in owner, Core and conversation.
//! The native timer also runs while the WebView is hidden in the tray.
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{Emitter, Manager};
use uuid::Uuid;

use crate::{bridge, voice};

#[derive(Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct WakeConfig {
    pub owner_id: Uuid,
    pub conversation_id: Uuid,
    pub include_context: bool,
    pub allow_tools: bool,
}

#[derive(Clone, PartialEq)]
struct Scope {
    core: String,
    config: WakeConfig,
}

#[derive(Default)]
struct Inner {
    scope: Option<Scope>,
    revision: u64,
    session: Option<Value>,
    message: String,
}

#[derive(Default)]
pub struct WakeManager(Mutex<Inner>);

#[derive(Serialize)]
pub struct WakeStatus {
    enabled: bool,
    session: Option<Value>,
    message: String,
}

impl WakeManager {
    pub fn disable(&self) {
        if let Ok(mut inner) = self.0.lock() {
            inner.scope = None;
            inner.revision += 1;
            inner.session = None;
            inner.message.clear();
        }
    }

    pub fn status(&self) -> WakeStatus {
        let inner = self.0.lock().expect("wake state");
        WakeStatus { enabled: inner.scope.is_some(), session: inner.session.clone(), message: inner.message.clone() }
    }
}

#[tauri::command]
pub fn voice_wake(app: tauri::AppHandle, config: Option<WakeConfig>) -> Result<(), String> {
    let scope = config.map(|config| {
        Ok::<_, String>(Scope { core: app.state::<bridge::BridgeState>().authenticated_origin()?, config })
    }).transpose()?;
    let manager = app.state::<WakeManager>();
    let mut inner = manager.0.lock().map_err(|_| "唤醒设置不可用")?;
    if inner.scope != scope {
        inner.scope = scope;
        inner.revision += 1;
        inner.session = None;
        inner.message.clear();
        app.state::<voice::VoiceManager>().stop();
    }
    Ok(())
}

struct Prepared {
    connection: bridge::VoiceConnection,
    id: Uuid,
}

fn error_message(error: &str) -> &'static str {
    if error.contains("AUTH_REQUIRED") || error.contains("登录") {
        "登录已失效，请重新登录以恢复语音唤醒。"
    } else if error.contains("PAUSED") {
        "Core 已暂停；恢复运行后自动回到唤醒待机。"
    } else if error.contains("BUSY") {
        "正在等待当前会话或语音结束，随后恢复唤醒待机。"
    } else {
        "唤醒暂不可用，正在等待连接和设备恢复；可在设置中关闭。"
    }
}

pub fn start(app: &tauri::AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut prepared: Option<Prepared> = None;
        let mut revision = 0;
        let mut request_id = Uuid::new_v4();
        let mut retry_at = Instant::now();
        loop {
            tokio::time::sleep(Duration::from_millis(500)).await;
            let manager = app.state::<WakeManager>();
            let (scope, current) = {
                let inner = manager.0.lock().expect("wake state");
                (inner.scope.clone(), inner.revision)
            };
            if current != revision {
                revision = current;
                request_id = Uuid::new_v4();
                retry_at = Instant::now();
            }
            if app.state::<voice::VoiceManager>().running() { continue; }
            if app.state::<voice::VoiceManager>().take_failure() {
                retry_at = Instant::now() + Duration::from_secs(15);
                let mut inner = manager.0.lock().expect("wake state");
                if inner.scope.is_some() { inner.message = "语音设备或连接中断，正在等待恢复。".into(); }
            }
            if Instant::now() < retry_at { continue; }
            // Resolve the previous session before preparing another. This never
            // replays speech, chat requests, tool calls, or a paid provider call.
            if let Some(old) = prepared.as_ref() {
                let result = old.connection.request("POST", &format!("/api/v1/voice/sessions/{}/stop", old.id), Some(json!({}))).await;
                match result {
                    Ok(_) => {},
                    Err(ref error) if error == "AUTH_REQUIRED" || error == "HTTP_404" => {},
                    Err(_) => { retry_at = Instant::now() + Duration::from_secs(5); continue; }
                }
                prepared = None;
                request_id = Uuid::new_v4();
            }
            let Some(scope) = scope else { continue; };
            let result = async {
                let connection = bridge::VoiceConnection::new(&app.state::<bridge::BridgeState>(), &scope.core).await?;
                if connection.owner_id != scope.config.owner_id { return Err("AUTH_REQUIRED".to_string()); }
                let saved = crate::auth::load_node()?.ok_or("请先配置本机 Node")?;
                let node_id = Uuid::parse_str(&saved.node_id).map_err(|_| "Node 标识无效")?;
                let mut session = connection.request("POST", "/api/v1/voice/sessions", Some(json!({
                    "request_id": request_id, "node_id": node_id,
                    "conversation_id": scope.config.conversation_id,
                    "allow_audio_model": true, "allow_model": true,
                    "include_context": scope.config.include_context,
                    "allow_tools": scope.config.allow_tools, "allow_work_results": scope.config.allow_tools,
                    "trigger": "wake", "proactive_speak": false, "proactive_listen": false
                }))).await?;
                let id = session["voice_session_id"].as_str().and_then(|s| Uuid::parse_str(s).ok()).ok_or("语音会话无效")?;
                prepared = Some(Prepared { connection, id });
                // Hold the settings lock through launch: turning off must win
                // even when the HTTP preparation finishes after the click.
                let mut inner = manager.0.lock().map_err(|_| "唤醒状态不可用")?;
                if inner.revision != current || inner.scope.as_ref() != Some(&scope) { return Ok(()); }
                voice::launch(&app, &app.state::<voice::VoiceManager>(), &app.state::<bridge::BridgeState>(), voice::VoiceLaunch {
                    request_id, conversation_id: scope.config.conversation_id,
                    wake: true, input_device: String::new(), output_device: String::new(),
                })?;
                session["conversation_id"] = json!(scope.config.conversation_id);
                session["resident"] = json!(true);
                inner.session = Some(session);
                inner.message.clear();
                Ok::<_, String>(())
            }.await;
            if let Err(error) = result {
                // A rejected idempotency key refers to an ended session. No
                // media is open; a new key may prepare standby on the next try.
                if error == "IDEMPOTENCY_CONFLICT" { request_id = Uuid::new_v4(); }
                let mut inner = manager.0.lock().expect("wake state");
                if inner.revision == current {
                    let message = error_message(&error);
                    if inner.message != message {
                        inner.message = message.into();
                        let _ = app.emit("nexus-voice", json!({"event":"wake_waiting","message":message}));
                    }
                }
                retry_at = Instant::now() + Duration::from_secs(15);
            }
        }
    });
}
