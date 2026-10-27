use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tauri::State;
use url::Url;

const APP_ORIGIN: &str = "https://tauri.localhost";
const MAX_API_BODY: usize = 2 * 1024 * 1024;
const MAX_API_RESPONSE: usize = 2 * 1024 * 1024;
const MAX_DELIVERABLE_BYTES: usize = 4 * 1024 * 1024;
const DELIVERABLE_TIMEOUT_SECS: u64 = 30;
// 16 MiB raw becomes 22,369,624 base64 characters. The extra room covers a
// bounded filename, request id and JSON field names; it is never a general
// API limit.
const MAX_ATTACHMENT_BASE64: usize = 23 * 1024 * 1024;
const MAX_ATTACHMENT_BODY: usize = MAX_ATTACHMENT_BASE64 + 64 * 1024;
const ATTACHMENT_TIMEOUT_SECS: u64 = 75;
const CANCEL_POLL_MS: u64 = 100;

pub struct BridgeState {
    client: reqwest::Client,
    transfer_client: reqwest::Client,
    core_url: Mutex<Option<String>>,
    cookie: Mutex<Option<String>>,
    transfers: Mutex<HashMap<String, Arc<AtomicBool>>>,
}

fn http_client(timeout: u64) -> reqwest::Client {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(timeout))
        .user_agent("NEXUS-Windows/0.1")
        .build()
        .expect("HTTP client")
}

impl BridgeState {
    pub(crate) fn authenticated_origin(&self) -> Result<String, String> {
        if self.cookie.lock().map_err(|_| "会话不可用")?.is_none() {
            return Err("请先登录 Core".into());
        }
        self.core_url.lock().map_err(|_| "Core 地址不可用")?.clone().ok_or("请先连接 Core".into())
    }

    pub fn new() -> Self {
        Self {
            client: http_client(30),
            transfer_client: http_client(ATTACHMENT_TIMEOUT_SECS),
            core_url: Mutex::new(None),
            cookie: Mutex::new(None),
            transfers: Mutex::new(HashMap::new()),
        }
    }
}

#[derive(Serialize)]
pub struct ApiResponse {
    status: u16,
    body: String,
}

// A snapshot is used only for native wake preparation/cleanup. Credentials
// never cross the WebView or get stored in wake preferences.
pub(crate) struct VoiceConnection {
    client: reqwest::Client,
    base: String,
    cookie: String,
    csrf: String,
    pub owner_id: uuid::Uuid,
}

impl VoiceConnection {
    pub async fn new(state: &BridgeState, expected: &str) -> Result<Self, String> {
        let base = state.authenticated_origin()?;
        if base != expected { return Err("AUTH_REQUIRED".into()); }
        let cookie = state.cookie.lock().map_err(|_| "会话不可用")?.clone().ok_or("AUTH_REQUIRED")?;
        let mut value = Self { client: state.client.clone(), base, cookie, csrf: String::new(), owner_id: uuid::Uuid::nil() };
        let info = value.request("GET", "/api/v1/session", None).await?;
        value.csrf = info["csrf_token"].as_str().ok_or("AUTH_REQUIRED")?.into();
        value.owner_id = info["owner_id"].as_str().and_then(|s| uuid::Uuid::parse_str(s).ok()).ok_or("AUTH_REQUIRED")?;
        Ok(value)
    }

    pub async fn request(&self, method: &str, path: &str, body: Option<serde_json::Value>) -> Result<serde_json::Value, String> {
        let (response, _) = exchange(&self.client, reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "请求无效")?,
            &format!("{}{path}", self.base), Some(self.cookie.clone()), body.map(|v| v.to_string()),
            Some(HashMap::from([("x-csrf-token".into(), self.csrf.clone())])),
            MAX_API_BODY, MAX_API_RESPONSE, Arc::new(AtomicBool::new(false)), "Core 请求超时").await?;
        let value: serde_json::Value = serde_json::from_str(&response.body).map_err(|_| "Core 响应无效")?;
        if !(200..300).contains(&response.status) {
            return Err(value["detail"].as_str().map(str::to_owned).unwrap_or_else(|| format!("HTTP_{}", response.status)));
        }
        Ok(value)
    }
}

