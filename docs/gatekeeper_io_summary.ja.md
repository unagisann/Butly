# Gatekeeper 入出力仕様・プロンプト注入まとめ

🌐 **日本語** | [English](gatekeeper_io_summary.md)

現在の `butly_core/core/gatekeeper/` パッケージの実装に基づく、Gatekeeper（前頭葉）モジュールでの入出力データと、判定された各Tier（階層）に応じてメインのAI（Brain）へ最終的に渡される情報の一覧です。

---

## アーキテクチャ概要（Phase 1.5）

Gatekeeper は以下の 4 コンポーネントに分割されています。
`Gatekeeper` クラスが facade として各コンポーネントをオーケストレーションします。

| コンポーネント | ファイル | 役割 |
|---|---|---|
| `ContextClassifier` | `context_classifier.py` | LLM に 3 スコア（`rc`/`ew`/`cn`）を出力させ、Python 側で reflex か mid を決定 |
| `StateUpdater` | `state_updater.py` | ユーザー発言から session_state の差分（state_delta）を生成 |
| `MemoryProbe` | `memory_probe.py` | LLM呼び出しなしの事実ベース記憶検索（ベクトル検索 + 用語集マッチ） |
| `MemoryBlockBuilder` | `memory_builder.py` | tier に応じた記憶ブロック辞書を構築し Brain へ渡す |

> **備考:** `TierClassifier`（4スコア・3値tier）と `SearchPlanner` は後方互換のため残存していますが、アクティブパスでは使用されていません。

### 処理フロー

```
ユーザー発言
  ↓
[A] ContextClassifier.classify()   ← LLM呼び出し（並列実行）
[B] StateUpdater.update()          ← LLM呼び出し（並列実行）
  ↓
[C] MemoryProbe.probe()            ← LLM不要（~100ms）
    ├─ Layer 1: Quick Vector Search（コサイン類似度）
    ├─ Layer 1.5: Glossary Match（term/aliases）
    └─ Layer 2: Deep Search（条件付き — 過去参照パターン検出時のみ）
  ↓
Gatekeeper.classify() が結果をマージ
  （mid + probe ヒット → 互換レイヤーで cortex に昇格）
  ↓
MemoryBlockBuilder.build()  → Brain へのプロンプト構築
```

---

## 1. Gatekeeper が【受け取る情報】（各コンポーネントへの入力）

### ContextClassifier への入力

- **ユーザーの最新の発言** (`user_input`)
- **直近の会話履歴** (`history_msgs`): 直近のやり取り（最大 3 ターン分）
- **現在のトピック** (`current_topic`): SessionState から渡される話題文字列
- **最近の会話ヘッドライン** (`recent_headlines`): `recent_digest_headlines.json` から抽出されたヘッドライン（Sleeptime 日次バッチで生成）

### StateUpdater への入力

- **ユーザーの最新の発言** (`user_input`)
- **直近の会話履歴** (`history_msgs`)
- **現在のセッション状態** (`current_state`): 以下のフィールドを持つ SessionState:
  - `topic`: 現在の話題（live topic。10ターン経過＋直近3ターン言及なしで自動消滅）
  - `mood`: 会話のムード（デフォルト: `neutral`）
  - `turn_count`: 経過ターン数
  - `last_tier`: 直前の処理 tier

### MemoryProbe への入力

- **ユーザーの最新の発言** (`user_input`)
- **Brain インスタンス** (`brain`): ナレッジDBに対するベクトル検索用
- **Memory manager** (`memory_manager`): 用語集の参照用

---

## 2. Gatekeeper の【出力】（`Gatekeeper.classify()` の返却値）

```python
{
    "tier": "reflex" | "mid" | "cortex",  # cortex = mid + MemoryProbe ヒット（互換レイヤー）
    "topic": str,          # state_delta または現在 topic
    "need": str | None,   # cortex 時のみ。"memory_probe_hit" or "memory_probe_deep_search"
    "search_targets": list[str] | None,  # cortex 時のみ
    "state_delta": {
        "topic": str | None,
        "mood": str | None,
    },
    "llm_scoring": {
        "response_complexity": float,      # 0〜1
        "emotional_weight": float,         # 0〜1
        "continuity_need": float           # 0〜1
    },
    "memory_probe": {
        "status": "hit" | "no_hit" | "deep_search",
        "candidates": list[dict],      # probe からの RAG 検索結果
        "glossary_hits": list[dict]     # マッチした用語集エントリ
    }
}
```

### tier 判定ルール（ContextClassifier）

ContextClassifier が LLM に 3 スコアを出力させ、Python 側で以下のルールで tier を決定します:

| 条件 | 結果 |
|---|---|
| `response_complexity <= 0.4` AND `continuity_need <= 0.3` | → `reflex` |
| 上記以外 | → `mid` |

> **互換レイヤー:** `tier == "mid"` かつ MemoryProbe がヒットした場合、後方互換のため `cortex` に昇格します。将来のフェーズで廃止予定。

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

#### 【 Tier 3 】 cortex（大脳皮質）— 互換レイヤー
- **追加情報**:
  - mid の情報すべて
  - ➕ **LONG-TERM MEMORY (RAG)**: `butly_memory.db` からの検索結果
    （MemoryProbe が返した候補を使用）
  - ※ MemoryProbe が `status: "no_hit"` を返した場合、tier は `mid` のままで RAG は注入されない
- tier が `mid` かつ MemoryProbe がヒットした場合に有効化。将来のフェーズで廃止予定の後方互換レイヤー。

---

## 4. SessionState の永続化

`SessionState` クラスが `session_state.json` への読み書きを担当します。`StateUpdater` が生成した `state_delta` を、`ChatService` 側で `SessionState.apply_delta()` で適用・保存します。

```python
# state_delta の構造
state_delta = {
    "topic": str | None,           # 話題の更新
    "mood": str | None,            # ムードの更新
}
```

**注記:** `goals`, `unresolved`, `add_goal`, `add_unresolved`, `resolve` フィールドは廃止されました。
