# Recent Changes

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
- **クリーンアップ**: `housekeeper.py` の `generate_embedding()` 内の不要な `import asyncio` を削除。`migrate_embeddings.py` の不要な `import asyncio` を削除。
- **プロジェクト全体**: `asyncio.run()` 使用箇所 **0件** を確認。テスト **137件全パス**。

## マルチプロバイダー対応リファクタリング (2026-03-22)
Gemini 専用だったアーキテクチャを **プロバイダー非依存** に改修し、OpenAI / Ollama をサポート。

- **BaseProvider 拡張**: `summarize()`, `embed()`, `classify()` 抽象メソッドを追加。
- **brain.py 刷新**: 全 `google.genai` 依存を除去し、ProviderFactory 経由の純粋 RAG エンジンに縮小（~861行 → ~253行）。
- **ChatService 統一**: RAG 検索を ChatService 側に移動、全パスを `Provider.generate()` 経由に統一。
- **GeminiProvider 完成**: brain.py から移管した Gemini 固有ロジック（検索リトライ、コンテキストキャッシュ、ハルシネーションフィルタ）を集約。
- **OpenAIProvider 追加**: GPT-4o 等対応。Vision、embed（text-embedding-3-small）、classify を実装。
- **OllamaProvider 追加**: ローカル LLM 対応。OpenAI 互換 API（`localhost:11434/v1`）経由。
- **Gatekeeper / Housekeeper**: `google.genai` 依存を除去し、Provider.classify() / embed() 経由に切り替え。
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
   - `housekeeper.py` における中期記憶の整理機能を拡張し、出来事と決定事項をまとめた「事実ダイジェスト」と、AIとユーザーの距離感を示す「関係性スナップショット」の二層ファイル生成パイプラインを構築。
2. **OSS向けオープン化準備 / リファクタリング**:
   - 「Jarvis」などのハードコードされた初期名や個人情報を排除し、設定ファイルやテンプレートから動的に読み込む汎用的な「Butly」プラットフォームへと改修。
3. **ステートフルAPI (Interactions API) の導入** *(削除済み — マルチプロバイダ統一のため廃止)*:
   - 会話ターンごとに長期履歴を全て手動で挿入する状態から、Google Gemini側のセッション履歴保持機構に移行し、不要なトークン消費を抑制。
4. **FastAPI + Streamlit への分離**:
   - 処理の非同期化とバックグラウンドタスク（Housekeeper）の安定稼働、UI側のレスポンス向上のため、単一スクリプトからAPIサーバーとフロントエンドの構成に分離。
5. **インスタンス別記憶の分離**:
   - 複数の別キャラクター・別用途AIを同時に動かせるよう、`butly_core/instances/` ディレクトリ配下で記憶DBとファイルを完全分離。
