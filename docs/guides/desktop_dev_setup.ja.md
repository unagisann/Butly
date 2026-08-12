# デスクトップ UI の起動手順（通常 / 開発）

[English](desktop_dev_setup.md) | **日本語**

正式デスクトップ Chat UI（Tauri + React）と legacy Streamlit の起動手順をまとめます。
UI を目で見ながら開発するときに、どのモードを選べばよいかもここで判断できます。

契約の仕様は [Desktop sidecar 仕様](../reference/desktop_sidecar.ja.md) と
[正式デスクトップ Chat UI](../reference/frontend_chat.ja.md) を参照してください。

## 前提

- Python venv 作成済み（`venv/bin/python`。Windows は `.venv\Scripts\python.exe`）
- Node.js 22 以上 + pnpm 10（frontend を触る場合のみ）
- `frontend/` で一度 `pnpm install --frozen-lockfile` を実行済み

---

## 通常の起動手順（利用する）

### 正式デスクトップ版（Windows）

installer（`.github/workflows/windows-desktop.yml` の CI artifact、または
`pnpm tauri build` の出力）を実行してインストールし、Butly を起動します。

sidecar の起動・port 割り当て・token 発行・終了はすべて Tauri が行うため、
利用者側で backend を起動する必要はありません。

### legacy Streamlit（評価画面・未移行の設定画面）

LoCoMo 評価や日本語 A/B などの評価機能は正式 UI へ移していないため、
Streamlit 側に残っています。

```bash
# ターミナル 1: backend（legacy routers 込みの互換 entrypoint）
venv/bin/python -m uvicorn main:app --port 8000 --reload

# ターミナル 2: Streamlit
venv/bin/python -m streamlit run app.py
```

ブラウザで `http://localhost:8501` を開きます。
Windows は `02_start_webui.bat` をダブルクリックすると両方が起動します。

---

## 開発時の起動手順（UI を見ながら直す）

用途によって 3 つのモードがあります。

| モード | 実行環境 | HMR | 確認できること | 確認できないこと |
|---|---|---|---|---|
| A. browser dev | Linux / Pi / 任意 | ○ | UI、実 backend との通信、SSE stream、i18n | Tauri shell の挙動すべて |
| B. Tauri dev | Windows（出荷ターゲット） | ○ | A に加えて sidecar spawn / token / crash 復帰 / version 判定 | installer の挙動 |
| C. installer smoke | Windows | × | 配布物そのもの（PyInstaller 同梱、インストール、初回起動） | — |

**普段の UI 作業は A、Tauri lifecycle に触れたときだけ B、リリース前に C** という
使い分けを想定しています。

### A. browser dev（Tauri なし）

sidecar を手動起動し、Vite dev server を素のブラウザで開きます。Tauri（WebKitGTK）を
必要としないので、Raspberry Pi のような headless 環境でも SSH port forward 経由で
手元 PC のブラウザから開発できます。

```bash
# ターミナル 1: sidecar（token 無し = 認証オフ、CORS は Vite origin のみ許可）
venv/bin/python -m butly_api.server --dev-cors --port 8000

# ターミナル 2: Vite dev server
cd frontend
VITE_BUTLY_DEV_BACKEND_URL=http://localhost:8000 pnpm dev
```

ブラウザで `http://localhost:1420` を開きます。

`VITE_BUTLY_DEV_BACKEND_URL` を設定すると、React が Tauri command の代わりに
このURLへ直接つなぐ dev 用 bridge（`frontend/src/lifecycle/bridge.ts` の
`DevBrowserBridge`）を選びます。`GET /api/v1/health` が通れば `ready` になり、
以降は通常どおり `/api/v1/ready` を polling します。

毎回指定するのが面倒なら `frontend/.env.development.local` に書けます（gitignore 済み）。

```
VITE_BUTLY_DEV_BACKEND_URL=http://localhost:8000
```

Windows PowerShell では
`$env:VITE_BUTLY_DEV_BACKEND_URL="http://localhost:8000"; pnpm dev` とします。

#### 別マシンのブラウザから開く（Pi 開発）

手元 PC から SSH port forward を張り、`localhost` として開きます。

