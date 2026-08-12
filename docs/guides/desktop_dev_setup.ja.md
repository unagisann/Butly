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
# ターミナル 1: sidecar（token 無し = 認証オフ）
venv/bin/python -m butly_api.server --port 8010

# ターミナル 2: Vite dev server
cd frontend
BUTLY_DEV_BACKEND_URL=http://127.0.0.1:8010 pnpm dev
```

ブラウザで `http://127.0.0.1:1420` を開きます。

`BUTLY_DEV_BACKEND_URL` は **Vite dev server が `/api` を proxy する先**です。
browser から見ると backend は同一 origin になるので、React は Tauri command の
代わりに同一 origin へつなぐ dev 用 bridge（`frontend/src/lifecycle/bridge.ts` の
`DevBrowserBridge`）を使います。`GET /api/v1/health` が通れば `ready` になり、
以降は通常どおり `/api/v1/ready` を polling します。SSE も proxy を素通しするので、
stream / cancel / 再送はそのまま確認できます。

同一 origin になることで、次が同時に不要になります。

- **CORS 設定**（`--dev-cors`）。ただし `--dev-cors` は sanitized な Chat debug
  summary も有効にするので、debug パネルを見たいときは付けるか
  `BUTLY_DEVELOPER_MODE=1` を指定してください。
- **backend port の port forward**。転送するのは 1420 の 1 本だけです。

port 8000 は legacy Streamlit の backend が使うので、併用するなら例のように
別 port（8010 など）にしてください。

毎回指定するのが面倒なら `frontend/.env.development.local` に書けます（gitignore 済み）。

```
BUTLY_DEV_BACKEND_URL=http://127.0.0.1:8010
```

Windows PowerShell では
`$env:BUTLY_DEV_BACKEND_URL="http://127.0.0.1:8010"; pnpm dev` とします。

未設定のまま browser で開くと proxy が無いので、画面に
`dev_backend_not_configured` が出ます（設定漏れとして区別できます）。

#### 別マシンのブラウザから開く（Pi 開発）

手元 PC から SSH port forward を張ります。転送するのは dev server だけです。

```bash
# 手元 PC 側
ssh -L 1420:127.0.0.1:1420 <pi-host>
# → ブラウザで http://127.0.0.1:1420
```

dev server は IPv4 loopback（`127.0.0.1`）に bind します。既定の `localhost` は
環境によって `::1` に解決され、`ssh -L` の転送先と食い違って繋がらないことが
あるためです。**`pnpm dev --host` で LAN に晒す運用は bind の前提から外れる**ので、
port forward を使ってください。

#### 制約

- **token は扱いません。**この経路は `BUTLY_DESKTOP_TOKEN` を設定していない
  loopback sidecar だけを対象とします。token 認証込みで確認したいときはモード B
  を使います。
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
| `dev_backend_not_configured` | `BUTLY_DEV_BACKEND_URL` 未設定で proxy が無い | env を付けて `pnpm dev` を再起動（dev server は env 変更も vite.config も再起動時にしか読みません） |
| `dev_health_failed_TypeError` | dev server 自体に届いていない（port forward 切れなど） | `curl http://127.0.0.1:1420/api/v1/health` を Pi 上と手元の両方で試す |
| `dev_health_http_502` / `504` | proxy 先の sidecar が落ちている / port 違い | sidecar のログと `BUTLY_DEV_BACKEND_URL` の port を確認 |
| `dev_health_http_401` | sidecar 側に `BUTLY_DESKTOP_TOKEN` が設定されている | browser dev では token 無しで起動する（またはモード B） |
| ブラウザからだけ繋がらない | `ssh -L` の転送先が `::1` に解決されている | `-L 1420:127.0.0.1:1420` と IPv4 で明示する |
| `version_mismatch`（Tauri） | `butly_api/version.py` と `backend.rs` の期待値がずれた | 両方を合わせる |
| debug panel が出ない | developer mode 無効 | `--dev-cors` で起動する、または `BUTLY_DEVELOPER_MODE=1` |
| `port 8000 is already in use` | legacy Streamlit の backend と衝突 | sidecar を別 port（8010 など）で起動する |

## push 前チェック

```bash
./scripts/check_before_push.sh          # backend（compileall / flake8 / pytest / pip check）
cd frontend && pnpm lint && pnpm typecheck && pnpm test
```
