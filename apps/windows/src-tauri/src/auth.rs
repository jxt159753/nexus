use serde::{Deserialize, Serialize};
use url::Url;

const SERVICE: &str = "ai.nexus.windows";
const ACCOUNT: &str = "owner";

#[derive(Serialize, Deserialize)]
struct SavedOwner {
    core_url: String,
    token: String,
}

#[derive(Serialize, Deserialize)]
struct SavedSession {
    core_url: String,
    cookie: String,
}

#[derive(Serialize, Deserialize)]
pub struct SavedNode {
    pub core_ws_url: String,
    pub node_id: String,
    pub token: String,
    pub workspace_root: String,
}

fn entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, ACCOUNT).map_err(|_| "Windows 凭据存储不可用".to_string())
}

fn node_entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, "node").map_err(|_| "Windows 凭据存储不可用".to_string())
}

fn session_entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, "session").map_err(|_| "Windows 凭据存储不可用".to_string())
}

fn normalize_core_url(value: &str) -> Result<String, String> {
    let url = Url::parse(value.trim()).map_err(|_| "Core 地址格式无效".to_string())?;
    let loopback = matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
    if url.scheme() != "https" && !(url.scheme() == "http" && loopback) {
        return Err("Core 必须使用 HTTPS".to_string());
    }
    Ok(url.origin().ascii_serialization())
}

#[tauri::command]
pub fn remember_owner(core_url: String, token: String) -> Result<(), String> {
    if !(43..=128).contains(&token.len()) || !token.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-') {
        return Err("Owner 令牌格式无效".to_string());
    }
    let value = SavedOwner {
        core_url: normalize_core_url(&core_url)?,
        token,
    };
    entry()?
        .set_password(&serde_json::to_string(&value).map_err(|_| "无法序列化登录信息".to_string())?)
        .map_err(|_| "无法写入 Windows 凭据存储".to_string())
}

#[tauri::command]
pub fn load_owner(core_url: String) -> Result<Option<String>, String> {
    let expected = normalize_core_url(&core_url)?;
    let stored = match entry()?.get_password() {
        Ok(value) => value,
        Err(keyring::Error::NoEntry) => return Ok(None),
        Err(_) => return Err("无法读取 Windows 凭据存储".to_string()),
    };
    let saved: SavedOwner = match serde_json::from_str(&stored) {
        Ok(value) => value,
        Err(_) => return Ok(None),
    };
    if saved.core_url != expected {
        return Ok(None);
    }
    Ok(Some(saved.token))
}

#[tauri::command]
pub fn forget_owner() -> Result<(), String> {
    match entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err("无法清除 Windows 凭据存储".to_string()),
    }
}

pub fn save_session(core_url: &str, cookie: &str) -> Result<(), String> {
    if !(43..=128).contains(&cookie.len()) {
        return Err("会话令牌格式无效".to_string());
    }
    let value = SavedSession {
        core_url: normalize_core_url(core_url)?,
        cookie: cookie.to_string(),
    };
    session_entry()?
        .set_password(&serde_json::to_string(&value).map_err(|_| "无法序列化会话".to_string())?)
        .map_err(|_| "无法写入 Windows 凭据存储".to_string())
}

pub fn load_session(core_url: &str) -> Result<Option<String>, String> {
    let expected = normalize_core_url(core_url)?;
    let stored = match session_entry()?.get_password() {
        Ok(value) => value,
        Err(keyring::Error::NoEntry) => return Ok(None),
        Err(_) => return Err("无法读取 Windows 凭据存储".to_string()),
    };
    let saved: SavedSession = match serde_json::from_str(&stored) {
        Ok(value) => value,
        Err(_) => return Ok(None),
    };
    if saved.core_url != expected {
        return Ok(None);
    }
    Ok(Some(saved.cookie))
}

#[tauri::command]
pub fn forget_session() -> Result<(), String> {
    match session_entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err("无法清除 Windows 凭据存储".to_string()),
    }
}

#[tauri::command]
pub fn remember_node(config: SavedNode) -> Result<(), String> {
    if !(43..=128).contains(&config.token.len())
        || !config.token.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return Err("Node 令牌格式无效".to_string());
    }
    node_entry()?
        .set_password(&serde_json::to_string(&config).map_err(|_| "无法序列化 Node 配置".to_string())?)
        .map_err(|_| "无法写入 Windows 凭据存储".to_string())
}

#[tauri::command]
pub fn load_node() -> Result<Option<SavedNode>, String> {
    let stored = match node_entry()?.get_password() {
        Ok(value) => value,
        Err(keyring::Error::NoEntry) => return Ok(None),
        Err(_) => return Err("无法读取 Windows 凭据存储".to_string()),
    };
    Ok(serde_json::from_str(&stored).ok())
}

#[tauri::command]
pub fn forget_node() -> Result<(), String> {
    match node_entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err("无法清除 Windows 凭据存储".to_string()),
    }
}
