# Desktop sidecar specification (Phase 1)

🌐 [日本語](desktop_sidecar.ja.md) | **English**

> Process / auth / lifecycle contract between the Tauri shell and the FastAPI
> sidecar (`butly_api/server.py`). Design background:
> [frontend_migration_plan.ja.md](../planning/active/frontend_migration_plan.ja.md) §6.

## Startup sequence

1. Tauri (`frontend/src-tauri/src/backend.rs`) generates a 32-byte CSPRNG token
   per launch (owned by `BackendManager`; handed to React memory-only via a
   command after ready).
2. It spawns the `butly-backend` sidecar with:
   - args: `--port 0 --parent-pid <tauri_pid>`
   - env: `BUTLY_DESKTOP_TOKEN=<token>` (**environment variable only** — never
     CLI args, logs, or URLs)
3. After binding, the sidecar writes a single JSON line to stdout:

   ```json
   {"event":"listening","host":"127.0.0.1","port":49321,"pid":1234,"backend_version":"0.1.0","api_version":"v1"}
   ```

4. Tauri polls `GET /api/v1/health` (unauthenticated) on the reported port and
   compares `backend_version` / `api_version` against the expected values
   (`EXPECTED_BACKEND_VERSION` / `EXPECTED_API_VERSION` in `backend.rs`; the
   backend source of truth is `butly_api/version.py`).
5. If versions match, it calls `GET /api/v1/ready` with the same token to
   verify both authentication and runtime readiness.
6. Successful readiness produces `ready`; a version mismatch produces
   `version_mismatch`; token rejection or timeout produces `unavailable`.

## Sidecar CLI (`butly_api/server.py`)

| Argument | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Loopback-only in production. Non-loopback requires an explicit host and a configured token (and logs a warning). |
| `--port` | `0` | With 0 the OS assigns a port, reported via the listening JSON. |
| `--parent-pid` | none | Parent-death watch (psutil); fallback against leftover processes. |
| `--data-dir` | frozen: `%LOCALAPPDATA%\Butly` / dev: repo root | User data location. |
| `--dev-cors` | off | Adds Vite dev origins (localhost/127.0.0.1 on 1420 and 5173) to the CORS allowlist. |

- There is **no CLI argument for the token** (it would be visible in the
  process list).
- Legacy (Streamlit-compat) routers are not included; the only surface is
  `/api/v1`.
- `POST /api/v1/shutdown` (202): graceful shutdown. Being under `/api/v1` it
  requires the desktop token. It is intentionally excluded from the public
  OpenAPI snapshot.
- The production CORS allowlist is `http://tauri.localhost` only; no
  wildcard + credentials.

## Authentication

- When `BUTLY_DESKTOP_TOKEN` is set, `/api/v1/*` (except `/api/v1/health`)
  requires `Authorization: Bearer <token>` (`butly_api/auth.py`).
- React keeps the token in memory only (returned by the
  `get_connection_info` command). Never persisted to localStorage or files.
- When unset, auth is disabled (dev / Streamlit mode). Production launches via
  Tauri always set it.

## Lifecycle states (emitted to React as `backend-state`)

| phase | Meaning | UI |
|---|---|---|
| `starting` | spawn → health confirmed | starting indicator |
| `ready` | health 200 + versions match + authenticated readiness | connected + backend/API versions |
| `unavailable` | sidecar missing / health timeout | restart button |
| `crashed` | unexpected process exit | restart button |
| `version_mismatch` | version handshake failed | restart button + expected values |

- The restart command gracefully stops a running child first, then spawns
  exactly one new process; double-launch is prevented by the `starting` /
  child-presence checks.
- After lifecycle readiness, React continues to probe authenticated
  `/api/v1/ready` with a timeout. If HTTP becomes unreachable while the process
  remains alive, chat enters `disconnected` and offers reconnect or sidecar
  restart.
- On window close: `POST /api/v1/shutdown` → wait up to 5s → kill if still
  alive → the sidecar's own `--parent-pid` watch remains as the final
  fallback.
- Sidecar stdout/stderr goes to `%LOCALAPPDATA%\Butly\logs\backend.log`
  (rotated to `.log.1` at 5MB). The token is never logged.

## Development mode

- Manual backend: `venv/bin/python -m butly_api.server --dev-cors --port 8000`
- `--dev-cors` also enables sanitized chat-debug summaries. Set
  `BUTLY_DEVELOPER_MODE=1` to enable debug without expanding CORS. Production
  bundles keep it disabled by default.
- Attach Tauri without spawning: set `BUTLY_DEV_BACKEND_PORT=8000` (when using
  a token, set the same `BUTLY_DESKTOP_TOKEN` for both Tauri and the backend).
- Open in a plain browser without Tauri: set
  `BUTLY_DEV_BACKEND_URL=http://127.0.0.1:8010` for Vite. The dev server proxies
  `/api` to that sidecar, and React uses `DevBrowserBridge`
  (`frontend/src/lifecycle/bridge.ts`) against the same origin, deriving
  readiness from `GET /api/v1/health`. Same-origin means no CORS setup is needed
  (`--dev-cors` only matters for debug summaries). It **never handles a token**
  (this path is only for a loopback sidecar started without one). It also
  performs no process supervision and no version check, so sidecar spawn, token
  handling, crash recovery, and `version_mismatch` must be verified under Tauri.
  Production builds drop the entire path as dead code.
- `main.py` remains the compatibility entrypoint (legacy routers + wildcard
  CORS).
- Step-by-step instructions:
  [Desktop UI Startup](../guides/desktop_dev_setup.md).

## Packaging

- `python scripts/build_backend_sidecar.py` — builds with PyInstaller
  **one-folder** (default) and places
  `frontend/src-tauri/binaries/butly-backend-<target-triple>(.exe)` plus
  `frontend/src-tauri/resources/backend/_internal/`. `--onefile` switches to
  one-file (fallback if one-folder proves unstable inside the Tauri bundle;
  record the reason and startup time in this file when switching).
- Forbidden bundle contents (`.env`, real `user_config.json`,
  `user_prompts.json`, `butly_core/instances/`, ...) are checked by the build
  script; only `*.example` templates are bundled.
- `python scripts/smoke_test_sidecar.py` — verifies startup, 401 without /
  with wrong token, graceful shutdown, no leftover process, and records
  startup time.
- CI: `.github/workflows/windows-desktop.yml` runs PyInstaller build → smoke
  test → `pnpm tauri build` (NSIS) on Windows x64 and uploads the installer as
  an artifact. No release creation or code signing yet.

## Startup time log

| Date | mode | to listening | to health 200 | Notes |
|---|---|---|---|---|
| (fill in after first CI run) | one-folder | - | - | reported by the smoke test |
