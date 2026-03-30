# Recent Changes

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
