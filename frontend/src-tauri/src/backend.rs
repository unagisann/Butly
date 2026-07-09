//! FastAPI sidecar の lifecycle 管理。
//!
//! - backend process / per-launch token / port は **Rust 側だけが所有**する。
//!   React には event（`backend-state`）と command の返り値だけを渡す。
//! - token は起動時に CSPRNG で生成し、環境変数 `BUTLY_DESKTOP_TOKEN` で
//!   sidecar へ渡す。command line / log には出さない。
//! - sidecar は `--port 0` で起動し、stdout の 1 行 JSON
//!   （`{"event":"listening","port":N,...}`）で実 port を受け取ってから
//!   `/api/v1/health` を polling して readiness / version を確認する。

use std::fs::{self, OpenOptions};
use std::io::{Read, Write as IoWrite};
use std::net::TcpStream;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// backend との version handshake の期待値。
/// backend 側の正本は `butly_api/version.py`。bundle 時は必ず同時に更新する。
pub const EXPECTED_API_VERSION: &str = "v1";
pub const EXPECTED_BACKEND_VERSION: &str = "0.1.0";

const SIDECAR_NAME: &str = "butly-backend";
const HEALTH_TIMEOUT_SECS: u64 = 60;
const GRACEFUL_SHUTDOWN_TIMEOUT_MS: u64 = 5_000;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Phase {
    Starting,
    Ready,
    Unavailable,
    Crashed,
    VersionMismatch,
}

impl Phase {
    fn as_str(&self) -> &'static str {
        match self {
            Phase::Starting => "starting",
            Phase::Ready => "ready",
            Phase::Unavailable => "unavailable",
            Phase::Crashed => "crashed",
            Phase::VersionMismatch => "version_mismatch",
        }
    }
}

/// React へ渡す状態 payload（frontend/src/lifecycle/types.ts と対応）。
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendStatePayload {
    pub phase: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub port: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backend_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub api_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConnectionInfo {
    pub base_url: String,
    pub token: Option<String>,
}

struct Inner {
    phase: Phase,
    port: Option<u16>,
    backend_version: Option<String>,
    api_version: Option<String>,
    detail: Option<String>,
    child: Option<CommandChild>,
    /// 再起動で古い監視タスクの通知を無効化する世代カウンタ
    generation: u64,
    /// 意図的な終了（アプリ終了 / restart）中は Terminated を crash 扱いしない
    shutting_down: bool,
    starting: bool,
}

pub struct BackendManager {
    token: String,
    inner: Mutex<Inner>,
}

fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).expect("OS CSPRNG unavailable");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

impl BackendManager {
    pub fn new() -> Self {
        Self {
            token: generate_token(),
            inner: Mutex::new(Inner {
                phase: Phase::Starting,
                port: None,
                backend_version: None,
                api_version: None,
                detail: None,
                child: None,
                generation: 0,
                shutting_down: false,
                starting: false,
            }),
        }
    }

    pub fn state_payload(&self) -> BackendStatePayload {
        let inner = self.inner.lock().unwrap();
        BackendStatePayload {
            phase: inner.phase.as_str().to_string(),
            port: if inner.phase == Phase::Ready {
                inner.port
            } else {
                None
            },
            backend_version: inner.backend_version.clone(),
            api_version: inner.api_version.clone(),
            detail: inner.detail.clone(),
        }
    }

    pub fn connection_info(&self) -> Option<ConnectionInfo> {
        let inner = self.inner.lock().unwrap();
        if inner.phase != Phase::Ready {
            return None;
        }
        let port = inner.port?;
        Some(ConnectionInfo {
            base_url: format!("http://127.0.0.1:{port}"),
            token: self.request_token(),
        })
    }

    fn dev_external_backend(&self) -> bool {
        std::env::var("BUTLY_DEV_BACKEND_PORT").is_ok()
    }

    fn request_token(&self) -> Option<String> {
        if self.dev_external_backend() {
            return std::env::var("BUTLY_DESKTOP_TOKEN")
                .ok()
                .filter(|token| !token.is_empty());
        }
        Some(self.token.clone())
    }