fn validate_core_url(value: &str) -> Result<String, String> {
    let url = Url::parse(value.trim()).map_err(|_| "Core 地址格式无效".to_string())?;
    if !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some() {
        return Err("Core 地址不能包含凭证、查询或片段".to_string());
    }
    let loopback = matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
    if url.scheme() != "https" && !(url.scheme() == "http" && loopback) {
        return Err("Core 必须使用 HTTPS；本机调试允许 http://127.0.0.1".to_string());
    }
    if url.path() != "/" && !url.path().is_empty() {
        return Err("Core 地址只填写站点根地址".to_string());
    }
    Ok(url.origin().ascii_serialization())
}

fn validate_api_path(path: &str) -> Result<(), String> {
    if !path.starts_with("/api/v1/") || path.contains('\\') || path.contains("..") || path.chars().any(|c| c.is_control()) {
        return Err("仅允许 Core 的 /api/v1 接口".to_string());
    }
    Ok(())
}

/*
 * Deliverables are the only binary endpoint the shell reads. The path must be
 * exactly the server-issued download URL for one UUID; no query, no host path
 * and no arbitrary Core file is accepted.
 */
fn validate_deliverable_path(path: &str) -> Result<(), String> {
    validate_api_path(path)?;
    let identifier = path
        .strip_prefix("/api/v1/plugins/deliverables/")
        .ok_or_else(|| "只允许读取认证交付物 download_url".to_string())?;
    if identifier.contains('/') || identifier.contains('?') || identifier.contains('#') {
        return Err("交付物路径无效".to_string());
    }
    if uuid::Uuid::parse_str(identifier).is_err() {
        return Err("交付物标识必须是 UUID".to_string());
    }
    Ok(())
}

#[derive(Serialize)]
pub struct DeliverableResponse {
    status: u16,
    body_base64: String,
    filename: String,
    sha256: String,
}

fn encode_base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity((bytes.len() + 2) / 3 * 4);
    for chunk in bytes.chunks(3) {
        let value = ((chunk[0] as u32) << 16)
            | ((chunk.get(1).copied().unwrap_or(0) as u32) << 8)
            | chunk.get(2).copied().unwrap_or(0) as u32;
        output.push(TABLE[((value >> 18) & 63) as usize] as char);
        output.push(TABLE[((value >> 12) & 63) as usize] as char);
        output.push(if chunk.len() > 1 { TABLE[((value >> 6) & 63) as usize] as char } else { '=' });
        output.push(if chunk.len() > 2 { TABLE[(value & 63) as usize] as char } else { '=' });
    }
    output
}

fn header_text(headers: &reqwest::header::HeaderMap, name: &str) -> String {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_string()
}

/*
 * Only the two bounded attachment transfers may use the larger byte budget:
 *
 *   POST /api/v1/chat/conversations/<uuid>/attachments?encoding=base64
 *   GET  /api/v1/chat/conversations/<uuid>/attachments/<uuid>/content?encoding=base64
 *
 * Every other endpoint keeps the original 2 MiB request and response limit.
 */
fn validate_attachment_transfer(method: &reqwest::Method, path: &str) -> Result<(), String> {
    let (route, query) = path.split_once('?').ok_or_else(|| "附件传输缺少 encoding=base64".to_string())?;
    if query != "encoding=base64" {
        return Err("附件传输只允许 encoding=base64".to_string());
    }
    let rest = route
        .strip_prefix("/api/v1/chat/conversations/")
        .ok_or_else(|| "附件传输路径不在会话接口下".to_string())?;
    let (conversation, tail) = rest.split_once('/').ok_or_else(|| "附件传输路径不完整".to_string())?;
    if uuid::Uuid::parse_str(conversation).is_err() {
        return Err("附件传输的会话标识无效".to_string());
    }
    if method == reqwest::Method::POST {
        if tail != "attachments" {
            return Err("只允许向会话附件集合发送 Base64 上传".to_string());
        }
        return Ok(());
    }
    if method == reqwest::Method::GET {
        let leaf = tail
            .strip_prefix("attachments/")
            .and_then(|value| value.split_once('/'))
            .ok_or_else(|| "附件下载路径无效".to_string())?;
        if uuid::Uuid::parse_str(leaf.0).is_err() || leaf.1 != "content" {
            return Err("只允许下载会话附件原件".to_string());
        }
        return Ok(());
    }
    Err("附件传输只允许 GET 或 POST".to_string())
}

fn session_cookie(headers: &reqwest::header::HeaderMap) -> Option<String> {
    for value in headers.get_all(reqwest::header::SET_COOKIE) {
        let Ok(text) = value.to_str() else { continue };
        for part in text.split(';') {
            let item = part.trim();
            if let Some(rest) = item.strip_prefix("nexus_session=") {
                let token = rest.trim();
                if !token.is_empty() {
                    return Some(token.to_string());
                }
            }
        }
    }
    None
}

