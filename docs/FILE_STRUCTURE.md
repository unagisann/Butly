# Butly ファイル構成一覧

> 最終更新: 2026-03-31

---

## ルートディレクトリ

| ファイル | 役割 |
|---|---|
| `main.py` | FastAPI アプリの起動エントリポイント |
| `app.py` | Streamlit Web UI（FastAPI バックエンド経由で動作） |
| `dependencies.py` | ルーター間共有のグローバル状態・ヘルパー |
| `sleeptime.py` | 記憶自動整理スクリプト（単体実行 & APIから呼び出し可） |
| `migrate_embeddings.py` | プロバイダー切り替え時の embedding 再生成ユーティリティ |

---

### `main.py`
FastAPI アプリの生成・起動引数のパース・各ルーターの `include_router`。  
ビジネスロジックは持たない。モジュール直下で `dependencies.py` のシングルトン（InstanceManager / Gatekeeper / MemoryBlockBuilder 等）を初期化し、lifespan ではバンドル環境向けの設定ファイルコピーのみ行う。

- `_watch_parent(parent_pid)` — 親プロセス（Flutter）の死亡監視スレッド
- `_load_env_from_data_dir()` — `.env` からAPIキーを環境変数へロード
- `lifespan(app)` — バンドル時の設定ファイルコピー・起動メッセージ
- `app` — FastAPI インスタンス。全 routers/ を include

---

### `app.py`
Streamlit 製 Web UI。インスタンス選択・チャット送信・過去ログ表示・設定画面・DB ブラウザ等を提供する。

チャット送信・インスタンス CRUD・設定変更等の書き込み操作はすべて **FastAPI バックエンド（`POST /chat` 等）に `requests.post()` で委譲** しており、Gatekeeper・記憶ブロック構築・会話保存は app.py では行わない。  
`ButlyMemory` 等を直接 import しているが、用途は **過去ログの読み取り表示**（`load_recent_sessions`）と **Chronos によるデバッグ用日時テキスト生成** に限定される。

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

## routers/

FastAPI のルーターモジュール群。各ルーターは `dependencies.py` から共有オブジェクトを参照する。

| ファイル | エンドポイント群 |
|---|---|
| `chat.py` | `/chat` (REST) / `/ws/chat` (WebSocket) |
| `instances.py` | `/instances` CRUD、`/config`、`/history`、`/glossary` |
| `sleeptime.py` | `/sleeptime/run`、`/sleeptime/status`、`/sleeptime/estimate` |
| `database.py` | `/database/cards` CRUD（ナレッジカード管理） |
| `settings.py` | `/settings`、`/api-key`、`/config`、`/prompts` |
| `dashboard.py` | `/status`（CPU/MEM）、`/discovery`、`/news` |
| `devices.py` | `/devices`、`/tv/key`、`/tv/launch`（Fire TV 制御） |

---

### `routers/chat.py`
チャット用 REST + WebSocket エンドポイント。内部では `ChatService.execute()` に委譲する。

- `ConnectionManager` — WebSocket 接続管理クラス（接続一覧・broadcast）
- `notify_ai_status(status)` — AI の処理状態を全クライアントへ通知
- `POST /chat` — REST 版チャットエンドポイント
- `WebSocket /ws/chat` — ストリーミング非対応のリアルタイムチャット

---

## butly_core/

コアロジックパッケージ。`config.py` が全体の定数・モデル設定を管理する。

### `butly_core/config.py`
アプリ全体の設定定数を管理。