    fn set_state(&self, app: &AppHandle, phase: Phase, detail: Option<String>) {
        {
            let mut inner = self.inner.lock().unwrap();
            inner.phase = phase;
            inner.detail = detail;
        }
        self.emit_state(app);
    }

    fn emit_state(&self, app: &AppHandle) {
        let payload = self.state_payload();
        if let Err(e) = app.emit("backend-state", payload) {
            log_line(&format!("[shell] failed to emit backend-state: {e}"));
        }
    }
}

fn local_data_dir() -> PathBuf {
    // backend（frozen 既定）と同じ %LOCALAPPDATA%\Butly を使う。
    // 非 Windows（開発時）は ~/.local/share/Butly 相当へフォールバック。
    if let Ok(appdata) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(appdata).join("Butly");
    }
    dirs_fallback().join("Butly")
}

fn dirs_fallback() -> PathBuf {
    if let Ok(home) = std::env::var("HOME") {
        return PathBuf::from(home).join(".local").join("share");
    }
    std::env::temp_dir()
}

fn log_path() -> PathBuf {
    local_data_dir().join("logs").join("backend.log")
}

/// sidecar の stdout / stderr を保存する。token はどこにも書かれない
/// （backend が出力しない + shell 側でも log へ渡さない）。
fn log_line(line: &str) {
    let path = log_path();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    // 素朴なローテーション: 5MB を超えたら .1 に退避
    if let Ok(meta) = fs::metadata(&path) {
        if meta.len() > 5 * 1024 * 1024 {
            let _ = fs::rename(&path, path.with_extension("log.1"));
        }
    }
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{line}");
    }
}

/// loopback 専用の最小 HTTP client。
/// 依存を増やさないため HTTP/1.0 + Connection: close で自前実装する。
fn http_request(
    port: u16,
    method: &str,
    path: &str,
    bearer: Option<&str>,
    timeout: Duration,
) -> Result<(u16, String), String> {
    let addr = format!("127.0.0.1:{port}");
    let stream = TcpStream::connect(&addr).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;
    let mut stream = stream;

    let mut req = format!("{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n");
    if let Some(token) = bearer {
        req.push_str(&format!("Authorization: Bearer {token}\r\n"));
    }
    if method == "POST" {
        req.push_str("Content-Length: 0\r\n");
    }
    req.push_str("Connection: close\r\n\r\n");
    stream
        .write_all(req.as_bytes())
        .map_err(|e| e.to_string())?;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&raw);
    let mut parts = text.splitn(2, "\r\n\r\n");
    let head = parts.next().unwrap_or("");
    let body = parts.next().unwrap_or("").to_string();
    let status: u16 = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| "malformed HTTP response".to_string())?;
    Ok((status, body))
}

