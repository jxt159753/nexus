mod auth;
mod bridge;
mod node;
mod voice;

use std::time::Duration;

use bridge::BridgeState;
use node::NodeManager;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

fn show_main(app: &tauri::AppHandle) {
    let splash = app.get_webview_window("splash");
    let position = splash.as_ref().and_then(|window| window.outer_position().ok());
    if let Some(window) = splash {
        let _ = window.close();
    }
    if let Some(main) = app.get_webview_window("main") {
        if let Some(position) = position {
            let _ = main.set_position(tauri::Position::Physical(position));
        }
        let _ = main.show();
        let _ = main.set_focus();
    }
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
        .setup(|app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("app.html".into()))
                .title("NEXUS")
                .inner_size(1280.0, 720.0)
                .min_inner_size(900.0, 600.0)
                .visible(false)
                .center()
                .use_https_scheme(true)
                .build()?;
            let splash = WebviewWindowBuilder::new(app, "splash", WebviewUrl::App("splash.html".into()))
                .title("NEXUS")
                .inner_size(1280.0, 720.0)
                .resizable(false)
                .decorations(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .center()
                .use_https_scheme(true)
                .build()?;
            let _ = splash.set_focus();
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(5600));
                show_main(&handle);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("NEXUS Windows shell")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                app.state::<NodeManager>().stop();
                app.state::<voice::VoiceManager>().stop();
            }
        });
}