- `AI_CONFIG` — モデル別の設定（chat / summary / knowledge / embedding / gatekeeper）
- `SYSTEM_CONFIG` — パス定義・メモリ上限・Brain パラメータ・検索モジュール設定等
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
1. コンポーネント取得 (Memory / Brain / Chronos / Cache)
2. 時刻コンテキスト生成 (Chronos.get_system_note → full_prompt の冒頭に付加)
3. Gatekeeper.classify → tier 判定 + state_delta 生成（Gatekeeper 無効時は mid 固定）
4. SessionState.apply_delta → セッション状態更新
5. MemoryBlockBuilder.build → 記憶ブロック辞書構築
5.5. Web検索実行（非Gemini + use_web_search=True 時のみ。Tavily API 経由。結果は memory_blocks["web_search_context"] に格納）
6. ProviderFactory.create → Provider 選択・vision チェック
7. RAG 検索結果の取得（cortex + use_rag=True 時のみ。Gatekeeper 経由 MemoryBlockBuilder が実行済みの RAG を流用）
8. provider.generate(full_prompt, attachments, context) → LLM 応答生成
9. memory.save_single_turn → 会話を short_term_json に保存
10. memory.maintain_memory → 閾値超過時に short_term → floating_summary へ折りたたみ
```

- `ChatService.execute(request, ...)` — 上記フローを実行する静的 async メソッド

---

## butly_core/core/

AIアシスタントのコアエンジン群。

### `butly_core/core/memory.py`
ファイルベースの多層記憶を管理するクラス。

- `ButlyMemory(base_dir, instance_name)` — 各インスタンスの記憶ディレクトリを初期化
  - `get_system_instruction()` — system_instruction.txt を読み込む
  - `get_key_memory()` — Key_Memory.txt（根幹記憶）を読み込む
  - `get_glossary()` — glossary.yaml からアクティブなエントリを `- term: definition` 形式で返す
  - `get_glossary_raw()` — glossary.yaml を dict できっちり返す（UI/API向け）
  - `save_glossary(data)` — glossary.yaml を書き出す
  - `get_mid_term_text_content()` — mid_term.txt を上限文字数でカットして返す
  - `get_mid_term_digest()` — mid_term_digest.txt（エピソード付きダイジェスト）を返す
  - `get_mid_term_relationship()` — mid_term_relationship.txt（関係性グラフ）を返す
  - `get_floating_summary()` — floating_summary.txt（浮動要約）を返す
  - `load_recent_sessions(limit)` — short_term_json から直近 N 件の会話を返す
  - `save_conversation(user_msg, ai_msg, ...)` — 会話を short_term_json に保存
  - `get_last_interaction_time()` — 最後のインタラクション日時を返す
  - `maintain_memory(brain)` — short_term が閾値超えたら floating_summary に折りたたむ

---

### `butly_core/core/brain.py`
LLM 呼び出しと RAG 検索のエンジン。Provider に依存しない中間層。

- `ButlyBrain(base_dir)` — 初期化
  - `get_embedding(text)` — Embedding ベクトルを取得
  - `extract_keywords(text, override_config)` — RAG 用キーワードを LLM で抽出
  - `search_knowledge(keywords, query, instance_name, limit, override_config)` — コサイン類似度で RAG 検索
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
    ※ mid + probe ヒット → 互換レイヤーで cortex に昇格
```

> **備考:** `TierClassifier`（旧 4 スコア方式）と `SearchPlanner` はコード上残存していますが、アクティブパスでは使用されていません。

---

### `gatekeeper/__init__.py`（`Gatekeeper` クラス）
外部 API 互換の Facade。`ChatService` からはここだけを呼ぶ。

- `Gatekeeper(base_dir)`
  - `classify(user_input, history_msgs, session_state, current_topic, override_config, instance_dir)` — ContextClassifier + StateUpdater を並列実行し、MemoryProbe で記憶を検索。統合結果を返す。`instance_dir` を受け取り、`recent_digest_headlines.json` を読み込んで ContextClassifier に渡す

**返却値の構造:**
```python
{
    "tier": "reflex" | "mid" | "cortex",  # cortex = mid + probe ヒット（互換レイヤー）
    "topic": str,
    "need": str | None,           # cortex のみ
    "search_targets": list | None, # cortex のみ
    "state_delta": dict,
    "llm_tier": str,
    "llm_reasoning": str,
    "llm_scoring": {
        "response_complexity": float,
        "emotional_weight": float,
        "continuity_need": float,
    },
    "memory_probe": {
        "status": "hit" | "no_hit" | "deep_search",
        "candidates": list[dict],
        "glossary_hits": list[dict],
    }
}
```

---

### `gatekeeper/context_classifier.py`
LLM に 3 スコア（0–1）を出力させ、Python 側でルールに基づき tier を決定する。

- `ContextClassifier(base_dir)`
  - `classify(user_input, history_msgs, current_topic, recent_headlines, override_config)` — tier 判定を実行。`recent_headlines` でダイジェストから抽出した見出しを注入

**tier 決定ロジック:**
| tier | 条件 |
|---|---|
| `reflex` | `response_complexity <= 0.4` AND `continuity_need <= 0.3` |
| `mid` | それ以外 |

---

### `gatekeeper/memory_probe.py`
LLM 呼び出しなしの事実ベース記憶検索。3 レイヤー構成。

- `MemoryProbe()`
  - `probe(user_input, brain, memory_manager)` — ベクトル検索 + 用語集マッチ + 条件付き深層検索

**検索レイヤー:**
| レイヤー | 内容 | 条件 |
|---|---|---|
| Layer 1 | Quick Vector Search（コサイン類似度） | 常時実行 |
| Layer 1.5 | Glossary Match（term/aliases） | 常時実行 |
| Layer 2 | Deep Search | 過去参照パターン検出時のみ |

---

### `gatekeeper/tier_classifier.py`（レガシー・後方互換）
旧 4 スコア方式の tier 判定。アクティブパスでは使用されていない。

- `TierClassifier(base_dir)`
  - `classify(...)` — 後方互換エイリアスとして残存

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