fn request_error(error: reqwest::Error, timeout: &str) -> String {
    if error.is_timeout() {
        timeout.to_string()
    } else if error.is_connect() {
        "无法连接 Core".to_string()
    } else {
        "Core 网络请求失败".to_string()
    }
}

async fn wait_cancel(flag: Arc<AtomicBool>) {
    while !flag.load(Ordering::SeqCst) {
        tokio::time::sleep(Duration::from_millis(CANCEL_POLL_MS)).await;
    }
}

/*
 * Read the response incrementally. The byte limit is enforced while chunks
 * arrive, so an oversized or hostile response is dropped before the whole body
 * is materialised. The cancel token aborts a stalled transfer.
 */
async fn read_bounded(
    mut response: reqwest::Response,
    limit: usize,
    cancel: Arc<AtomicBool>,
    timeout: &str,
) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    loop {
        let chunk = tokio::select! {
            value = response.chunk() => value.map_err(|error| request_error(error, timeout))?,
            _ = wait_cancel(cancel.clone()) => return Err("附件传输已取消".to_string()),
        };
        match chunk {
            Some(data) => {
                if bytes.len() + data.len() > limit {
                    return Err("Core 响应过大".to_string());
                }
                bytes.extend_from_slice(&data);
            }
            None => break,
        }
    }
    Ok(bytes)
}

#[allow(clippy::too_many_arguments)]
async fn exchange(
    client: &reqwest::Client,
    method: reqwest::Method,
    url: &str,
    cookie: Option<String>,
    body: Option<String>,
    headers: Option<HashMap<String, String>>,
    body_limit: usize,
    response_limit: usize,
    cancel: Arc<AtomicBool>,
    timeout: &str,
) -> Result<(ApiResponse, Option<String>), String> {
    let mut request = client
        .request(method.clone(), url)
        .header("Origin", APP_ORIGIN)
        .header("Accept", "application/json");
    if let Some(value) = cookie {
        request = request.header("Cookie", format!("nexus_session={value}"));
    }
    if let Some(body) = body {
        if body.len() > body_limit {
            return Err("请求体过大".to_string());
        }
        request = request.header("Content-Type", "application/json").body(body);
    }
    if let Some(headers) = headers {
        for (key, value) in headers {
            if key.eq_ignore_ascii_case("x-csrf-token") {
                request = request.header("X-CSRF-Token", value);
            }
        }
    }
    let response = tokio::select! {
        value = request.send() => value.map_err(|error| request_error(error, timeout))?,
        _ = wait_cancel(cancel.clone()) => return Err("附件传输已取消".to_string()),
    };
    let status = response.status().as_u16();
    let cookie = session_cookie(response.headers());
    let bytes = read_bounded(response, response_limit, cancel, timeout).await?;
    Ok((
        ApiResponse {
            status,
            body: String::from_utf8_lossy(&bytes).into_owned(),
        },
        cookie,
    ))
}

fn core_url(state: &BridgeState) -> Result<String, String> {
    state
        .core_url
        .lock()
        .map_err(|_| "Core 状态不可用".to_string())?
        .clone()
        .ok_or_else(|| "尚未设置 Core 地址".to_string())
}

#[tauri::command]
pub fn set_core_url(app: tauri::AppHandle, state: State<'_, BridgeState>, url: String) -> Result<(), String> {
    let base = validate_core_url(&url)?;
    let mut current = state.core_url.lock().map_err(|_| "Core 状态不可用")?;
    if current.as_deref() == Some(base.as_str()) {
        return Ok(());
    }
    *current = Some(base);
    *state.cookie.lock().map_err(|_| "会话状态不可用")? = None;
    drop(current);
    use tauri::Manager;
    app.state::<crate::wake::WakeManager>().disable();
    app.state::<crate::voice::VoiceManager>().stop();
    Ok(())
}

#[tauri::command]
pub fn load_session(state: State<'_, BridgeState>) -> Result<bool, String> {
    let base = core_url(&state)?;
    let Some(cookie) = crate::auth::load_session(&base)? else {
        return Ok(false);
    };
    *state.cookie.lock().map_err(|_| "会话状态不可用")? = Some(cookie);
    Ok(true)
}

