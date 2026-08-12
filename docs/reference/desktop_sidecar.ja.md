# Desktop sidecar 仕様（Phase 1）

🌐 **日本語** | [English](desktop_sidecar.md)

> Tauri shell と FastAPI sidecar（`butly_api/server.py`）の間の process / 認証 /
> lifecycle 契約。設計の背景は
> [frontend_migration_plan.ja.md](../planning/active/frontend_migration_plan.ja.md) §6。

## 起動シーケンス

1. Tauri（`frontend/src-tauri/src/backend.rs`）が起動ごとに 32 byte の
   CSPRNG token を生成する（`BackendManager` が所有。React へは ready 後に
   command 経由で memory 渡しのみ）。
2. sidecar `butly-backend` を次で spawn する。
   - args: `--port 0 --parent-pid <tauri_pid>`
   - env: `BUTLY_DESKTOP_TOKEN=<token>`（**環境変数のみ**。CLI 引数・log・URL に出さない）
3. sidecar は bind 完了後、stdout に 1 行 JSON を書く。

   ```json
   {"event":"listening","host":"127.0.0.1","port":49321,"pid":1234,"backend_version":"0.1.0","api_version":"v1"}
   ```

4. Tauri は通知された port の `GET /api/v1/health`（無認証）を polling し、
   `backend_version` / `api_version` を期待値
   （`backend.rs` の `EXPECTED_BACKEND_VERSION` / `EXPECTED_API_VERSION`、
   backend 側の正本は `butly_api/version.py`）と比較する。
5. version が一致したら、同じ token で `GET /api/v1/ready` を呼び、
   認証と runtime readiness の両方を確認する。
6. readiness も通れば `ready`、version 不一致なら `version_mismatch`、
   token 拒否や timeout なら `unavailable` を React へ通知する。

## sidecar CLI（`butly_api/server.py`）

| 引数 | 既定 | 説明 |
|---|---|---|
| `--host` | `127.0.0.1` | production は loopback 固定。非 loopback は明示指定かつ token 設定時のみ（警告を出す） |
| `--port` | `0` | 0 なら OS が割り当て、実 port を listening JSON で通知 |
| `--parent-pid` | なし | 親 process 死亡監視（psutil）。残存 process 防止の fallback |
| `--data-dir` | frozen: `%LOCALAPPDATA%\Butly` / 開発: repo root | ユーザーデータの場所 |
| `--dev-cors` | off | Vite dev origin（localhost/127.0.0.1 の 1420・5173）を CORS allowlist へ追加 |

- token 用の CLI 引数は**存在しない**（process list に露出するため）。
- legacy routers（Streamlit 互換 route）は含まない。公開surface は `/api/v1` のみ。
- `POST /api/v1/shutdown`（202）: graceful shutdown 用。`/api/v1` 配下なので
  desktop token 必須。OpenAPI snapshot（公開 contract）には含めない。
- production CORS allowlist は `http://tauri.localhost` のみ。
  wildcard + credentials は使わない。

## 認証

- `BUTLY_DESKTOP_TOKEN` 設定時、`/api/v1/*`（`/api/v1/health` 除く）は
  `Authorization: Bearer <token>` 必須（`butly_api/auth.py`）。
- token は React では memory のみ（`get_connection_info` command の返り値）。
  localStorage / config file への保存禁止。
- 未設定時は認証無効（開発 / Streamlit 併用モード）。Tauri 経由の production
  起動では必ず設定される。

## lifecycle 状態（React へ `backend-state` event で通知）

| phase | 意味 | UI |
|---|---|---|
| `starting` | spawn 〜 health 確認まで | 起動中表示 |
| `ready` | health 200 + version 一致 + token 付き readiness 成功 | 接続済み + backend/API version 表示 |
| `unavailable` | sidecar 不在 / health timeout | 再起動ボタン |
| `crashed` | 予期しない process 終了 | 再起動ボタン |
| `version_mismatch` | version handshake 不一致 | 再起動ボタン + 期待値表示 |

- restart command は「稼働中 child を graceful 停止 → 1 process だけ再 spawn」。
  二重起動は `starting` / `child` 存在チェックで防ぐ。
- React は lifecycle の `ready` 後も認証付き `/api/v1/ready` を timeout 付きで
  定期確認する。process が生存していても HTTP が到達不能なら `disconnected` として
  chat を止め、手動再接続または sidecar restart を案内する。
- window 終了時: `POST /api/v1/shutdown` → 5 秒待って残っていれば kill →
  それでも残る場合は sidecar 側の `--parent-pid` 監視が最終 fallback。
- sidecar の stdout/stderr は `%LOCALAPPDATA%\Butly\logs\backend.log` に保存
  （5MB で `.log.1` へ退避）。token は記録されない。

## 開発モード

- backend 手動起動: `venv/bin/python -m butly_api.server --dev-cors --port 8000`
- `--dev-cors` は sanitized Chat debug summary も有効にする。CORS を広げず debug だけ
  有効にする場合は `BUTLY_DEVELOPER_MODE=1` を指定する。production bundle は既定で無効。
- Tauri を spawn なしで繋ぐ場合: `BUTLY_DEV_BACKEND_PORT=8000`（token を使うなら
  Tauri と backend の両方に同じ `BUTLY_DESKTOP_TOKEN` を設定）。
- `main.py` は従来どおりの互換 entrypoint（legacy routers + wildcard CORS）。

## packaging

- `python scripts/build_backend_sidecar.py` — PyInstaller **one-folder**（既定）で
  build し、`frontend/src-tauri/binaries/butly-backend-<target-triple>(.exe)` と
  `frontend/src-tauri/resources/backend/_internal/` へ配置する。
  `--onefile` で one-file へ切替可能（one-folder が Tauri 同梱で不安定な場合の
  代替。切替時は理由と起動時間をこのファイルに記録する）。
- bundle 禁止物（`.env` / 実 `user_config.json` / `user_prompts.json` /
  `butly_core/instances/` 等）は build script が混入検査する。雛形は
  `*.example` のみ同梱。
- `python scripts/smoke_test_sidecar.py` — build 済み exe の起動 / token 401 /
  graceful shutdown / process 残存なし / 起動時間計測。
- CI: `.github/workflows/windows-desktop.yml` が Windows x64 で
  PyInstaller build → smoke test → `pnpm tauri build`（NSIS）→ installer を
  artifact upload する。release 作成・署名はまだ行わない。

## 起動時間の記録

| 日付 | mode | listening まで | health 200 まで | 備考 |
|---|---|---|---|---|
| （CI 初回実行後に記入） | one-folder | - | - | smoke test が出力する |
