# Butly 🤵

🌐 **日本語** | [English](README.md)

> ⚠️ 本プロジェクトは現在開発中です。

**Butly** は多層的な記憶システムを持つパーソナルAIアシスタント基盤です。
過去の会話を記憶し、時間とともにナレッジを蓄積し、
現在のメッセージだけでなく蓄積されたコンテキストに基づいて応答を適応させます。

**マルチプロバイダー対応**（Google Gemini / OpenAI / xAI / Ollama）、
**複数AIインスタンス**（ペルソナ）管理、会話履歴からの**RAGベースの知識検索**、
リアルタイム描画のための **SSE ストリーミング** をサポートしています。

---

## 主な機能

### 記憶システム

Butly は複数の記憶レイヤーを連携させて動作します：

| レイヤー | 説明 |
|---------|------|
| **短期記憶** | 直近の会話ターン（JSON） |
| **会話圧縮ログ** | 会話単位のローリング要約（相対時刻ヘッダー付き） |
| **中期ダイジェスト** | エピソード付き事実ダイジェスト（日次更新） |
| **関係性スナップショット** | AIとユーザーの関係性認識（週次更新） |
| **ナレッジカード** | ベクトル埋め込み付きでSQLiteに保存された蒸留知識（RAG検索用） |
| **Glossary / Lorebook** | インスタンス別 YAML の用語・別名辞書。毎ターン走査されて意味記憶として注入 |
| **根幹記憶** | ユーザーとペルソナに関する永続的な核心情報（テキスト） |

### Gatekeeper（メタ認知エンジン）

応答生成の前に、Gatekeeperがユーザーメッセージを分類し、どの程度の記憶コンテキストを注入するかを判断します：

- **reflex** — 最小コンテキストで十分な軽い応答
- **mid** — 記憶注入が有効な会話

tier 閾値（`tier_rc_threshold` / `tier_cn_threshold`）はインスタンス単位で設定可能です。
RAG 注入は **tier とは独立** で、「LLM 意図 + 事実裏付け」の 2 段構えで決定されます:
ContextClassifier が `need_intent` (`past_fact` / `glossary` / `relationship` / `null`) を出力し、
**MemoryProbe** がベクトル検索 / 用語集マッチ / 条件付き deep search で裏付けます（すべて LLM 不要）。

Glossary scan は regex のみ・~ms オーダーなので、`need_intent` に関わらず毎ターン実行されます。

また**SessionState**（トピック、ムード、ターン数）はセッション全体で永続化されます。

### Sleeptime（記憶の定期整理）

生の会話ログを構造化されたナレッジに蒸留するバックグラウンドプロセスです：

- **日次**: 中期ダイジェスト生成 + ナレッジカード作成
- **週次**: 関係性スナップショット更新

一日の会話がひと段落したタイミングで手動実行、またはWeb UIから実行できます。

### マルチインスタンス

複数のAIペルソナを作成・切り替え可能。それぞれ独自の性格、記憶、会話履歴を持ちます。

### マルチプロバイダー LLM

ロールごとにプロバイダーを混在可能 — 例: チャットはOpenAI、埋め込みはGemini、GatekeeperはOllama：

| プロバイダー | モデルプレフィックス | APIキー |
|------------|-------------------|--------|
| **Gemini** | `gemini-*` / `models/gemini-*` | `GOOGLE_API_KEY` |
| **OpenAI** | `gpt-*` / `o1` / `o3` / `o4` | `OPENAI_API_KEY` |
| **xAI (Grok)** | `grok-*` / `xai/*` | `XAI_API_KEY` |
| **Ollama** | `ollama/*` | 不要（ローカル実行） |

OpenAI / xAI / Ollama は `butly_core/llm/_openai_compat.py` の共通ヘルパーを使い、メッセージ構築・推論モデル（`o1`/`o3`/`o4`）対応・履歴ロール変換（`model` → `assistant`）を共有しています。

### RAG検索（ButlyBrain）

キーワードフィルタリング（SQLite LIKE）とベクトルコサイン類似度リランキングを組み合わせたハイブリッド検索。時間減衰スコアリング、クロスインスタンスDB検索にも対応。閾値や減衰率は古いカードも到達可能になるよう調整されており、各レイヤーの診断情報はチャット debug ログに記録されます。

### ストリーミング応答 (SSE)

`POST /chat/stream` は `text/event-stream` 形式で `metadata` → `chunk` → `done` イベントを順次返します。Streamlit UI のチャットヘッダーに「Streaming」トグルがあり、バッファ応答と逐次応答をターンごとに切替できます。Gatekeeper メタデータ（tier / need / scores）は最初の chunk より前に送出され、`StateUpdater` は並列実行でクリティカルパス外に逃がしています。

### Web検索

- **Geminiモデル使用時**: Google Search Grounding（組み込み）
- **その他のプロバイダー**: Tavily API または Ollama Cloud Web Search（`OLLAMA_WEB_SEARCH_API_KEY`）

---

## クイックスタート

### 1. クローン

```bash
git clone https://github.com/unagisann/Butly.git
cd Butly
```

### 2. 依存パッケージのインストール

**Linux / macOS:**

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Windows:**
`01_setup_requirements.bat` をダブルクリック — `.venv` の作成と依存パッケージのインストールが自動で行われます。

### 3. APIキーの設定

**Linux / macOS:**

```bash
cp APIkey.env .env
```

**Windows:** バッチファイル実行時に自動で処理されます。

`.env` に使用するプロバイダーのキーを設定：

```env
# Google Gemini（デフォルト）
GOOGLE_API_KEY=AIza...

# OpenAI
OPENAI_API_KEY=sk-...

# xAI (Grok)
XAI_API_KEY=xai-...

# Web 検索（非 Gemini 時）
TAVILY_API_KEY=tvly-...
OLLAMA_WEB_SEARCH_API_KEY=...

# Ollama — キー不要（ローカル実行）
```