#[tauri::command]
pub fn remember_session(state: State<'_, BridgeState>) -> Result<(), String> {
    let base = core_url(&state)?;
    let cookie = state
        .cookie
        .lock()
        .map_err(|_| "会话状态不可用")?
        .clone()
        .ok_or_else(|| "当前没有可保存的会话".to_string())?;
    crate::auth::save_session(&base, &cookie)
}

#[tauri::command]
pub async fn api_request(
    app: tauri::AppHandle,
    state: State<'_, BridgeState>,
    method: String,
    path: String,
    body: Option<String>,
    headers: Option<HashMap<String, String>>,
) -> Result<ApiResponse, String> {
    validate_api_path(&path)?;
    let base = core_url(&state)?;
    let cookie = state.cookie.lock().map_err(|_| "会话状态不可用")?.clone();
    let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "请求方法无效".to_string())?;
    if method == reqwest::Method::DELETE && path == "/api/v1/session" {
        use tauri::Manager;
        app.state::<crate::wake::WakeManager>().disable();
        app.state::<crate::voice::VoiceManager>().stop();
    }
    let (response, cookie) = exchange(
        &state.client,
        method.clone(),
        &format!("{base}{path}"),
        cookie,
        body,
        headers,
        MAX_API_BODY,
        MAX_API_RESPONSE,
        Arc::new(AtomicBool::new(false)),
        "Core 请求超时",
    )
    .await?;
    if let Some(token) = cookie {
        *state.cookie.lock().map_err(|_| "会话状态不可用")? = Some(token);
    }
    if method == reqwest::Method::DELETE && path == "/api/v1/session" {
        *state.cookie.lock().map_err(|_| "会话状态不可用")? = None;
    }
    Ok(response)
}

/*
 * Authenticated binary download for `present` deliverables. Kept separate from
 * the JSON bridge because arbitrary bytes cannot be represented as lossy UTF-8
 * text; the response is bounded to 4 MiB and returned as Base64 only.
 */
#[tauri::command]
pub async fn read_deliverable(
    state: State<'_, BridgeState>,
    path: String,
) -> Result<DeliverableResponse, String> {
    validate_deliverable_path(&path)?;
    let base = core_url(&state)?;
    let cookie = state.cookie.lock().map_err(|_| "会话状态不可用")?.clone();
    let client = http_client(DELIVERABLE_TIMEOUT_SECS);
    let mut request = client
        .get(format!("{base}{path}"))
        .header("Origin", APP_ORIGIN)
        .header("Accept", "application/octet-stream");
    if let Some(value) = cookie {
        request = request.header("Cookie", format!("nexus_session={value}"));
    }
    let cancel = Arc::new(AtomicBool::new(false));
    let response = tokio::select! {
        value = request.send() => value.map_err(|error| request_error(error, "交付物请求超时"))?,
        _ = wait_cancel(cancel.clone()) => return Err("交付物请求已取消".to_string()),
    };
    let status = response.status().as_u16();
    let filename = header_text(response.headers(), "content-disposition");
    let sha256 = header_text(response.headers(), "x-content-sha256");
    let bytes = read_bounded(response, MAX_DELIVERABLE_BYTES, cancel, "交付物请求超时").await?;
    Ok(DeliverableResponse {
        status,
        body_base64: encode_base64(&bytes),
        filename,
        sha256,
    })
}
#[tauri::command]
pub async fn attachment_request(
    state: State<'_, BridgeState>,
    id: String,
    method: String,
    path: String,
    body: Option<String>,
    headers: Option<HashMap<String, String>>,
) -> Result<ApiResponse, String> {
    if id.is_empty() || id.len() > 64 || !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        return Err("附件传输标识无效".to_string());
    }
    validate_api_path(&path)?;
    let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "请求方法无效".to_string())?;
    validate_attachment_transfer(&method, &path)?;
    if method != reqwest::Method::POST && body.is_some() {
        return Err("该附件传输不允许请求体".to_string());
    }
    if method == reqwest::Method::POST && body.as_deref().map(str::len).unwrap_or(0) > MAX_ATTACHMENT_BODY {
        return Err("附件请求体过大".to_string());
    }
    let base = core_url(&state)?;
    let cookie = state.cookie.lock().map_err(|_| "会话状态不可用")?.clone();
    let cancel = Arc::new(AtomicBool::new(false));
    state
        .transfers
        .lock()
        .map_err(|_| "附件传输状态不可用")?
        .insert(id.clone(), cancel.clone());
    let result = exchange(
        &state.transfer_client,
        method,
        &format!("{base}{path}"),
        cookie,
        body,
        headers,
        MAX_ATTACHMENT_BODY,
        MAX_ATTACHMENT_BASE64,
        cancel,
        "附件传输超时",
    )
    .await;
    if let Ok(mut transfers) = state.transfers.lock() {
        transfers.remove(&id);
    }
    let (response, cookie) = result?;
    if let Some(token) = cookie {
        *state.cookie.lock().map_err(|_| "会话状态不可用")? = Some(token);
    }
    Ok(response)
}

