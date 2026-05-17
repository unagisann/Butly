# Recent Changes

🌐 **日本語** | [English](recent_changes.md)

## Floating Summary 表示の相対時刻化 (2026-05-17)

`ButlyMemory.get_floating_summary()` がファイル名・絶対タイムスタンプを排除し、相対時刻ヘッダー（例: `--- 約30分前 ---`）に統一。LLM が「2 つの別タイムスタンプ = 別会話」と誤認識する問題を解消。

- 新ヘルパー: `_format_relative_time(dt, now)` / `_parse_session_filename_timestamp(name)` / `_strip_legacy_time_line(text)`
- 旧形式（先頭行に `Time: 2026-...` を含むファイル）は読み取り時に除去
- Sleeptime が日次でクリアする想定で、最大半日以内のスパンを前提
- 旧 `floating_summary.txt` も互換読み取り

## RAG ベクトル検索の閾値/減衰を緩和 + Layer 別診断 (2026-05-17)

- `SYSTEM_CONFIG["memory_probe"]["vector_search_threshold"]`: 0.6 → 0.4（時間減衰込みの実効値で判定するため）
- `SYSTEM_CONFIG["brain"]["time_decay_rate"]`: 0.005 → 0.003 に変更（半減期 ~138 日 → 230 日 相当）
- `SYSTEM_CONFIG["brain"]["fallback_fetch_limit"]`: 0.005 decay 下で 3ヶ月以上前のカードも候補に残るよう拡大
- `MemoryProbe.probe()` の返却値に `layers` 辞書を追加（vector / glossary / deep の `executed` / `reason` / `result_count` 等）
- `ChatService.debug_info.gatekeeper.memory_probe_layers` で UI / debug log に伝播

## Glossary scan を need_intent ゲートから外して常時実行に (2026-05-17)

`MemoryProbe._match_glossary()` は regex のみで LLM 不要・~ms オーダーなので、`need_intent` の値に関わらず常時実行する設計に変更。これにより固有名詞・別名の認識が安定し、用語が常に意味記憶として注入される。Layer 1 (vector) と Layer 2 (deep search) は従来通り `need_intent` でゲート。

## Streaming 対応 Stage 1+2 — SSE エンドポイント + Streamlit UI (#43, 2026-05)

### SSE エンドポイント
- **新規エンドポイント**: `POST /chat/stream` — `text/event-stream` を返す。
- **イベント順序**: `metadata`（Gatekeeper 判定の即送信）→ `chunk`（逐次）→ `done`（debug_info / session_state / sources を含む完了通知）／途中 `error` でリカバラブル/非リカバラブルを区別。
- **`ChatService.execute_stream()`**: 非ストリーミング版と同じ Gatekeeper / MemoryBlockBuilder / Provider 経路を辿り、Provider の `async_generate_stream()` を呼ぶ。StateUpdater は並列実行で done より前に確定。
- **Provider 側**: `BaseProvider.async_generate_stream()` のデフォルト実装は `generate()` を threadpool で実行して 1 回 `chunk` を yield するフォールバック。Gemini / OpenAI / Ollama / xAI は実 stream 対応をオーバーライド。

### Streamlit UI
- チャットヘッダーに「Streaming」トグルを常時表示。
- トグル ON 時は `requests.post(..., stream=True)` で SSE を読み、`metadata` / `chunk` / `done` を順次処理。
- 既存のバッファ応答 (`POST /chat`) との切替はユーザー判断。

## ChatService に debug_info 自動保存を追加 (2026-05)

`instance_dir/debug_logs/latest.json` を毎ターン上書き、`history/{YYYYMMDD_HHMMSS_uuid}.json` をローテーション保存（デフォルト最大 20 件）。保存失敗は warning ログのみで応答に影響させない。

含まれるフィールド:
- `timing`: gatekeeper_ms / memory_build_ms / generation_ms / state_update_ms / total_ms（streaming 時は ttfb_ms も）
- `token_estimate`: prompt / response の概算
- `gatekeeper`: tier / scores / need / need_intent / memory_probe_status / memory_probe_layers / session_state
- `rag`: query / results (title, score, episode)
- `prompt` / `prompt_full`: メッセージ配列
- `provider` / `model`

## tier 閾値の設定化 + StateUpdater を post-response/並列化 (2026-05)

