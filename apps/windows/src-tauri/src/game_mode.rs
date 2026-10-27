//! A local resource veto, independent of Core task/permission authority.
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tauri::{Emitter, Manager};
use uuid::Uuid;

#[derive(Clone, Copy, Default, Deserialize, Serialize, PartialEq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum Mode { #[default] Auto, On, Off }

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Preference { mode: Mode, request_id: Uuid }

#[derive(Clone, Serialize)]
pub struct Status {
    mode: Mode, request_id: Uuid, pub active: bool,
    reason: String, updated_at_ms: u64, error: String,
}

struct Inner { status: Status, last_fullscreen: Option<Instant> }
pub struct GameMode(Mutex<Inner>, Mutex<()>);

impl Default for GameMode {
    fn default() -> Self {
        Self(Mutex::new(Inner { status: Status { mode: Mode::Auto, request_id: Uuid::nil(),
            active: false, reason: "idle".into(), updated_at_ms: 0, error: String::new() }, last_fullscreen: None }), Mutex::new(()))
    }
}

impl GameMode {
    pub fn active(&self) -> bool { self.0.lock().expect("game mode").status.active }
}

fn directory(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    Ok(app.path().app_data_dir().map_err(|_| "无法定位游戏模式设置")?.join("node"))
}

fn atomic_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let parent = path.parent().ok_or("游戏模式路径无效")?;
    fs::create_dir_all(parent).map_err(|_| "无法创建游戏模式设置目录")?;
    let temporary = parent.join(format!(".game-mode-{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&temporary).map_err(|_| "无法保存游戏模式")?;
        file.write_all(&serde_json::to_vec(value).map_err(|_| "游戏模式设置无效")?).map_err(|_| "无法写入游戏模式")?;
        file.sync_all().map_err(|_| "无法保存游戏模式")?;
        drop(file);
        fs::rename(&temporary, path).map_err(|error| format!("无法替换游戏模式设置：{error}"))
    })();
    let _ = fs::remove_file(temporary);
    Ok(result?)
}

fn read_preference(path: &Path) -> Result<Preference, String> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| "游戏模式设置无法读取，请重新选择模式".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let value = Preference { mode: Mode::Auto, request_id: Uuid::new_v4() };
            atomic_json(path, &value)?;
            Ok(value)
        },
        Err(_) => Err("无法读取游戏模式设置".into()),
    }
}

// QUNS_BUSY also covers borderless/fullscreen applications. This deliberately
// reports a full-screen signal, not certainty about the foreground game's name.
fn full_screen() -> Result<bool, String> {
    #[cfg(windows)] {
        #[link(name = "shell32")]
        extern "system" { fn SHQueryUserNotificationState(state: *mut i32) -> i32; }
        let mut state = 0;
        let result = unsafe { SHQueryUserNotificationState(&mut state) };
        if result < 0 { return Err("Windows 全屏检测暂不可用，可手动选择游戏模式".into()); }
        Ok(matches!(state, 2 | 3))
    }
    #[cfg(not(windows))] { Ok(false) }
}

fn effective(mode: Mode, detected: bool, last: &mut Option<Instant>, now: Instant) -> (bool, &'static str) {
    if detected { *last = Some(now); }
    match mode {
        Mode::On => (true, "manual"),
        Mode::Off => (false, "manual"),
        Mode::Auto if detected => (true, "fullscreen"),
        Mode::Auto if last.is_some_and(|time| now.duration_since(time) < Duration::from_secs(15)) => (true, "cooldown"),
        Mode::Auto => (false, "idle"),
    }
}

fn refresh(app: &tauri::AppHandle) -> Result<Status, String> {
    let directory = directory(app)?;
    let manager = app.state::<GameMode>();
    let _update = manager.1.lock().map_err(|_| "游戏模式更新不可用")?;
    let mut inner = manager.0.lock().map_err(|_| "游戏模式状态不可用")?;
    let previous = inner.status.clone();
    let selected = read_preference(&directory.join("game-mode.json"))?;
    let detected = full_screen();
    let (active, reason) = effective(selected.mode, detected.as_ref().copied().unwrap_or(previous.active), &mut inner.last_fullscreen, Instant::now());
    inner.status = Status { mode: selected.mode, request_id: selected.request_id, active,
        reason: reason.into(), updated_at_ms: SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64,
        error: if selected.mode == Mode::Auto { detected.err().unwrap_or_default() } else { String::new() } };
    let status = inner.status.clone();
    // Release before taking the voice lock. Launch rechecks this veto while it
    // owns that lock, so an in-flight preparation cannot reopen the microphone.
    drop(inner);
    if active { app.state::<crate::voice::VoiceManager>().stop(); }
    atomic_json(&directory.join("game-mode-status.json"), &status)?;
    if previous.active != active || previous.request_id != status.request_id || previous.error != status.error {
        let _ = app.emit("nexus-game-mode", &status);
    }
    Ok(status)
}

#[tauri::command]
pub fn game_mode_status(app: tauri::AppHandle) -> Result<Status, String> {
    Ok(app.state::<GameMode>().0.lock().map_err(|_| "游戏模式状态不可用")?.status.clone())
}

#[tauri::command]
pub fn game_mode_set(app: tauri::AppHandle, mode: Mode) -> Result<Status, String> {
    atomic_json(&directory(&app)?.join("game-mode.json"), &Preference { mode, request_id: Uuid::new_v4() })?;
    refresh(&app)
}

pub fn start(app: &tauri::AppHandle) {
    if let Err(error) = refresh(app) { eprintln!("{error}"); }
    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(1));
        if let Err(error) = refresh(&app) {
            let manager = app.state::<GameMode>();
            if let Ok(mut inner) = manager.0.lock() { inner.status.error = error; };
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_selection_wins_and_auto_survives_alt_tab() {
        let now = Instant::now(); let mut last = None;
        assert_eq!(effective(Mode::Auto, true, &mut last, now), (true,"fullscreen"));
        assert_eq!(effective(Mode::Off, true, &mut last, now), (false,"manual"));
        assert_eq!(effective(Mode::On, false, &mut last, now+Duration::from_secs(60)), (true,"manual"));
        assert_eq!(effective(Mode::Auto, false, &mut last, now+Duration::from_secs(5)), (true,"cooldown"));
        assert_eq!(effective(Mode::Auto, false, &mut last, now+Duration::from_secs(16)), (false,"idle"));
    }
    #[test]
    fn preference_replacement_survives_restart_and_rejects_invalid_mode() {
        let directory = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../var/game-mode").join(format!("native-{}",Uuid::new_v4()));
        let path = directory.join("game-mode.json");
        let first = read_preference(&path).unwrap(); assert_eq!(first.mode, Mode::Auto);
        let value = Preference { mode: Mode::Off, request_id: Uuid::new_v4() };
        atomic_json(&path, &value).unwrap();
        assert_eq!(read_preference(&path).unwrap().request_id, value.request_id);
        fs::write(&path, r#"{"mode":"invalid","request_id":"invalid"}"#).unwrap();
        assert!(read_preference(&path).is_err());
        fs::remove_file(path).unwrap(); fs::remove_dir(directory).unwrap();
    }
    #[test]
    fn windows_fullscreen_probe_is_callable() { assert!(full_screen().is_ok()); }
}