/// backend の起動。既に起動処理中 / 稼働中なら何もしない（二重起動防止）。
pub fn start_backend(app: AppHandle) {
    let manager = app.state::<Arc<BackendManager>>().inner().clone();
    {
        let mut inner = manager.inner.lock().unwrap();
        if inner.starting || inner.child.is_some() {
            log_line("[shell] start_backend ignored: already starting/running");
            return;
        }
        inner.starting = true;
        inner.shutting_down = false;
        inner.generation += 1;
        inner.phase = Phase::Starting;
        inner.port = None;
        inner.backend_version = None;
        inner.api_version = None;
        inner.detail = None;
    }
    manager.emit_state(&app);

    // 開発モード: 手動起動済み backend へ接続するだけ（spawn しない）
    if let Ok(port_str) = std::env::var("BUTLY_DEV_BACKEND_PORT") {
        match port_str.parse::<u16>() {
            Ok(port) => {
                let generation = manager.inner.lock().unwrap().generation;
                {
                    let mut inner = manager.inner.lock().unwrap();
                    inner.port = Some(port);
                    inner.starting = false;
                }
                spawn_health_probe(app, manager, port, generation);
            }
            Err(_) => {
                let mut inner = manager.inner.lock().unwrap();
                inner.starting = false;
                drop(inner);
                manager.set_state(
                    &app,
                    Phase::Unavailable,
                    Some("invalid BUTLY_DEV_BACKEND_PORT".into()),
                );
            }
        }
        return;
    }

    let sidecar = match app.shell().sidecar(SIDECAR_NAME).map(|cmd| {
        cmd.args([
            "--port",
            "0",
            "--parent-pid",
            &std::process::id().to_string(),
        ])
        .env("BUTLY_DESKTOP_TOKEN", manager.token.clone())
    }) {
        Ok(cmd) => cmd,
        Err(e) => {
            let mut inner = manager.inner.lock().unwrap();
            inner.starting = false;
            drop(inner);
            manager.set_state(
                &app,
                Phase::Unavailable,
                Some(format!("sidecar not found: {e}")),
            );
            return;
        }
    };

    let (mut rx, child) = match sidecar.spawn() {
        Ok(pair) => pair,
        Err(e) => {
            let mut inner = manager.inner.lock().unwrap();
            inner.starting = false;
            drop(inner);
            manager.set_state(
                &app,
                Phase::Unavailable,
                Some(format!("failed to spawn sidecar: {e}")),
            );
            return;
        }
    };

    let generation = {
        let mut inner = manager.inner.lock().unwrap();
        inner.child = Some(child);
        inner.starting = false;
        inner.generation
    };

    let app_for_events = app.clone();
    let manager_for_events = manager.clone();
    tauri::async_runtime::spawn(async move {
        let mut port_seen = false;
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                    log_line(&format!("[backend] {line}"));
                    if !port_seen {
                        if let Some(port) = parse_listening_port(&line) {
                            port_seen = true;
                            {
                                let mut inner = manager_for_events.inner.lock().unwrap();
                                inner.port = Some(port);
                            }
                            spawn_health_probe(
                                app_for_events.clone(),
                                manager_for_events.clone(),
                                port,
                                generation,
                            );
                        }
                    }
                }
                CommandEvent::Stderr(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                    log_line(&format!("[backend:err] {line}"));
                }
                CommandEvent::Terminated(status) => {
                    log_line(&format!(
                        "[shell] backend terminated (code={:?})",
                        status.code
                    ));
                    let should_report = {
                        let mut inner = manager_for_events.inner.lock().unwrap();
                        let stale = inner.generation != generation;
                        if !stale {
                            inner.child = None;
                        }
                        !stale && !inner.shutting_down
                    };
                    if should_report {
                        manager_for_events.set_state(
                            &app_for_events,
                            Phase::Crashed,
                            status.code.map(|c| format!("exit_code={c}")),
                        );
                    }
                    break;
                }
                _ => {}
            }
        }
    });
}

fn parse_listening_port(line: &str) -> Option<u16> {
    if !line.starts_with('{') {
        return None;
    }
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    if value.get("event")?.as_str()? != "listening" {
        return None;
    }
    u16::try_from(value.get("port")?.as_u64()?).ok()
}