- **tier 閾値**: `SYSTEM_CONFIG["gatekeeper"]["tier_rc_threshold"]` (デフォルト 0.4) / `tier_cn_threshold` (デフォルト 0.3) を追加。インスタンス config の `gatekeeper.*` で上書き可能。
- **StateUpdater 並列化**: `Gatekeeper.classify()` 内では呼ばず、`ChatService` で応答生成と `asyncio.gather()` で並列実行。結果は応答完了後に `session_state.apply_delta()` で反映。
- **互換**: 今ターンの `topic` は前ターンの session_state を参照する（1 ターン遅延、許容範囲）。

## cortex / 旧 Gatekeeper レガシーの cleanup (#40, 2026-04)

tier を `reflex` / `mid` 2 値に簡素化。`tier_classifier.py` / `search_planner.py` / `memory_judge.py` / `prompts/control/tier_classifier.txt` 等の旧コードを削除。`docs/tier_rag_separation_impact.md` は作業履歴として保存（完了マーク済み）。

## Lorebook 機能を Glossary 拡張として実装 (#24, 2026-04)

- **glossary.yaml フィールド拡張**: term / definition / aliases / category / status (`active`) / priority。
- **`_match_glossary()` 拡張**: `scan_depth` (デフォルト 2 ターン) / `scan_target` (`user` / `assistant` / `both`) で履歴スキャン範囲を制御。raw hits を返却し、`priority` / `_yaml_index` / `match_source` を付与。
- **`_build_glossary()` 振り分け**: definition が複数行（`\n` 含む）→「関連設定」、単一行 →「用語説明」セクションへ振り分け、`max_entries` / `max_chars` greedy skip。
- **`SYSTEM_CONFIG["glossary"]`**: `scan_depth=2`, `scan_target="both"`, `max_entries=20`, `max_chars=4000`。

## Provider Refactoring v3.1: xAI 追加・OpenAI互換コード共通化・バグ修正 (2026-04-19)

OpenAI 互換プロバイダーの共通コードを抽出し、xAI (Grok) プロバイダーを追加。Ollama Cloud Web Search を統合。テスト中に発見した 3 件のクリティカルバグを修正。

### _openai_compat.py — OpenAI互換共通ヘルパー抽出
- **新規ファイル**: `butly_core/llm/_openai_compat.py` — OpenAI / Ollama / xAI で重複していた以下のコードを共通化:
  - `load_env_file()`: APIkey.env / .env の読込
  - `is_reasoning_model()`: OpenAI o1/o3/o4 系判定（temperature 禁止、max_completion_tokens 使用）
  - `resolve_position()` / `resolve_system_instruction()` / `resolve_context_prefix()`: system_instruction の配置位置解決
  - `build_user_content()`: テキスト + 画像を OpenAI 形式に変換
  - `convert_history()`: Butly 履歴を OpenAI messages 形式に変換（`role: "model"` → `"assistant"` マッピング含む）
  - `build_messages()`: system / context / history / user を position に応じて配列化
  - `merge_chat_config()` / `build_chat_completion_kwargs()` / `build_chat_response()`: API 呼び出しパラメータ構築
- **OpenAI / Ollama プロバイダー**: `generate()` を `_openai_compat` ヘルパーに委譲。`_build_system_instruction()` / `_build_user_content()` 等のプライベートメソッドを削除。

### xAI (Grok) プロバイダー
- **新規ファイル**: `butly_core/llm/providers/xai.py` — OpenAI SDK + `base_url="https://api.x.ai/v1"` で Chat Completions を利用。
- **Vision 対応**: grok-4 系は Vision 対応、grok-code-fast は非対応。
- **Embedding**: xAI は embedding API 未提供のため `None` を返す（別プロバイダーで対応）。
- **factory.py**: `grok-*` / `xai/*` → `XaiProvider` へのルーティングを追加。

### Ollama Cloud Web Search
- **新規ファイル**: `butly_core/search/ollama_provider.py` — `https://ollama.com/api/web_search` を利用。`OLLAMA_WEB_SEARCH_API_KEY` で認証。
- **search/__init__.py**: `create_search_provider(chat_model="")` シグネチャ変更。Ollama chat + key 設定済み → OllamaWebSearchProvider、それ以外 → TavilySearchProvider。

### UsageTracker プロバイダー別カウント
- **usage_tracker.py**: 旧形式 `{YYYY-MM: int}` → 新形式 `{YYYY-MM: {tavily: N, ollama: M}}` への lazy migration。`increment(provider)` でプロバイダー名を明示指定。