```bash
# 手元 PC 側
ssh -L 1420:localhost:1420 -L 8000:localhost:8000 <pi-host>
# → ブラウザで http://localhost:1420
```

`--dev-cors` の allowlist は `localhost` / `127.0.0.1` の 1420・5173 に限定されており、
sidecar の bind も loopback 既定です。**`pnpm dev --host` で LAN に晒す運用は
CORS からも bind からも外れる**ため、port forward を使ってください。

#### 制約

- **token は扱いません。**`VITE_*` は bundle に inline されるため、この経路は
  `BUTLY_DESKTOP_TOKEN` を設定していない loopback sidecar だけを対象とします。
  token 認証込みで確認したいときはモード B を使います。
- backend の process 管理をしないため、UI の「再起動」ボタンは health 再確認に
  なります。sidecar 自体の再起動はターミナルで行ってください。
- version 期待値の正本は Rust 側なので、`version_mismatch` の判定は行いません。
- production build ではこの経路のコードごと削除されます（dead code elimination）。

### B. Tauri dev（Windows 実機）

出荷ターゲットは Windows です。Tauri shell の挙動を確認するときはこちらを使います。

```powershell
# ターミナル 1: sidecar
.venv\Scripts\python.exe -m butly_api.server --dev-cors --port 8000

# ターミナル 2: Tauri + React
cd frontend
$env:BUTLY_DEV_BACKEND_PORT="8000"; pnpm tauri dev
```

`BUTLY_DEV_BACKEND_PORT` を設定すると、Tauri は sidecar を spawn せず、
起動済みの backend に health probe だけを行います。token 認証も確認したい場合は
Tauri と backend の**両方**に同じ `BUTLY_DESKTOP_TOKEN` を設定します。

spawn 自体（production と同じ経路）を確認したい場合は、環境変数を外したうえで
先に sidecar を build します。

```powershell
.venv\Scripts\python.exe scripts\build_backend_sidecar.py
cd frontend
pnpm tauri dev
```

### C. installer smoke（Windows）

`.github/workflows/windows-desktop.yml` が push / PR ごとに PyInstaller build →
smoke test → NSIS installer を artifact として生成します。Windows 側に開発環境を
用意しなくても、artifact を落としてインストールすれば配布物を確認できます。
HMR は無いので UI 調整用ではなく、最終確認向けです。

### Raspberry Pi で Tauri を動かすことについて

aarch64 Linux 向け build 自体は可能ですが、WebKitGTK 一式とデスクトップセッション
（X / Wayland）が必要になり、X 転送では GL 周りが実用になりません。加えて描画は
出荷ターゲットの Windows WebView2 とは別物です。**Pi では A、Tauri 固有の確認は
Windows で B**、という分担を推奨します。

---

## 停止

- Vite dev server / sidecar: 各ターミナルで `Ctrl+C`
- Tauri dev: ウィンドウを閉じる（sidecar は Tauri が終了させます）

## よくある詰まり

| 症状 | 原因 | 対処 |
|---|---|---|
| 画面が `not_running_in_tauri` のまま | browser 実行で `VITE_BUTLY_DEV_BACKEND_URL` 未設定 | env を付けて `pnpm dev` を再起動（dev server は env 変更を自動で拾いません） |
| `dev_health_failed_TypeError` | sidecar 未起動 / port 違い / CORS 拒否 | sidecar のログと port、ブラウザの console を確認 |
| `dev_health_http_401` | sidecar 側に `BUTLY_DESKTOP_TOKEN` が設定されている | browser dev では token 無しで起動する（またはモード B） |
| CORS エラー | `localhost:1420` 以外の origin で開いた | port forward で `localhost:1420` として開く |
| `version_mismatch`（Tauri） | `butly_api/version.py` と `backend.rs` の期待値がずれた | 両方を合わせる |
| debug panel が出ない | developer mode 無効 | `--dev-cors` で起動する、または `BUTLY_DEVELOPER_MODE=1` |

## push 前チェック

```bash
./scripts/check_before_push.sh          # backend（compileall / flake8 / pytest / pip check）
cd frontend && pnpm lint && pnpm typecheck && pnpm test
```
