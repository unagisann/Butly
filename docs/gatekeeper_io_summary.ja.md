# Gatekeeper 入出力仕様・プロンプト注入まとめ

🌐 **日本語** | [English](gatekeeper_io_summary.md)

現在の `butly_core/core/gatekeeper/` パッケージの実装に基づく、Gatekeeper（前頭葉）モジュールでの入出力データと、判定された各Tier（階層）に応じてメインのAI（Brain）へ最終的に渡される情報の一覧です。

---

## アーキテクチャ概要

Gatekeeper は以下の 4 コンポーネントに分割されています。
`Gatekeeper` クラスが facade として各コンポーネントをオーケストレーションします。

| コンポーネント | ファイル | 役割 |
|---|---|---|
| `TierClassifier` | `tier_classifier.py` | LLM に 4 スコアを出力させ、Python 側で tier を最終決定 |
| `StateUpdater` | `state_updater.py` | ユーザー発言から session_state の差分（state_delta）を生成 |
| `SearchPlanner` | `search_planner.py` | cortex 時のみ呼び出され、RAG 検索キーワードを生成。`need: null` で RAG スキップ可 |
| `MemoryBlockBuilder` | `memory_builder.py` | tier に応じた記憶ブロック辞書を構築し Brain へ渡す |

### 処理フロー

```
ユーザー発言
  ↓
[A] TierClassifier.classify()   ← LLM呼び出し（並列実行）
[B] StateUpdater.update()       ← LLM呼び出し（並列実行）
[C] SearchPlanner.plan()        ← cortex 時のみ
  ↓
Gatekeeper.classify() が結果をマージして返却
  ↓
MemoryBlockBuilder.build()  → Brain へのプロンプト構築
```

---

## 1. Gatekeeper が【受け取る情報】（各コンポーネントへの入力）

### TierClassifier への入力

- **ユーザーの最新の発言** (`user_input`)
- **直近の会話履歴** (`history_msgs`): 直近のやり取り（最大 3 ターン分）
- **現在のトピック** (`current_topic`): SessionState から渡される話題文字列

### StateUpdater への入力

- **ユーザーの最新の発言** (`user_input`)
- **直近の会話履歴** (`history_msgs`)
- **現在のセッション状態** (`current_state`): 以下のフィールドを持つ SessionState:
  - `topic`: 現在の話題
  - `mood`: 会話のムード（デフォルト: `neutral`）
  - `goals`: 目標リスト（最大 5 件）
  - `unresolved`: 未解決事項リスト（最大 8 件）
  - `turn_count`: 経過ターン数
  - `last_tier`: 直前の処理 tier

### SearchPlanner への入力（cortex 時のみ）

- **ユーザーの最新の発言** (`user_input`)
- **直近の会話履歴** (`history_msgs`)
- **現在のトピック** (`current_topic`)

---

## 2. Gatekeeper の【出力】（`Gatekeeper.classify()` の返却値）

```python
{
    "tier": "reflex" | "mid" | "cortex",
    "topic": str,          # state_delta または現在 topic
    "need": str | None,   # cortex 時のみ。null の場合 RAG 検索をスキップ
    "search_targets": list[str] | None,  # cortex 時のみ。need が null なら null
    "state_delta": {
        "topic": str | None,
        "mood": str | None,
        "add_goal": str | None,
        "add_unresolved": str | None,
        "resolve": str | None
    },
    "llm_scoring": {
        "response_complexity": float,      # 0〜1
        "emotional_weight": float,         # 0〜1
        "memory_reference_likelihood": float,  # 0〜1
        "continuity_need": float           # 0〜1
    }
}
```

### tier 判定ルール（TierClassifier）

LLM が出力する 4 スコアを Python 側で以下のルールで tier を決定します:

| 条件 | 結果 |
|---|---|
| `memory_reference_likelihood ≥ 0.7` | → `cortex` |
| `response_complexity ≥ 0.8` または `continuity_need ≥ 0.8` | → `mid` 以上 |
| `emotional_weight ≥ 0.7` | → `mid` 以上 |
| 上記にかからない | → `reflex` |

---

## 3. 各Tier判定後にメインAIへ【渡される情報（注入されるプロンプト）】

`MemoryBlockBuilder.build()` が tier に応じた記憶ブロックを構築し、Brain（応答生成LLM）へ渡します。

### 🔘 全Tier共通で【必ず渡される情報】

| 順序 | 情報 | 内容 |
|---|---|---|
| 1 | **SYSTEM INSTRUCTION** | AIの基本性格・システム設定 |
| 2 | **KEY MEMORY** | ユーザーに関する根幹・不変の記憶 |
| 3 | **CURRENT TIME** | 現在時刻（システムノート） |
| 4 | **GLOSSARY** | 共通言語辞書（意味記憶。glossary.yaml のアクティブエントリ） |
| 5 | **MID-TERM（条件付）** | mid 以上のみ（下記参照） |
| 6 | **RAG（条件付）** | cortex + need有りのみ（下記参照） |
| 7 | **FLOATING SUMMARY** | 最新の対話の浮動要約 |
| 8 | **TIER INFO** | 現在の思考モード（reflex/mid/cortex） |
| 9 | **WEB SEARCH RESULTS**（条件付） | 非Gemini + use_web_search=True 時のみ。Tavily API 経由のWeb検索結果 |
| 10 | **Short Term** | 直近 6 ターンの会話履歴 |

### 🔵 Tier別に追加される情報

#### 【 Tier 1 】 reflex（脊髄反射）
- **追加情報**: なし
- 挨拶、相槌、「うんわかった」系の短い返事で発動。知識検索を待たず即座に返す。

#### 【 Tier 2 】 mid（中脳・感情系）
- **追加情報**: `use_summarized_mid_term` 設定により切り替わる:
  - `False`（RAWモード）: **MID-TERM MEMORY** （`mid_term.txt` の全文テキスト）
  - `True`（要約モード）: **MID-TERM DIGEST** + **RELATIONSHIP SNAPSHOT** のセット
    （要約ファイルがない場合は RAW にフォールバック）
- 現在の話題に関する質問や、少し前のやり取りの前提を踏まえる会話で発動。

#### 【 Tier 3 】 cortex（大脳皮質）
- **追加情報**:
  - mid の情報すべて
  - ➕ **LONG-TERM MEMORY (RAG)**: `butly_memory.db` からの検索結果
    （SearchPlanner が生成した `search_targets` をキーワードに使用）
  - ※ SearchPlanner が `need: null` を返した場合は RAG 検索をスキップ
- 「あの時の」といった過去への言及や、深い考察が必要な問いで発動。

---

## 4. SessionState の永続化

`SessionState` クラスが `session_state.json` への読み書きを担当します。`StateUpdater` が生成した `state_delta` を、`ChatService` 側で `SessionState.apply_delta()` で適用・保存します。

```python
# state_delta の構造
state_delta = {
    "topic": str | None,           # 話題の更新
    "mood": str | None,            # ムードの更新
    "add_goal": str | None,        # 目標を追加
    "add_unresolved": str | None,  # 未解決事項を追加
    "resolve": str | None          # 未解決事項を解決済みに移動
}
```
