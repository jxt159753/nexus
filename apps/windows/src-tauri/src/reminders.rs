use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use serde::{Deserialize, Serialize};
use tauri::{Manager, State};
use crate::bridge::BridgeState;

#[derive(Clone, Serialize, Deserialize)]
struct Settings { url: String, token: String, client_id: String, #[serde(default)] posted_ids: Vec<String> }

pub struct Reminders(Mutex<Option<tauri::async_runtime::JoinHandle<()>>>);
impl Reminders {
    pub fn new() -> Self { Self(Mutex::new(None)) }
    pub fn stop(&self) { if let Ok(mut task) = self.0.lock() { if let Some(task) = task.take() { task.abort(); } } }
}
fn entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new("ai.nexus.windows", "reminders").map_err(|_| "提醒凭据存储不可用".into())
}

// Static script; content is JSON through stdin, never interpolated as code.
fn show(title: &str, body: &str, identifier: &str, quiet: bool) -> Result<(), String> {
    let script = r#"
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[System.Text.Encoding]::UTF8
$item=[Console]::In.ReadToEnd() | ConvertFrom-Json
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.NotificationSetting,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null
$xml=New-Object Windows.Data.Xml.Dom.XmlDocument
$markup='<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>'
if($item.quiet) { $markup=$markup.Replace('</toast>','<audio silent="true"/></toast>') }
$xml.LoadXml($markup)
$texts=$xml.GetElementsByTagName('text')
$texts.Item(0).InnerText=$item.title
$texts.Item(1).InnerText=$item.body
$toast=[Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.Tag=$item.tag
$toast.Group='NEXUS'
$toast.SuppressPopup=[bool]$item.quiet
$notifier=[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ai.nexus.windows')
if($notifier.Setting -ne 'Enabled') { throw 'Notifications disabled' }
$notifier.Show($toast)
"#;
    let mut command = Command::new("powershell.exe");
    command.args(["-NoProfile", "-NonInteractive", "-Command", script]).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)] {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().map_err(|_| "无法显示系统提醒")?;
    let value = serde_json::json!({"title":title,"body":body,"quiet":quiet,"tag":identifier.chars().take(16).collect::<String>()});
    if let Some(mut input) = child.stdin.take() {
        if input.write_all(value.to_string().as_bytes()).is_err() { let _=child.kill();let _=child.wait();return Err("提醒内容未能传入系统".into()); }
    }
    let deadline=Instant::now()+Duration::from_secs(15);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return if status.success() { Ok(()) } else { Err("系统未接受提醒，请检查通知设置".into()) },
            Ok(None) if Instant::now()<deadline => std::thread::sleep(Duration::from_millis(100)),
            _ => { let _=child.kill();let _=child.wait();return Err("系统提醒超时".into()); }
        }
    }
}

