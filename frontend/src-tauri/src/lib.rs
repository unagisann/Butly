mod backend;

use std::sync::Arc;

use backend::{BackendManager, BackendStatePayload, ConnectionInfo};
use tauri::{Manager, RunEvent, State};

#[tauri::command]
fn get_backend_state(manager: State<Arc<BackendManager>>) -> BackendStatePayload {
    manager.state_payload()
}

/// token は ready 後にこの command 経由でのみ React へ渡す。
/// React 側は memory 保持のみ（localStorage 等への保存禁止）。
#[tauri::command]
fn get_connection_info(manager: State<Arc<BackendManager>>) -> Option<ConnectionInfo> {
    manager.connection_info()
}

#[tauri::command]
fn restart_backend(app: tauri::AppHandle) -> Result<(), String> {
    backend::restart_backend(app)
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 二重起動時は既存 window にフォーカスするだけ（backend も 1 つを維持）
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .manage(Arc::new(BackendManager::new()))
        .invoke_handler(tauri::generate_handler![
            get_backend_state,
            get_connection_info,
            restart_backend
        ])
        .setup(|app| {
            backend::start_backend(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Butly desktop shell")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                let manager = app.state::<Arc<BackendManager>>().inner().clone();
                backend::shutdown_backend_blocking(&manager);
            }
        });
}
