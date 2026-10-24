mod auth;
mod bridge;
mod node;
mod voice;

use std::time::Duration;

use bridge::BridgeState;
use node::NodeManager;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

fn show_main(app: &tauri::AppHandle, from_tray: bool) -> tauri::Result<()> {
    let splash = app.get_webview_window("splash");
    // Closing during startup or opening from the tray consumes the splash.
    // The delayed callback must not reopen a window the user already hid.
    if !from_tray && splash.is_none() {
        return Ok(());
    }
    let position = splash.as_ref().and_then(|window| window.outer_position().ok());
    let minimized = splash.as_ref().is_some_and(|window| window.is_minimized().unwrap_or(false));
    if let Some(main) = app.get_webview_window("main") {
        // A minimized Windows window reports off-screen coordinates.
        if !minimized {
            if let Some(position) = position {
                main.set_position(tauri::Position::Physical(position))?;
            }
        }
        main.show()?;
        if minimized && !from_tray {
            main.minimize()?;
        } else if from_tray {
            main.unminimize()?;
            main.set_focus()?;
        }
        if let Some(window) = splash {
            // destroy bypasses CloseRequested; this is an internal transition.
            window.destroy()?;
        }
    }
    Ok(())
}

fn create_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "打开 NEXUS", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    TrayIconBuilder::with_id("nexus")
        .icon(app.default_window_icon().expect("bundled application icon").clone())
        .tooltip("NEXUS · 点击打开，右键退出")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => {
                if let Err(error) = show_main(app, true) {
                    eprintln!("无法打开 NEXUS 窗口：{error}");
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(event, TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            }) {
                if let Err(error) = show_main(tray.app_handle(), true) {
                    eprintln!("无法打开 NEXUS 窗口：{error}");
                }
            }
        })
        .build(app)?;
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .manage(BridgeState::new())
        .manage(NodeManager::new())
        .manage(voice::VoiceManager::new())
        .invoke_handler(tauri::generate_handler![
            bridge::set_core_url,
            bridge::load_session,
            bridge::remember_session,
            bridge::api_request,
            bridge::read_deliverable,
            bridge::attachment_request,
            bridge::cancel_attachment_request,
            auth::remember_owner,
            auth::load_owner,
            auth::forget_owner,
            auth::forget_session,
            auth::remember_node,
            auth::load_node,
            auth::forget_node,
            node::node_status,
            node::node_start,
            node::node_stop,
            voice::voice_status,
            voice::voice_devices,
            voice::voice_start,
            voice::voice_stop,
            voice::voice_interrupt
        ])
        .on_window_event(|window, event| {
            if matches!(window.label(), "main" | "splash") {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    // Keep the main WebView and its managed processes alive.
                    // Only suppress closing once hiding has succeeded.
                    if window.hide().is_ok() {
                        api.prevent_close();
                        if window.label() == "splash" {
                            let _ = window.destroy();
                        }
                    }
                }
            }
        })
        .setup(|app| {
            create_tray(app)?;
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("app.html".into()))
                .title("NEXUS")
                .inner_size(1280.0, 720.0)
                .min_inner_size(900.0, 600.0)
                .visible(false)
                .focused(false)
                .center()
                .use_https_scheme(true)
                .build()?;
            WebviewWindowBuilder::new(app, "splash", WebviewUrl::App("splash.html".into()))
                .title("NEXUS · 正在启动")
                .inner_size(1280.0, 720.0)
                .resizable(false)
                .decorations(true)
                .closable(true)
                .minimizable(true)
                .maximizable(false)
                .always_on_top(false)
                .focused(false)
                .center()
                .use_https_scheme(true)
                .build()?;
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(5600));
                let app = handle.clone();
                let _ = handle.run_on_main_thread(move || {
                    if let Err(error) = show_main(&app, false) {
                        eprintln!("无法显示 NEXUS 主窗口：{error}");
                    }
                });
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("NEXUS Windows shell")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                app.state::<voice::VoiceManager>().stop();
                app.state::<NodeManager>().stop();
            }
        });
}
