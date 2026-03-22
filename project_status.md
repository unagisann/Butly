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
  - `butly_core/llm/providers/gemini.py`: Gemini プロバイダー（検索リトライ、コンテキストキャッシュ、Interactions API）
  - `butly_core/llm/providers/openai.py`: OpenAI プロバイダー（GPT-4o 等、Vision 対応）
  - `butly_core/llm/providers/ollama.py`: Ollama プロバイダー（ローカル LLM、OpenAI 互換 API 経由）
- **コアモジュール:** `butly_core/`
  - `gatekeeper.py`: ユーザー発言のティア(reflex/mid/cortex)判定とプロンプト構成ブロックの構築
  - `memory.py`: 記憶ファイル（RAW/Digest/Relationship）のI/O管理とフローティングサマリー制御
  - `brain.py`: RAG検索エンジン（キーワード抽出 + ベクトル類似度リランキング）— LLM 非依存
  - `housekeeper.py`: バックグラウンドでの記憶の抽象化（事実ダイジェスト・関係性のスナップショット生成）

## 現在のフェーズとステータス
- **マルチプロバイダー対応完了 (2026-03-22)**: Gemini 専用アーキテクチャをプロバイダー非依存に改修。`google.genai` の import を `GeminiProvider` のみに隔離し、OpenAI / Ollama プロバイダーを追加。`user_config.json` の `model_name` 変更のみでプロバイダー切り替え可能。埋め込みマイグレーションツール (`migrate_embeddings.py`) も提供。
- **Raspi V2 (画像チャット対応) 完了**: DTOの共通化、GeminiProviderへの画像処理の隠蔽化、`ChatService`の導入による責務分離アーキテクチャへのリファクタリングが完了。
- **Phase 4完了**: 中期記憶の二層要約（事実ダイジェスト＋関係性スナップショット）の動的プロンプト注入機能が完成。UIからの要約モードトグルにも対応。
- **マルチインスタンス対応完了**: インスタンスごとに独立したディレクトリ (`butly_core/instances/[name]`) で記憶DB・設定・プロンプトを管理。

## 開発上の留意点（他のAIへ向けて）
- **設定の優先順位**: `butly_core/config.py` がグローバルのデフォルト設定ですが、ユーザー設定は `user_config.json` でオーバーライドされ、さらにインスタンス固有の設定は `instances/[name]/config.json` に保存されます。
- **プロンプトの構成**: 記憶ブロックは一括で渡されるのではなく、Gatekeeper (`butly_core/core/gatekeeper.py` 内の `build_system_instruction_from_blocks`) によって適切なセクション（根幹記憶、ダイジェスト、RAG、現在時刻など）ごとに論理立てて組み立てられ、Brainに渡されます。
- **バックグラウンド処理**: `housekeeper.py` (記憶整理プロセス) は重いLLM呼び出しを伴うため、メインの `app.py` UI側からFastAPIのエンドポイントを叩き、別スレッドで非同期に実行する構成になっています。
- **Provider抽象化層**: チャットのリクエストは `ChatService` でオーケストレーションされ、`ProviderFactory` 経由で Gemini / OpenAI / Ollama のプロバイダーに委譲されます。画像エンコード・検索リトライ・コンテキストキャッシュなどの固有処理は `main.py` や `brain.py` ではなく、プロバイダー層 (`butly_core/llm/providers/`) で完結します。`google.genai` の import は `gemini.py` のみに隔離されています。
