# Butly ファイル構成一覧

🌐 **日本語** | [English](FILE_STRUCTURE.md)

> 最終更新: 2026-05-24

---

## ルートディレクトリ

| ファイル | 役割 |
|---|---|
| `main.py` | FastAPI アプリの互換 entrypoint（app 構築は `butly_api.create_app()` に委譲） |
| `app.py` | Streamlit Web UI（FastAPI バックエンド経由で動作） |
| `dependencies.py` | ルーター間共有のグローバル状態・ヘルパー |
| `sleeptime.py` | 記憶自動整理スクリプト（単体実行 & APIから呼び出し可） |
| `migrate_embeddings.py` | プロバイダー切り替え時の embedding 再生成ユーティリティ |
| `butly_api/` | 正式フロントエンド向け `/api/v1` transport 層（app factory / schemas / error contract） |
| `openapi/butly.openapi.json` | `/api/v1` の OpenAPI 3.1 snapshot（`scripts/generate_openapi.py` で生成） |
| `evals/locomo/` | LoCoMo長期記憶評価CLI。正式APIへ混入せず、checkpoint付き隔離Workspace上でReplay → Sleeptime → QA → 公式互換採点・レポートまで実行 |

---

### `main.py`
互換 entrypoint。起動引数のパース・データディレクトリ解決・`.env` ロード・`ButlyRuntime` 初期化を行い、app 本体の構築は `butly_api.create_app()` に委譲する。legacy routers（Streamlit 互換）と wildcard CORS はここで注入する。

- `_watch_parent(parent_pid)` — 親プロセス（デスクトップ shell）の死亡監視スレッド
- `_load_env_from_data_dir()` — `.env` からAPIキーを環境変数へロード
- `lifespan(app)` — バンドル時の設定ファイルコピー・起動メッセージ
- `_api_context` — `/api/v1/ready` 等が参照する `ApiContext`
- `app` — `create_app()` の戻り値。`/api/v1` + 全 routers/ を include

---

### `app.py`
Streamlit 製 Web UI。インスタンス選択・チャット送信・過去ログ表示・設定画面・DB ブラウザ等を提供する。

チャット送信・インスタンス CRUD・設定変更等の書き込み操作はすべて **FastAPI バックエンド（`POST /chat` 等）に `requests.post()` で委譲** しており、Gatekeeper・記憶ブロック構築・会話保存は app.py では行わない。  
インスタンス一覧と会話履歴の読み取りも **新 API（`GET /api/v1/instances` / `GET /api/v1/instances/{name}/messages`）経由**で、`INSTANCES_DIR` / `ButlyMemory` の直読みは撤去済み（backend 到達不能時は明示的なエラー表示になり、直読みへはフォールバックしない）。`ButlyMemory` 等の import は Chronos の日時テキスト生成など残存用途に限られ、正式 UI 移行完了時に撤去する。

**画面構成:**
- `render_home_screen()` — ホーム。インスタンス一覧・新規作成
- `render_chat_screen()` — チャット画面。`POST /chat` で応答取得、デバッグ表示
- `render_settings_screen()` — グローバル設定・APIキー・プロバイダー設定
- `render_instance_settings_screen()` — インスタンス個別の性格設定・config 編集
- `render_sleeptime_screen()` — Sleeptime の実行・進捗表示
- `render_database_browser_screen()` — ナレッジカード一覧
- `render_card_edit_screen()` — カード詳細・編集
- `render_onboarding_screen()` — 初回セットアップウィザード
- `initialize_system(base_dir, instance_name)` — Memory / Brain / Chronos / Cache の初期化（`@st.cache_resource`）

---

### `dependencies.py`
ルーター間で共有されるシングルトンと共通ヘルパーを管理。

- `get_instance_components(instance_name)` — Memory / Brain / Chronos / Cache を遅延初期化して返す
- グローバル変数: `instance_manager`, `gatekeeper`, `mem_block_builder`, `instance_store`
- `FIRE_TV_AVAILABLE` — ADB モジュールが利用できるかのフラグ

---

### `sleeptime.py`
蓄積された short_term_json を要約・ナレッジカード化・DB 登録する記憶整理エンジン。  
`python sleeptime.py` でも、HTTP API 経由でも実行できる。

**主要クラス・関数**
- `ButlySleeptime` — 整理処理の本体クラス
  - `ButlySleeptime(base_dir=None, instances_dir=None)` — 既定は従来のプロジェクトパス。評価・テストでは全Stage、DBバックアップ、人物統計の保存先を隔離パスへ差し替え可能
  - `get_instance_key_memory(instance_name)` — インスタンス別 Key_Memory 取得
  - `get_instance_instruction(instance_name)` — インスタンス別 system_instruction 取得
  - `process_instance(instance_path)` — Stage 1 → Stage 2 の順に実行（`skip_knowledge_generation` による Stage 2 スキップ対応）
  - `stage_1_cleanup(instance_path)` — short_term_json flush、mid_term 追記、digest・headlines・relationship 生成
  - `_generate_daily_digest(instance_path, new_text)` — 当日RAWからダイジェスト生成（`digest_max_input_chars` 超過時は日付ヘッダ区切りでチャンク分割）
  - `_split_text_by_date_headers(text, max_chars)` — 日付ヘッダ `[YYYY-MM-DD ...]` を区切りにテキストをチャンク分割するヘルパー
  - `stage_2_knowledgeize(instance_path, db_type)` — 1_integrated JSON を日付グループ化し、ファイル単位のチャンク分割でナレッジカードを生成・DB登録（`knowledge_max_input_chars` で制御）
  - `ask_gemini_to_summarize(session_text, db_type)` — LLM にナレッジカード抽出を依頼
  - `run_with_progress(instance_name)` — 上記を順番に実行し進捗を更新（`skip_knowledge_generation` 対応）
  - `estimate_workload(instance_name)` — 処理量の見積もりを返す
  - `update_status(instance_name, state, progress, message)` — 実行ステータス更新
- `sleeptime_store` — インスタンス別の実行ステータスを保持するグローバル dict

---

### `migrate_embeddings.py`
プロバイダーを切り替えた際に既存ナレッジカードの `embedding_blob` を再生成する。

- `get_db_path(instance_name)` — インスタンスの DB パスを解決
- `migrate_instance(instance_name, batch_size, dry_run)` — 全カードの embedding を再生成
- CLI: `--instance` / `--batch-size` / `--dry-run` / `--all`

---

## evals/locomo/

LoCoMo公式JSONの固定会話をButlyへ投入する、環境非依存の評価CLI。
公式データは同梱せず、`tests/evals/fixtures/mini_locomo.json`には同じスキーマの
合成データのみを置く。評価データはrun ID単位の`workspace/`へ隔離し、
本番`butly_core/instances/`配下への出力を拒否する。

