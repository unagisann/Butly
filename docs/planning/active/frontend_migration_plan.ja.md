# 正式フロントエンド移行計画: Streamlit から Windows デスクトップアプリへ

> **ステータス: Phase 1 実装済み（2026-07-09、CI での Windows 検証待ち）。次は Phase 2（Chat vertical slice）。**
> 段階移行を前提に `docs/planning/active/` で管理し、§18 の完了条件を満たしたら
> archived へ移す。
>
> Phase 0 実装済み: `butly_api/` app factory（`create_app()`）、
> `/api/v1/health` `/ready` `/app-info` `/capabilities`、`ApiError` envelope +
> request ID、OpenAPI 3.1 snapshot（`openapi/butly.openapi.json` +
> `scripts/generate_openapi.py`）、typed instance list（`GET /api/v1/instances`）、
> typed history（`GET /api/v1/instances/{name}/messages`）、typed chat
> （`POST /api/v1/chat` / `/api/v1/chat/stream`、SSE discriminated union）、
> desktop token auth（`butly_api/auth.py`、`BUTLY_DESKTOP_TOKEN` 設定時のみ強制）、
> SSE parser contract fixture（`openapi/sse_fixtures/` +
> `scripts/generate_sse_fixture.py`）、Streamlit の instance 一覧 / 履歴の
> 新 API 利用（`app.py` の `INSTANCES_DIR.iterdir()` /
> `load_recent_sessions()` 直読みを撤去）。
> Phase 0 の残置（後続 Phase で対応）: instance-templates API（Phase 3）、
> Streamlit chat 送信の legacy route 利用（新 UI が primary になるため移行対象外）、
> `os.environ` の Web 検索 key 判定（capabilities 拡張時）。
>
> 起票: 2026-06-23 / 最初の対象 OS: **Windows 10 / 11 (x64)** /
> 想定スパン: 段階的、Streamlit と並行稼働しながら移行
>
> 関連計画:
> [pydantic-settings 設定統合計画](pydantic_settings_plan.ja.md) /
> [記憶ストア正規化計画](memory_store_normalization_plan.ja.md)

## 0. 結論（採用方針）

正式フロントエンドは、次の構成を第一選択として実装する。

- **デスクトップシェル:** Tauri v2
- **UI:** React + TypeScript + Vite
- **バックエンド:** 既存 FastAPI を維持し、PyInstaller で単体実行ファイル化して
  Tauri の sidecar として同梱
- **通信:** `127.0.0.1` 上の versioned REST API + POST SSE
- **契約:** FastAPI が生成する OpenAPI 3.1 を唯一の正とし、TypeScript client を自動生成
- **データ:** `%LOCALAPPDATA%\Butly` に保存。UI はデータファイルを直接読まない
- **移行:** Streamlit は機能比較用の参照実装として残し、画面単位で置き換える
- **初期リリース:** Windows 10 / 11 x64。macOS / Linux / Web / mobile は今すぐ配布対象にしないが、
  transport と API を分離して将来のクライアント追加を妨げない

Tauri は任意の Web frontend と組み合わせられ、React / TypeScript の公式テンプレートを持つ。
また、PyInstaller で固めた Python API server は sidecar の代表的用途として公式に案内されている。

