# Butly ファイル構成一覧

> 最終更新: 2026-03-29

---

## ルートディレクトリ

| ファイル | 役割 |
|---|---|
| `main.py` | FastAPI アプリの起動エントリポイント |
| `app.py` | Streamlit Web UI（FastAPI バックエンド経由で動作） |
| `dependencies.py` | ルーター間共有のグローバル状態・ヘルパー |
| `housekeeper.py` | 記憶自動整理スクリプト（単体実行 & APIから呼び出し可） |
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
- `render_housekeeper_screen()` — Housekeeper の実行・進捗表示
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

### `housekeeper.py`
蓄積された short_term_json を要約・ナレッジカード化・DB 登録する記憶整理エンジン。  
`python housekeeper.py` でも、HTTP API 経由でも実行できる。

**主要クラス・関数**
- `ButlyHousekeeper` — 整理処理の本体クラス
  - `get_instance_key_memory(instance_name)` — インスタンス別 Key_Memory 取得
  - `get_instance_instruction(instance_name)` — インスタンス別 system_instruction 取得
  - `summarize_files(instance_name)` — short_term_json を floating_summary に折りたたむ
  - `integrate_summaries(instance_name)` — floating → mid_term への統合
  - `knowledgeize(instance_name)` — mid_term から knowledge_cards を生成して DB 登録
  - `run_with_progress(instance_name)` — 上記を順番に実行し進捗を更新
  - `estimate_workload(instance_name)` — 処理量の見積もりを返す
  - `update_status(instance_name, state, progress, message)` — 実行ステータス更新
- `housekeeper_store` — インスタンス別の実行ステータスを保持するグローバル dict

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
| `instances.py` | `/instances` CRUD、`/config`、`/history` |
| `housekeeper.py` | `/housekeeper/run`、`/housekeeper/status`、`/housekeeper/estimate` |
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
- `SYSTEM_CONFIG` — パス定義・メモリ上限・Brain パラメータ等
- `USER_CONFIG_PATH` — ユーザーカスタム設定ファイルのパス
- `_recursive_update(base, override)` — 設定辞書を再帰マージするユーティリティ

---

## butly_core/chat/

チャット機能の DTO とオーケストレーション層。

### `butly_core/chat/types.py`
チャット機能で使う Pydantic モデルとバリデーション関数。LLM プロバイダーに依存しない。

- `Attachment` — 添付ファイル（kind / mime_type / data_base64 / name / size）
- `ChatRequest` — チャットリクエスト（text / attachments / instance_name / model_name / use_rag 等）
- `ChatResponse` — チャット応答（text / keywords / references / tier / need / session_state 等）
- `validate_attachments(attachments)` — 枚数・サイズ・MIME タイプのバリデーション
- `normalize_ws_payload(payload)` — WebSocket ペイロードを `ChatRequest` に正規化

---

### `butly_core/chat/service.py`
チャット実行のステートレスオーケストレーター。1 リクエストごとに以下の順番で処理する。

```
1. コンポーネント取得 (Memory / Brain / Chronos / Cache)
2. 時刻コンテキスト生成 (Chronos.get_system_note → full_prompt の冒頭に付加)
3. Gatekeeper.classify → tier 判定 + state_delta 生成
4. SessionState.apply_delta → セッション状態更新
5. MemoryBlockBuilder.build → 記憶ブロック辞書構築
6. ProviderFactory.create → Provider 選択・vision チェック
7. キーワード抽出 + RAG 検索 (use_rag=True 時。cortex で Gatekeeper 済み RAG があれば流用)
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
    ├─ TierClassifier.classify()    → tier (reflex / mid / cortex) を決定
    ├─ StateUpdater.update()        → session_state の差分を生成
    └─ SearchPlanner.plan()         → cortex 時のみ RAG 検索キーワードを生成
```

---

### `gatekeeper/__init__.py`（`Gatekeeper` クラス）
外部 API 互換の Facade。`ChatService` からはここだけを呼ぶ。

- `Gatekeeper(base_dir)`
  - `classify(user_input, history_msgs, session_state, current_topic, override_config)` — 上記 3 サブクラスを協調実行し、統合結果を返す