| ファイル | 役割 |
|---|---|
| `dataset.py` | `LocomoTurn` / `LocomoSession` / `LocomoQuestion` / `LocomoConversation` DTOと公式JSON parser |
| `workspace.py` | run ID単位の隔離ディレクトリ、`ButlyRuntime` / `ButlySleeptime`生成 |
| `adapter.py` | `speaker_a=user` / `speaker_b=assistant`変換と元日時・evidence追跡用meta付き保存 |
| `sleeptime_runner.py` | Stage 1/2の同期実行と`results/sleeptime_log.jsonl`記録 |
| `qa_runner.py` | RAG有効・外部検索無効で`ButlyRuntime.chat()`を実行し、QA結果とTraceを保存 |
| `replay.py` | セッションReplay、Sleeptime、QA、checkpoint更新のオーケストレーション。`resume_evaluation()`で途中再開 |
| `artifacts.py` | JSON/JSONL、Traceコピー、セッション前後スナップショットの保存 |
| `scorer.py` | LoCoMo公式互換採点（正規化+stemming Token F1、カテゴリ別規則、No-info判定）とButly固有指標。`scores.json` / `errors.jsonl`出力 |
| `stemming.py` | 依存追加なしのPorter (1980) stemmer。公式のnltk stemmerと稀な語で差が出る旨をdocstringに明記 |
| `report.py` | `scores.json`から`summary.md`を生成 |
| `checkpoint.py` | セッション/Sleeptime/QA単位のatomicなcheckpoint。run ID照合と破損検出つき |
| `config.py` | CLI設定DTO（`from_json_dict()`でresume時復元）とprofile YAML読込 |
| `cli.py` | `run` / `resume` / `score` / `report` subcommands。`run`は採点・レポートまで実行 |
| `profiles/` | Full Local / Fixed Memory Pipelineのprofile例（`*.example.yaml`） |
| `colab/` | Drive・モデルサーバー・CLI呼び出しのみの薄いNotebook（評価ロジック禁止） |

---

## routers/

FastAPI のルーターモジュール群。各ルーターは `dependencies.py` から共有オブジェクトを参照する。

| ファイル | エンドポイント群 |
|---|---|
| `chat.py` | `/chat` (REST) / `/chat/stream` (SSE) / `/ws` (WebSocket) |
| `instances.py` | `/instances` CRUD、`/config`、`/history`、`/glossary` |
| `sleeptime.py` | `/sleeptime/run`、`/sleeptime/status`、`/sleeptime/estimate` |
| `database.py` | `/database/cards` CRUD（ナレッジカード管理） |
| `settings.py` | `/settings`、`/api-key`、`/config`、`/prompts` |
| `dashboard.py` | `/status`（CPU/MEM）、`/discovery`、`/news` |
| `devices.py` | `/devices`、`/tv/key`、`/tv/launch`（Fire TV 制御） |

---

### `routers/chat.py`
チャット用 REST + SSE + WebSocket エンドポイント。内部では `ChatService.execute()` / `ChatService.execute_stream()` に委譲する。

- `ConnectionManager` — WebSocket 接続管理クラス（接続一覧・broadcast）
- `notify_ai_status(status)` — AI の処理状態を全クライアントへ通知
- `POST /chat` — REST 版チャットエンドポイント（バッファ応答）
- `POST /chat/stream` — SSE ストリーミングエンドポイント。`metadata` → `chunk` → `done` の順にイベントを送出
- `_sse_event(event_name, data)` — SSE メッセージのフォーマッタ
- `WebSocket /ws` — 双方向 WebSocket（チャット + AI ステータス通知）

---

## butly_api/

正式フロントエンド移行（`docs/planning/active/frontend_migration_plan.ja.md`）Phase 0 で導入した versioned API（`/api/v1`）の transport 層。HTTP の変換だけを担い、業務ロジックは Runtime / service へ委譲する。legacy routers は import しない。

| ファイル | 役割 |
|---|---|
| `app.py` | `create_app(context, lifespan, extra_routers)` — side effect の少ない app factory。OpenAPI 生成は context なしで行う。OpenAPI に `DesktopToken`（HTTP Bearer）security scheme を付与 |
| `auth.py` | `DesktopTokenAuthMiddleware` — `/api/v1/*`（health 除く）への Bearer token 認証（pure ASGI）。`BUTLY_DESKTOP_TOKEN` 未設定なら無効（開発 / Streamlit 併用モード） |
| `context.py` | `ApiContext` — readiness 判定に使う実行時状態（data_dir / runtime supplier / auth_token 等） |
| `errors.py` | `ApiException` と `/api/v1` 共通 error envelope（`ApiError`）への正規化 handler。legacy route は FastAPI default（`{"detail": ...}`）を維持 |
| `middleware.py` | `RequestIDMiddleware` — `X-Request-ID` の採番・伝播（pure ASGI） |
| `version.py` | `BACKEND_VERSION` / `API_VERSION` / `API_V1_PREFIX` |
| `routers/system.py` | `GET /api/v1/health` / `/ready` / `/app-info` / `/capabilities` |
| `routers/instances.py` | `GET /api/v1/instances`（typed 一覧） / `GET /api/v1/instances/{name}/messages`（typed 履歴 + `last_interaction_at`。cursor pagination は記憶ストア正規化後） |
| `routers/chat.py` | `POST /api/v1/chat`（non-stream fallback） / `POST /api/v1/chat/stream`（typed SSE: metadata → chunk* → done、失敗時 error 終端）。`ButlyRuntime` へ委譲する transport adapter |
| `schemas/common.py` | `ApiError` envelope |
| `schemas/system.py` | health / readiness / app-info / capabilities の DTO |
| `schemas/chat.py` | chat / message history / SSE event（discriminated union）の contract schema |
| `schemas/instances.py` | `InstanceSummary` / `InstanceListResponse` |

契約 artifact は `scripts/generate_openapi.sh` で再生成する: OpenAPI snapshot
（`openapi/butly.openapi.json`、`tests/test_openapi_snapshot.py` が差分検出）と
SSE parser contract fixture（`openapi/sse_fixtures/*.sse`、
`scripts/generate_sse_fixture.py` で生成、`tests/test_sse_fixture.py` が差分検出。
frontend の手書き SSE parser と契約を共有するための正本）。

---

## butly_core/

コアロジックパッケージ。`config.py` が全体の定数・モデル設定を管理する。

### `butly_core/config.py`
アプリ全体の設定定数を管理。

- `AI_CONFIG` — モデル別の設定（chat / summary / knowledge / embedding / gatekeeper / context_classifier）
- `SYSTEM_CONFIG` — パス定義・メモリ上限・Brain パラメータ・`memory_probe`・`gatekeeper` tier 閾値・`chat.streaming_enabled`・`glossary` 等
- `USER_CONFIG_PATH` — ユーザーカスタム設定ファイルのパス
- `_recursive_update(base, override)` — 設定辞書を再帰マージするユーティリティ

---

## butly_core/chat/

チャット機能の DTO とオーケストレーション層。