#[tauri::command]
pub fn cancel_attachment_request(state: State<'_, BridgeState>, id: String) -> Result<(), String> {
    if let Some(flag) = state.transfers.lock().map_err(|_| "附件传输状态不可用")?.get(&id) {
        flag.store(true, Ordering::SeqCst);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{validate_attachment_transfer, validate_core_url, validate_deliverable_path};
    use reqwest::Method;

    const CONVERSATION: &str = "11111111-1111-4111-8111-111111111111";
    const ATTACHMENT: &str = "22222222-2222-4222-8222-222222222222";

    #[test]
    fn only_the_two_base64_attachment_endpoints_get_the_larger_limit() {
        let upload = format!("/api/v1/chat/conversations/{CONVERSATION}/attachments?encoding=base64");
        let download = format!(
            "/api/v1/chat/conversations/{CONVERSATION}/attachments/{ATTACHMENT}/content?encoding=base64"
        );
        assert!(validate_attachment_transfer(&Method::POST, &upload).is_ok());
        assert!(validate_attachment_transfer(&Method::GET, &download).is_ok());
        for path in [
            format!("/api/v1/chat/conversations/{CONVERSATION}/attachments"),
            format!("/api/v1/chat/conversations/{CONVERSATION}/attachments?encoding=raw"),
            format!("/api/v1/chat/turns/{ATTACHMENT}/stop"),
            format!("/api/v1/chat/conversations/not-a-uuid/attachments?encoding=base64"),
            format!("/api/v1/chat/conversations/{CONVERSATION}/attachments/{ATTACHMENT}/preview?encoding=base64"),
            format!("/api/v1/chat/conversations/{CONVERSATION}/attachments/{ATTACHMENT}/content?encoding=base64&x=1"),
            format!("https://evil.example/api/v1/chat/conversations/{CONVERSATION}/attachments?encoding=base64"),
        ] {
            assert!(validate_attachment_transfer(&Method::POST, &path).is_err(), "{path}");
            assert!(validate_attachment_transfer(&Method::GET, &path).is_err(), "{path}");
        }
        assert!(validate_attachment_transfer(&Method::GET, &upload).is_err());
        assert!(validate_attachment_transfer(&Method::POST, &download).is_err());
    }

    #[test]
    fn core_url_keeps_tls_and_rejects_credentials_or_paths() {
        assert!(validate_core_url("https://core.example.com").is_ok());
        assert!(validate_core_url("http://127.0.0.1:8080").is_ok());
        assert!(validate_core_url("http://core.example.com").is_err());
        assert!(validate_core_url("https://user:pass@core.example.com").is_err());
        assert!(validate_core_url("https://core.example.com/sub").is_err());
        assert!(validate_core_url("https://core.example.com?x=1").is_err());
    }

    #[test]
    fn deliverable_path_is_exactly_one_server_uuid() {
        let valid = "/api/v1/plugins/deliverables/11111111-1111-4111-8111-111111111111".to_string();
        assert!(validate_deliverable_path(&valid).is_ok());
        for path in [
            "/api/v1/plugins/deliverables/not-a-uuid".to_string(),
            "/api/v1/plugins/deliverables/..".to_string(),
            "/api/v1/plugins/deliverables/11111111-1111-4111-8111-111111111111?x=1".to_string(),
            "/api/v1/plugins/deliverables/11111111-1111-4111-8111-111111111111/extra".to_string(),
            "/api/v1/plugins/versions/11111111-1111-4111-8111-111111111111".to_string(),
            "C:\\Windows\\win.ini".to_string(),
        ] {
            assert!(validate_deliverable_path(&path).is_err(), "{path}");
        }
    }

    #[test]
    fn base64_encoding_preserves_binary_length() {
        for (input, expected) in [
            (b"".as_slice(), ""),
            (b"f".as_slice(), "Zg=="),
            (b"fo".as_slice(), "Zm8="),
            (b"foo".as_slice(), "Zm9v"),
            (b"\x00\xff\x10".as_slice(), "AP8Q"),
        ] {
            assert_eq!(super::encode_base64(input), expected);
        }
    }
}