### 4. 設定ファイルの準備

**Linux / macOS:**

```bash
cp user_config.json.example user_config.json
```

**Windows:** バッチファイル実行時に自動で処理されます。

`user_config.json` でモデル、パラメータ、エージェント名をカスタマイズできます。
`user_config.json.example` に Gemini / OpenAI / Ollama の設定例が含まれています。

> **注意**: プロバイダーを切り替えるとEmbeddingの次元が変わり、RAG検索精度が低下します。
> 以下のコマンドで埋め込みを再生成してください：
> ```bash
> python migrate_embeddings.py --all
> ```

### 5. 起動

**Linux / macOS:**

```bash
# バックエンド（FastAPI）
uvicorn main:app --port 8000 --reload

# フロントエンド（Streamlit）— 別ターミナルで
streamlit run app.py
```

ブラウザで `http://localhost:8501` を開きます。

**Windows:**
`02_start_webui.bat` をダブルクリック — 両サーバーが起動しブラウザが自動で開きます。

---

## 初回起動後の設定

### ① 言語設定

**⚙️**（右上）→ **基本設定** → 言語を選択 → **💾 保存**

### ② LLM / APIキー設定

**⚙️** → **LLMプロバイダー** タブ：

1. APIキーを入力 → **💾 保存**
2. 各ロール（Chat / Summary / Gatekeeper / Embedding）にモデルを割り当て
   - プリセットから選択、または **「✏️ カスタム入力...」** で任意のモデル名を入力
   - Ollamaモデル: `ollama/` プレフィックスを付ける（例: `ollama/phi3`）
3. **💾 モデル設定を保存**

### ③ AIインスタンスの作成

1. ホーム画面の **➕ 新しいインスタンスを作成** を展開
2. **インスタンス名**（半角英数字・`_`）を入力
3. **性格テンプレート** を選択（Butly / Creator / Analyst / Friendly / Caring / カスタム）
4. **AIの名前** を入力（必須）
5. **作成** をクリック → AI名をクリックしてチャット開始

---

## アーキテクチャ

```mermaid
flowchart TD
    A((ユーザー発言)) --> B["⧫ Gatekeeper<br/>ContextClassifier + MemoryProbe"]
    B --> C{tier}
    C -->|reflex| D["⚡ 最小コンテキスト"]
    C -->|mid| E["◎ 記憶注入あり"]
    B --> N{need?<br/>(tier 非依存)}
    N -->|有| R["⌕ RAG ブロック<br/>MemoryProbe の candidates から"]
    D --> F["◆ ChatService<br/>Provider.generate() / .async_generate_stream()"]
    E --> F
    R --> F
    F -.->|並列実行| SU["⟳ StateUpdater<br/>(post-response)"]
    F --> G((返答 / SSE chunks))
    G --> H["▣ short_term_json 保存"]
    H -.->|定期処理| I["⚙ Sleeptime<br/>日次 + 週次バッチ"]
    I -.->|ナレッジ生成| J[("⛁ ナレッジDB<br/>(SQLite + Embeddings)")]
    J -.->|RAG検索| F
```

### 設計原則

| 原則 | 説明 |
|------|------|
| **状態中心** | 発話に反応するのではなく、会話ごとに内部状態を更新する |
| **不足前提中心** | 似ている記憶ではなく、今の判断に必要な前提を探す |
| **統合記憶中心** | 生エピソードだけでなく、反省・要約・一般化を別層で持つ |
| **メタ認知中心** | ルール決め打ちではなく、AI自身に「何を知る必要があるか」を考えさせる |

---

## デフォルトモデル（Gemini）

| ロール | モデル | 用途 | 頻度 |
|--------|-------|------|------|
| Chat | gemini-3-flash-preview | 応答生成 | 1回/ターン |
| Gatekeeper | gemini-3.1-flash-lite-preview | tier判定・メタ認知 | 1回/ターン |
| Summary | gemini-3.1-flash-lite-preview | ダイジェスト・関係性 | 日次バッチ |
| Knowledge | gemini-3.1-pro-preview | ナレッジカード生成 | 日次バッチ |
| Embedding | gemini-embedding-001 | ベクトル検索 | カード生成時 |
| Session Digest | gemini-3.1-flash-lite-preview | 溢れ圧縮 | リアルタイム |

すべて `user_config.json` で変更可能。

---

## 技術スタック

- **LLM**: Google Gemini / OpenAI / xAI (Grok) / Ollama — マルチプロバイダー
- **バックエンド**: FastAPI + Uvicorn（REST `/chat`、SSE `/chat/stream`、WebSocket `/ws`）
- **フロントエンド**: Streamlit
- **DB**: SQLite（ベクトル検索: コサイン類似度 + NumPy）
- **Web検索**: Tavily API / Ollama Cloud Web Search / Google Search Grounding

---

## ドキュメント

- [ドキュメント一覧](docs/README.ja.md)
- [Discord 連携セットアップ](docs/guides/discord_integration_setup.ja.md)
- [LINE 連携セットアップ](docs/guides/line_integration_setup.ja.md)
- [アーキテクチャ図集](docs/reference/DIAGRAMS.ja.md)
- [Gatekeeper 入出力仕様](docs/reference/gatekeeper_io_summary.ja.md)
- [記憶ライフサイクル](docs/reference/memory_lifecycle.ja.md)
- [ファイル構成](docs/reference/FILE_STRUCTURE.ja.md)
- [context_levels](docs/reference/context_levels.ja.md)
- [プロジェクト状況](docs/history/project_status.ja.md) / [更新履歴](docs/history/recent_changes.ja.md)

---

## ライセンス

MIT License