### バグ修正（xAIテスト中に発見）
1. **ChatService model_name 優先度修正** (`chat/service.py`): `request.model_name or AI_CONFIG["chat"]["model_name"]` だと常にグローバル Gemini モデルが使用されていた。インスタンス config → リクエスト → グローバル の 3 段階優先に修正。
2. **convert_history role マッピング** (`_openai_compat.py`): Gemini は `role: "model"` を使用するが、OpenAI/xAI は `role: "assistant"` を要求。`_ROLE_MAP = {"model": "assistant"}` を追加。
3. **SleepTime インスタンス固有設定対応** (`sleeptime.py`): `_resolve_conf()` ヘルパーを追加。`ask_gemini_to_summarize` / `_generate_daily_digest` / `_generate_recent_headlines` / `_update_recent_snapshot_if_due` / `_propose_key_memory_updates_if_due` / `generate_embedding` の 6 メソッドがインスタンス config を参照するよう修正。

### UI 変更 (app.py)
- xAI モデル（grok-4-1-fast-non-reasoning / grok-4-1-mini-fast-non-reasoning）をモデルリストに追加。
- API キー管理に xAI / Ollama Web Search を追加（4列 UI）。

### テスト
- `tests/test_openai_compat.py` 新規（40件）: ヘルパー関数の単体テスト。
- `tests/test_xai_provider.py` 新規（12件）: xAI プロバイダーの単体テスト。
- `tests/test_ollama_web_search.py` 新規（6件）: Ollama Web Search テスト。
- `tests/test_search_factory.py` 新規（6件）: 検索ファクトリテスト。
- 既存テスト含む全 459 件パス（391 → 459、68件追加、回帰 0）。

## Gatekeeper Phase 1.5: MemoryJudge → MemoryProbe 事実ベース判定 (2026-04-06)

MemoryJudge の LLM 呼び出しを廃止し、実際の検索結果に基づく事実ベース判定に置換。レイテンシ削減 + Glossary の選択的注入を実現。

### MemoryProbe 3層構造
- **Layer 1: Quick Vector Search (~100ms)**: `Brain.quick_vector_search()` を新設。キーワード抽出なしで user_input の embedding と knowledge_cards の cosine similarity を直接比較。閾値（デフォルト 0.6）以上のヒットを candidates に格納。
- **Layer 1.5: Glossary Match (数ms)**: user_input の単語と glossary entries の term/aliases をマッチング。ヒットした glossary エントリを `glossary_hits` に格納。
- **Layer 2: Deep Search (1-2s, 条件付き)**: Layer 1 でヒットなし、かつ具体的な過去参照パターン（「前に」「覚えてる」「だっけ」等）がある場合のみ `Brain.extract_keywords()` + `search_knowledge()` を実行。

### Gatekeeper Facade 変更
- **並列実行**: 3並列（CC + MemoryJudge + StateUpdater）→ 2並列（CC + StateUpdater）。MemoryProbe は LLM 不要のため並列の外で即実行。
- **引数追加**: `Gatekeeper.classify()` に `brain` / `memory_manager` パラメータを追加。`chat/service.py` から渡す。
- **互換レイヤー**: probe `status != "no_hit"` + candidates あり → cortex として返却。`need` に `"memory_probe_hit"` / `"memory_probe_deep_search"` を設定。
- **返却値**: `memory_probe` dict（status / candidates / glossary_hits）を追加。

### MemoryBlockBuilder 変更
- **RAG 検索廃止**: `build()` 内の `brain.extract_keywords()` + `brain.search_knowledge()` 呼び出しを削除。probe candidates から直接 `rag_context` を構築。
- **Glossary 選択的注入**: `_build_glossary()` で `glossary_hits` があれば関連エントリのみ注入、なければ従来通り全件注入。

### 設定・ファイル変更
- **config.py**: `SYSTEM_CONFIG["memory_probe"]` 追加（`vector_search_limit` / `vector_search_threshold` / `deep_search_enabled`）。
- **prompt_registry.yaml**: `memory_judge` エントリ削除。
- **削除**: `memory_judge.py`、`control/memory_judge.txt`、`test_memory_judge.py`。