/// 通知された port へ health polling し、ready / version_mismatch /
/// unavailable を確定する。
fn spawn_health_probe(app: AppHandle, manager: Arc<BackendManager>, port: u16, generation: u64) {
    std::thread::spawn(move || {
        let deadline = std::time::Instant::now() + Duration::from_secs(HEALTH_TIMEOUT_SECS);
        loop {
            if manager.inner.lock().unwrap().generation != generation {
                return; // 再起動済み。古い probe は黙って終わる
            }
            if std::time::Instant::now() > deadline {
                manager.set_state(
                    &app,
                    Phase::Unavailable,
                    Some("health_check_timeout".into()),
                );
                return;
            }
            match http_request(port, "GET", "/api/v1/health", None, Duration::from_secs(3)) {
                Ok((200, body)) => {
                    let parsed: Option<(String, String)> =
                        serde_json::from_str::<serde_json::Value>(&body)
                            .ok()
                            .and_then(|v| {
                                Some((
                                    v.get("backend_version")?.as_str()?.to_string(),
                                    v.get("api_version")?.as_str()?.to_string(),
                                ))
                            });
                    let Some((backend_version, api_version)) = parsed else {
                        manager.set_state(
                            &app,
                            Phase::Unavailable,
                            Some("malformed_health_response".into()),
                        );
                        return;
                    };
                    let mismatch = api_version != EXPECTED_API_VERSION
                        || backend_version != EXPECTED_BACKEND_VERSION;
                    {
                        let mut inner = manager.inner.lock().unwrap();
                        if inner.generation != generation {
                            return;
                        }
                        inner.backend_version = Some(backend_version.clone());
                        inner.api_version = Some(api_version.clone());
                        if mismatch {
                            inner.phase = Phase::VersionMismatch;
                            inner.detail = Some(format!(
                                "expected backend {EXPECTED_BACKEND_VERSION} / api {EXPECTED_API_VERSION}, got {backend_version} / {api_version}"
                            ));
                        }
                    }
                    if mismatch {
                        manager.emit_state(&app);
                        return;
                    }

                    let token = manager.request_token();
                    match http_request(
                        port,
                        "GET",
                        "/api/v1/ready",
                        token.as_deref(),
                        Duration::from_secs(3),
                    ) {
                        Ok((200, ready_body)) => {
                            let ready = serde_json::from_str::<serde_json::Value>(&ready_body)
                                .ok()
                                .and_then(|value| value.get("ready")?.as_bool())
                                .unwrap_or(false);
                            if ready {
                                let mut inner = manager.inner.lock().unwrap();
                                if inner.generation != generation {
                                    return;
                                }
                                inner.phase = Phase::Ready;
                                inner.detail = None;
                                drop(inner);
                                manager.emit_state(&app);
                                return;
                            }
                        }
                        Ok((401, _)) => {
                            manager.set_state(
                                &app,
                                Phase::Unavailable,
                                Some("readiness_unauthorized".into()),
                            );
                            return;
                        }
                        Ok(_) | Err(_) => {}
                    }
                }
                Ok(_) | Err(_) => {
                    std::thread::sleep(Duration::from_millis(250));
                }
            }
        }
    });
}

/// graceful shutdown: `POST /api/v1/shutdown`（token 付き）→ timeout 後 kill。
/// アプリ終了時と restart の両方から呼ぶ。
pub fn shutdown_backend_blocking(manager: &Arc<BackendManager>) {
    let (port, has_child) = {
        let mut inner = manager.inner.lock().unwrap();
        inner.shutting_down = true;
        (inner.port, inner.child.is_some())
    };
    if !has_child {
        return;
    }

    if let Some(port) = port {
        let _ = http_request(
            port,
            "POST",
            "/api/v1/shutdown",
            Some(&manager.token),
            Duration::from_secs(3),
        );
    }

    let deadline = std::time::Instant::now() + Duration::from_millis(GRACEFUL_SHUTDOWN_TIMEOUT_MS);
    while std::time::Instant::now() < deadline {
        if manager.inner.lock().unwrap().child.is_none() {
            log_line("[shell] backend exited gracefully");
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }

    // timeout: 強制終了（--parent-pid 監視が最終 fallback として残る）
    let child = manager.inner.lock().unwrap().child.take();
    if let Some(child) = child {
        log_line("[shell] graceful shutdown timed out; killing backend");
        let _ = child.kill();
    }
}

/// restart command 本体。稼働中なら止めてから 1 プロセスだけ起動し直す。
pub fn restart_backend(app: AppHandle) -> Result<(), String> {
    let manager = app.state::<Arc<BackendManager>>().inner().clone();
    {
        let inner = manager.inner.lock().unwrap();
        if inner.starting {
            return Err("backend is already starting".into());
        }
    }
    shutdown_backend_blocking(&manager);
    {
        let mut inner = manager.inner.lock().unwrap();
        inner.shutting_down = false;
        inner.port = None;
    }
    start_backend(app);
    Ok(())
}
