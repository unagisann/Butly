# Desktop UI Startup (Normal And Development)

**English** | [日本語](desktop_dev_setup.ja.md)

How to start the official desktop Chat UI (Tauri + React) and the legacy Streamlit
app, and which mode to pick when you want to iterate on the UI visually.

For the contracts themselves, see
[Desktop Sidecar Specification](../reference/desktop_sidecar.md) and
[Official Desktop Chat UI](../reference/frontend_chat.md).

## Prerequisites

- A Python venv (`venv/bin/python`; `.venv\Scripts\python.exe` on Windows)
- Node.js 22+ and pnpm 10 (only when working on the frontend)
- `pnpm install --frozen-lockfile` run once inside `frontend/`

---

## Normal Startup (Using Butly)

### Official Desktop Build (Windows)

Install and launch the installer produced by
`.github/workflows/windows-desktop.yml` (CI artifact) or by `pnpm tauri build`.

Tauri owns sidecar startup, port assignment, token issuance, and shutdown, so
users never start the backend themselves.

### Legacy Streamlit (Evaluation And Not-Yet-Migrated Settings)

Evaluation features such as LoCoMo and Japanese A/B runs stay in Streamlit and
are deliberately not moved into the official UI.

```bash
# Terminal 1: backend (compatibility entrypoint with legacy routers)
venv/bin/python -m uvicorn main:app --port 8000 --reload

# Terminal 2: Streamlit
venv/bin/python -m streamlit run app.py
```

Open `http://localhost:8501`. On Windows, `02_start_webui.bat` starts both.

---

## Development Startup (Iterating On The UI)

Three modes, by purpose.

| Mode | Environment | HMR | Covers | Does not cover |
|---|---|---|---|---|
| A. Browser dev | Linux / Pi / anywhere | yes | UI, real backend calls, SSE streaming, i18n | everything Tauri-shell specific |
| B. Tauri dev | Windows (ship target) | yes | A plus sidecar spawn / token / crash recovery / version checks | installer behavior |
| C. Installer smoke | Windows | no | the shipped artifact (bundled PyInstaller, install, first launch) | — |

Use **A for everyday UI work, B when touching Tauri lifecycle, C before a release.**

### A. Browser Dev (No Tauri)

Start the sidecar manually and open the Vite dev server in a plain browser.
Because WebKitGTK is not involved, this works on a headless machine such as a
Raspberry Pi: forward the ports over SSH and use the browser on your laptop.

```bash
# Terminal 1: sidecar (no token = auth off, CORS limited to the Vite origins)
venv/bin/python -m butly_api.server --dev-cors --port 8000

# Terminal 2: Vite dev server
cd frontend
VITE_BUTLY_DEV_BACKEND_URL=http://localhost:8000 pnpm dev
```

Open `http://localhost:1420`.

With `VITE_BUTLY_DEV_BACKEND_URL` set, React selects a development bridge
(`DevBrowserBridge` in `frontend/src/lifecycle/bridge.ts`) that talks to that URL
directly instead of going through Tauri commands. A successful
`GET /api/v1/health` moves the app to `ready`, after which `/api/v1/ready`
polling proceeds as usual.

To avoid repeating the variable, put it in `frontend/.env.development.local`
(already gitignored):

```
VITE_BUTLY_DEV_BACKEND_URL=http://localhost:8000
```

On Windows PowerShell:
`$env:VITE_BUTLY_DEV_BACKEND_URL="http://localhost:8000"; pnpm dev`.

#### Opening From Another Machine (Pi Development)

Forward the ports and open them as `localhost`:

```bash
# On your laptop
ssh -L 1420:localhost:1420 -L 8000:localhost:8000 <pi-host>
# → open http://localhost:1420
```

The `--dev-cors` allowlist covers only `localhost` / `127.0.0.1` on ports 1420
and 5173, and the sidecar binds loopback by default. **Exposing the dev server
with `pnpm dev --host` falls outside both**, so use port forwarding instead.

#### Limitations

- **No token support.** `VITE_*` values are inlined into the bundle, so this path
  targets only a loopback sidecar started without `BUTLY_DESKTOP_TOKEN`. Use
  mode B to exercise token authentication.
- No process supervision, so the UI restart button re-probes health instead of
  restarting the sidecar. Restart it from its terminal.
- Expected versions live on the Rust side, so `version_mismatch` is not detected.
- The whole path is removed from production builds by dead-code elimination.

### B. Tauri Dev (Real Windows Machine)

Windows is the ship target; use this mode whenever Tauri shell behavior matters.

```powershell
# Terminal 1: sidecar
.venv\Scripts\python.exe -m butly_api.server --dev-cors --port 8000

# Terminal 2: Tauri + React
cd frontend
$env:BUTLY_DEV_BACKEND_PORT="8000"; pnpm tauri dev
```

With `BUTLY_DEV_BACKEND_PORT` set, Tauri attaches to the already-running backend
with a health probe instead of spawning one. To exercise token authentication as
well, set the same `BUTLY_DESKTOP_TOKEN` for **both** Tauri and the backend.

To exercise the production spawn path, drop those variables and build the sidecar
first:

```powershell
.venv\Scripts\python.exe scripts\build_backend_sidecar.py
cd frontend
pnpm tauri dev
```

### C. Installer Smoke (Windows)

`.github/workflows/windows-desktop.yml` runs PyInstaller build → smoke test →
NSIS installer on every push and PR, publishing the installer as an artifact. You
can verify the shipped build by downloading that artifact, without setting up a
Windows development environment. There is no HMR, so this is for final
verification rather than UI iteration.

### About Running Tauri On A Raspberry Pi

An aarch64 Linux build is possible, but it needs the full WebKitGTK stack and a
desktop session (X / Wayland); X forwarding is not usable in practice because of
GL. The rendering engine also differs from the shipped Windows WebView2. Prefer
**mode A on the Pi and mode B on Windows** for Tauri-specific checks.

---

## Shutdown

- Vite dev server / sidecar: `Ctrl+C` in each terminal
- Tauri dev: close the window (Tauri terminates the sidecar)

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Stuck on `not_running_in_tauri` | Browser run without `VITE_BUTLY_DEV_BACKEND_URL` | Restart `pnpm dev` with the variable (the dev server does not pick up env changes) |
| `dev_health_failed_TypeError` | Sidecar not running, wrong port, or CORS rejection | Check the sidecar log and port, and the browser console |
| `dev_health_http_401` | The sidecar was started with `BUTLY_DESKTOP_TOKEN` | Start it without a token for browser dev, or use mode B |
| CORS error | Opened from an origin other than `localhost:1420` | Use port forwarding so the origin is `localhost:1420` |
| `version_mismatch` (Tauri) | `butly_api/version.py` and `backend.rs` expectations drifted | Align both |
| Debug panel missing | Developer mode disabled | Start with `--dev-cors`, or set `BUTLY_DEVELOPER_MODE=1` |

## Pre-Push Checks

```bash
./scripts/check_before_push.sh          # backend (compileall / flake8 / pytest / pip check)
cd frontend && pnpm lint && pnpm typecheck && pnpm test
```