- [Tauri: Create a Project](https://v2.tauri.app/start/create-project/)
- [Tauri: Embedding External Binaries](https://v2.tauri.app/develop/sidecar/)

既存 [main.py](../../../main.py) には `--parent-pid` と親プロセス監視があり、
デスクトップ shell が backend を子プロセスとして所有する構成と整合する。ただし、
現在の `argparse` import-time 実行、固定/不一致ポート、`0.0.0.0` bind、CORS 全許可は
正式版向けに整理する必要がある（§6）。

---

## 1. なぜ移行するか

Streamlit 版は機能検証と backend 分離を進める役割を十分に果たした。一方、正式アプリとしては
次の制約が残る。

1. **UI lifecycle の制御が弱い**: rerun と `st.session_state` が画面遷移、入力、SSE、
   long-running job の状態を兼ね、キャンセルや復帰の意味が曖昧。
2. **frontend / backend 境界が未完成**: [app.py](../../../app.py) は FastAPI を使う一方、
   `ButlyMemory` / `ButlyBrain` / `ButlyChronos` / `InstanceManager` を直接 import し、
   instance directory、環境変数、prompt template も直接読む。
3. **配布が開発環境前提**: Python、venv、Streamlit、FastAPI を別々に起動する必要があり、
   単一 installer、process supervision、update、crash recovery がない。
4. **API 契約が暗黙的**: request model は一部あるが、response model が付く endpoint は
   実質 `POST /chat` だけ。多くが生の dict/list で、frontend と backend のずれを build 時に検出できない。
5. **ローカル API の防御不足**: direct 起動では `0.0.0.0` bind、CORS wildcard + credentials、
   request authentication なし。個人用でも、同一端末の別プロセスや LAN からの誤操作を防ぐ境界が必要。
6. **正式UIに必要なデータがAPIから欠落**: 現行 `/history/{instance_name}` は
   timestamp、最終対話時刻などを落とすため、Streamlit は memory を直読みして補っている。

本計画は単なる UI の書き換えではなく、**Butly の frontend boundary を正式な製品境界にする**ことを目的とする。

---

## 2. ゴール / 非ゴール

### 2.1 ゴール

- Windows installer から起動し、利用者が Python / Node / Rust を別途導入せず使える。
- Tauri が FastAPI sidecar を起動・監視・終了し、backend crash を UI で検知できる。
- frontend は公開 API 以外から Butly のファイル、環境変数、Python module を読まない。
- REST の request / response / error は Pydantic model で型付けされ、OpenAPI に完全に現れる。
- TypeScript API client と DTO は OpenAPI から生成し、手書き型との二重管理をしない。
- チャット、履歴、画像添付、Gemini Google Search、SSE、設定、Sleeptime、DB browser、
  pairing の現行機能を段階的に移植する。
- Streamlit と新UIが同じ backend API を使い、移行中も比較・回帰確認できる。
- API transport を分離し、将来の macOS / Linux / Web / remote backend client を妨げない。
- 新UI導入を理由に `ButlyRuntime` / `ChatService` / provider / memory の内部責務を frontend へ漏らさない。

### 2.2 非ゴール

- 初回リリースで mobile app を出さない。
- 初回リリースで cloud multi-user service にしない。
- Gemini / OpenAI / xAI / Ollama の provider 実装を frontend へ移さない。
- 記憶ファイルを一括 DB 化しない。記憶の保存形式は記憶ストア正規化計画の責務とする。
- 新UIのために全 backend を一括リライトしない。
- 初回から dashboard / Fire TV / discovery / news の全機能を磨き込まない。
- OpenAPI だけで SSE の runtime parser を自動生成できるとはみなさない（§9.6）。

---

## 3. 現行コードの棚卸し

### 3.1 現行構成の強み

- `ButlyRuntime` が chat 実行の入口となり、FastAPI、Discord、LINE から共有されている。
- `ChatService.execute()` / `execute_stream()` に REST / SSE の主要ロジックが集約されている。
- provider は `connection + model_name` で解決され、Gemini 固有処理は backend 内に閉じている。
- instance CRUD、設定、Sleeptime、knowledge card、pairing には既存 API がある。
- `/chat/stream` の event 順序と provider streaming には既存テストがある。
- API key は現在も backend の `.env` / environment にあり、browser JavaScript へ渡していない。

このため、正式UIへの移行で core logic を作り直す必要はない。主作業は **API contract の完成、
process packaging、UI の再実装**である。

### 3.2 Streamlit 画面一覧と移行先

| 現行画面 / 機能 | 主な責務 | 新UIでの移行先 | 優先度 |
|---|---|---|---|
| Home | instance 一覧・作成、DB / pairing / settings 入口 | `/instances`, `/instances/new` | P1 |
| Chat | 履歴、SSE/REST、画像、検索 toggle、debug | `/instances/:name/chat` | **P0** |
| Global Settings / Basic | 接続先、holiday、debug、streaming、locale | `/settings/general` | P2 |
| Global Prompts | prompt 一覧・編集 | `/settings/prompts` | P3 |
| LLM Providers | API key、connection、model、temperature、reindex | `/settings/providers` | P2 |
| Pairing | LINE pairing 承認 / 却下 | `/integrations/pairing` | P3 |
| Sleeptime | estimate、run、progress polling | `/instances/:name/sleeptime` | P2 |
| Database Browser | filter / search / card list | `/instances/:name/memories` | P2 |
| Card Edit | edit / pin / delete | `/instances/:name/memories/:id` | P2 |
| Instance Settings / Basic | profile、prompts、model overrides、readable instances | `/instances/:name/settings` | P1 |
| Instance Settings / Advanced | RAG、memory、Sleeptime、context levels、glossary、rename | 同上の subroutes / tabs | P2-P3 |
| Onboarding | 初回 instance 作成 | `/welcome` wizard | P1 |

`render_onboarding_screen()` は現在の main routing から呼ばれず、instance がない場合も Home の
作成 UI が使われる。正式版では Home の作成 form と統合した単一 onboarding wizard にする。

### 3.3 frontend が直接触っている backend 内部（撤去対象）

| 現行の直結 | 用途 | 正式な置換 |
|---|---|---|
| `INSTANCES_DIR.iterdir()` | instance 一覧 / 再走査 | typed `GET /api/v1/instances` |
| `ButlyMemory.load_recent_sessions()` | chat history + timestamp | typed message history API |
| `ButlyMemory.get_last_interaction_time()` | Chat の時間表示 | history response の `last_interaction_at` |
| `ButlyChronos.get_system_note()` | UI debug 用（現在は表示無効） | UIから削除。生成時刻 context は backend のみ |
| `ButlyBrain` initialization | 実質未使用 | UIから完全削除 |
| `InstanceManager` initialization | 実質未使用 / legacy | UIから完全削除 |
| prompt template directory scan | instance 作成 template | `GET /api/v1/instance-templates` |
| `model_registry` fallback import | model candidate fallback | backend `model-candidates` のみ。offline cache は client DTO cache |
| `AI_CONFIG` direct import | instance settings の global default 表示 | settings API の `effective` / `inherited_from` |
| `_migrate_legacy_agent()` | config 表示前の in-memory migration | backend repository / schema migration 内で完結 |
| `DEFAULT_CONTEXT_ORDER` / presets import | context levels UI | metadata endpoint から options / defaults を返す |
| `os.environ` | Web Search key 有無判定 | `/capabilities` または provider status |
| `ButlyDatabase` import | DB browser（現状 import は不要） | knowledge-card API のみ |
| Streamlit cache/session | server state と UI state の混在 | query cache + route state + component state に分離 |

### 3.4 現行 API の主な契約上の問題

1. response model がほぼないため OpenAPI schema が `{}` / `any` になる。
2. `/config` と instance config が巨大な生 dict の全置換で、未知フィールドの消失や concurrent update を検出できない。
3. `/history/{instance_name}` は message timestamp と `last_interaction_at` を返さない。
4. chat の REST DTO と内部 DTO で `message` / `response` と `text` が分かれている。
5. `attachments: List[Dict[str, Any]]` のため添付仕様が OpenAPI に現れない。
6. SSE `metadata` / `done` / `error` payload が Pydantic model ではない。
7. error が `detail: str`、成功 body 内 `status: error`、chat body 内 `Error: ...` に分散している。
8. cards list は配列のみで、total / next cursor がない。さらに list query は `episode` を返さないのに
   Streamlit は一覧で `episode` を表示しようとしている。
9. pin endpoint は `Key_Memory.txt` へ追記し、unpin で戻さない。現行の YAML 正本化計画とも衝突する。
10. Sleeptime / embedding reindex は process memory の状態または fire-and-forget で、共通 job ID がない。
11. `/settings` は現在空の placeholder、`/config` が実際の設定 API になっている。
12. `/ws` は全 client broadcast で request correlation がなく、正式 chat transport には向かない。

---

## 4. 技術選定

### 4.1 採用: Tauri v2 + React + TypeScript + Vite

| 観点 | 評価 |
|---|---|
| Windows installer | Tauri は NSIS `.exe` / WiX `.msi` を生成可能 |
| Python backend 同梱 | PyInstaller binary を sidecar として公式に扱える |
| OpenAPI | TypeScript client generator の選択肢が多い |
| SSE | WebView2 の `fetch()` + `ReadableStream` で POST SSE を読める |
| UI 開発 | Chat、form、table、routing、accessibility の Web ecosystem を使える |
| 将来 Web | Tauri adapter を外せば React UI と API client を再利用しやすい |
| 将来他OS | Tauri自体は cross-platform。backend bundle だけ target ごとに用意できる |
| binary size / startup | Chromium 本体を同梱せず Windows WebView2 を使うため比較的軽い |

Windows では Tauri が WebView2 を利用する。Windows installer は不足時の WebView2 導入方法を選べる。
初期リリースは Windows 10 / 11 を対象とし、通常 installer では system WebView2 を利用する。
完全 offline installer が必要になった時だけ WebView2 offline bundle を選ぶ。

- [Tauri: Windows Installer](https://v2.tauri.app/distribute/windows-installer/)
- [Tauri: Webview Versions](https://v2.tauri.app/reference/webview-versions/)

### 4.2 Flutter を今回は採用しない理由

[main.py](../../../main.py) の help text に Flutter の記述はあるが、repository に Dart / Flutter code、
`pubspec.yaml`、widget、Flutter-specific bridge は存在しない。したがって既存投資を捨てる判断にはならない。

Flutter は Windows / macOS / Linux desktop を正式サポートしており、mobile-first を最優先にするなら有力である。
ただし現在の Butly は local Python sidecar、複雑な settings form、OpenAPI client、POST SSE が中心であり、
Windows-first の開発速度と Web 版への派生を優先して Tauri + React を選ぶ。

- [Flutter: Desktop support](https://docs.flutter.dev/platform-integration/desktop)

将来 Flutter client を追加しても、OpenAPI contract に従う限り backend の変更は不要である。

### 4.3 frontend directory（提案）

```text
frontend/
├── package.json
├── pnpm-lock.yaml
├── vite.config.ts
├── src/
│   ├── app/                 # route, provider, error boundary
│   ├── api/
│   │   ├── generated/       # OpenAPI 生成物。手編集禁止
│   │   ├── client.ts        # base URL / auth / error normalization
│   │   ├── sse.ts           # POST SSE parser（手書き、schema は共有）
│   │   └── transport.ts     # local / remote を隠す interface
│   ├── features/
│   │   ├── chat/
│   │   ├── instances/
│   │   ├── settings/
│   │   ├── memories/
│   │   ├── sleeptime/
│   │   └── integrations/
│   ├── components/          # domain 非依存 UI
│   ├── i18n/
│   └── styles/
├── src-tauri/
│   ├── capabilities/
│   ├── binaries/            # build 時に backend sidecar を配置（生成物は原則 ignore）
│   ├── src/
│   │   ├── backend.rs       # spawn / readiness / shutdown / logs
│   │   └── lib.rs
│   └── tauri.conf.json
└── tests/
```

server state は query cache、route identity は router、入力途中の値は component/form state、
app lifecycle（backend ready / crashed / version mismatch）は app-level state に分ける。
単一の巨大 global store に全状態を集めない。

---

## 5. 目標アーキテクチャ

```text
┌─────────────────────────────────────────────────────────────┐
│ Butly Desktop (Tauri)                                       │
│                                                             │
│  React UI ── ApiTransport ── generated REST client           │
│      │                  └── hand-written typed SSE client    │
│      │                                                      │
│      └── Tauri lifecycle bridge (ready/crash/restart/log)    │
└──────────────────────────┬──────────────────────────────────┘
                           │ 127.0.0.1:<ephemeral>
                           │ Bearer <per-launch-token>
┌──────────────────────────▼──────────────────────────────────┐
│ FastAPI sidecar                                             │
│  /api/v1/* ─ routers ─ application services ─ ButlyRuntime  │
│  /integrations/* ─ signature-authenticated webhooks         │
│                          │                                  │
│          provider / memory / settings / SQLite              │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    %LOCALAPPDATA%\Butly
```

### 5.1 境界の原則

- React は localhost API と Tauri lifecycle bridge だけを知る。
- Tauri Rust は backend process の起動・終了・ready signal と OS integration だけを知る。
- FastAPI router は HTTP の変換を担い、業務ロジックは Runtime / service / repository に委譲する。
- provider-specific data は backend で共通 DTO に正規化する。
- frontend は Gemini SDK / OpenAI SDK を導入しない。
- file path を API resource ID として返さない。
- secret value、full prompt、raw debug log は通常 response に含めない。
- Web / remote backend を後から足せるよう、`ApiTransport` は Tauri API を直接 import しない。

---

## 6. Windows process / 配布設計

### 6.1 起動シーケンス

1. Tauri main process が `%LOCALAPPDATA%\Butly` と log directory を準備。
2. cryptographically secure な per-launch token を生成。
3. loopback の空き port を選び、backend sidecar に次を渡す。
   - `--host 127.0.0.1`
   - `--port <selected>`
   - `--parent-pid <tauri_pid>`
   - `--data-dir <LOCALAPPDATA path>`
   - `--auth-token-env BUTLY_DESKTOP_TOKEN`（token 本体は環境変数で渡す）
4. Tauri が `GET /api/v1/health` を短い interval で polling。
5. health response の `api_version` / `backend_version` を検証。
6. ready 後に React app へ base URL と token を memory-only で渡す。
7. UI close 時に graceful shutdown を試み、期限後に child process を terminate。

token を command-line argument に直接置くと process list に見えるため、環境変数または親子 pipe を使う。
localStorage / config file には保存しない。

### 6.2 backend app factory 化

現在の [main.py](../../../main.py) は import 時に argument parse、directory 作成、env load、Runtime 生成、
router include、startup settings 適用を行う。正式版と OpenAPI generation/test のため、概ね次に分ける。

```text
butly_api/
├── app.py          # create_app(runtime, server_settings) -> FastAPI
├── auth.py         # desktop token / loopback guard
├── errors.py       # ApiError handler
├── schemas/        # transport DTO
└── server.py       # CLI parse + uvicorn entry
```

`main.py` は互換 entrypoint として薄く残してよい。`create_app()` は user data や実APIへアクセスせず
OpenAPI schema を生成できることを必須にする。

### 6.3 bind / CORS / CSP

- production sidecar は必ず `127.0.0.1` に bind。`0.0.0.0` は明示的な developer / remote mode のみ。
- internal `/api/v1/*` は bearer token 必須。起動監視用の `/api/v1/health` だけを無認証にする場合も、
  status とversion以外の情報を返さない。`/ready`, `/app-info`, `/capabilities` は認証対象にする。
- CORS は Tauri production origin と Vite development origin の allowlist。wildcard + credentials は撤去。
- Tauri CSP の `connect-src` は利用中の loopback URL のみに絞る。
- Tauri capabilities は sidecar spawn と必要最小限の window / dialog 権限だけを許可する。
- `/line/webhook` は desktop token の対象外とし、現行どおり LINE signature で認証する。
- pairing management は loopback guard **かつ** desktop token の二重条件にする。

Tauri の capability は window / webview ごとに許可を限定できるため、`shell:*` や `fs:*` の
広い default permission を main webview に与えない。

- [Tauri: Capabilities](https://v2.tauri.app/security/capabilities/)
- [FastAPI: CORS](https://fastapi.tiangolo.com/tutorial/cors/)

### 6.4 port 方針

現状は `main.py --port` の default が `48266`、Streamlit と README / batch は `8000` で不一致。

- production: Tauri が選ぶ ephemeral port。値は process memory のみ。
- development: FastAPI `8000`、Vite `5173` を既定としてよい。
- legacy Streamlit: 移行中は `8000` を維持。
- remote mode: 将来別 profile として HTTPS URL を保存。production local mode と混在させない。

port 選択から child bind までの race は Windows spike で検証する。必要なら backend が port `0` で bind し、
実 port を JSON ready line / rendezvous file で親へ通知する方式へ移る。

### 6.5 packaging / installer

- backend: PyInstaller one-folder を第一候補にする（one-file の展開時間・AV誤検知を spike 比較）。
- Tauri bundle: 初期は NSIS `-setup.exe`。CI で Windows x64 build。
- Python / prompt / template / migration asset と user-writable data を分離する。
- first-run で `.example` から user config を生成する処理は backend が所有する。
- installer / executable の code signing は一般配布前の release gate。
- auto updater は最初の内部版を安定させた後に追加し、backend と frontend を常に同一 bundle version で配る。
- backend stdout/stderr は rotating log に保存し、UIから「ログフォルダを開く」を提供する。
- single-instance lock を使い、同じ data directory に複数 backend を立ち上げない。

---

## 7. API化の優先原則

1. **resource-oriented `/api/v1`** を新設し、既存 route は Streamlit 互換 adapter とする。
2. request / response / error すべて Pydantic model を持つ。
3. wire format は当面 `snake_case` を維持し、Python と TypeScript の alias 二重管理を避ける。
4. datetime は timezone 付き ISO 8601。新規保存は UTC、表示時に frontend が local timezone へ変換。
5. enum 候補や limit を schema に明記する。
6. list は将来件数が増えるものだけ pagination envelope を使う。
7. destructive action は明示 endpoint + UI confirmation。HTTP body の文字列一致を認可代わりにしない。
8. secret は write-only。status は `configured: bool` と `last_validated_at` のみ。
9. provider / Gemini SDK の raw object を response に出さず、Butly DTO へ normalize する。
10. full config の read-modify-write を避け、section PATCH または typed command endpoint に分ける。
11. long-running operation は共通 Job resource に寄せる。
12. Streamlit 直読みを API fallback で温存しない。backend 到達不能は明示的な offline/error state とする。

---

## 8. API化リスト（詳細）

以下は **正式 frontend contract** として必要な一覧である。path は最終案であり、実装時の naming review は許容するが、
resource と DTO の責務は維持する。

### 8.1 Runtime / application

| Method / Path | Request | Response（主フィールド） | 用途 / 現状との差 |
|---|---|---|---|
| `GET /api/v1/health` | なし | `status`, `backend_version`, `api_version` | process 起動確認。外部APIやDBを叩かず、無認証でもよい最小応答 |
| `GET /api/v1/ready` | なし | `ready`, `checks[]`, `data_dir_writable`, `settings_loaded` | UIを開いてよいか。Gemini key の有無は readiness failure にしない |
| `GET /api/v1/app-info` | なし | version、locale、platform、feature flags | frontend/backend version handshake |
| `GET /api/v1/capabilities` | なし | chat/search/vision/integrations/devices の availability と理由 | `os.environ` や model prefix 判定をUIから撤去 |

`/status` の CPU / memory / network は app readiness と分離し、必要なら
`GET /api/v1/system/metrics` として developer feature にする。

### 8.2 Instance / onboarding

| Method / Path | Request | Response | 用途 / 注意 |
|---|---|---|---|
| `GET /api/v1/instances` | optional query | `items: InstanceSummary[]` | 文字列配列をやめ、`name`, `ai_name`, `locale`, `updated_at`, `has_database` を返す |
| `POST /api/v1/instances` | `CreateInstanceRequest` | `201 InstanceDetail` | template IDまたはcustom text、profilesを型付け |
| `GET /api/v1/instances/{name}` | なし | `InstanceDetail` | Home / header 用。存在しない時404 |
| `POST /api/v1/instances/{name}/rename` | `new_name` | 新 `InstanceDetail` | 現行 semantics 維持。将来 stable ID 導入余地を残す |
| `DELETE /api/v1/instances/{name}` | なし | `204` | 全記憶削除。frontend confirmation 必須 |
| `POST /api/v1/instances/{name}/session/reset` | なし | `SessionState` または `204` | 現行 `/reload` を意味の明確な名前へ |
| `GET /api/v1/instance-templates` | `locale` | `items: InstanceTemplate[]` | prompt directory の直読みを撤去 |
| `GET /api/v1/metadata/instance-config` | なし | field options、defaults、context section metadata | frontend の Python constants import を撤去 |

`CreateInstanceRequest` は最低限 `name`, `template_id | template_text`, `agent_profile`, `user_profile` を持つ。
instance name の regex / 最大長 / reserved names は Pydantic schema に含める。

### 8.3 Instance settings / profiles / prompts

| Method / Path | Request | Response | 方針 |
|---|---|---|---|
| `GET /api/v1/instances/{name}/config` | なし | `InstanceConfigView` | `overrides`, `effective`, `inherited_fields`, `revision` を区別 |
| `PATCH /api/v1/instances/{name}/config` | typed partial config + `revision` | 更新後 view | 生dict全置換を廃止。競合は409 |
| `GET /api/v1/instances/{name}/profiles` | なし | `agent_profile`, `user_profile` | legacy migration はbackend内で完了 |
| `PUT /api/v1/instances/{name}/profiles` | typed profiles | 保存後 profiles | profile form を巨大configから分離 |
| `GET /api/v1/instances/{name}/prompts` | なし | typed prompt fields | `system_instruction`, low variant、必要な編集可能field |
| `PUT /api/v1/instances/{name}/prompts` | typed fields + revision | 保存後 prompts | prompts と config の二重保存失敗を減らす |
| `POST /api/v1/instances/{name}/raw-memory/rebuild` | typed format/token params | `202 Job` | 現行 synchronous command をjob化 |

設定 schema の完成は [pydantic_settings_plan.ja.md](pydantic_settings_plan.ja.md) と協調する。
同計画の `InstanceConfig` が現在 `extra="allow"` の placeholder である間は、
chat vertical slice を先行してよいが、正式 settings UI の保存実装は full typed schema 完成後に行う。

### 8.4 Chat / message history

| Method / Path | Request | Response | 方針 |
|---|---|---|---|
| `GET /api/v1/instances/{name}/messages` | `limit`, `before` cursor | `MessagePage` | history直読みを置換。`items`, `next_cursor`, `last_interaction_at` |
| `POST /api/v1/chat` | `ChatRequest` | `ChatResult` | non-stream fallback / test 用。既存 `/chat` のversioned版 |
| `POST /api/v1/chat/stream` | `ChatRequest` | `text/event-stream` | primary chat transport |
| `GET /api/v1/chat/requests/{request_id}` | なし | request state（任意、後段） | reconnect / diagnostics の拡張点 |
| `POST /api/v1/chat/requests/{request_id}/cancel` | なし | cancellation result（後段） | 真のserver-side cancelが実装できるまで公開しない |

`Message` の最低フィールド:

```text
id: string
role: "user" | "assistant"
text: string
created_at: datetime
attachments: AttachmentSummary[]
sources: CitationSource[]
status: "completed" | "failed" | "cancelled"
```

現行 `save_single_turn()` は text だけを保存し、historical attachment / sources / debug_info を会話記憶には保存しない。
最初は `attachments=[]`, `sources=[]` で過去データを表現して後方互換を保つ。正式UIで画像履歴の再表示を要件にする場合、
storage policy（容量、削除、private data）を別途決めてから backend 保存形式を拡張する。

`ChatRequest` は `attachments: Attachment[]` を生dictでなく型付けし、次を明示する。

- MIME: `image/jpeg | image/png | image/webp`
- 最大3枚
- 1枚20MB（現行上限）
- `connection` / `model_name` は developer override。通常UIはinstance configに従う
- `use_google_search` と `use_web_search` は相互排他的にbackend validation
- `client_request_id` を受け、double submit の識別に使う
- `include_debug` は developer mode + local token の時だけ許可

### 8.5 SSE event contract

Pydantic model を次の discriminated union として定義する。

```text
ChatMetadataEvent { event: "metadata", request_id, data: ChatMetadata }
ChatChunkEvent    { event: "chunk",    request_id, sequence, data: { text } }
ChatDoneEvent     { event: "done",     request_id, data: ChatDone }
ChatErrorEvent    { event: "error",    request_id, data: ApiError, recoverable }
```

不変条件:

1. `request_id` は全eventで同一。
2. 成功時は `metadata` 1回 → `chunk` 0回以上 → `done` 1回。
3. 失敗時は `error` 1回で終端し、`done` は送らない。
4. `sequence` は chunk ごとに単調増加。
5. `done.full_text` は全chunk連結と一致する。
6. heartbeat comment を追加可能にし、parser は未知eventを無視せず protocol error として記録する。
7. schema version を event または response header で確認できる。

POST SSE のため browser `EventSource` は使わず、`fetch` + `ReadableStream` の小さな専用clientを書く。
UIの「停止」は初期段階では `AbortController` による受信停止だけになり得る。現行 Gemini stream は
thread executor 内の同期 generator を使うため、network切断だけでは provider処理の即時停止を保証できない。
**server-side cancellation semantics と会話保存の扱いを決めるまで、「生成を停止」機能を完成扱いにしない。**

### 8.6 Global settings / secrets / providers

| Method / Path | Request | Response | 方針 |
|---|---|---|---|
| `GET /api/v1/settings` | なし | typed `GlobalSettingsView` | 現行空 `/settings` と生 `/config` を統合 |
| `PATCH /api/v1/settings` | typed partial + revision | 更新後 view | pydantic-settings schema を利用 |
| `GET /api/v1/settings/secrets/status` | なし | provider別 configured/validated | 値は絶対返さない |
| `PUT /api/v1/settings/secrets/{secret_id}` | secret value | `204` / status | Google/OpenAI/xAI/search keys |
| `DELETE /api/v1/settings/secrets/{secret_id}` | なし | `204` | key消去を正式対応 |
| `POST /api/v1/settings/secrets/{secret_id}/validate` | なし | validation result | 保存後の疎通を明示 |
| `GET /api/v1/connections` | なし | `ConnectionSummary[]` | built-in + user-defined |
| `POST /api/v1/connections` | typed connection | `201 ConnectionSummary` | protocol enum / URL validation |
| `PATCH /api/v1/connections/{id}` | partial | updated connection | 現行はaddで置換しているため明示化 |
| `DELETE /api/v1/connections/{id}` | なし | `204` | built-inは409/403 |
| `POST /api/v1/connections/{id}/test` | optional model listing flag | typed test result | Gemini list_modelsを含む |
| `POST /api/v1/connections/test` | 未保存のtyped connection draft | typed test result | 保存前疎通。secretはresponse/logへ出さない |
| `GET /api/v1/models` | role, connection, refresh | paged/cached candidates | 現行 model_candidates。同期外部呼び出しをcache |
| `GET /api/v1/prompts` | なし | editable prompt list + revision | global prompts |
| `PATCH /api/v1/prompts/{id}` | content + revision | updated prompt | 1項目ずつ安全に更新 |
| `POST /api/v1/maintenance/embedding-reindex` | target | `202 Job` | 現行 fire-and-forget をjob化 |

secret store は最初は現行 `.env` を backend 内で atomic write してよい。ただし API surface は
`SecretRepository` を介し、将来 Windows Credential Manager 等へ変えても frontend contract を変えない。
custom connection の `api_key_env` だけ作れて値をUIから保存できない現状も、`secret_id` 対応で解消する。

### 8.7 Capabilities / model metadata

`GET /api/v1/capabilities` と `GET /api/v1/models` は UI の条件分岐の正本にする。

最低限返す情報:

- active connection / model
- chat / vision / streaming / native_google_search / generic_web_search
- API key configured
- feature available と unavailable reason
- streaming mode: `incremental | buffered_fallback | unsupported`
- attachment MIME / count / byte limits
- provider model candidate の `stable`, `preview`, `deprecated`, `replacement`

frontend が `model_name.startswith("gemini")` や environment variable の有無で分岐してはいけない。
user-defined connection や将来の provider 追加に追従できなくなるためである。

### 8.8 Glossary / Key Memory

| Method / Path | Request | Response | 方針 |
|---|---|---|---|
| `GET /api/v1/instances/{name}/glossary` | filter/search query optional | typed glossary document | version / entriesを型付け |
| `PUT /api/v1/instances/{name}/glossary` | full typed doc + revision | updated doc | unknown field / duplicate term validation |
| `GET /api/v1/instances/{name}/key-memories` | なし | typed entries | YAML正本をbackendで隠す |
| `POST /api/v1/instances/{name}/key-memories` | target/content | `201 Entry` | target enum / content limits |
| `PATCH /api/v1/instances/{name}/key-memories/{id}` | partial | updated entry | PUT生dictを整理 |
| `DELETE /api/v1/instances/{name}/key-memories/{id}` | なし | `204` | typed 404 |
| `GET /api/v1/instances/{name}/key-memory-proposals` | status filter | proposal list | proposal indexでなく安定proposal IDを推奨 |
| `POST .../proposals/{id}/approve` | optional edited content | updated proposal/entry | idempotency / already processedは409 |
| `POST .../proposals/{id}/reject` | なし | updated proposal | 同上 |

Glossary の category / status / scan target と context level options は schema enum または metadata endpoint から取得する。

### 8.9 Knowledge cards

| Method / Path | Request | Response | 方針 |
|---|---|---|---|
| `GET /api/v1/instances/{name}/knowledge-cards` | cursor, limit, category, search, pinned, archived, sort | `KnowledgeCardPage` | total/next cursor。list itemに必要fieldを返す |
| `GET /api/v1/instances/{name}/knowledge-cards/{id}` | なし | `KnowledgeCardDetail` | embeddingは返さない |
| `PATCH /api/v1/instances/{name}/knowledge-cards/{id}` | editable fields + revision | updated detail | importance 0..10等をvalidate |
| `DELETE /api/v1/instances/{name}/knowledge-cards/{id}` | なし | `204` | 関連sourceがある場合のpolicyを明示 |
| `PUT /api/v1/instances/{name}/knowledge-cards/{id}/pin` | `is_pinned` | updated summary | idempotent。Key Memoryへの副作用を分離 |
| `PUT /api/v1/instances/{name}/knowledge-cards/{id}/archive` | `is_archived` | updated summary | 現在router未公開のDB機能 |
| `GET /api/v1/metadata/knowledge-cards` | なし | categories / limits / sort options | UIハードコード撤去 |

pin は card state の変更だけにする。Key Memory へ昇格したい場合は別 command
`POST .../{id}/promote-to-key-memory` とし、unpin と意味を混ぜない。

### 8.10 Long-running jobs（Sleeptime / reindex / raw rebuild）

| Method / Path | Request | Response | 方針 |
|---|---|---|---|
| `GET /api/v1/instances/{name}/sleeptime/estimate` | なし | typed workload estimate | 現行estimateの生dictを型付け |
| `POST /api/v1/instances/{name}/sleeptime/runs` | options | `202 Job` | 既に同一instance実行中なら既存jobまたは409 |
| `POST /api/v1/maintenance/embedding-reindex` | target | `202 Job` | 全instance対応 |
| `POST /api/v1/instances/{name}/raw-memory/rebuild` | format/options | `202 Job` | instance commandだが共通Jobを返す |
| `GET /api/v1/jobs/{job_id}` | なし | typed Job status | state/progress/stage/message/error/timestamps |
| `GET /api/v1/jobs` | filters | recent jobs | app再起動後の表示に備える |
| `POST /api/v1/jobs/{job_id}/cancel` | なし | result | cooperative cancel可能なjobだけ |

最初は polling でよい。SSE job event は必要になってから追加する。Job state は少なくとも process crash 時に
`running` のまま永遠に見えないよう、startup recovery で `interrupted` へ遷移させる。

### 8.11 Integrations / pairing

| Method / Path | Request | Response | 方針 |
|---|---|---|---|
| `GET /api/v1/integrations` | なし | LINE/Discord状態、設定不足理由 | secret値なし |
| `GET /api/v1/pairings` | status/source | `PendingPairing[]` | current pending + instancesを分離可能 |
| `POST /api/v1/pairings/{code}/approve` | instance | approved result | loopback + token |
| `POST /api/v1/pairings/{code}/reject` | なし | rejected result | loopback + token |
| `POST /line/webhook` | LINE payload | existing response | 外部契約。versioned desktop APIへ移さない |

external user ID は現行UI同様 mask した表示用値だけ返し、生IDの露出を最小化する。

### 8.12 Dashboard / device（初期は延期）

現行 `/status`, `/discovery`, `/news`, `/devices`, `/tv/key`, `/tv/launch`, `/ws` は
Streamlit の現在の main routing から使われていない。削除はせず、正式 frontend P0-P3 の契約対象から外す。

採用する際は次の通り versioned typed API にする。

- `GET /api/v1/system/metrics`
- `GET /api/v1/discovery`
- `GET /api/v1/news`
- `GET /api/v1/devices`
- `POST /api/v1/devices/{id}/commands`

現在の WebSocket は全接続へ broadcast し request / instance / client の相関がないため、正式UIで再利用する前に
typed envelope、client ID、authorization、backpressure、reconnect policy を設計する。

### 8.13 現行endpointの移行対応表

新endpointの実装漏れを防ぐため、現行routerの全operationを次のように扱う。`adapter` は
旧pathから新application serviceを呼ぶ薄い互換層を意味し、Streamlit終了後に利用状況を確認して削除する。

| 現行endpoint | 正式contract / 扱い | 移行上の要点 |
|---|---|---|
| `POST /chat` | `POST /api/v1/chat` | request/attachment/result/errorを型付け。旧pathはadapter |
| `POST /chat/stream` | `POST /api/v1/chat/stream` | typed SSE union、request ID、順序保証。旧pathはadapter |
| `WS /ws` | P0-P3では延期 | broadcast設計を流用せず、必要時に認証付きtyped channelとして再設計 |
| `GET /instances` | `GET /api/v1/instances` | 文字列配列から`InstanceSummary` pageへ |
| `POST /instances` | `POST /api/v1/instances` | template/profileをtyped requestへ統合 |
| `POST /instances/{name}/rename` | `POST /api/v1/instances/{name}/rename` | typed result、conflict/error code追加 |
| `DELETE /instances/{name}` | `DELETE /api/v1/instances/{name}` | `204`、削除範囲を明文化 |
| `POST /instances/{name}/reload` | `POST /api/v1/instances/{name}/session/reset` | reloadという曖昧名を廃止 |
| `GET/POST /instances/{name}/config` | `GET/PATCH /api/v1/instances/{name}/config` | revision付きpartial updateへ。旧POST全置換はadapterのみ |
| `GET/POST /instances/{name}/prompts` | `GET/PUT /api/v1/instances/{name}/prompts` | prompt DTOとrevisionを追加 |
| `GET /history/{name}` | `GET /api/v1/instances/{name}/messages` | timestamp、cursor、last interactionを追加 |
| `GET/POST /instances/{name}/glossary` | `GET/PUT /api/v1/instances/{name}/glossary` | document schema、revision、validation追加 |
| `POST /instances/{name}/rebuild_raw_cache` | `POST /api/v1/instances/{name}/raw-memory/rebuild` | 同期的な意味をやめ共通Jobを返す |
| `GET/POST/PUT/DELETE /instances/{name}/key_memory...` | `/api/v1/instances/{name}/key-memories...` | typed entry、revision、安定IDへ |
| `GET/POST .../key_memory/proposals...` | `/api/v1/instances/{name}/key-memory-proposals...` | 配列index指定を安定proposal IDへ移行 |
| `GET/POST /settings` | `GET/PATCH /api/v1/settings` | 空placeholderを廃止しtyped global settingsへ |
| `GET/POST /config` | section別settings/config API | giant raw dictの公開・全置換を廃止 |
| `POST /settings/api_key` | `PUT /api/v1/settings/secrets/{secret_id}` | key typeをsecret IDへ。write-only |
| `GET /settings/api_key_status` | `GET /api/v1/settings/secrets/status` | configured/validatedのみ返す |
| `POST /settings/ollama_test` | `POST /api/v1/connections/test` | provider個別pathを共通typed connection testへ |
| `POST /settings/reindex_embeddings` | `POST /api/v1/maintenance/embedding-reindex` | fire-and-forgetをJob化 |
| `GET/POST /prompts` | `GET /api/v1/prompts`, `PATCH /api/v1/prompts/{id}` | 一括raw更新をrevision付き項目更新へ |
| `GET/POST/DELETE /settings/connections...` | `/api/v1/connections...` | CRUD semantics、secret参照、built-in保護を型付け |
| `POST /settings/test_connection` | `POST /api/v1/connections/{id}/test` | timeoutとprovider別errorを共通resultへ正規化 |
| `GET /settings/model_candidates` | `GET /api/v1/models` | role/connection filter、cache、refresh、capabilities追加 |
| `GET /sleeptime/estimate/{name}` | `GET /api/v1/instances/{name}/sleeptime/estimate` | typed estimate |
| `POST /sleeptime/run` | `POST /api/v1/instances/{name}/sleeptime/runs` | instanceをpathへ、共通Jobを返す |
| `GET /sleeptime/status/{name}` | `GET /api/v1/jobs/{job_id}` | process内instance statusを永続/recover可能なJobへ |
| `GET /database/cards/{name}` | `GET /api/v1/instances/{name}/knowledge-cards` | cursor/total/filter/sortとtyped summary追加 |
| `GET/PUT/DELETE /database/cards/{name}/{id}` | `GET/PATCH/DELETE /api/v1/instances/{name}/knowledge-cards/{id}` | detail DTO、revision、validation追加 |
| `POST /database/cards/{name}/{id}/pin` | `PUT .../knowledge-cards/{id}/pin` | idempotent化しKey Memory追記副作用を分離 |
| `GET /pairing/pending` | `GET /api/v1/pairings` | typed list、masked external ID、token認証 |
| `POST /pairing/approve`, `/reject` | `POST /api/v1/pairings/{code}/approve`, `/reject` | codeをpathへ、loopback + token、typed result |
| `POST /line/webhook` | 現行pathを維持 | 外部LINE契約。desktop tokenでなくsignature認証 |
| `GET /status` | `GET /api/v1/system/metrics`（延期） | health/readinessと分離。外部疎通を起動判定にしない |
| `GET /discovery`, `/news` | versioned typed API（延期） | cache schemaと更新時刻を型付けしてから採用 |
| `GET /devices` | `GET /api/v1/devices`（延期） | deviceごとのstable IDとcapabilityを追加 |
| `POST /tv/key`, `/tv/launch` | `POST /api/v1/devices/{id}/commands`（延期） | command union、認証、結果、timeoutを型付け |

---

## 9. OpenAPI を frontend 契約にする

### 9.1 契約の正本

正本は以下の順とする。

1. transport Pydantic models + FastAPI path operation
2. そこから生成された OpenAPI 3.1 document
3. OpenAPI から生成された TypeScript DTO / REST client
4. React feature code

frontend 内で同じ DTO を手書きしない。FastAPI は OpenAPI から TypeScript SDK を生成する運用を
公式に案内している。

- [FastAPI: Generating SDKs](https://fastapi.tiangolo.com/advanced/generate-clients/)

### 9.2 schema package

HTTP DTO は core domain model と分離する。

```text
butly_api/schemas/
├── common.py          # ApiError, Page, Job, Health
├── chat.py            # requests/results/SSE events/messages
├── instances.py
├── settings.py
├── providers.py
├── memories.py
├── integrations.py
└── devices.py
```

理由:

- internal `ChatResponse.text/refs` と public `response/references` のような変換を明示できる。
- provider SDK の型が public contract に漏れない。
- domain refactor と wire compatibility を独立させられる。
- Pydantic settings model をそのまま公開して secret / internal path を誤って返す事故を防げる。

### 9.3 path operation の必須項目

すべての frontend-facing endpoint に次を付ける。

- `prefix="/api/v1"`
- domain `tags`
- 明示的で安定した `operation_id`
- request model
- `response_model`
- success `status_code`
- documented error responses
- summary / description
- deprecated legacy operation には `deprecated=True`

`operation_id` は generator の関数名になるため、path由来の自動生成名に依存しない。

認証は OpenAPI の `components.securitySchemes` に HTTP Bearer scheme として登録し、
`/api/v1/*` の operation に security requirement を付ける。無認証のhealthとLINE webhookだけは
operation単位で明示的に解除する。生成clientはtoken注入hookを1か所に持ち、query parameterや
生成済みmethodごとにtoken処理を重複させない。CORS preflightの`OPTIONS`は認証middlewareより先に処理する。

### 9.4 error contract

FastAPI default `{"detail": ...}` と成功body内 errorを混在させず、次に統一する。

```json
{
  "code": "instance_not_found",
  "message": "Instance 'foo' was not found.",
  "details": {},
  "request_id": "..."
}
```

- `code` は frontend 分岐用の安定値。
- `message` は表示可能だが、UIは既知codeをlocalizeしてよい。
- validation error も同 envelope に normalize。
- exception string、file path、API key、raw provider response を返さない。
- server log と突合できる `request_id` を付ける。

### 9.5 schema artifact と client generation

提案 workflow:

1. side effect のない `create_app()` から OpenAPI を生成。
2. `openapi/butly.openapi.json` を deterministic format で出力。
3. OpenAPI 3.1 対応 generator（第一候補 `@hey-api/openapi-ts`）で
   `frontend/src/api/generated/` を生成。
4. generated directory は手編集禁止。生成 command を1本化。
5. CI で schema と generated client を再生成し、git diff が出たら失敗。
6. backend contract change は generated client の compile error と frontend test で検出。

例（最終 command 名は実装時に確定）:

```text
./scripts/generate_openapi.sh
pnpm --dir frontend generate:api
pnpm --dir frontend typecheck
```

OpenAPI snapshot を commit する利点は review で breaking change が見えること。欠点は diff が大きくなること。
deterministic sort と generator version pin を必須にする。

### 9.6 SSE と OpenAPI の扱い

OpenAPI は `text/event-stream` response 自体は記述できるが、event framing と逐次 union の client生成は
generatorごとの差が大きい。したがって:

- request body と各 event payload は Pydantic model / OpenAPI components に載せる。
- endpoint response media type は `text/event-stream` と記述する。
- `ChatStreamEvent` は `oneOf` + discriminator で表現する。
- transport parser は `frontend/src/api/sse.ts` に手書きする。
- parser fixture は backend の `_sse_event` 出力から生成し、contract test で共有する。
- REST generator が stream を正しく扱わなくても、DTO生成は利用する。

AsyncAPI の追加は WebSocket / event channel が増えてから検討し、初期段階では導入しない。

### 9.7 versioning / compatibility policy

- `/api/v1` 内の additive field は許容。
- required field削除、意味変更、enum縮小は breaking change とし `/api/v2` または移行期間を設ける。
- frontend は unknown response field を許容する。
- backend は unknown request field を原則拒否し、typo を早く検出する。legacy endpoint だけ互換を緩める。
- frontend / backend bundle version mismatch は起動時に検出し、互換範囲外なら操作を止める。
- Streamlit legacy route は新UI parity 完了まで維持し、削除時期をdeprecation表に記録する。

### 9.8 contract test

- 全 `/api/v1` operation に `operation_id`, tags, response schema があること。
- operation ID が一意であること。
- OpenAPI generation が user config / instance / external API に依存しないこと。
- schema snapshot test。
- representative response が response model validation を通ること。
- error envelope test（404 / 409 / 422 / 500）。
- SSE event 順序、JSON decode、multi-line data、UTF-8、途中切断、unknown event、done全文一致。
- generated TypeScript client の typecheck。

---

## 10. Gemini への影響と確認事項

### 10.1 結論

**正式 frontend 化によって Gemini 利用が壊れる構造上の問題はない。**

Gemini API は引き続き backend の `GeminiProvider` から呼ぶ。React/Tauri は Butly API だけを呼び、
Gemini SDKもGemini API keyも持たない。この proxy 構成は、Google が client-side app で key を露出せず
backend proxy を使うよう案内している方針とも一致する。

- [Gemini API key security](https://ai.google.dev/gemini-api/docs/interactions/api-key)

OpenAPI 化の対象は **Butly API** であり、Gemini API をOpenAPIへ写経することではない。
Gemini SDK変更は provider adapter 内に閉じ、public DTO を保つ。

### 10.2 現行で維持できるもの

- `connection_id="google"` + `model_name` による routing
- global / instance / per-request model override
- native Gemini streaming
- inline image / Gemini Files API upload
- Google Search grounding
- source title / URL の返却
- model list の動的探索
- system instruction の Gemini native parameter 化
- embedding / summary / gatekeeper / knowledge role

### 10.3 正式UIで明示すべき Gemini 固有挙動

1. **Google Search ON時の stream**
   - 現行 provider は grounding metadata の都合で non-stream generation に fallback し、
     完成文を1つの `chunk` として返す。
   - capabilities の `streaming_mode="buffered_fallback"` でUIへ知らせる。
   - UIはchunk数や到着間隔を成功条件にしない。

2. **Grounding source**
   - 現行は `grounding_chunks.web` の title / URI だけを `sources` に正規化する。
   - Google response には query、support segment、search entry point 等もある。
   - inline citation を実装する場合、backend共通 `CitationSource` / `CitationSpan` へ正規化し、
     Google Search の表示要件を確認してからUIを作る。
   - [Gemini: Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)

3. **画像**
   - frontend → Butly API は現在base64 JSONで、最大3枚・各20MB。base64膨張と複製でmemory負荷が大きい。
   - P0は現行互換。計測後、`multipart/form-data` upload + attachment ID方式を検討する。
   - backend → Gemini は小さい画像をinline、大きい画像をFiles APIへ送る現行方針を維持する。
   - Gemini Files APIのupload fileは一時保存されるため、privacy説明とcleanupを確認する。
   - [Gemini: Files API](https://ai.google.dev/gemini-api/docs/files)

4. **model discovery**
   - `client.models.list()` はbackendだけで実行する。
   - 現在は settings GET の中で同期的に全 connection を探索し得るため、TTL cache、refresh、
     per-connection timeoutを導入する。
   - モデル名prefixではなくbackend capabilitiesをUIの正本にする。

5. **API key**
   - key value はwrite-onlyでUIへ返さない。
   - 保存直後の connection test を提供する。
   - 2026年のGemini key policy変更に備え、認証失敗を「frontend接続失敗」と混同せず、
     key type / restriction 更新を案内できるerror codeを用意する。
   - 現在の設定画面が保存する `GOOGLE_API_KEY` と provider が読む
     `GEMINI_API_KEY` / `GOOGLE_API_KEY` の優先順位もsettings統合時に一本化する。

6. **新 Gemini API への追従**
   - 現行の stable `generateContent` / chat path をfrontend移行中に変更しない。
   - Google Search込みのより細かなstreamingが必要なら、provider内部で新APIを別spikeする。
   - frontend移行とprovider API移行を同じPRにしない。

### 10.4 Gemini 回帰テスト

- API key未設定: readyは成功、capabilitiesでGemini unavailable、chatはtyped error。
- model list成功 / timeout / auth failure。
- text non-stream / native stream。
- Google Search ON: 1 chunk + done + sources。
- image inline / Files API path / vision非対応model。
- per-instance model override / per-request override。
- grounding source normalization。
- frontend SSE parserが日本語、1 chunk、複数chunkの両方を同じように扱う。
- integration marker の実Gemini testは通常CIから分離し、release candidate時に明示実行する。

---

## 11. frontend UX / 状態設計

### 11.1 app-level states

起動直後は画面を無理に通常routeへ入れず、次の状態を明示する。

- `starting_backend`
- `migrating_data`
- `ready`
- `backend_unavailable`
- `backend_crashed`
- `version_mismatch`
- `fatal_configuration_error`

Gemini key未設定やOllama offlineは app fatal ではなく、provider capability の部分的 unavailable とする。

### 11.2 chat state machine

```text
idle
  → submitting
  → waiting_metadata
  → streaming
  → finalizing
  → completed
  ↘ failed
  ↘ disconnected
```

- double submit を禁止または queue semantics を明示。
- optimistic user message と server確定messageを `client_request_id` で紐付ける。
- route移動時にstreamをどう扱うか決める（P0はroute guardで継続中離脱を確認）。
- retryは「同じrequestを再送」し、二重保存防止のidempotencyをbackendと協調する。
- debug panelはdeveloper setting時だけlazy load / displayする。
- Markdownはsanitizeし、raw HTMLを既定で許可しない。
- source URLは `http/https` schemeだけを許可し、external openはTauri allowlistを通す。

### 11.3 locale

- UI stringは最初から i18n key（ja/en）で管理。
- backend errorは `code` を正本とし、既知codeはfrontendでlocalize。
- prompt locale / agent locale とUI localeを混同しない。
- datetimeはbackend UTC、frontend local timezone表示。

### 11.4 accessibility / keyboard

- chat入力送信、改行、停止、再試行をkeyboardで操作可能にする。
- focus ringを消さない。
- streaming更新をscreen readerへ毎token通知せず、適度にbatchする。
- destructive actionはbutton labelとdialog titleで対象instance/cardを明示する。

---

## 12. 段階移行

### Phase 0 — API contract foundation

**目的:** 新UIを書く前に境界を成立させる。

- `create_app()` / CLI entrypoint 分離。
- `/api/v1/health`, `/ready`, `/app-info`, `/capabilities`。
- common error envelope / request ID / desktop token auth。
- OpenAPI schema package、stable operation IDs、generation script。
- typed instance list、history、chat REST / SSE DTO。
- SSE parser contract fixture。
- legacy routeは維持。
- `app.py` のhistory / instance list / templates / env判定を新APIへ寄せ、
  新APIがStreamlitでも使えることを先に証明する。

**完了条件:** API clientを生成し、UIなしでもcontract testが緑。

### Phase 1 — Windows sidecar spike + app shell

**目的:** 一番不確実な配布 / process問題を先に潰す。

- `frontend/` scaffold。
- development backend起動とproduction PyInstaller sidecar起動。
- dynamic port / token / readiness / graceful shutdown。
- backend crash画面とrestart。
- Windows x64 NSIS installerをCI artifactとして生成。
- `%LOCALAPPDATA%\Butly` で既存dataを読み書きできることを確認。

**完了条件:** clean Windows環境でinstaller → 起動 → backend health → 終了時process残存なし。

> **Phase 1 実装状況（2026-07-09）**
>
> - 実装済み: `butly_api/server.py`（sidecar CLI: loopback 既定 / port 0 +
>   listening JSON 通知 / token は `BUTLY_DESKTOP_TOKEN` 環境変数のみ /
>   production CORS = `http://tauri.localhost` / token 必須の
>   `POST /api/v1/shutdown`）、`frontend/`（pnpm + Tauri v2 + React + TS +
>   Vite、strict、`@hey-api/openapi-ts` 生成 client、app shell の
>   starting / ready / unavailable / crashed / version_mismatch 表示 +
>   restart）、Rust lifecycle（per-launch CSPRNG token・spawn・health polling・
>   token 付き readiness・version handshake・crash 検知・restart・graceful shutdown・
>   single-instance・最小 capabilities / CSP）、PyInstaller one-folder spec +
>   build / smoke test script、`.github/workflows/windows-desktop.yml`
>   （Windows x64 NSIS installer artifact）。仕様の正本:
>   [desktop_sidecar.ja.md](../../reference/desktop_sidecar.ja.md)。
> - テスト: backend（CLI / data-dir / bind / token 401 / listening 通知 /
>   subprocess E2E で graceful shutdown と process 残存なし）、frontend
>   （状態遷移と restart 表示の vitest）。`check_before_push.sh` に frontend
>   lint / typecheck / test / build を統合（pnpm 不在時は SKIP を明示）。
> - 未検証（完了条件に未到達の項目）: Windows 実機での
>   installer → 起動 → 終了確認、および CI workflow の初回実行
>   （Tauri build / NSIS / one-folder 同梱の安定性）。one-folder が不安定な
>   場合は one-file へ切替え、理由と起動時間を desktop_sidecar.ja.md に記録する。

### Phase 2 — Chat vertical slice

**目的:** Butlyの中心価値を最初に完成させる。

- instance list / select。
- typed history。
- text chat POST SSE。
- native multi-chunk / Gemini Google Search buffered fallback。
- sources、error、retry、backend disconnect。
- image attachment（まず現行base64互換）。
- developer debug panel。
- end-to-end test。

**完了条件:** Gemini / non-Gemini mockで履歴→送信→stream→done→再読込が一致。

### Phase 3 — Onboarding / instance basic settings

- first-run wizard、template API。
- instance create / rename / delete / session reset。
- profiles / prompts。
- model assignment / readable instances。
- pydantic instance config schemaとの統合。

### Phase 4 — Memory administration

- knowledge cards list/detail/edit/pin/archive/delete。
- glossary。
- Key Memory entries / proposals。
- Sleeptime / raw rebuild / embedding reindex の共通Job UI。
- pagination / filters / progress recovery。

### Phase 5 — Global settings / integrations

- global typed settings / prompts。
- secret status / write / delete / validation。
- connections / model discovery cache。
- LINE pairing。
- remote backend profileは別設計が済んだ場合のみ。

### Phase 6 — Release hardening / Streamlit retirement

- installer signing、update、log export、crash recovery、migration backup。
- Windows clean-machine E2E、upgrade / uninstall / data preservation test。
- legacy route usageを計測し0を確認。
- Streamlit dependency / `app.py` / start batchを削除またはdeveloper legacy packageへ隔離。
- reference docs ja/enを正式UIに更新。

---

## 13. 既存計画との順序

### 13.1 pydantic-settings

- Phase 0のhealth/chat/history schemaはsettings完全型付けを待たず進められる。
- Global / instance settingsの正式保存UIはtyped settings Phase 2以降と揃える。
- `/config` の生dictをそのままOpenAPI contractとして固定しない。
- settings側のdefault / override / effective値の意味をAPI View Modelで明示する。

### 13.2 記憶ストア正規化

- frontendはAPIだけを見るため、storage正規化と独立して開発できる。
- history / glossary / Key Memory / knowledge card routerは将来Store/Repositoryへ委譲する。
- `app.py` のdirect memory readを先に撤去すると、Store移行時のfrontend影響が消える。
- card pin → `Key_Memory.txt` 追記の現行挙動は、YAML正本化より前に責務分離する。

同じファイルを大きく触るPRを重ねない。特に `routers/settings.py`, `app.py`,
`InstanceManager`, memory path周辺はphaseごとにmergeしてから次へ進む。

---

## 14. テスト戦略

### 14.1 backend

- schema / OpenAPI snapshot test。
- auth / loopback / CORS test。
- router contract test（成功 + 主要error）。
- service unit testは現行を維持。
- SSE framing / ordering / disconnect / cancellation test。
- job lifecycle / restart recovery test。
- data migrationはtmp data directoryでtestし、実instanceを使わない。

### 14.2 frontend

- TypeScript strict + generated client typecheck。
- component test: form validation、error state、capability分岐。
- SSE parser unit test: chunk分割位置、CRLF/LF、UTF-8、複数data行、途中切断。
- feature integration test: mocked APIでchat / settings / cards / jobs。
- accessibility test。

### 14.3 desktop E2E

- backend sidecar起動 / ready / crash / restart / shutdown。
- port conflict。
- tokenなし / 不正token拒否。
- instance作成→chat→再起動→履歴復元。
- installer upgradeで `%LOCALAPPDATA%\Butly` が保持される。
- uninstall時のuser data policy確認。
- Windows display scaling、IME日本語入力、pathに日本語/spaceを含むuser profile。

### 14.4 CI / push前チェック

既存の唯一の正 `./scripts/check_before_push.sh` は維持する。追加でfrontend導入後はCIに次を足す。

```text
backend checks（既存）
OpenAPI deterministic generation + diff
frontend install --frozen-lockfile
frontend lint / typecheck / unit test / build
Windows Tauri build（少なくともrelease branch / nightly）
```

Python testからNode/Tauri buildまでを既存scriptに全部入れるかは、日常実行時間を計測して決める。
少なくともpush gateの正本は最終的に1つに保つ。

---

## 15. Security / privacy checklist

- [ ] production backendはloopback only。
- [ ] per-launch tokenを全internal APIへ要求。
- [ ] token / API keyをlog、URL query、localStorageへ出さない。
- [ ] CORS wildcardを撤去。
- [ ] Tauri CSP / capabilitiesを最小化。
- [ ] secret responseはconfigured statusのみ。
- [ ] debug/full promptはdeveloper modeのみで、通常responseへ含めない。
- [ ] attachment / prompt / memoryをcrash telemetryへ送らない。
- [ ] external URL schemeをallowlist。
- [ ] LINE webhookはsignature認証を維持し、desktop token middlewareから除外。
- [ ] pairingはloopback + token。
- [ ] log rotationと「機密ログ削除」を用意。
- [ ] installer / updater署名を一般配布前に導入。
- [ ] backend bundleから `.env`, user config, instancesを除外。

---

## 16. リスクと対策

| リスク | 影響 | 対策 |
|---|---|---|
| PyInstallerがGoogle/OpenAI SDKのdynamic importを取りこぼす | 起動後providerだけ失敗 | provider別bundle smoke test、hidden import管理 |
| AV / SmartScreenがsidecarを警告 | 配布障害 | one-folder比較、署名、reputation構築 |
| dynamic port race | 起動不安定 | port 0 + ready通知方式をspike |
| WebView2環境差 | blank window / stream差 | Windows clean VM、minimum version、offline installer option |
| SSE generator差 | generated clientがstreamを読めない | REST生成と専用SSE parserを分離 |
| network切断後もLLM処理継続 | quota消費 / 二重保存 | request ID、cancellation design、idempotency |
| base64画像でmemory急増 | crash / latency | size計測、multipart + attachment IDへ段階移行 |
| settings生dictをcontract固定 | 将来migration困難 | typed settings完成まで正式settings saveを待つ |
| model discoveryが遅い | settings画面freeze | TTL cache、manual refresh、per-connection status |
| Google Search citation表示要件漏れ | UX / compliance問題 | grounding metadata正規化と公式要件review |
| Streamlitと新UIの二重実装が長期化 | maintenance増 | phaseごとのparity表とretirement gate |
| backend内の既存silent fallback | UIが成功/失敗を誤認 | typed error code、log、触る箇所から狭いexceptionへ |
| user data migration失敗 | 記憶損失 | migration前backup、dry-run、version marker、rollback |

---

## 17. 想定変更ファイル

### 新規

- `frontend/` 一式
- `butly_api/`（app factory、auth、errors、schemas）
- `openapi/butly.openapi.json`
- OpenAPI/client generation scripts
- frontend / desktop tests
- PyInstaller spec / Windows build workflow

### 段階改修

- [main.py](../../../main.py) — thin entrypoint / loopback / CLI / data dir
- [routers/chat.py](../../../routers/chat.py) — typed DTO / versioned route / SSE schema
- [routers/instances.py](../../../routers/instances.py) — history / templates / typed settings
- [routers/settings.py](../../../routers/settings.py) — typed settings / secrets / connections / model cache
- [routers/sleeptime.py](../../../routers/sleeptime.py) — Job serviceへ委譲
- [routers/database.py](../../../routers/database.py) — pagination / typed card / pin副作用分離
- [routers/pairing.py](../../../routers/pairing.py) — versioned management API + token
- [dependencies.py](../../../dependencies.py) — app factory injectionへ段階移行
- [app.py](../../../app.py) — 新APIを先行利用後、parity完了時にretire
- [requirements.txt](../../../requirements.txt) — Streamlit削除は最終phaseのみ

### docs

- `docs/reference/FILE_STRUCTURE.ja.md` / `.md`
- `docs/reference/DIAGRAMS.ja.md` / `.md`
- setup / LINE guides の Streamlit 記述
- root README ja/en
- release / troubleshooting guide

---

## 18. 完了条件

- [ ] Windows 10 / 11 x64 clean環境でinstallerから起動できる。
- [ ] Python / Node / Rustのuser-side事前installが不要。
- [ ] Tauri終了後にbackend processが残らない。
- [ ] frontendに`butly_core` import、instance file read、environment readがない。
- [ ] 全frontend-facing APIが `/api/v1`、typed request/response/error、stable operation IDを持つ。
- [ ] OpenAPI artifactとgenerated TypeScript clientがCIで同期される。
- [ ] Chat text / image / SSE / Gemini Google Search fallback / sourcesが動く。
- [ ] instance CRUD、profiles、settings、prompts、glossary、Key Memory、cards、Sleeptime、pairingがparity表を満たす。
- [ ] Gemini keyがfrontend bundle / log / responseに出ない。
- [ ] loopback bind + token + explicit CORS + Tauri CSP/capabilitiesが有効。
- [ ] upgrade前backupとuser data保持が確認される。
- [ ] legacy Streamlit routeの利用が0で、Streamlitを削除または明確にlegacy隔離できる。
- [ ] docs ja/enが正式構成と一致。
- [ ] `./scripts/check_before_push.sh` と追加frontend checksが緑。

---

## 19. 最初の実装単位（推奨PR分割）

### PR 1: API契約の土台

- app factory
- `/api/v1/health`, `/ready`, `/app-info`
- common error model
- OpenAPI generation / snapshot
- current behaviorの変更なし

### PR 2: Chat contract

- typed instance summary / history
- typed attachment / chat response
- typed SSE event components + tests
- legacy route adapter
- StreamlitのhistoryをAPI経由へ変更

### PR 3: Windows sidecar spike

- minimal Tauri window
- PyInstaller backend
- dynamic port / token / readiness / shutdown
- CI installer artifact

### PR 4: Chat UI vertical slice

- instance list
- history
- POST SSE
- error / retry / Gemini buffered fallback
- minimal E2E

この4本が通るまで、巨大なsettings画面やvisual polishへ広げない。ここまでで
「正式frontendの技術・配布・契約が成立するか」という最大の不確実性を解消できる。