### `gatekeeper/search_planner.py`（レガシー・後方互換）
旧 cortex tier 判定時に RAG 検索キーワードを生成していた。MemoryProbe に置き換え済み。

- `SearchPlanner(base_dir)`
  - `plan(...)` — 後方互換として残存

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
| tier | short_term | floating | mid_term | rag_context |
|------|-----------|---------|---------|------------|
| `reflex` | ✅ | ✅ | — | — |
| `mid` | ✅ | ✅ | ✅ | — |
| `cortex` | ✅ | ✅ | ✅ | ✅ (RAG) |

- `build_system_instruction_from_blocks(blocks, memory_manager, use_google_search)` — **不変セクション**（system_instruction + Key_Memory）のみを結合して system_instruction 文字列を生成
- `build_context_prefix(blocks, memory_manager, use_google_search)` — **可変セクション**（現在時刻 / glossary / mid_term / RAG / floating / tier 情報 / Google 検索注意書き / Web 検索結果）を結合し、Provider が会話履歴の先頭に user メッセージとして注入する文字列を生成

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

- `UsageTracker` — `butly_core/search_usage.json` に YYYY-MM キーで累計を記録
  - `increment()` — 当月カウントを +1
  - `get_current_month_count()` — 当月の使用回数を返す
  - `get_all()` — 全月の使用量を dict で返す

### `butly_core/search/__init__.py`
パッケージ公開。

- `create_search_provider()` — 設定に基づいて検索プロバイダーをファクトリ生成（現在は Tavily 固定、将来の差し替えポイント）

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

---

### `butly_core/llm/factory.py`
モデル名からプロバイダーを生成するファクトリ。

- `ProviderFactory`
  - `create(model_name)` — モデル名のプレフィックスで Gemini / OpenAI / Ollama を自動選択

---

### `butly_core/llm/providers/gemini.py`
Gemini API プロバイダー。コンテキストキャッシュ・画像アップロード・グラウンディングを管理。

- `GeminiProvider`
  - `generate(text, attachments, context)` — Gemini API でチャット応答を生成
  - `supports_vision(model_name)` — `VISION_UNSUPPORTED_MODELS` リストで判定
  - `embed(text)` — `models/gemini-embedding-001` で embedding を生成
  - `classify(prompt, config)` — Gatekeeper 用の分類（temperature=0 推奨）

---

### `butly_core/llm/providers/openai.py`
OpenAI (GPT) / Azure OpenAI 互換プロバイダー。

- `OpenAIProvider`
  - `generate(text, attachments, context)` — Chat Completions API で応答を生成
  - `supports_vision(model_name)` — `_VISION_MODELS` セットで判定
  - `embed(text)` — `text-embedding-3-small` で embedding を生成

---

### `butly_core/llm/providers/ollama.py`
ローカル Ollama サーバー（OpenAI 互換 API）プロバイダー。

- `OllamaProvider`
  - `generate(text, attachments, context)` — ローカル LLM で応答を生成
  - `supports_vision(model_name)` — `_VISION_MODELS` セットで判定（llava 等）
  - `embed(text)` — Ollama の embedding エンドポイントを使用

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
| `context_classifier` | control/ | Gatekeeper の tier 判定（Phase 1.5、3スコア方式） |
| `tier_classifier` | control/ | Gatekeeper の tier 判定（レガシー、4スコア方式） |
| `state_updater` | control/ | session_state の差分生成 |
| `search_planner` | control/ | RAG 検索キーワード生成（レガシー） |
| `brain_extract_keywords` | control/ | キーワード抽出 |
| `sleeptime_summarize` | locales/ | 会話の要約 |
| `brain_summarize_conversation` | locales/ | 会話の折りたたみ要約 |
| `midterm_digest` | locales/ | 中期ダイジェスト生成 |
| `midterm_relationship` | locales/ | 関係性グラフ生成 |
| `web_ui_default_template` | locales/ | システムインストラクションのデフォルトテンプレ |

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
⑥ [cortex + need有りのみ] RAG 検索結果  ← 関連ナレッジカード
⑦ floating_summary.txt        ← 直近の会話の浮動要約
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
reflex  ⊂  mid  ⊂  cortex

reflex: ①②③④⑦⑧⑩⑪
mid:    ①②③④⑤⑦⑧⑩⑪
cortex: ①②③④⑤⑥⑦⑧⑩⑪  (RAG あり。need=null 時はスキップ)
```

**mid_term のモード分岐** (`SYSTEM_CONFIG.memory.use_summarized_mid_term`):
| モード | 使用ファイル | 説明 |
|---|---|---|
| `raw`（デフォルト） | `mid_term.txt` | 生の会話要約テキストをそのまま使用 |
| `summary` | `mid_term_digest.txt` + `mid_term_relationship.txt` | Sleeptime が生成した構造化ダイジェスト + 関係性スナップショット |