### `butly_core/chat/types.py`
チャット機能で使う Pydantic モデルとバリデーション関数。LLM プロバイダーに依存しない。

- `Attachment` — 添付ファイル（kind / mime_type / data_base64 / name / size）
- `ChatRequest` — チャットリクエスト（text / attachments / instance_name / model_name / use_rag / use_web_search 等）
- `ChatResponse` — チャット応答（text / keywords / references / tier / need / session_state 等）
- `validate_attachments(attachments)` — 枚数・サイズ・MIME タイプのバリデーション
- `normalize_ws_payload(payload)` — WebSocket ペイロードを `ChatRequest` に正規化

---

### `butly_core/chat/service.py`
チャット実行のステートレスオーケストレーター。1 リクエストごとに以下の順番で処理する。

```
1. コンポーネント取得 (Memory / Brain / Chronos)
2. 時刻コンテキスト生成 (Chronos.get_system_note → full_prompt の冒頭に付加)
3. Gatekeeper.classify → tier 判定 + need / need_intent / probe candidates（Gatekeeper 無効時は mid 固定 + use_rag に応じて need を仮設定）
4. SessionState.increment_turn → ターン数・last_tier 更新
5. MemoryBlockBuilder.build → 記憶ブロック辞書構築（need 有り + use_rag=True 時のみ brain を渡す）
5.5. Web検索実行（非Gemini + use_web_search=True 時のみ。Tavily / Ollama Cloud Web Search。結果は memory_blocks["web_search_context"] に格納）
6. ProviderFactory.create → Provider 選択（インスタンス config > リクエスト > グローバル）、vision チェック
7. provider.generate(full_prompt, attachments, context) と gatekeeper.update_state(...) を `asyncio.gather()` で並列実行
8. state_delta があれば session_state.apply_delta で反映
9. debug_info を統合し debug_logs/ に保存。Trace Graph (trace.json) を traces/ に保存（issue #51）
10. memory.save_single_turn → 会話を short_term_json に保存
11. memory.maintain_memory → 閾値超過時に short_term → session_digest へ折りたたみ
```

- `ChatService.execute(request, ...)` — 上記フローを実行する静的 async メソッド（バッファ応答 / `ChatResponse` 返却）
- `ChatService.execute_stream(request, ...)` — SSE 用のストリーミング版。Provider の `async_generate_stream()` を呼び、`metadata` / `chunk` / `done` / `error` イベントを順次 `yield` する。StateUpdater は `asyncio.create_task` で並列実行され、`done` 直前に await される
- `_is_gemini_model(model_name)` — モデル名から Gemini かを判定するヘルパー（Web 検索分岐用）
- `_write_rotating_json(target_dir, payload, max_history, *, log_label)` — `latest.json` + `history/{ts}.json` ローテーション書き出しの共通ヘルパー
- `_save_debug_log(instance_dir, payload, max_history=20)` — `debug_logs/` へデバッグ情報を保存
- `_save_trace(instance_dir, trace_payload, max_history=20)` — `traces/` へ Trace Graph (trace.json) を保存
- `_build_and_save_trace(...)` — 実行事実 + collector の `llm_calls` から `build_chat_trace()` で TraceGraph を構築し `_save_trace` で保存（issue #51。`SYSTEM_CONFIG["trace"].enabled=false` で保存スキップ。構築/保存失敗は応答に影響させない）。`execute()` / `execute_stream()` は本体全体を `start_collection()` / `reset_collection()`（try/finally）で包む

---

## butly_core/trace/