**返却値の構造:**
```python
{
    "tier": "reflex" | "mid" | "cortex",
    "topic": str,
    "need": str | None,           # cortex のみ
    "search_targets": list | None, # cortex のみ
    "state_delta": dict,
    "llm_tier": str,
    "llm_reasoning": str,
    "llm_scoring": {
        "response_complexity": float,
        "emotional_weight": float,
        "memory_reference_likelihood": float,
        "continuity_need": float,
    }
}
```

---

### `gatekeeper/tier_classifier.py`
LLM に 4 スコア（0–1）を出力させ、Python 側でルールに基づき tier を決定する。

- `TierClassifier(base_dir)`
  - `classify(user_input, history_msgs, current_topic, override_config)` — tier 判定を実行

**tier 決定ロジック:**
| tier | 条件 |
|---|---|
| `cortex` | `memory_reference_likelihood >= 0.7` |
| `reflex` | `response_complexity <= 0.2` AND `memory_reference_likelihood <= 0.3` AND `continuity_need <= 0.3` |
| `mid` | それ以外 |

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
    "add_goal": str | None,      # 追加するゴール
    "add_unresolved": str | None,# 追加する未解決事項
    "resolve": str | None,       # 解決済みにする未解決事項
}
```

---

### `gatekeeper/search_planner.py`
`cortex` tier 判定時のみ呼ばれ、RAG 検索に必要なキーワードを LLM で生成する。

- `SearchPlanner(base_dir)`
  - `plan(user_input, history_msgs, current_topic, override_config)` — `{need, search_targets}` を返す

---

### `gatekeeper/session_state.py`
会話セッション全体の内部状態を JSON ファイルで永続管理する。

- `SessionState(instance_dir)`
  - `apply_delta(delta)` — state_delta を現在の state に適用する
  - `increment_turn(tier)` — ターン数と最後の tier を更新する
  - `to_dict()` — 現在の状態を dict で返す
  - `_load()` / `_save()` — `session_state.json` との I/O

**管理するフィールド:**
```python
{
    "topic": str,       # 現在の会話の話題
    "mood": str,        # ユーザーの気分 (neutral / positive / negative 等)
    "goals": list,      # 達成したいゴール
    "unresolved": list, # 未解決事項
    "turn_count": int,  # ターン数
    "last_tier": str,   # 直前の tier
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
- `build_context_prefix(blocks, memory_manager, use_google_search)` — **可変セクション**（現在時刻 / mid_term / RAG / floating / tier 情報 / Google 検索注意書き）を結合し、Provider が会話履歴の先頭にユーザーメッセージとして注入する文字列を生成

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
旧定数名（`HOUSEKEEPER_SUMMARIZE_PROMPT` 等）でのアクセスを維持するためのラッパー。  
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
| `tier_classifier` | control/ | Gatekeeper の tier 判定 |
| `state_updater` | control/ | session_state の差分生成 |
| `search_planner` | control/ | RAG 検索キーワード生成 |
| `brain_extract_keywords` | control/ | キーワード抽出 |
| `housekeeper_summarize` | locales/ | 会話の要約 |
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
④ [mid tier 以上] mid_term 記憶 ← 2 モードあり:
   - raw モード: mid_term.txt をそのまま
   - summary モード: mid_term_digest.txt + mid_term_relationship.txt
⑤ [cortex のみ] RAG 検索結果  ← 関連ナレッジカード
⑥ floating_summary.txt        ← 直近の会話の浮動要約
⑦ tier 情報 + topic
⑧ [該当時] Google 検索注意書き

[user turn]
─────────────────────────────────────
⑨ Chronos 日時 + ユーザーメッセージ  ← ChatService が結合した full_prompt

[history（マルチターン）]
─────────────────────────────────────
⑩ short_term_json の直近 6 件
```

**tier 別のコンテキスト包含関係:**
```
reflex  ⊂  mid  ⊂  cortex

reflex: ①②③⑥⑦⑨⑩
mid:    ①②③④⑥⑦⑨⑩
cortex: ①②③④⑤⑥⑦⑨⑩  (RAG あり)
```

**mid_term のモード分岐** (`SYSTEM_CONFIG.memory.use_summarized_mid_term`):
| モード | 使用ファイル | 説明 |
|---|---|---|
| `raw`（デフォルト） | `mid_term.txt` | 生の会話要約テキストをそのまま使用 |
| `summary` | `mid_term_digest.txt` + `mid_term_relationship.txt` | Housekeeper が生成した構造化ダイジェスト + 関係性スナップショット |
