# Project Status: Butly AI Agent Platform

> ⚠️ **凍結済み（2026-05-24 時点のスナップショット）。現行仕様の正ではありません。**
> 現行の情報はルート [README.ja.md](../../README.ja.md) と [docs/reference/](../reference/) を参照してください。
> 経緯は [Old の説明](README.ja.md) を参照。

🌐 **日本語** | [English](project_status.md)

> 最終更新: 2026-05-24

## 概要
Butly は、多層的な記憶管理（短期 / 会話圧縮ログ / 中期ダイジェスト・関係性 / 長期ベクトル DB / Glossary / 根幹記憶）を備えたパーソナル AI アシスタント基盤です。FastAPI バックエンド + Streamlit UI で動作し、SSE ストリーミング応答、マルチプロバイダー LLM (Gemini / OpenAI / xAI / Ollama)、Gatekeeper による tier・RAG 制御、Sleeptime バッチによる記憶整理を備えます。

## アーキテクチャ構成
- **フロントエンド:** Streamlit (`app.py`) — Web UI、チャット、データベースブラウザ、Sleeptime 管理、Streaming トグル
- **バックエンド:** FastAPI (`main.py`) — REST `/chat`、SSE `/chat/stream`、WebSocket `/ws`、各種設定エンドポイント
- **データベース:** SQLite (`butly_memory.db`) — インスタンスごとのナレッジカード + 埋め込み
- **AI エンジン:** マルチプロバイダー対応 (Google Gemini / OpenAI / Ollama / xAI) — Phase 1–3 リファクタ（2026-05）により `Connection` + `ModelRef` + Protocol Adapter 経路に統合
  - `butly_core/llm/base.py`: 抽象基底 (`generate`, `summarize`, `embed`, `classify`, `async_generate_stream`)
  - `butly_core/llm/connections.py`: `Connection` データクラス + レジストリ（built-in `openai` / `xai` / `ollama` / `google` + `user_config.json` の `LLM_CONNECTIONS`）
  - `butly_core/llm/model_registry.py`: `ModelRef`（connection_id + model_name）と `ModelPreset`。`normalize_model_ref()` / `infer_connection_id()` で旧 API（文字列 model_name）互換
  - `butly_core/llm/factory.py`: `ProviderFactory.create(model)` が `ModelRef` / dict / str を受理し、対応 Adapter をインスタンス化
  - `butly_core/llm/protocols/{openai_compat,gemini_native}.py`: Protocol Adapter 群（Provider シムから利用）
  - `butly_core/llm/_openai_compat.py`: OpenAI 互換 API の低レベル共通ヘルパー
  - `butly_core/llm/providers/{gemini,openai,ollama,xai}.py`: Connection を Adapter に紐付ける薄いシム
- **コアモジュール:** `butly_core/`
  - `chat/service.py`: チャットオーケストレーター。`execute()`（バッファ応答）と `execute_stream()`（SSE）の 2 系統
  - `core/gatekeeper/`: ContextClassifier（tier + need_intent）/ MemoryProbe（事実裏付け）/ StateUpdater（並列実行）/ MemoryBlockBuilder（記憶ブロック構築）
  - `core/memory.py`: ファイルベース多層記憶 I/O。Session Digest は相対時刻ヘッダー表示
  - `core/brain.py`: RAG エンジン (`quick_vector_search_diag` / `search_knowledge` / 時間減衰)
  - `core/key_memory.py`: Key_Memory の構造化・正規化
  - `sleeptime.py`: 日次・週次バッチによる記憶蒸留
- **検索モジュール:** `butly_core/search/` — Tavily / Ollama Cloud Web Search 切替対応

## 現在のフェーズとステータス

- **モデルルーティング / Streaming ターンカウント修正 (2026-05-24)**: Phase 1–3 リファクタ後の追従修正。`tests/test_chat_stream.py` / `tests/test_chatservice_connection_routing.py` で担保。

- **Phase 3 LLM refactor: UI + Dynamic Discovery + リクエスト単位 override (2026-05)**: Settings UI が Connection 別にモデルを列挙（built-in + user-defined）。`model_candidates` で Gemini モデルを動的取得（`models/` プレフィックス除去・表示統一）。`POST /chat` / `POST /chat/stream` のリクエスト単位 `model_name` 上書きを正しく解決。