### テスト
- `tests/test_memory_probe.py` 新規作成（46 テスト）: パターン検出、Layer 2 トリガー判定、Glossary マッチ、Headline マッチ、probe 統合、Gatekeeper 統合。
- 既存テスト含む全 330 件パス。
- **想定レイテンシ削減**: Gatekeeper + MemoryBuild 合計 ~5s → ~1.5s。

## Sleeptime リソース最適化：Stage 2 スキップ＆チャンク分割 (2026-04-04)

ローカルLLM運用やAPIの長文コンテキスト処理の安定性向上のため、`sleeptime.py` にリソース最適化機能を追加。

### Stage 2 スキップ機能
- **`skip_knowledge_generation`** (bool): `config.json > sleeptime` セクションに追加。`true` の場合 Stage 2（ナレッジ化）をスキップし、RAWデータを `1_integrated` に保持する。後日高性能モデルで一括処理可能。
- `process_instance()` と `run_with_progress()` の両方で対応。

### Stage 1 (Digest) チャンク分割
- **`digest_max_input_chars`** (int): `_generate_daily_digest()` の1回あたりの最大入力文字数。0 = 無制限。
- 日付ヘッダ `[YYYY-MM-DD ...]` を区切りにして行単位で分割。日付行の途中で切れないよう保証。
- 新規ヘルパー `_split_text_by_date_headers()` を追加。

### Stage 2 (Knowledge) チャンク分割
- **`knowledge_max_input_chars`** (int): `stage_2_knowledgeize()` の1回あたりの最大入力文字数。0 = 無制限。
- JSONファイル単位で分割。「次のファイルを追加すると上限超過 → ここまでで1チャンク」として処理。ファイルの途中で切らない。

### UI (app.py)
- Sleeptime 設定画面に3項目追加: 「ナレッジ化スキップ」チェックボックス、「Digest 最大入力文字数」、「Knowledge 最大入力文字数」。

## 汎用Web検索モジュール追加 (2026-03-31)

Gemini 以外のプロバイダー（OpenAI / Ollama）で Web 検索を利用可能にする汎用検索モジュールを実装。

### 新規パッケージ: `butly_core/search/`
- **base.py**: `BaseSearchProvider` 抽象基底クラス（`search()` / `is_available()`）。将来の差し替え（DuckDuckGo、SerpAPI 等）に備えた設計。
- **tavily_provider.py**: `TavilySearchProvider` — Tavily Search API 実装。環境変数 `TAVILY_API_KEY` で認証。
- **types.py**: `SearchResult` DTO（title / url / content / score）。
- **usage_tracker.py**: `UsageTracker` — 月次 API 使用量を `butly_core/search_usage.json` に記録。
- **__init__.py**: `create_search_provider()` ファクトリ関数。

### ChatService 統合
- **service.py**: `_is_gemini_model()` ヘルパーを追加。非 Gemini + `use_web_search=True` 時に Tavily 検索を実行し、結果を `memory_blocks["web_search_context"]` に格納。検索ソース URL をレスポンスの `sources` に追加。
- **設計方針**: 検索の ON/OFF はユーザーがトグルで決定（パターンA）。LLM に検索判断させるエージェント方式（パターンB）は別 Issue へ。

### MemoryBuilder 対応
- **memory_builder.py**: `DEFAULT_CONTEXT_ORDER` に `web_search` を追加。`_build_web_search()` ビルダーで `web_search_context` が存在する場合のみセクションを出力。
- **section_headers.yaml**: ja/en に `web_search` ヘッダー（Web検索結果の参照注釈付き）を追加。

### DTO / Router / UI
- **types.py**: `ChatRequest` に `use_web_search: bool = False` フィールドを追加。`normalize_ws_payload()` でも受け渡し対応。
- **routers/chat.py**: REST の `ChatRequest` に `use_web_search` を追加し、内部リクエスト変換に反映。
- **app.py**: 非 Gemini 時に 🔍 トグル表示（`TAVILY_API_KEY` 未設定時は disabled）。ペイロードに `use_web_search` を追加。

### 設定・依存関係
- **config.py**: `SYSTEM_CONFIG["search"]` にデフォルト設定（provider / max_results / search_depth）を追加。
- **requirements.txt**: `tavily-python>=0.5.0` 追加。
- **.env.example**: `TAVILY_API_KEY` の説明を追加。

### テスト
- `tests/test_search_types.py`: SearchResult DTO テスト（4 件）。
- `tests/test_tavily_provider.py`: TavilySearchProvider テスト（is_available / モック検索 / エラー処理、7 件）。
- `tests/test_usage_tracker.py`: UsageTracker テスト（increment / get / 破損ファイル対応、6 件）。
- 既存テスト含む全 57 件パス。

