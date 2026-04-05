# Project Status: Butly AI Agent Platform

## 概要
Butlyは、高度な記憶管理機能（短期記憶・中期記憶・長期記憶・浮動要約）を備えたパーソナルAIアシスタントプラットフォームです。

## アーキテクチャ構成
- **フロントエンド:** Streamlit (`app.py`) - WEB UI、チャット、データベースブラウザ、Housekeeper管理
- **バックエンド:** FastAPI (`main.py`) - APIサーバー、バックグラウンド処理稼働
- **データベース:** SQLite (`butly_memory.db`) - インスタンスごとの記憶ベクトル化保存とRAG検索
- **AIエンジン:** マルチプロバイダー対応 (Google Gemini / OpenAI / Ollama)
  - `butly_core/llm/base.py`: 抽象基底クラス (`generate`, `summarize`, `embed`, `classify`)
  - `butly_core/llm/factory.py`: モデル名プレフィックスによるプロバイダー自動ルーティング
  - `butly_core/llm/providers/gemini.py`: Gemini プロバイダー（検索リトライ、コンテキストキャッシュ）
  - `butly_core/llm/providers/openai.py`: OpenAI プロバイダー（GPT-4o 等、Vision 対応）
  - `butly_core/llm/providers/ollama.py`: Ollama プロバイダー（ローカル LLM、OpenAI 互換 API 経由）
- **コアモジュール:** `butly_core/`
  - `gatekeeper/`: ユーザー発言のティア(reflex/mid)判定（ContextClassifier）、事実ベース記憶検索（MemoryProbe）、セッション状態更新（StateUpdater）、プロンプト構成ブロックの構築（MemoryBlockBuilder）
  - `memory.py`: 記憶ファイル（RAW/Digest/Relationship）のI/O管理とフローティングサマリー制御
  - `brain.py`: RAG検索エンジン（キーワード抽出 + ベクトル類似度リランキング + quick_vector_search）— LLM 非依存
  - `housekeeper.py`: バックグラウンドでの記憶の抽象化（事実ダイジェスト・関係性のスナップショット生成）

## 現在のフェーズとステータス
- **Gatekeeper Phase 1.5: 事実ベース MemoryProbe (2026-04-06)**: MemoryJudge（LLM ~2s）を MemoryProbe（LLM不使用 ~100ms）に置換。3層構造: Layer 1 quick_vector_search / Layer 1.5 glossary match / Layer 2 deep search（条件付き）。Gatekeeper 並列実行を 3→2 に削減。memory_builder 内の Brain RAG 呼び出しを廃止し probe candidates から直接注入。Glossary の選択的注入を実現。想定レイテンシ: Gatekeeper+MemBuild ~5s → ~1.5s。テスト 330 件全パス。
- **Housekeeper リソース最適化 (2026-04-04)**: ローカルLLM運用やAPI長文コンテキスト処理の安定性向上。Stage 2（ナレッジ化）のスキップ機能（`skip_knowledge_generation`）、Stage 1 Digest の日付ヘッダ区切りチャンク分割（`digest_max_input_chars`）、Stage 2 Knowledge のファイル単位チャンク分割（`knowledge_max_input_chars`）を実装。UIにも3項目追加。
- **汎用Web検索モジュール追加 (2026-03-31)**: Gemini 以外のプロバイダー（OpenAI / Ollama）でもWeb検索を利用可能に。`butly_core/search/` パッケージとして Tavily Search API を統合。検索結果は `ChatService` が `context_prefix` に注入し、LLM には通常のコンテキストとして渡すパターンA方式を採用。Gemini は従来通り Native Grounding を使用。UI では非 Gemini 時に 🔍 トグルを表示し、月次使用量トラッキングも搭載。
- **Provider 同期/非同期統一完了 (2026-03-23)**: 全プロバイダーの `generate()` / `summarize()` を `async def` → `def`（同期）に統一。FastAPI の実行中イベントループと `asyncio.run()` が競合する問題を根本解決。`ChatService` 側で `run_in_threadpool()` に逃がす設計に変更。`BaseProvider` に将来の段階的非同期移行用として `async_generate()` / `async_summarize()` / `async_embed()` のデフォルト実装（同期版ラップ）を追備。
- **マルチプロバイダー対応完了 (2026-03-22)**: Gemini 専用アーキテクチャをプロバイダー非依存に改修。`google.genai` の import を `GeminiProvider` のみに隔離し、OpenAI / Ollama プロバイダーを追加。`user_config.json` の `model_name` 変更のみでプロバイダー切り替え可能。埋め込みマイグレーションツール (`migrate_embeddings.py`) も提供。
- **Raspi V2 (画像チャット対応) 完了**: DTOの共通化、GeminiProviderへの画像処理の隠蔽化、`ChatService`の導入による責務分離アーキテクチャへのリファクタリングが完了。
- **Phase 4完了**: 中期記憶の二層要約（事実ダイジェスト＋関係性スナップショット）の動的プロンプト注入機能が完成。UIからの要約モードトグルにも対応。
- **マルチインスタンス対応完了**: インスタンスごとに独立したディレクトリ (`butly_core/instances/[name]`) で記憶DB・設定・プロンプトを管理。

## 同期/非同期設計方針
- **全プロバイダーメソッドは同期**: `generate()`, `summarize()`, `embed()`, `classify()` はすべて `def`（同期）。FastAPIのイベントループをブロックしないよう、`ChatService` 内で `starlette.concurrency.run_in_threadpool()` 経由で実行。
- **非同期移行への布石**: `BaseProvider` に `async_generate()` / `async_summarize()` / `async_embed()` のデフォルト実装（同期版を `run_in_threadpool` でラップ）を備備。プロバイダーを段階的に非同期化する際は、これらをオーバーライドするだけで移行可能。
- **プロジェクト全体で `asyncio.run()` は 0 件**。

## 開発上の留意点（他のAIへ向けて）
- **設定の優先順位**: `butly_core/config.py` がグローバルのデフォルト設定ですが、ユーザー設定は `user_config.json` でオーバーライドされ、さらにインスタンス固有の設定は `instances/[name]/config.json` に保存されます。
- **プロンプトの構成**: 記憶ブロックは一括で渡されるのではなく、Gatekeeper (`butly_core/core/gatekeeper/memory_builder.py` の `build_system_instruction_from_blocks` + `build_context_prefix`) によって適切なセクション（根幹記憶、Glossary、ダイジェスト、RAG、現在時刻など）ごとに論理立てて組み立てられ、Brainに渡されます。
- **バックグラウンド処理**: `housekeeper.py` (記憶整理プロセス) は重いLLM呼び出しを伴うため、メインの `app.py` UI側からFastAPIのエンドポイントを叩き、別スレッドで非同期に実行する構成になっています。
- **Provider抽象化層**: チャットのリクエストは `ChatService` でオーケストレーションされ、`ProviderFactory` 経由で Gemini / OpenAI / Ollama のプロバイダーに委譲されます。画像エンコード・検索リトライ・コンテキストキャッシュなどの固有処理は `main.py` や `brain.py` ではなく、プロバイダー層 (`butly_core/llm/providers/`) で完結します。`google.genai` の import は `gemini.py` のみに隔離されています。