Trace Graph (issue #51): 1 回答の生成フローを「ノード + エッジ」のグラフとして記録・
可視化するデバッグ機能。フロントエンド非依存で、`trace.json` 保存と Mermaid 生成までを担う。

### `butly_core/trace/types.py`
Trace Graph の DTO（Pydantic モデル）と定数・ヘルパー。

- `TraceNode` — 処理フロー上の 1 ノード（id / label / type / status / summary / metadata）
- `TraceEdge` — 有向エッジ。JSON では `from` / `to` キーで出力（フィールド名は source/target + alias）
- `TraceGraph` — 1 ターン分のフロー全体。`to_json_dict()` で `from`/`to` 形式の dict を返す
- `NodeType` — `input` / `loader` / `decision` / `retrieval` / `tool` / `context` / `provider` / `llm` / `formatter` / `memory` / `housekeeper` / `end`
- `TraceStatus` — `active`（通った） / `skipped`（候補だが未使用） / `fallback` / `error` / `warning`
- `TRACE_SCHEMA_VERSION` — trace.json スキーマのバージョン
- `summarize_text(text, limit=80)` — summary 用にテキストを 1 行へ切り詰める（長文をそのまま残さないための長さ切り詰め。PII 除去・匿名化ではない）

### `butly_core/trace/collector.py`
1 ターン中の **全 LLM 呼び出し**を ContextVar 経由で収集する軽量コレクター。
ContextVar に可変 list を入れることで、`run_in_threadpool` / `asyncio.create_task` へ
コピーされた context からも同じ list に append が届く（並列生成 + StateUpdater 対応）。

- `start_collection()` — 収集開始。reset 用 Token を返す（ChatService が try/finally で使用）
- `reset_collection(token)` — 収集終了。context 不一致時はログのみで無害
- `record_llm_call(*, purpose, model, connection_id, duration_ms, prompt_chars, error, metadata)` — 1 呼び出しを記録。**収集未開始なら no-op**（sleeptime 等には副作用なし）
- `get_collected()` / `is_collecting()`

**記録ポイント（purpose）:** `context_classifier`（ContextClassifier.classify）/ `state_updater`（StateUpdater.update）/ `embedding`（ButlyBrain.get_embedding）/ `keyword_extract`（ButlyBrain.extract_keywords）/ `chat_generate`（ChatService の main 生成・stream 両方）

### `butly_core/trace/builder.py`
ChatService の実行事実（gatekeeper 出力・記憶ブロック・Provider 情報・timing・collector の
`llm_calls`）から `TraceGraph` を再構成する純粋関数。

- `build_chat_trace(..., llm_calls=None)` — `user_message → gatekeeper → (memory_probe / rag / web_search) → context_assembly → provider → llm_call → memory_write → state_update / response` のノードとエッジを構築。RAG 未注入・Web 検索 OFF・Gatekeeper フォールバック・LLM 生成失敗（`generation_error`）等を status で表現する
- 補助 LLM ノード: `llm_calls` の記録から `llm_context_classifier` / `llm_embedding`（複数回は連番）/ `llm_keyword_extract` / `llm_state_updater` を親ノード（gatekeeper / memory_probe / state_update）にぶら下げて生成。`metadata.aux=True` + `metadata.purpose` を持つ。**main 生成（chat_generate）は新ノードにせず既存 `llm_call` の metadata（prompt_chars 等）を拡充する**

### `butly_core/trace/filters.py`
表示フィルタ。**trace.json には常に全ノードを保存**し、表示側でこのフィルタを適用する。

- `filter_trace(trace, *, detail="full", hidden_nodes=())` — `detail="summary"` で補助 LLM ノード（`metadata.aux`）を除外。`hidden_nodes` は **purpose**（例: `"embedding"` で全 embedding ノード）または node id（例: `"web_search"`）で指定

### `butly_core/trace/mermaid.py`
`TraceGraph` を Mermaid flowchart 文字列へ変換する軽量レンダラー。

- `render_mermaid(trace, *, direction="TD", detail="full", hidden_nodes=())` — `filter_trace` を適用後、ノード宣言 + エッジ + classDef を出力。active/skipped/fallback/error/warning を色分けし、skipped/fallback/error のエッジは線種で区別する
- `_sanitize(text)` — Mermaid ラベル構文を壊す文字（`"` `[` `]` 等）を置換

**保存先:** `butly_core/instances/{instance}/traces/latest.json` + `traces/history/{ts}.json`（debug_logs と同じローテーション方式。再構築可能な telemetry のため atomic write 対象外）。

**設定（`SYSTEM_CONFIG["trace"]` / `get_settings().system.trace`）:**

| キー | デフォルト | 説明 |
|---|---|---|
| `enabled` | `true` | trace.json の**保存** ON/OFF |
| `detail` | `"full"` | 表示フィルタ。`"summary"` で補助 LLM ノードを非表示（保存は常に full） |
| `hidden_nodes` | `[]` | 表示フィルタ。purpose / node id で個別非表示 |

---

## butly_core/core/

AIアシスタントのコアエンジン群。

### `butly_core/core/memory.py`
ファイルベースの多層記憶を管理するクラス。

- `ButlyMemory(base_dir, instance_name)` — 各インスタンスの記憶ディレクトリを初期化
  - `get_system_instruction()` — system_instruction.txt を読み込む
  - `get_key_memory()` — Key_Memory.txt（根幹記憶）を読み込む
  - `get_glossary()` — glossary.yaml からアクティブなエントリを `- term: definition` 形式で返す
  - `get_glossary_raw()` — glossary.yaml を dict で返す（UI/API 向け）
  - `save_glossary(data)` — glossary.yaml を書き出す
  - `get_mid_term_text_content()` — mid_term.txt を上限文字数でカットして返す
  - `get_mid_term_digest()` — mid_term_digest.txt（エピソード付きダイジェスト）を返す
  - `get_mid_term_relationship()` — mid_term_relationship.txt（関係性グラフ）を返す
  - `get_session_digest()` — `session_digests/*.txt` を相対時刻ヘッダー（例: `--- 約30分前 ---`）付きで結合して返す。旧 `session_digest.txt` も互換読み取り
  - `load_recent_sessions(limit)` — short_term_json から直近 N 件の会話を返す
  - `save_single_turn(user_msg, ai_msg, meta=None, created_at=None)` — 会話を short_term_json に保存。`meta`（話者帰属: person_id / display_name / lane / source / channel_key）指定時は user メッセージに構造化メタデータとして刻む。`created_at` 指定時は元日時を保持し、同一日時の重複は連番ファイル名で上書きを防ぐ
  - `get_last_interaction_time()` — 最後のインタラクション日時を返す
  - `maintain_memory(brain)` — short_term が閾値超えたら session_digest に折りたたむ。溢れバッチに複数話者がいる場合は user 発言を `「display_name」` でラベリング
- ヘルパー関数:
  - `_format_relative_time(dt, now)` — `datetime` から「約30分前」等の文字列を生成
  - `_parse_session_filename_timestamp(name)` — `session_YYYYMMDD_HHMMSS[_ffffff].txt` 形式のファイル名から日時を復元
  - `_strip_legacy_time_line(text)` — 先頭行に `Time: 2026-...` を含む旧形式を除去

---

### `butly_core/core/turn_meta.py`
会話ターン message の話者帰属メタ（person_id / lane 等）の読み出しヘルパ。
**meta 欠落時は owner / direct / web と解釈する**後方互換規則の実装（マイグレーション不要）。

- `effective_meta(msg, owner_person_id=...)` — 後方互換規則を適用した meta を返す
- `normalize_lane(value)` — lane の正規化（欠落 → `direct`、未知値 → `other`）
- `has_multiple_speakers(messages)` — user メッセージに複数の person_id がいるか
- `speaker_label(msg, default_user_name)` / `user_label(msg, ..., multi_speaker=)` — 整形用ラベル。複数話者時のみ `「display_name」` 形式
- `message_text(msg)` — parts[0]（str / dict 両対応）から本文を取り出す

---

### `butly_core/core/brain.py`
LLM 呼び出しと RAG 検索のエンジン。Provider に依存しない中間層。

- `ButlyBrain(base_dir)` — 初期化
  - `get_embedding(text)` — Embedding ベクトルを取得
  - `extract_keywords(text, override_config)` — RAG 用キーワードを LLM で抽出
  - `search_knowledge(keywords, query, instance_name, limit, override_config)` — コサイン類似度で RAG 検索（時間減衰込み）
  - `quick_vector_search(user_input, instance_name, limit, threshold, override_config)` — キーワード抽出なしの純粋なベクトル検索
  - `quick_vector_search_diag(...)` — Layer 別診断情報（候補数 / 閾値判定 / 平均スコア等）を含む診断付き版
  - `summarize_conversation(conversation_text, override_config)` — 会話テキストを要約（summary モデル使用）
  - `generate_knowledge_card(text, override_config)` — ナレッジカード JSON を生成（knowledge モデル使用）
  - `_calculate_cosine_similarity(vec1, vec2)` — コサイン類似度計算
  - `_get_provider(model_name)` — ProviderFactory 経由で Provider を取得

---

### `butly_core/core/chronos.py`
時刻・曜日・前回からの経過時間を管理し、会話コンテキストへの注入文字列を生成する。

- `ButlyChronos`
  - `get_delta_text(last_time)` — 前回インタラクションからの経過時間を「○分ぶり」形式で返す
  - `_get_time_segment(hour, is_weekday, is_holiday_override)` — 時間帯モードを返す
  - `get_system_note(is_holiday, is_work_time, last_interaction_time)` — 現在日時テキストを返す（システムプロンプトの冒頭に追加）

---

### `butly_core/core/database.py`
ナレッジカードの SQLite CRUD を管理するクラス。

- `ButlyDatabase(db_path)` — テーブルの作成・マイグレーションを自動実行
  - `register_knowledge(card_data)` — カードを登録または更新（count をインクリメント）
  - `get_cards(limit, offset, category, search)` — カード一覧を取得
  - `get_card(card_id)` — 1 件取得
  - `update_card(card_id, fields)` — フィールドを更新
  - `delete_card(card_id)` — 削除
  - `pin_card(card_id, is_pinned)` — ピン留め状態を更新
  - `log_access(card_id)` — アクセスログを記録

**テーブル**:
- `knowledge_cards` — ナレッジカード本体（embedding_blob 含む）
- `access_logs` — カードのアクセス履歴

---

### `butly_core/core/instance_manager.py`
AIインスタンス（ペルソナ）のファイルシステム管理。

- `InstanceManager(base_dir)`
  - `create_instance(name, template_text, key_memory)` — インスタンスフォルダと初期ファイル群を作成
  - `delete_instance(name)` — フォルダを削除
  - `rename_instance(old_name, new_name)` — フォルダをリネーム
  - `list_instances()` — インスタンス名の一覧を返す
  - `update_instruction(instance_name, new_text)` — system_instruction.txt を上書き
  - `get_instance_prompts(instance_name)` — system_instruction / key_memory を取得
  - `get_instance_config(instance_name)` — インスタンス固有の config.json を取得

---

### `butly_core/core/fire_tv.py`
ADB over TCP で Fire TV を制御するモジュール。ADB 未インストール環境でも import 可能（try import）。

- `connect()` — ADB 接続
- `get_status()` — 接続・再生状態をキャッシュ TTL 付きで返す
- `send_key(keycode)` — キー入力送信
- `launch_app(package_name)` — アプリ起動
- `KEYCODES` / `APPS` — キーコードとアプリのマッピング辞書

---

## butly_core/core/gatekeeper/

ユーザー発言を受け取り、「どの記憶レイヤーを使うか」を決定するメタ認知エンジン。

```
Gatekeeper.classify(user_input, history, session_state)
    ├─ ContextClassifier.classify()  → tier (reflex / mid) を決定（3スコア: rc/ew/cn）
    ├─ StateUpdater.update()         → session_state の差分を生成
    └─ MemoryProbe.probe()           → LLM不要の事実ベース記憶検索（ベクトル検索 + 用語集マッチ）
    ※ tier と need は独立。need は MemoryProbe のヒットから設定され、tier に関係なく RAG 注入を決定する
```

---

### `gatekeeper/__init__.py`（`Gatekeeper` クラス）
外部 API 互換の Facade。`ChatService` からはここだけを呼ぶ。

- `Gatekeeper(base_dir)`
  - `classify(user_input, history_msgs, session_state, override_config, instance_dir, brain, memory_manager)` — ContextClassifier を実行 → 結果の `need_intent` を見て MemoryProbe を選択実行。最終 `need` の決定（LLM 意図 + 事実裏付け 2 段判定）も内部で行う。`instance_dir` から `recent_digest_headlines.json` を読み込み ContextClassifier に渡す。**StateUpdater は呼ばない**（並列実行は ChatService 側で行う）
  - `update_state(user_input, history_msgs, session_state, override_config, instance_dir)` — StateUpdater を単独で実行し state_delta を返す。`ChatService.execute()` / `execute_stream()` が応答生成と並列で呼ぶ
  - `migrate_context_order_to_levels(config)` — 旧 `context_order` 形式の config を新 `context_levels` 形式へ自動移行するヘルパー

**返却値の構造:**
```python
{
    "tier": "reflex" | "mid",     # ContextClassifier の出力（RAG とは独立）
    "topic": str,
    "need": str | None,           # LLM 意図 + 事実裏付け の両方が成立した時のみ
    "need_intent": str | None,    # LLM が出した意図: past_fact / glossary / relationship / None
    "search_targets": list | None, # need 有時の候補タイトル / glossary 用語
    "state_delta": dict,
    "llm_tier": str,
    "llm_reasoning": str,
    "llm_scoring": {
        "response_complexity": float,
        "emotional_weight": float,
        "continuity_need": float,
    },
    "memory_probe": {
        "status": "hit" | "no_hit" | "deep_search" | "skipped",
        "candidates": list[dict],
        "glossary_hits": list[dict],
        "layers": dict,   # Layer 別診断 (glossary / vector / deep)
    }
}
```

---

### `gatekeeper/context_classifier.py`
LLM に 3 スコア（0–1）+ `need_intent` を出力させ、Python 側でルールに基づき tier を決定する。

- `ContextClassifier(base_dir)`
  - `classify(user_input, history_msgs, current_topic, recent_headlines, override_config)` — tier 判定 + need_intent 出力を実行。`recent_headlines` でダイジェストから抽出した見出しを注入

**tier 決定ロジック:**
| tier | 条件 |
|---|---|
| `reflex` | `response_complexity <= 0.4` AND `continuity_need <= 0.3` |
| `mid` | それ以外 |

**need_intent (LLM 出力):**
| 値 | 用途 |
|---|---|
| `past_fact` | 具体的な過去の出来事/決定/会話の参照 |
| `glossary` | 用語や固有名詞の意味問い合わせ |
| `relationship` | 関係性・ムード推移・習慣の質問 |
| `null` | 長期記憶不要（挨拶・将来設計・自己完結発話） |

parse 失敗時は `asks_for_specific_past_detail()` を fallback として使用（マッチで past_fact、なしで null）。

---

### `gatekeeper/memory_probe.py`
LLM 呼び出しなしの事実ベース記憶検索。Glossary scan は常時実行、vector / deep は need_intent でゲート。

- `MemoryProbe()`
  - `probe(user_input, brain, memory_manager, history_msgs=None, need_intent=None, recent_headlines=None, override_config=None, instance_name=None)` — Layer 1.5 を必ず実行し、`need_intent` に応じて Layer 1 / Layer 2 を選択的に実行。返却値の `layers` に Layer 別の診断情報を含む
  - `_match_glossary(user_input, memory_manager, history_msgs=None, override_config=None)` — Lorebook 統合: term/aliases を user_input + 直近履歴でマッチし、raw hits を返却（フィルタ・ソート無し）。各 hit に `priority` / `_yaml_index` / `match_source` ("user"|"history") を付与
  - `_quick_vector_search_diag(user_input, brain, instance_name, limit, threshold, override_config)` — `brain.quick_vector_search_diag()` のラッパー
  - `_deep_search_diag(user_input, brain, instance_name, override_config)` — keyword 抽出 + `search_knowledge` を呼び、診断データ付きで返す
  - `_check_headline_match(user_input, recent_headlines)` — `recent_digest_headlines.json` の見出しと一致するかチェック
  - `_extract_history_text(history_msgs, scan_depth, scan_target)` — 履歴から scan_target に応じてメッセージを抽出（1 ターン = user+assistant 1 ペア）

**検索レイヤー:**
| レイヤー | 内容 | 実行条件 |
|---|---|---|
| Layer 1.5 | Glossary Match (term/aliases、user_input + 履歴 scan_depth ターン) | **常時実行**（regex のみ・LLM 不要） |
| Layer 1 | Quick Vector Search（コサイン類似度） | `need_intent ∈ {past_fact, relationship}` のみ |
| Layer 2 | Deep Search | Layer 1 ヒット無し + 過去参照パターン検出時のみ |

`need_intent=None` の場合、Glossary scan だけ実行され、glossary ヒット有無で `status="hit"`/`"no_hit"` を返す（vector / deep は skip）。`memory_manager=None` のときは Glossary もスキップ、`brain=None` のときは vector / deep をスキップ。

**`layers` 診断情報** (例):
```python
{
  "glossary": {"executed": True, "matches": 2},
  "vector":   {"executed": True, "result_count": 3, "max_score": 0.71, ...},
  "deep":     {"executed": False, "reason": "no past_ref_pattern"},
}
```

---

### `gatekeeper/state_updater.py`
ユーザー発言から `session_state` の差分（state_delta）を LLM で生成する。

- `StateUpdater(base_dir)`
  - `update(user_input, history_msgs, current_state, override_config)` — state_delta を返す

**state_delta の構造:**
```python
{
    "topic": str | None,         # 現在の話題（変化した場合）
    "mood": str | None,          # ユーザーの気分
}
```

---

### `gatekeeper/session_state.py`
会話セッション全体の内部状態を JSON ファイルで永続管理する。

- `SessionState(instance_dir)`
  - `apply_delta(delta)` — state_delta を現在の state に適用する
  - `increment_turn(tier, history_msgs)` — ターン数と最後の tier を更新する。topic 寿命チェック（10ターン＋直近3ターン言及なしで自動リセット）を含む
  - `to_dict()` — 現在の状態を dict で返す（内部管理フィールド `topic_set_at_turn` は除外）
  - `_load()` / `_save()` — `session_state.json` との I/O

**管理するフィールド:**
```python
{
    "topic": str,        # 現在の会話の話題（live topic）
    "mood": str,         # ユーザーの気分 (neutral / casual / focused 等)
    "turn_count": int,   # ターン数
    "last_tier": str,    # 直前の tier
    "topic_set_at_turn": int,  # 内部管理用。topic が設定されたターン数
}
```

---

### `gatekeeper/memory_builder.py`
tier に応じて、LLM プロバイダーに渡す「記憶ブロック辞書」を組み立てる。

- `MemoryBlockBuilder`
  - `build(tier, memory_manager, brain, user_input, instance_name, ...)` — 記憶ブロック辞書を構築して返す

**tier 別の記憶ブロック構成:**
| tier | short_term | session_digest | mid_term | rag_context |
|------|-----------|---------|---------|------------|
| `reflex` | ✅ | ✅ | — | `need` 有時 ✅ |
| `mid` | ✅ | ✅ | ✅ | `need` 有時 ✅ |

RAG (`rag_context`) は `need` に連動する独立判定で、tier ではなく `MemoryProbe` のヒットで注入される。

- `build_system_instruction_from_blocks(blocks, memory_manager, use_google_search)` — **不変セクション**（system_instruction + Key_Memory）のみを結合して system_instruction 文字列を生成
- `build_context_prefix(blocks, memory_manager, use_google_search)` — **可変セクション**（現在時刻 / glossary / mid_term / RAG / session_digest / tier 情報 / Google 検索注意書き / Web 検索結果）を結合し、Provider が会話履歴の先頭に user メッセージとして注入する文字列を生成
- `is_long_definition(definition)` — glossary エントリの definition が複数行（= 長文 / 「関連設定」扱い）かを判定。`strip()` 後の `\n` 有無で判定
- `_build_glossary(blocks, memory_manager, level, h)` — Lorebook 統合: probe ヒットを短文/長文に振り分け、`(priority, _yaml_index)` で安定ソート、`max_entries` 件数制限、`max_chars` greedy skip を適用。「用語説明」セクションを先に、「関連設定」セクションを後に出力

**Glossary 注入の制御 (SYSTEM_CONFIG["glossary"] + instance_config["glossary"] で上書き可):**

| キー | デフォルト | 説明 |
|---|---|---|
| `scan_depth` | 2 | 直近何ターン分の履歴をスキャンするか (1 ターン = user+assistant 1 ペア)。0 で user_input のみ |
| `scan_target` | "both" | "user" / "assistant" / "both" |
| `max_entries` | 20 | 注入する最大エントリ数 |
| `max_chars` | 4000 | 注入合計文字数の上限。greedy skip で個別エントリをスキップ |

---

## butly_core/external/

外部プラットフォーム（Discord / LINE 等）との接続層。SDK 依存は各 adapter に閉じ、
解決ロジック（account_mapping / person_registry / pairing）は純粋ロジックとして分離。

### `butly_core/external/person_registry.py`
人物レジストリ。外部アカウント `(source, external_user_id)` を内部の person_id に解決する。
保存先は `DATA_DIR/persons.json`（外部 ID を含むため gitignore 対象。雛形: `persons.json.example`）。

- `provisional_person_id(source, external_user_id)` — 決定的な仮 ID `p_{source}_{hash}` を発行（書き込み不要、外部 ID は RAW ログに直接出さない）
- `PersonRegistry(data_dir)`
  - `resolve(source, external_user_id)` — aliases 完全一致 → 仮 ID 発行。`merged_into` は読み出し時に解決
  - `resolve_person_id(person_id)` / `display_name(person_id)` / `owner_person_id()`
  - `merge_person(from_id, to_id)` — レジストリに `merged_into` を記録（RAW ログは書き換えない）
  - `record_appearances({person_id: {count, first_seen, last_seen}})` — adoption gate 用の登場集計（Sleeptime Stage 1 から呼ばれる）

このほか `account_mapping.py`（instance 解決）、`discord_adapter.py` / `line_adapter.py`
（受信 → `ChatRequest` 組み立て）、`pairing.py`（LINE ペアリング）、`message_splitter.py` /
`reply_profiles.py`（返信整形）を含む。

---

## butly_core/search/

プロバイダー非依存の汎用 Web 検索モジュール。Gemini 以外のプロバイダー（OpenAI / Ollama）で Web 検索を利用するためのパッケージ。

### `butly_core/search/types.py`
検索結果の Pydantic DTO。

- `SearchResult` — 検索結果 1 件（title / url / content / score）

### `butly_core/search/base.py`
検索プロバイダーの抽象基底クラス。

- `BaseSearchProvider` (ABC)
  - `search(query, max_results)` — クエリを実行し SearchResult リストを返す（同期）
  - `is_available()` — API キー設定済みか等、利用可能かを返す

### `butly_core/search/tavily_provider.py`
Tavily Search API を使った検索プロバイダー実装。

- `TavilySearchProvider(BaseSearchProvider)` — 環境変数 `TAVILY_API_KEY` で認証
  - `search(query, max_results)` — Tavily API で検索、結果を SearchResult に変換
  - `is_available()` — API キーが設定済みかチェック

### `butly_core/search/usage_tracker.py`
月次の検索 API 使用量カウンター。

- `UsageTracker` — `butly_core/search_usage.json` に YYYY-MM キーでプロバイダー別累計を記録
  - `increment(provider="tavily")` — 指定プロバイダーの当月カウントを +1
  - `get_current_month_count(provider=None)` — 当月の使用回数を返す（provider=None で合計）
  - `get_all()` — 全月の使用量を dict で返す
  - 旧形式 `{YYYY-MM: int}` → 新形式 `{YYYY-MM: {tavily: N, ollama: M}}` への lazy migration 対応

### `butly_core/search/ollama_provider.py`
Ollama Cloud の Web Search API を使用した検索プロバイダー。

- `OllamaWebSearchProvider(BaseSearchProvider)` — 環境変数 `OLLAMA_WEB_SEARCH_API_KEY` で認証
  - `search(query, max_results)` — `https://ollama.com/api/web_search` で検索
  - `is_available()` — API キーが設定済みかチェック

### `butly_core/search/__init__.py`
パッケージ公開。

- `create_search_provider(chat_model="")` — chat モデルに応じて検索プロバイダーをファクトリ生成。Ollama chat + `OLLAMA_WEB_SEARCH_API_KEY` → OllamaWebSearchProvider、それ以外 → TavilySearchProvider

---

## butly_core/llm/

LLM プロバイダーの抽象化レイヤー。

### `butly_core/llm/base.py`
全プロバイダーが実装する抽象基底クラス。

- `BaseProvider` (ABC)
  - `generate(text, attachments, context)` — テキスト+添付から応答を生成（同期）
  - `supports_vision(model_name)` — vision 対応モデルかを判定（静的）
  - `embed(text)` — テキストの embedding ベクトルを返す
  - `classify(prompt, config)` — Gatekeeper 用の分類プロンプトを実行
  - `async_generate(...)` / `async_summarize(...)` / `async_embed(...)` — 同期版を `run_in_threadpool` でラップしたデフォルト async 実装（段階的非同期化用）
  - `async_generate_stream(text, attachments, context)` — SSE ストリーミング向け async generator。デフォルトは `generate()` を threadpool 実行し 1 回 `{"type": "chunk", "text": ...}` を yield、最後に `{"type": "done", ...}` を yield するフォールバック。Provider 個別に逐次出力をオーバーライドする

---

### `butly_core/llm/factory.py`
`ModelRef` / dict / str（旧 API） から Provider を生成するファクトリ。Phase 1–3（2026-05）のリファクタで Connection ベース経路に統合された。

- `ProviderFactory`
  - `create(model)` — 入力を `model_registry.normalize_model_ref()` で `ModelRef` に統一 → `Connection` を引く → `Connection.protocol` に応じた Adapter (`OpenAICompatAdapter` / `GeminiNativeAdapter`) をインスタンス化
  - 旧 API 互換: 文字列 `model_name`（例: `"gpt-4o"`, `"gemini-2.5-flash"`, `"ollama/llama3.2"`, `"grok-4"`）も受理。`infer_connection_id()` で connection を推定する

---

### `butly_core/llm/connections.py`
LLM の「接続情報」を表現する一級オブジェクトと、そのレジストリ。

- `Connection` (dataclass, frozen)
  - フィールド: `connection_id` / `protocol` (`"openai_compat"` | `"gemini_native"`) / `display_name` / `base_url_env` / `api_key_env` / `default_base_url` / `model_prefix` 等
  - `display_label()` / `resolve_base_url()` / `resolve_api_key()` / `strip_model_prefix(model_name)`
- `ConnectionRegistry` — built-in 4 件（`openai` / `xai` / `ollama` / `google`）+ user_config.json `LLM_CONNECTIONS` 由来のユーザー定義を管理
  - `get(id)` / `require(id)` / `list_all()` / `list_user_defined()` / `is_builtin(id)` / `register(conn, *, overwrite_user=False)` / `unregister(id)` / `reset_to_builtin()`
- モジュール関数: `get_connection(id)` / `try_get_connection(id)` / `register_connection(conn)` / `list_connections()` / `is_builtin_connection(id)` / `get_registry()`

---

### `butly_core/llm/model_registry.py`
モデルプリセットと `ModelRef`（Connection + ModelName）の正規化を担う。`config.py` から import せず循環を避ける。

- `ModelRef` (dataclass): `connection_id`, `model_name` / `to_dict()`
- `ModelPreset` (dataclass): 役割 (`chat` / `summary` / `knowledge` / `embedding` / `gatekeeper` / `context_classifier`) ごとの推奨モデル。`ref()` で `ModelRef` を取得
- `infer_connection_id(model_name)` — プレフィックスから connection を推定（旧 API 互換）
- `normalize_model_ref(input)` — str / dict / ModelRef を `ModelRef` に統一
- `resolve_role_model_ref(role, ai_config)` — role 別デフォルトを `AI_CONFIG` から解決
- `get_presets_for_role(role)` / `find_preset(connection_id, model_name)` — Settings UI のドロップダウン用
- `is_deprecated(connection_id, model_name)` / `get_replacement(...)` — 旧モデル名検知＋差し替え推奨

---

### `butly_core/llm/protocols/`
Protocol Adapter 群。`Connection` を受けて、具体的な API protocol を実装する。`providers/` の各シムから利用される。

- `protocols/__init__.py` — `OpenAICompatAdapter` / `GeminiNativeAdapter` を re-export
- `protocols/openai_compat.py` — `OpenAICompatAdapter`。OpenAI 互換エンドポイント（OpenAI / xAI / Ollama / 将来の Groq 等）を共通実装で駆動する。内部で `_openai_compat` ヘルパーを呼ぶ
  - `generate(...)` / `async_generate_stream(...)`
  - サブクラス（`OpenAIProvider` 等）からは `_build_client()` をオーバーライドして接続先 SDK を差し替え可能
- `protocols/gemini_native.py` — `GeminiNativeAdapter`。`providers/gemini.py` の実装をそのまま委譲する薄いシム

---

### `butly_core/llm/providers/gemini.py`
Gemini API プロバイダー。コンテキストキャッシュ・画像アップロード・グラウンディングを管理。

- `GeminiProvider`
  - `generate(text, attachments, context)` — Gemini API でチャット応答を生成
  - `supports_vision(model_name)` — `VISION_UNSUPPORTED_MODELS` リストで判定
  - `embed(text)` — `models/gemini-embedding-001` で embedding を生成
  - `classify(prompt, config)` — Gatekeeper 用の分類（temperature=0 推奨）

---

### `butly_core/llm/_openai_compat.py`
OpenAI 互換プロバイダー (OpenAI / Ollama / xAI) で共通利用するヘルパー関数群。継承ではなく import して使う設計。

- `load_env_file()` — APIkey.env / .env を探してロード
- `is_reasoning_model(model_name)` — OpenAI o1/o3/o4 系の判定（temperature 禁止、max_completion_tokens 使用）
- `resolve_position(context)` — system_instruction の配置位置を解決（context_levels → context_order → "top"）
- `resolve_system_instruction(context)` / `resolve_context_prefix(context)` — Gatekeeper の build 関数を呼び出す
- `build_user_content(text, attachments)` — テキスト + 画像を OpenAI 形式に変換
- `convert_history(history)` — Butly 履歴を OpenAI messages 形式に変換（`role: "model"` → `"assistant"`）
- `build_messages(...)` — system / context / history / user を position に応じて配列化
- `merge_chat_config(base_conf, override_config)` — AI_CONFIG と instance override をマージ
- `build_chat_completion_kwargs(chat_conf, messages, model_name)` — reasoning / 通常の 2 系統で API kwargs を構築
- `build_chat_response(response_text, rag_results)` — ChatResponse を組み立て

---

### `butly_core/llm/providers/openai.py`
OpenAI (GPT) / Azure OpenAI 互換プロバイダー。Phase 1 リファクタ以降は `OpenAICompatAdapter` を継承する薄シム。

- `OpenAIProvider(OpenAICompatAdapter)`
  - `__init__()` — `connection=get_connection("openai")` を渡して親に委譲
  - `_build_client()` — テスト互換のため module-level `_get_client()` を呼ぶ
  - `supports_vision(model_name)` — `_VISION_MODELS` セットで判定（reasoning モデル含む）

---

### `butly_core/llm/providers/ollama.py`
ローカル Ollama サーバー（OpenAI 互換 API）の `OpenAICompatAdapter` シム。

- `OllamaProvider(OpenAICompatAdapter)` — `connection=get_connection("ollama")`。`localhost:11434/v1` に接続
  - `supports_vision(model_name)` — `_VISION_MODELS` セットで判定（llava 等）

---

### `butly_core/llm/providers/xai.py`
xAI (Grok) の `OpenAICompatAdapter` シム。OpenAI SDK + `base_url="https://api.x.ai/v1"` で Chat Completions を利用。

- `XaiProvider`
  - `generate(text, attachments, context)` — xAI Chat Completions API で応答を生成
  - `supports_vision(model_name)` — grok-4 系は Vision 対応、grok-code-fast は非対応
  - `embed(text)` — xAI は embedding API 未提供のため `None` を返す（別プロバイダーで対応）
  - `classify(prompt, config)` — Gatekeeper / SleepTime 用の分類
  - `summarize(conversation_text, config)` — 会話要約

---

## butly_core/prompts/

プロンプトのロード・管理パッケージ。

### `butly_core/prompts.py`（後方互換ラッパー）
旧定数名（`SLEEPTIME_SUMMARIZE_PROMPT` 等）でのアクセスを維持するためのラッパー。  
実体は `butly_core/prompts/__init__.py` の `PromptLoader`。旧コードからの import を壊さないために残されている。

---

### `butly_core/prompts/__init__.py`（`PromptLoader` クラス）
`control/` と `locales/` の 2 種類のプロンプトを統一インターフェースで提供する。

**解決優先順位:**
```
user_prompts.json (ユーザーオーバーライド)
  → control/{name}.txt (機能プロンプト、英語固定)
  → locales/{locale}/{name}.txt (人格プロンプト)
  → locales/en/{name}.txt (言語フォールバック)
```

- `PromptLoader(locale=None)` — locale 未指定時は SYSTEM_CONFIG の値を使用
  - `get(name, **kwargs)` — プロンプトを取得し、`{変数}` を kwargs で展開して返す
  - `get_section_header(key)` — `section_headers.yaml` からセクションヘッダー文字列を取得

**プロンプト名と配置:**
| プロンプト名 | 配置 | 用途 |
|---|---|---|
| `context_classifier` | control/ | Gatekeeper の tier 判定（3スコア方式: rc/ew/cn） |
| `state_updater` | control/ | session_state の差分生成 |
| `brain_extract_keywords` | control/ | キーワード抽出 |
| `sleeptime_summarize` | locales/ | 会話の要約 |
| `brain_summarize_conversation` | locales/ | 会話の折りたたみ要約 |
| `midterm_digest` | locales/ | 中期ダイジェスト生成 |
| `midterm_relationship` | locales/ | 関係性グラフ生成 |
| `web_ui_default_template` | locales/ | システムインストラクションのデフォルトテンプレ |

---

## docs/

| パス | 役割 |
|---|---|
| `docs/README.md` / `README.ja.md` | ドキュメント索引 |
| `docs/guides/` | 現行のセットアップ・運用手順 |
| `docs/reference/` | 現行のアーキテクチャ・機能仕様 |
| `docs/history/` | プロジェクト状況のスナップショット・更新履歴 |
| `docs/planning/active/` | 未完了の実装作業が残る計画書 |
| `docs/planning/archived/` | 実装済みで設計履歴として保管する計画書 |

アーカイブ済み計画は現在の挙動の正ではありません。現行コード・テスト・
セットアップ資料を優先してください。

---

## チャット時のコンテキスト構成フロー

1 回の会話で LLM に渡されるコンテキストの全体像。  
`build_system_instruction_from_blocks` と `build_context_prefix` で 2 分割されている。

```
[system_instruction] ← build_system_instruction_from_blocks() が生成（不変セクション）
─────────────────────────────────────
① system_instruction.txt      ← インスタンスの人格設定
② Key_Memory.txt               ← 根幹記憶（変わらない事実）

[context_prefix] ← build_context_prefix() が生成（可変セクション）
  Provider が会話履歴の先頭に user メッセージとして注入する
─────────────────────────────────────
③ 現在時刻（Chronos.get_system_note）
④ GLOSSARY（共通言語辞書 / 意味記憶） ← glossary.yaml のアクティブエントリ
⑤ [mid tier 以上] mid_term 記憶 ← 2 モードあり:
   - raw モード: mid_term.txt をそのまま
   - summary モード: mid_term_digest.txt + mid_term_relationship.txt
⑥ [need 有り時のみ・tier 非依存] RAG 検索結果  ← MemoryProbe の candidates から構築
⑦ session_digest.txt        ← 直近の会話の会話圧縮ログ
⑧ tier 情報 + topic
⑨ [該当時] Google 検索注意書き

[user turn]
─────────────────────────────────────
⑩ Chronos 日時 + ユーザーメッセージ  ← ChatService が結合した full_prompt

[history（マルチターン）]
───────────────────────────────────
⑪ short_term_json の直近 6 件
```

**tier 別のコンテキスト包含関係:**
```
reflex  ⊂  mid

reflex: ①②③④⑦⑧⑩⑪
mid:    ①②③④⑤⑦⑧⑩⑪

RAG（⑥）は tier と独立。need が設定されている時のみ、reflex / mid どちらでも追加注入される。
```

**mid_term のモード分岐** (`SYSTEM_CONFIG.memory.use_summarized_mid_term`):
| モード | 使用ファイル | 説明 |
|---|---|---|
| `raw`（デフォルト） | `mid_term.txt` | 生の会話要約テキストをそのまま使用 |
| `summary` | `mid_term_digest.txt` + `mid_term_relationship.txt` | Sleeptime が生成した構造化ダイジェスト + 関係性スナップショット |