## Glossary（意味記憶）導入・RAG 一元化・GK/RAG トグル (2026-04-02)

### Glossary（共通言語辞書）
- **glossary.yaml**: インスタンス別に `instances/{name}/glossary.yaml` を追加。term / definition / aliases / category / status フィールドを持つ YAML 形式の意味記憶。
- **memory.py**: `get_glossary()` / `get_glossary_raw()` / `save_glossary()` の 3 メソッドを追加。
- **memory_builder.py**: `build_context_prefix()` に `_build_glossary()` ビルダーを追加。全 tier で context_prefix に注入（CURRENT TIME の直後、MID-TERM の前）。
- **section_headers.yaml**: ja/en に `glossary` ヘッダーと `note_glossary` 注釈を追加。
- **API**: `GET /instances/{name}/glossary` / `POST /instances/{name}/glossary` エンドポイントを追加。
- **UI**: `app.py` にインスタンス設定画面のGlossary管理セクション（フィルタ・追加・削除・ステータス変更・保存）を追加。

### SearchPlanner need:null
- **search_planner.txt**: `need: null` / `search_targets: null` の出力を許可。「検索が不要な場合」の説明を【Important】セクションとして追加。
- **search_planner.py**: LLM が返す `"None"` / `"null"` / `""` 文字列を Python `None` に正規化。
- **state_updater.py**: 同様の `"None"` / `"null"` 正規化を実装。
- **memory_builder.py**: `need` が null の場合に RAG 検索をスキップするロジックを追加（cortex でも RAG 不要時にコストを抑制）。

### ChatService RAG 一元化
- **service.py**: ChatService 独自の RAG 検索パス（キーワード抽出 + 検索）を完全に削除。RAG は MemoryBlockBuilder 経由の単一パスに統一。
- **Gatekeeper ON/OFF**: `config.gatekeeper.enabled` で Gatekeeper 全体を無効化可能（無効時は mid tier 固定、RAG なし）。
- **RAG ON/OFF**: `config.brain.use_rag` で RAG 検索を無効化可能。cortex 時でも use_rag=False なら brain を渡さない。
- **UI**: `app.py` に「🧬 Gatekeeper 設定」トグルと「RAG検索」トグルを追加。

### その他
- **Streamlit checkbox 警告修正**: 空ラベル `""` を `f"有効: {sid}"` + `label_visibility="collapsed"` に変更。
- **テスト**: `TestContextPrefixGlossary` クラス（6 テスト）を追加。全 216 テストパス。

## モデル選択UIにカスタム入力欄を追加 (2026-03-29)

- **カスタムモデル入力**: 各ロール（Chat / Summary / Gatekeeper / Embedding）のモデル選択に「✏️ カスタム入力...」オプションを追加。プリセット以外の任意のモデル名を直接入力可能に。
- **用途**: 最新APIモデル・旧モデルの利用、Ollama経由の多数のローカルLLMへの対応。
- **Ollamaガイド**: ローカルLLM使用時の `ollama/` プレフィックスに関する案内を表示。
- **空白ガード**: モデル名が未入力のまま保存できないようバリデーションを追加。

## 最新の実装：Provider 同期/非同期統一 (2026-03-23)
`asyncio.run() cannot be called from a running event loop` エラーを根本解決。全プロバイダーの非同期メソッドを完全に同期化した。

- **`BaseProvider` 同期化**: `generate()`, `summarize()` の `@abstractmethod` を `async def` → `def` に変更。
- **将来への布石**: `async_generate()` / `async_summarize()` / `async_embed()` のデフォルト実装（同期版を `run_in_threadpool` でラップ）を `BaseProvider` に追追。個別プロバイダーをオーバーライドするだけで段階的非同期化が可能。
- **`GeminiProvider` 全同期化**: `generate()`, `summarize()`, `_start_chat()`, `_try_search_with_retry()` の4メソッドを `def` に変更。`client.aio.chats.create()` → `client.chats.create()` に切り替え。全 `await` 除去。
- **`OpenAIProvider` / `OllamaProvider`**: `generate()`, `summarize()` を `def` に変更（中身の変更なし）。
- **`ChatService` 修正**: `await provider.generate(...)` → `await run_in_threadpool(provider.generate, ...)` に変更。`from starlette.concurrency import run_in_threadpool` を追追。
- **クリーンアップ**: `sleeptime.py` の `generate_embedding()` 内の不要な `import asyncio` を削除。`migrate_embeddings.py` の不要な `import asyncio` を削除。
- **プロジェクト全体**: `asyncio.run()` 使用箇所 **0件** を確認。テスト **137件全パス**。