- **Phase 2 LLM refactor: AI_CONFIG / ChatService を ModelRef ルートへ (2026-05)**: `AI_CONFIG` 各エントリに `connection` + `model_name` を持たせ、`ChatService` / `Brain` / `ContextClassifier` / `StateUpdater` / `sleeptime` が `ProviderFactory.create(ModelRef)` 経由で動くよう整備。旧形式の文字列 `model_name` も継続受理。

- **Phase 1 LLM refactor: Connection / ModelRef / OpenAICompatAdapter 導入 (2026-05)**: `connections.py` / `model_registry.py` / `protocols/`（`OpenAICompatAdapter`, `GeminiNativeAdapter`）を新設。Provider クラスは Connection を Adapter に紐付ける薄シムへ縮退。Groq 等のユーザー定義 Connection 拡張に向けた土台を整備。

- **Knowledge カード `usage_count` 追加 (2026-05)**: `knowledge_cards` テーブルに `usage_count` フィールドを新設し、RAG 経由でのカード参照実績を `last_accessed_at` と独立して追跡。

- **Session Digest 表示の相対時刻化 (2026-05-17)**: `ButlyMemory.get_session_digest()` がファイル名・絶対タイムスタンプを排除し、「約30分前」「約2時間前」などの相対時刻ヘッダーに置換。Sleeptime が日次で整理する前提のため、半日以内のスパンに最適化。旧形式 `Time: 2026-...` を含むファイルは後方互換ロジックで除去。

- **RAG ベクトル検索の閾値緩和 + Layer 別診断 (2026-05-17)**: `vector_search_threshold` を 0.6 → 0.4 に緩和（時間減衰込みの実効値で判定するため）、`time_decay_rate` のデフォルトをさらに小さくして古いカードも候補に残るよう調整。MemoryProbe は Layer ごとの診断情報（実行可否、ヒット数、reason）を返却するようになり、`debug_info.gatekeeper.memory_probe_layers` に含まれる。

- **Glossary scan の need_intent ゲート解除 (2026-05-17)**: Glossary 走査は regex のみ・~ms オーダーなので `need_intent` の値に関わらず毎ターン実行する設計に変更。これにより固有名詞・別名の認識が安定し、用語が事前に意味記憶として注入される。

