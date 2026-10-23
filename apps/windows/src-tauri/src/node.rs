use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};
use url::Url;
use uuid::Uuid;

// Baseline local tools written on first setup. plugin.call@1 is only added when
// the owner explicitly opts in through the local App setting; a model, Core
// response or Node self-report can never add it here.
const DEFAULT_TOOLS: &[&str] = &[
    "system.info@1",
    "filesystem.list@1",
    "filesystem.read@1",
    "filesystem.search@1",
    "filesystem.write_preview@1",
    "filesystem.write@1",
    "filesystem.read_image@1",
];
const PLUGIN_CALL: &str = "plugin.call@1";

pub struct NodeManager {
    inner: Mutex<NodeInner>,
}

#[derive(Default)]
struct NodeInner {
    child: Option<Child>,
    error: Option<String>,
}

#[derive(Deserialize)]
pub struct NodeStartConfig {
    core_ws_url: String,
    node_id: String,
    node_token: String,
    workspace_root: String,
    /// Owner-controlled local policy. `None` preserves the existing list.
    allow_plugin_call: Option<bool>,
}

#[derive(Serialize, Clone)]
pub struct NodeStatus {
    running: bool,
    pid: Option<u32>,
    exe: Option<String>,
    error: Option<String>,
    allowed_tools: Vec<String>,
    plugins_enabled: bool,
    plugin_call_allowed: bool,
    config_path: Option<String>,
    config_error: Option<String>,
    /// True when the currently written config contains host plugin policy.
    /// The App never treats this local report as Core trust.
    plugin_policy_configured: bool,
}

impl NodeManager {
    pub fn new() -> Self {
        Self { inner: Mutex::new(NodeInner::default()) }
    }

    fn executable(app: &AppHandle) -> Result<PathBuf, String> {
        let bundled = app
            .path()
            .resource_dir()
            .map_err(|_| "无法定位应用资源目录".to_string())?
            .join("binaries")
            .join("nexus-node.exe");
        if bundled.is_file() {
            return Ok(bundled);
        }
        let development = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join("nexus-node.exe");
        if development.is_file() {
            return Ok(development);
        }
        Err("安装包缺少 nexus-node.exe".to_string())
    }

    fn config_path(app: &AppHandle) -> Result<PathBuf, String> {
        Ok(app
            .path()
            .app_data_dir()
            .map_err(|_| "无法定位应用数据目录".to_string())?
            .join("node.toml"))
    }

    fn status_for(app: &AppHandle, inner: &mut NodeInner) -> NodeStatus {
        let exe = Self::executable(app).ok();
        let mut error = inner.error.clone();
        let pid = if let Some(child) = inner.child.as_mut() {
            match child.try_wait() {
                Ok(None) => Some(child.id()),
                Ok(Some(status)) => {
                    error = Some(format!("Node 已退出（{}）", status));
                    inner.child = None;
                    None
                }
                Err(value) => {
                    error = Some(format!("无法读取 Node 状态：{value}"));
                    None
                }
            }
        } else {
            None
        };
        let config_path = Self::config_path(app).ok();
        let policy = config_path
            .as_deref()
            .map(read_policy)
            .unwrap_or_else(|| PolicySummary::missing());
        if let Some(value) = policy.error.clone() {
            if error.is_none() {
                error = Some(value);
            }
        }
        NodeStatus {
            running: pid.is_some(),
            pid,
            exe: exe.map(|path| path.to_string_lossy().into_owned()),
            error,
            allowed_tools: policy.allowed_tools,
            plugins_enabled: policy.plugins_enabled,
            plugin_call_allowed: policy.plugin_call_allowed,
            config_path: config_path.map(|path| path.to_string_lossy().into_owned()),
            config_error: policy.error,
            plugin_policy_configured: policy.plugins_enabled && policy.plugin_call_allowed,
        }
    }

    pub fn stop(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            if let Some(mut child) = inner.child.take() {
                terminate_tree(&mut child);
            }
        }
    }
}

#[derive(Default)]
struct PolicySummary {
    allowed_tools: Vec<String>,
    plugins_enabled: bool,
    plugin_call_allowed: bool,
    error: Option<String>,
}

impl PolicySummary {
    fn missing() -> Self {
        Self::default()
    }
}