fn start(app: &tauri::AppHandle, mut settings: Settings) {
    let state=app.state::<Reminders>();state.stop();
    let handle=app.clone();
    let task=tauri::async_runtime::spawn(async move {
        let Ok(client)=reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(20)).build() else { return };
        let mut quiet_backlog=false;
        loop {
            if handle.state::<crate::game_mode::GameMode>().active() {
                quiet_backlog=true;
                tokio::time::sleep(Duration::from_secs(5)).await;
                continue;
            }
            let response=client.get(format!("{}/api/v1/reminders/delivery",settings.url)).bearer_auth(&settings.token).send().await;
            if let Ok(response)=response {
                if matches!(response.status().as_u16(),401|403) { break }
                if response.status().is_success() && response.content_length().unwrap_or(0)<=256*1024 {
                    if let Ok(bytes)=response.bytes().await {
                        if bytes.len()<=256*1024 {
                            if let Ok(value)=serde_json::from_slice::<serde_json::Value>(&bytes) {
                                if let Some(rows)=value["reminders"].as_array() {
                                    if rows.is_empty() { quiet_backlog=false; }
                                    for row in rows.iter().take(20) {
                                        if handle.state::<crate::game_mode::GameMode>().active() { quiet_backlog=true; break; }
                                        let Some(id)=row["reminder_id"].as_str().filter(|id| uuid::Uuid::parse_str(id).is_ok()) else { continue };
                                        let title=row["title"].as_str().unwrap_or("NEXUS 提醒").chars().take(100).collect::<String>();
                                        let body=row["body"].as_str().unwrap_or("").chars().take(2000).collect::<String>();
                                        if !settings.posted_ids.iter().any(|seen| seen==id) {
                                            let tag=id.to_string();
                                            let quiet=quiet_backlog;
                                            let check=handle.clone();
                                            let result=tauri::async_runtime::spawn_blocking(move || show(&title,&body,&tag,quiet || check.state::<crate::game_mode::GameMode>().active())).await;
                                            if !matches!(result,Ok(Ok(()))) { break }
                                            settings.posted_ids.push(id.to_string());
                                            if settings.posted_ids.len()>256 { settings.posted_ids.remove(0); }
                                            let saved=entry().and_then(|entry| entry.set_password(&serde_json::to_string(&settings).unwrap()).map_err(|_| "无法保存提醒回执".into()));
                                            if saved.is_err() { break }
                                        }
                                        let _=client.post(format!("{}/api/v1/reminders/delivery/{}",settings.url,id)).bearer_auth(&settings.token).send().await;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    });
    if let Ok(mut current)=state.0.lock() { *current=Some(task); };
}

pub fn restore(app: &tauri::AppHandle) {
    if let Ok(value)=entry().and_then(|entry| entry.get_password().map_err(|_| "没有提醒设置".into())) {
        if let Ok(settings)=serde_json::from_str::<Settings>(&value) { start(app,settings); }
    }
}

#[tauri::command]
pub fn reminders_start(app: tauri::AppHandle, bridge: State<'_,BridgeState>, token:String, client_id:String) -> Result<(),String> {
    let url=bridge.authenticated_origin()?;
    if token.len()!=43 || !token.chars().all(|c| c.is_ascii_alphanumeric() || c=='_' || c=='-') || uuid::Uuid::parse_str(&client_id).is_err() { return Err("提醒凭据无效".into()) }
    let settings=Settings{url,token,client_id,posted_ids:Vec::new()};
    entry()?.set_password(&serde_json::to_string(&settings).map_err(|_| "提醒设置无效")?).map_err(|_| "无法保存提醒设置")?;
    start(&app,settings);Ok(())
}

#[tauri::command]
pub fn reminders_stop(state:State<'_,Reminders>) -> Result<(),String> {
    state.stop();
    if let Ok(entry)=entry() { let _=entry.delete_credential(); }
    Ok(())
}

#[cfg(all(test, windows))]
mod tests {
    #[test]
    #[ignore = "Posts one real, temporary Windows notification; run explicitly"]
    fn system_toast_reaches_notification_history() {
        super::show("NEXUS 提醒验证", "后台提醒测试：<内容> $(literal)", "nexus-test-toast", true).unwrap();
        let script = r#"
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotificationHistory,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
$history=[Windows.UI.Notifications.ToastNotificationManager]::History
$found=@($history.GetHistory('ai.nexus.windows') | Where-Object { $_.Tag -eq 'nexus-test-toast' })
$history.Remove('nexus-test-toast','NEXUS','ai.nexus.windows')
# History reconstructs the ToastNotification; SuppressPopup is not persisted.
if($found.Count -ne 1 -or -not $found[0].Content.GetXml().Contains('silent="true"')) { exit 1 }
"#;
        use std::os::windows::process::CommandExt;
        assert!(std::process::Command::new("powershell.exe").args(["-NoProfile","-NonInteractive","-Command",script]).creation_flags(0x08000000).status().unwrap().success());
    }
}
