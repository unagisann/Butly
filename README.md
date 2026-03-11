# Butly 🤵

Google Gemini API をベースにした、**多層的記憶システムを持つパーソナルAIアシスタント**プラットフォームです。
複数のAIインスタンス（ペルソナ）を管理でき、過去の会話から知識を蓄積・検索（RAG）する機能を備えています。

---

## 特徴

- 🧠 **5層記憶システム** — 短期・浮動要約・中期・長期(RAG)・根幹記憶
- 🎭 **マルチインスタンス** — 複数のAIペルソナを作成・切り替え
- 🔍 **RAG検索** — Embedding + コサイン類似度による知識検索
- 🧹 **Housekeeper** — 記憶の自動整理・ナレッジ化バッチ処理
- 🧬 **Gatekeeper** — ユーザー発言をtier判定し、必要な記憶のみを注入

---

## アーキテクチャ

```
┌─────────────────────────────────┐
│   Streamlit フロントエンド (app.py)   │  ← チャットUI / DB閲覧 / 設定
└────────────┬────────────────────┘
             │ HTTP (REST API)
┌────────────▼────────────────────┐
│  FastAPI バックエンド (main.py)    │  ← チャット処理 / RAG / Housekeeper
│  ├── gatekeeper.py (Tier 判定)    │
└────────────┬────────────────────┘
             │
┌────────────▼────────────────────┐
│         butly_core/             │  ← AIコアモジュール
│  ├── brain.py    (LLM/RAG)      │
│  ├── memory.py   (記憶管理)       │
│  ├── database.py (SQLite操作)    │
│  ├── instance_manager.py        │
│  └── config.py / prompts.py     │
└─────────────────────────────────┘
```

---

## セットアップ

### 1. リポジトリのクローン

```bash
git clone https://github.com/unagisann/Butly.git
cd butly
```

### 2. 環境構築

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. APIキーの設定

```bash
cp .env.example .env
```

`.env` を編集し、Google Gemini の API キーを記入します：

```
GOOGLE_API_KEY=AIza...
```

### 4. 設定ファイルの準備

```bash
cp user_config.json.example user_config.json
```

`user_config.json` でAIモデル名やパラメータ、エージェント名を自由にカスタマイズできます。

### 5. 起動

**バックエンド（FastAPI）を起動：**

```bash
uvicorn main:app --port 8000 --reload
```

**フロントエンド（Streamlit）を起動：**

```bash
streamlit run app.py
```

ブラウザで `http://localhost:8501` を開きます。

---

## Housekeeper（記憶の定期整理）

短期記憶をナレッジDB（SQLite）へ変換する定期処理です。

```bash
python housekeeper.py
```

または Web UI の「🧹 記憶の整理」ボタンから実行できます。

> **推奨**: 毎日深夜など、会話が少ない時間帯に実行してください。

---

## ファイル構成

| ファイル | 説明 |
|---|---|
| `main.py` | FastAPI バックエンドサーバー |
| `app.py` | Streamlit フロントエンド |
| `housekeeper.py` | 記憶整理バッチ処理 |
| `user_config.json` | ユーザー設定（AIモデル・閾値など） |
| `user_prompts.json` | プロンプトカスタマイズ |
| `requirements.txt` | Python 依存パッケージ |

```
butly_core/
├── config.py          ← AI/システム設定のデフォルト値
├── prompts.py         ← プロンプトテンプレート
└── core/
    ├── brain.py       ← LLM呼び出し / RAG / 要約
    ├── memory.py      ← 記憶の読み書き管理
    ├── database.py    ← SQLite操作（知識カード）
    ├── gatekeeper.py  ← Tier判定（Gatekeeper）
    ├── instance_manager.py ← インスタンスの作成・管理
    └── chronos.py     ← 時刻コンテキスト生成

butly_core/instances/{instance_name}/
├── config.json            ← インスタンス固有設定
├── system_instruction.txt ← 性格設定（system prompt）
├── Key_Memory.txt         ← 根幹記憶（不変の事実）
├── mid_term.txt           ← 中期記憶（累積テキスト）
├── butly_memory.db        ← 長期記憶 SQLite DB
├── short_term_json/       ← 直近の会話ログ (JSON)
├── floating_summaries/    ← 浮動要約（一時的な要約）
└── memory_archive/
    ├── 1_integrated/      ← Housekeeper処理待ちログ
    ├── 2_knowledgeized/   ← ナレッジ化済みログ（保存用）
    └── 3_log/             ← 長期アーカイブテキスト
```

---

## 記憶システムの概要

5層の記憶構造で、会話履歴を「鮮度」と「重要度」に応じて管理します。

| 層 | 名称 | 形式 | 参照タイミング |
|:---|:---|:---|:---|
| 1 | **短期記憶** | JSON | 会話履歴としてそのまま渡す |
| 2 | **浮動要約** | TXT | 常にシステムプロンプトに注入 |
| 3 | **中期記憶** | TXT | 常にシステムプロンプトに注入 |
| 4 | **長期記憶** | SQLite | 発言に応じてRAG検索 |
| 5 | **根幹記憶** | TXT | 常にシステムプロンプトに注入 |

---

## 技術スタック

- **LLM**: Google Gemini API (`google-genai`)
- **バックエンド**: FastAPI + Uvicorn
- **フロントエンド**: Streamlit
- **DB**: SQLite（ベクトル検索: Cosine Similarity + Numpy）
- **Embedding**: `models/gemini-embedding-001`

---

## ライセンス

MIT License