fn read_policy(path: &Path) -> PolicySummary {
    let text = match fs::read_to_string(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return PolicySummary::missing(),
        Err(error) => {
            return PolicySummary {
                error: Some(format!("无法读取 Node 配置：{error}")),
                ..PolicySummary::default()
            }
        }
    };
    let root: toml::Table = match toml::from_str(&text) {
        Ok(value) => value,
        Err(error) => {
            return PolicySummary {
                error: Some(format!("现有 Node 配置无法解析，未覆盖：{error}")),
                ..PolicySummary::default()
            }
        }
    };
    let allowed_tools: Vec<String> = root
        .get("allowed_tools")
        .and_then(toml::Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(toml::Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let plugins_enabled = root
        .get("plugins")
        .and_then(|value| value.get("enabled"))
        .and_then(toml::Value::as_bool)
        .unwrap_or(false);
    let plugin_call_allowed = allowed_tools.iter().any(|value| value == PLUGIN_CALL);
    PolicySummary {
        allowed_tools,
        plugins_enabled,
        plugin_call_allowed,
        error: None,
    }
}

pub(crate) fn terminate_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let pid = child.id().to_string();
        let _ = Command::new("taskkill")
            .args(["/PID", &pid, "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(0x08000000)
            .status();
    }
    #[cfg(not(windows))]
    {
        let _ = child.kill();
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn validate_ws_url(value: &str) -> Result<(String, bool), String> {
    let url = Url::parse(value.trim()).map_err(|_| "Core WebSocket 地址格式无效".to_string())?;
    if !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some() {
        return Err("Core WebSocket 地址不能包含凭证、查询或片段".to_string());
    }
    if url.path() != "/ws/v1/node" {
        return Err("Core WebSocket 地址必须是 /ws/v1/node".to_string());
    }
    let loopback = matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
    let local_http = match url.scheme() {
        "wss" => false,
        "ws" if loopback => true,
        "ws" => return Err("非本机 Core 必须使用 WSS".to_string()),
        _ => return Err("Core WebSocket 必须使用 WSS".to_string()),
    };
    Ok((url.to_string(), local_http))
}

fn validate_token(value: &str) -> Result<(), String> {
    if !(43..=128).contains(&value.len())
        || !value.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return Err("Node 令牌格式无效".to_string());
    }
    Ok(())
}

/*
 * Merge only the connection/workspace fields owned by the App. Every other
 * table (plugins, commands, interaction_targets, custom LSP/MCP/browser setup)
 * stays byte-for-byte equivalent at the semantic TOML level. Invalid existing
 * TOML is never overwritten; the start fails instead.
 */
fn merged_config(
    existing: Option<&str>,
    node_id: &str,
    data_dir: &Path,
    core_url: &str,
    local_http: bool,
    workspace: &Path,
    allow_plugin_call: Option<bool>,
) -> Result<String, String> {
    let mut root: toml::Table = match existing {
        Some(text) => toml::from_str(text).map_err(|error| format!("现有 Node 配置无法解析，未覆盖：{error}"))?,
        None => toml::Table::new(),
    };
    root.insert("node_id".into(), toml::Value::String(node_id.to_string()));
    root.insert(
        "data_dir".into(),
        toml::Value::String(data_dir.to_string_lossy().into_owned()),
    );
    root.insert("core_url".into(), toml::Value::String(core_url.to_string()));
    root.insert("local_http".into(), toml::Value::Boolean(local_http));

    let mut tools: Vec<String> = root
        .get("allowed_tools")
        .and_then(toml::Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(toml::Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_else(|| DEFAULT_TOOLS.iter().map(|value| value.to_string()).collect());
    if let Some(allow) = allow_plugin_call {
        tools.retain(|value| value != PLUGIN_CALL);
        if allow {
            tools.push(PLUGIN_CALL.to_string());
        }
    }
    root.insert(
        "allowed_tools".into(),
        toml::Value::Array(tools.into_iter().map(toml::Value::String).collect()),
    );

    let roots = root
        .entry("roots".to_string())
        .or_insert_with(|| toml::Value::Table(toml::Table::new()));
    let roots = roots
        .as_table_mut()
        .ok_or_else(|| "现有 Node 配置的 roots 不是表，未覆盖".to_string())?;
    roots.insert(
        "workspace".into(),
        toml::Value::String(workspace.to_string_lossy().into_owned()),
    );

    toml::to_string_pretty(&root).map_err(|error| format!("无法序列化 Node 配置：{error}"))
}

#[tauri::command]
pub fn node_status(app: AppHandle, state: State<'_, NodeManager>) -> NodeStatus {
    let mut inner = state.inner.lock().expect("node state");
    NodeManager::status_for(&app, &mut inner)
}

#[tauri::command]
pub fn node_start(
    app: AppHandle,
    state: State<'_, NodeManager>,
    config: NodeStartConfig,
) -> Result<NodeStatus, String> {
    let uuid = Uuid::parse_str(config.node_id.trim()).map_err(|_| "Node ID 必须是 UUID".to_string())?;
    let (core_url, local_http) = validate_ws_url(&config.core_ws_url)?;
    validate_token(&config.node_token)?;
    let workspace = PathBuf::from(config.workspace_root.trim());
    if !workspace.is_absolute() {
        return Err("Workspace 必须是绝对路径".to_string());
    }
    fs::create_dir_all(&workspace).map_err(|_| "无法创建 Workspace 目录".to_string())?;
    if !workspace.is_dir() {
        return Err("Workspace 不是目录".to_string());
    }
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|_| "无法定位应用数据目录".to_string())?
        .join("node");
    fs::create_dir_all(&data_dir).map_err(|_| "无法创建 Node 数据目录".to_string())?;
    let executable = NodeManager::executable(&app)?;
    let config_path = NodeManager::config_path(&app)?;

    let existing = match fs::read_to_string(&config_path) {
        Ok(value) => Some(value),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(format!("无法读取现有 Node 配置：{error}")),
    };
    let content = merged_config(
        existing.as_deref(),
        &uuid.to_string(),
        &data_dir,
        &core_url,
        local_http,
        &workspace,
        config.allow_plugin_call,
    )?;

    let mut inner = state.inner.lock().map_err(|_| "Node 状态不可用")?;
    let current = NodeManager::status_for(&app, &mut inner);
    if current.running {
        return Ok(current);
    }
    fs::write(&config_path, content).map_err(|_| "无法写入 Node 配置".to_string())?;

    let mut command = Command::new(&executable);
    command
        .arg("--config")
        .arg(&config_path)
        .env("NEXUS_NODE_TOKEN", &config.node_token)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    match command.spawn() {
        Ok(child) => {
            inner.child = Some(child);
            inner.error = None;
            Ok(NodeManager::status_for(&app, &mut inner))
        }
        Err(error) => {
            inner.error = Some(format!("启动 Node 失败：{error}"));
            Ok(NodeManager::status_for(&app, &mut inner))
        }
    }
}

#[tauri::command]
pub fn node_stop(app: AppHandle, state: State<'_, NodeManager>) -> NodeStatus {
    let mut inner = state.inner.lock().expect("node state");
    if let Some(mut child) = inner.child.take() {
        terminate_tree(&mut child);
    }
    inner.error = None;
    NodeManager::status_for(&app, &mut inner)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merge_preserves_plugin_policy_and_workspace_tables() {
        let original = r#"
node_id = '11111111-1111-4111-8111-111111111111'
data_dir = 'C:\old'
core_url = 'ws://127.0.0.1:8000/ws/v1/node'
local_http = true
allowed_tools = ['filesystem.read@1']

[plugins]
enabled = true
allow_host_python = true
max_jobs = 4
[plugins.lsp.python]
executable = 'C:\Python\python.exe'
argv = ['-m', 'pyright']

[plugins.mcp.local]
[plugins.mcp.local.process]
executable = 'C:\MCP\server.exe'
argv = ['--stdio']

[commands.build]
executable = 'C:\Tools\build.exe'
executable_sha256 = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
argv = ['--release']

[roots]
workspace = 'C:\old\workspace'
"#;
        let merged = merged_config(
            Some(original),
            "22222222-2222-4222-8222-222222222222",
            Path::new("C:\\new-data"),
            "wss://core.example.com/ws/v1/node",
            false,
            Path::new("C:\\new-workspace"),
            Some(true),
        )
        .expect("merge");
        let value: toml::Table = toml::from_str(&merged).expect("parse");
        assert_eq!(value["node_id"].as_str(), Some("22222222-2222-4222-8222-222222222222"));
        assert_eq!(value["roots"]["workspace"].as_str(), Some("C:\\new-workspace"));
        assert_eq!(value["plugins"]["enabled"].as_bool(), Some(true));
        assert_eq!(value["plugins"]["allow_host_python"].as_bool(), Some(true));
        assert!(value["plugins"]["lsp"]["python"].is_table());
        assert!(value["commands"]["build"].is_table());
        assert!(value["allowed_tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item.as_str() == Some("plugin.call@1")));
        assert!(value["allowed_tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item.as_str() == Some("filesystem.read@1")));
    }

    #[test]
    fn invalid_existing_config_is_not_replaced() {
        let error = merged_config(
            Some("not = [valid"),
            "22222222-2222-4222-8222-222222222222",
            Path::new("C:\\new-data"),
            "wss://core.example.com/ws/v1/node",
            false,
            Path::new("C:\\new-workspace"),
            None,
        )
        .expect_err("invalid TOML must be rejected");
        assert!(error.contains("未覆盖"));
    }

    #[test]
    fn opt_out_removes_only_plugin_call() {
        let original = r#"
node_id = '11111111-1111-4111-8111-111111111111'
allowed_tools = ['system.info@1', 'plugin.call@1']
[roots]
workspace = 'C:\old'
"#;
        let merged = merged_config(
            Some(original),
            "11111111-1111-4111-8111-111111111111",
            Path::new("C:\\data"),
            "ws://127.0.0.1:8000/ws/v1/node",
            true,
            Path::new("C:\\new"),
            Some(false),
        )
        .expect("merge");
        let value: toml::Table = toml::from_str(&merged).expect("parse");
        assert!(!value["allowed_tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item.as_str() == Some("plugin.call@1")));
        assert!(value["allowed_tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item.as_str() == Some("system.info@1")));
    }
}