## マルチプロバイダー対応リファクタリング (2026-03-22)
Gemini 専用だったアーキテクチャを **プロバイダー非依存** に改修し、OpenAI / Ollama をサポート。

- **BaseProvider 拡張**: `summarize()`, `embed()`, `classify()` 抽象メソッドを追加。
- **brain.py 刷新**: 全 `google.genai` 依存を除去し、ProviderFactory 経由の純粋 RAG エンジンに縮小（~861行 → ~253行）。
- **ChatService 統一**: RAG 検索を ChatService 側に移動、全パスを `Provider.generate()` 経由に統一。
- **GeminiProvider 完成**: brain.py から移管した Gemini 固有ロジック（検索リトライ、コンテキストキャッシュ、ハルシネーションフィルタ）を集約。
- **OpenAIProvider 追加**: GPT-4o 等対応。Vision、embed（text-embedding-3-small）、classify を実装。
- **OllamaProvider 追加**: ローカル LLM 対応。OpenAI 互換 API（`localhost:11434/v1`）経由。
- **Gatekeeper / Sleeptime**: `google.genai` 依存を除去し、Provider.classify() / embed() 経由に切り替え。
- **埋め込みマイグレーション**: プロバイダー切り替え時に embedding_blob を再生成する `migrate_embeddings.py` を追加。
- **設定ファイル整理**: `.env.example` に全プロバイダーのキーテンプレート、`user_config.json.example` に OpenAI / Ollama の設定例を追加。
- **app.py / main.py**: `brain.prepare_cache()` を Provider 経由に修正（hasattr チェックで非 Gemini 対応）。

## Raspi V2 画像付きチャット対応と責務分離 (2026-03-21)
- **DTOの導入**: `butly_core/chat/types.py` を作成し、`ChatRequest`, `ChatResponse`, `Attachment` の標準モデルを定義。WebSocketとRESTからの入力を正規化する処理を追加。
- **Provider抽象化**: `butly_core/llm/` に Provider 抽象層を作成し、`GeminiProvider` を実装。これまで `main.py` や `brain.py` に点在していた Gemini 固有の画像処理（inline送信 / Files API 分岐）を Provider 内に隠蔽化。
- **ChatService導入**: チャットのオーケストレーションを担うステートレスな `ChatService` (`butly_core/chat/service.py`) を実装し、`main.py` から LLM 依存コードを排除。
- **brain.py のクリーンアップ**: 画像変換ロジックを Provider 側に移行し、`brain.py` の画像関連引数 (`images`) を削除。※記憶注入ロジックは変更なし。

## 直近の主要な実装履歴
1. **Phase 4 中期記憶要約の動的注入切替**:
1. **Phase 3 二層要約パイプラインの実装**:
   - `sleeptime.py` における中期記憶の整理機能を拡張し、出来事と決定事項をまとめた「事実ダイジェスト」と、AIとユーザーの距離感を示す「関係性スナップショット」の二層ファイル生成パイプラインを構築。
2. **OSS向けオープン化準備 / リファクタリング**:
   - 「Jarvis」などのハードコードされた初期名や個人情報を排除し、設定ファイルやテンプレートから動的に読み込む汎用的な「Butly」プラットフォームへと改修。
3. **ステートフルAPI (Interactions API) の導入** *(削除済み — マルチプロバイダ統一のため廃止)*:
   - 会話ターンごとに長期履歴を全て手動で挿入する状態から、Google Gemini側のセッション履歴保持機構に移行し、不要なトークン消費を抑制。
4. **FastAPI + Streamlit への分離**:
   - 処理の非同期化とバックグラウンドタスク（Sleeptime）の安定稼働、UI側のレスポンス向上のため、単一スクリプトからAPIサーバーとフロントエンドの構成に分離。
5. **インスタンス別記憶の分離**:
   - 複数の別キャラクター・別用途AIを同時に動かせるよう、`butly_core/instances/` ディレクトリ配下で記憶DBとファイルを完全分離。