- **Streaming Stage 1+2: SSE エンドポイント + Streamlit UI (#43, 2026-05)**: `POST /chat/stream` を追加。`ChatService.execute_stream()` で Provider の `async_generate_stream()` を呼び、`metadata` → `chunk` → `done` の SSE イベントを順次返却。Streamlit 側はチャットヘッダーにストリーミングトグルを常時表示し、ユーザーがターンごとに切替できる。Gemini / OpenAI / Ollama / xAI すべてで stream 対応。

- **ChatService debug_info 自動保存 (2026-05)**: `instance_dir/debug_logs/latest.json` および `history/{ts}.json`（最大 20 件）にデバッグ情報を保存。timing / token_estimate / gatekeeper / RAG / プロンプト全文を含む。保存失敗は応答に影響させない。

- **tier 閾値の設定化 + StateUpdater 並列化 (2026-05)**: `SYSTEM_CONFIG["gatekeeper"]["tier_rc_threshold"]` (デフォルト 0.4) / `tier_cn_threshold` (デフォルト 0.3) を追加。インスタンスごとに上書き可能。StateUpdater は応答生成と並列実行され、クリティカルパスから外れる（次ターンの context に反映される 1 ターン遅延）。

- **MemoryProbe を need_intent でゲート (#42, 2026-04)**: ContextClassifier が `need_intent` (`past_fact` / `glossary` / `relationship` / `null`) を出力し、MemoryProbe が事実裏付けを行う 2 段構え。LLM 意図と事実裏付け両方を満たした時のみ `need` が立つ。Glossary scan のみ常時実行（前項参照）。

- **Lorebook 機能 (Glossary 拡張, #24, 2026-04)**: Glossary を Lorebook として拡張し、term / aliases / category / status / priority をサポート。`_match_glossary` は user_input + 直近履歴の `scan_depth` ターンをスキャンし、`scan_target` (`user` / `assistant` / `both`) で対象を制御。MemoryBlockBuilder 側で「用語説明」「関連設定」に振り分け、`max_entries` / `max_chars` で注入制限。

- **Cortex 廃止 / Gatekeeper 2 層化 (2026-04)**: tier を `reflex` / `mid` の 2 値に簡素化。RAG は tier ではなく `need` で独立判定される設計に統一。`tier_classifier.py` / `search_planner.py` / `memory_judge.py` 等の旧コードを cleanup。

- **Provider Refactoring v3.1: xAI 追加・互換コード共通化 (2026-04-19)**: OpenAI 互換コードを `_openai_compat.py` に共通化し OpenAI/Ollama/xAI で共有。xAI (Grok) プロバイダー新規追加。Ollama Cloud Web Search プロバイダー追加。UsageTracker をプロバイダー別カウントに拡張。`ChatService` の model_name 優先度を「インスタンス > リクエスト > グローバル」に修正。`convert_history` で `role: "model"` → `"assistant"` 自動変換。テスト 459 件パス（68 追加、回帰 0）。

- **Sleeptime リソース最適化 (2026-04-04)**: ローカル LLM・API 長文コンテキストの安定性向上。Stage 2 スキップ（`skip_knowledge_generation`）、Stage 1 Digest の日付ヘッダ区切りチャンク分割（`digest_max_input_chars`）、Stage 2 Knowledge のファイル単位チャンク分割（`knowledge_max_input_chars`）。

- **汎用 Web 検索モジュール (2026-03-31)**: Gemini 以外（OpenAI / Ollama / xAI）でも Tavily / Ollama Cloud Web Search を利用可能に。`butly_core/search/` パッケージとしてプロバイダー切替可能な設計。`ChatService` が `memory_blocks["web_search_context"]` に注入。

- **マルチプロバイダー対応 (2026-03-22)**: Gemini 専用アーキテクチャをプロバイダー非依存化。`google.genai` は `GeminiProvider` のみに隔離。`migrate_embeddings.py` で再生成可能。

- **マルチインスタンス対応**: インスタンスごとに独立ディレクトリ (`butly_core/instances/[name]`) で記憶 DB・設定・プロンプトを管理。

## 同期 / 非同期設計方針
- 全プロバイダーメソッド (`generate` / `summarize` / `embed` / `classify`) は同期 (`def`)。`ChatService` 内で `run_in_threadpool()` 経由で実行し、FastAPI のイベントループをブロックしない。
- ストリーミングだけは `async_generate_stream(text, attachments, context)` を別経路で提供。Provider 内部で逐次 `yield {"type": "chunk", ...}` を生成。
- 段階的非同期化のため `BaseProvider` に `async_generate()` / `async_summarize()` / `async_embed()` のデフォルト実装（同期版を `run_in_threadpool` でラップ）が用意されている。

## 開発上の留意点（他の AI へ向けて）
- **設定優先順位**: `butly_core/config.py` (グローバル) → `user_config.json` (ユーザー) → `instances/[name]/config.json` (インスタンス)。
- **モデル選択優先順位**: インスタンス config > リクエスト `model_name` > グローバル `AI_CONFIG`。
- **記憶ブロック構築**: `Gatekeeper` 経由で `MemoryBlockBuilder.build_system_instruction_from_blocks()` (不変) と `build_context_prefix()` (可変) の 2 経路に分割される。可変側は `context_levels` (`normal` / `compact` / `low` / `custom`) で各セクションを `high` / `low` / `off` に圧縮可能。
- **バックグラウンド処理**: `sleeptime.py` は重い LLM 呼び出しを伴うため、API エンドポイント経由で別スレッド実行。
- **Provider 抽象化層**: ChatService → `ProviderFactory.create(ModelRef|dict|str)` → Connection → Protocol Adapter。OpenAI / Ollama / xAI は `OpenAICompatAdapter`（+ `_openai_compat.py` ヘルパー）で実装され、Gemini は `GeminiNativeAdapter` 経由で `providers/gemini.py`（`google.genai`）を呼ぶ。
- **Gatekeeper のフロー**: ContextClassifier (LLM, ~1s) + MemoryProbe (~100ms) を直列実行 → MemoryBlockBuilder → 応答生成 と StateUpdater (LLM, ~1s) は並列実行。
