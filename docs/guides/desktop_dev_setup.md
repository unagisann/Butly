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
# Terminal 1: sidecar (no token = auth off)
venv/bin/python -m butly_api.server --port 8010

# Terminal 2: Vite dev server
cd frontend
BUTLY_DEV_BACKEND_URL=http://127.0.0.1:8010 pnpm dev
```

Open `http://127.0.0.1:1420`.

`BUTLY_DEV_BACKEND_URL` is **the target the Vite dev server proxies `/api` to**.
The backend is therefore same-origin from the browser's point of view, so React
uses a development bridge (`DevBrowserBridge` in
`frontend/src/lifecycle/bridge.ts`) against the current origin instead of going
through Tauri commands. A successful `GET /api/v1/health` moves the app to
`ready`, after which `/api/v1/ready` polling proceeds as usual. SSE passes
through the proxy unbuffered, so streaming, cancel, and retry all work.

Being same-origin removes two things at once:

- **CORS configuration** (`--dev-cors`). It still enables sanitized chat-debug
  summaries, so pass it (or `BUTLY_DEVELOPER_MODE=1`) when you want the debug
  panel.
- **Forwarding the backend port.** Only 1420 needs a tunnel.

Port 8000 belongs to the legacy Streamlit backend, so pick another port (8010 in
the examples) when running both.

To avoid repeating the variable, put it in `frontend/.env.development.local`
(already gitignored):

```
BUTLY_DEV_BACKEND_URL=http://127.0.0.1:8010
```

On Windows PowerShell:
`$env:BUTLY_DEV_BACKEND_URL="http://127.0.0.1:8010"; pnpm dev`.

Opening the browser without the variable set leaves no proxy in place, and the
app reports `dev_backend_not_configured` so the omission is distinguishable from
a broken backend.

#### Opening From Another Machine (Pi Development)

Forward only the dev server:

```bash
# On your laptop
ssh -L 1420:127.0.0.1:1420 <pi-host>
# → open http://127.0.0.1:1420
```

The dev server binds IPv4 loopback (`127.0.0.1`) because the default `localhost`
can resolve to `::1`, which then mismatches the `ssh -L` destination. **Exposing
it with `pnpm dev --host` falls outside that assumption**, so use port
forwarding instead.

#### Limitations

- **No token support.** This path targets only a loopback sidecar started
  without `BUTLY_DESKTOP_TOKEN`. Use mode B to exercise token authentication.
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
| `dev_backend_not_configured` | `BUTLY_DEV_BACKEND_URL` unset, so no proxy exists | Restart `pnpm dev` with the variable (env changes and vite.config are only read at startup) |
| `dev_health_failed_TypeError` | The dev server itself is unreachable (dropped tunnel) | Try `curl http://127.0.0.1:1420/api/v1/health` on the Pi and from your laptop |
| `dev_health_http_502` / `504` | The proxy target is down or on another port | Check the sidecar log and the port in `BUTLY_DEV_BACKEND_URL` |
| `dev_health_http_401` | The sidecar was started with `BUTLY_DESKTOP_TOKEN` | Start it without a token for browser dev, or use mode B |
| Reachable on the Pi but not from the browser | `ssh -L` resolved the destination to `::1` | Spell it out as `-L 1420:127.0.0.1:1420` |
| `version_mismatch` (Tauri) | `butly_api/version.py` and `backend.rs` expectations drifted | Align both |
| Debug panel missing | Developer mode disabled | Start with `--dev-cors`, or set `BUTLY_DEVELOPER_MODE=1` |
| `port 8000 is already in use` | Collides with the legacy Streamlit backend | Start the sidecar on another port (8010) |

## Pre-Push Checks

```bash
./scripts/check_before_push.sh          # backend (compileall / flake8 / pytest / pip check)
cd frontend && pnpm lint && pnpm typecheck && pnpm test
```
