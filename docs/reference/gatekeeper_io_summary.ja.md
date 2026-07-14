# Gatekeeper 入出力仕様・プロンプト注入まとめ

🌐 **日本語** | [English](gatekeeper_io_summary.md)

現在の `butly_core/core/gatekeeper/` パッケージの実装に基づく、Gatekeeper（前頭葉）モジュールでの入出力データと、判定された各Tier（階層）に応じてメインのAI（Brain）へ最終的に渡される情報の一覧です。

---

## アーキテクチャ概要

Gatekeeper は以下の 4 コンポーネントに分割されています。
`Gatekeeper` クラスが facade として各コンポーネントをオーケストレーションします。

| コンポーネント | ファイル | 役割 |
|---|---|---|
| `ContextClassifier` | `context_classifier.py` | LLM に 3 スコア（`rc`/`ew`/`cn`）を出力させ、Python 側で reflex か mid を決定 |
| `StateUpdater` | `state_updater.py` | ユーザー発言から session_state の差分（state_delta）を生成 |
| `MemoryProbe` | `memory_probe.py` | LLM呼び出しなしの事実ベース記憶検索（ベクトル検索 + 用語集マッチ） |
| `MemoryBlockBuilder` | `memory_builder.py` | tier に応じた記憶ブロック辞書を構築し Brain へ渡す |

tier は `reflex` / `mid` の 2 値のみ。RAG 注入は tier ではなく `need`（MemoryProbe 由来）で独立に決定されます。

### 処理フロー

```
ユーザー発言
  ↓
[A] ContextClassifier.classify()   ← LLM呼び出し (~1s)
[B] MemoryProbe.probe()            ← LLM不要 (~100ms)
    ├─ Layer 1.5: Glossary Match — 常時実行 (regex のみ・~ms)
    ├─ Layer 1:   Quick Vector Search — need_intent ∈ {past_fact, relationship} のみ
    └─ Layer 2:   Deep Search — Layer 1 ヒット無し + (need_intent=past_fact または過去参照パターン) 時のみ
  ↓
Gatekeeper.classify() が結果をマージし、最終 need を決定
  ↓
MemoryBlockBuilder.build()  → Brain へのプロンプト構築
  ↓
[C] provider.generate() / .async_generate_stream()    ←──┐
[D] gatekeeper.update_state() — StateUpdater を呼ぶ   ←─┴── ChatService で並列実行 (post-response)
  ↓
session_state.apply_delta() → 次ターンの context に反映
```

StateUpdater は **応答生成と並列**で動かす（buffered なら `asyncio.gather()`、streaming なら `asyncio.create_task()`）ことでクリティカルパスから外している。今ターンの `topic` は session_state の前ターン値を使用する（1 ターン遅延、許容範囲）。

Glossary scan は regex のみ・~ms オーダーなのでゲートを外している。`need_intent` の値に関わらず常時実行することで、固有名詞・別名認識を安定させる（`null` 時も走る）。

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
- **Brain インスタンス** (`brain`): ナレッジ DB に対するベクトル検索用。`None` の場合は vector / deep スキップ
- **Memory manager** (`memory_manager`): 用語集の参照用。`None` の場合は glossary スキップ
- **history_msgs**: Glossary の履歴スキャン（`scan_depth` ターン）用
- **need_intent**: vector / deep を実行するかのゲート（`null` の時は glossary のみ）
- **recent_headlines**: `_check_headline_match` 用（Layer 2 トリガー判定で参照）

---

## 2. Gatekeeper の【出力】（`Gatekeeper.classify()` の返却値）

```python
{
    "tier": "reflex" | "mid",          # ContextClassifier の出力（RAG とは独立）
    "topic": str,                       # state_delta または現在 topic
    "need": str | None,                 # 最終 need (LLM 意図 + 事実裏付けの両方が成立した時のみ設定)
    "need_intent": str | None,          # LLM が出した意図種別: past_fact / glossary / relationship / None
    "classifier_status": "ok" | "fallback",   # LLM 出力を採用できたか（部分 fallback 含む）
    "fallback_reason": str | None,      # provider_error / empty_response / parse_error
                                        # / missing_need_intent / invalid_need_intent / not_configured
    "original_need_intent": str | None, # floor 適用前の need_intent
    "intent_floor_applied": bool,       # null → past_fact の floor が適用されたか
    "search_targets": list[str] | None, # need 有時の上位候補タイトル / glossary 用語
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
        "status": "hit" | "no_hit" | "deep_search" | "skipped",
        "candidates": list[dict],      # probe からの RAG 検索結果
        "glossary_hits": list[dict],   # マッチした用語集エントリ
        "layers": dict,                # Layer 別診断 (glossary / vector / deep)
    }
}
```

### Layer 別診断 (`memory_probe.layers`)

probe は各 Layer の診断を返却し、`debug_info.gatekeeper.memory_probe_layers` に伝播される:

```python
{
  "glossary": {"executed": True, "matches": 2},
  "vector":   {"executed": True, "result_count": 3, "max_score": 0.71, "above_threshold_count": 1, ...},
  "deep":     {"executed": False, "reason": "no deep trigger"},
}
```

そのターンで RAG が発火した／しなかった理由のデバッグに使える。

### tier 判定ルール（ContextClassifier）

ContextClassifier が LLM に 3 スコアを出力させ、Python 側で以下のルールで tier を決定します:

| 条件 | 結果 |
|---|---|
| `response_complexity <= tier_rc_threshold` AND `continuity_need <= tier_cn_threshold` | → `reflex` |
| 上記以外 | → `mid` |

**閾値はデフォルト rc=0.4 / cn=0.3**。`SYSTEM_CONFIG["gatekeeper"]` または `instance_config["gatekeeper"]` で上書き可:

```python
# config.json (instance level) など
{
  "gatekeeper": {
    "tier_rc_threshold": 0.5,  # 緩和すると reflex 範囲が広がる
    "tier_cn_threshold": 0.6
  }
}
```

人/会話スタイルによる感じ方の違い (mid 連発 / reflex 連発) を吸収するために設定化されている。

`need`（RAG 要否）は tier とは独立で、**LLM の意図出力 (need_intent) + MemoryProbe の事実裏付け** の 2 段で決定されます。詳細は次節。

### RAG 判定: 「LLM 意図 + 事実裏付け」の 2 段構え

ContextClassifier は tier に加えて `need_intent` フィールドを出力する:

| need_intent | 意味 | MemoryProbe の挙動 |
|---|---|---|
| `past_fact` | ユーザーが具体的な過去の出来事/決定/会話を参照している | Layer 1 (vector) + 1.5 (glossary) + 条件付き Layer 2 |
| `glossary` | 用語や固有名詞の意味を知りたい | Layer 1.5 のみ実行 (vector skip) |
| `relationship` | 関係性・ムード推移・習慣に関する質問 | Layer 1 + 1.5 + 条件付き Layer 2 |
| `null` | 長期記憶不要 (挨拶・雑談・将来設計など) | **Layer 1.5 (glossary) のみ実行**。vector/deep はスキップ（glossary ヒット有無で status を返す） |

最終 `need` の決定:
- `need_intent == null`            → `need = null` (LLM が「不要」と判断)
- probe が候補も glossary も返さない → `need = null` (事実裏付け失敗 — LLM が誤判定したとみなす)
- それ以外                         → `need = need_intent`

この 2 段構えにより、LLM の意図捕捉と事実裏付けの両方をパスした時のみ RAG ブロックが注入される。reflex tier でも need は有り得る（例: 「前に話したあの曲なんだっけ？」）。

### need_intent のルール安全網（fallback + floor）

`asks_for_specific_past_detail(user_input)`（「前に」「以前」「だっけ」「When did」「いつ〜した」等の明示的過去参照・時点質問パターン）を使う 2 段のルール安全網がある:

1. **fallback（parse 失敗時）**: LLM 出力が 4 値以外・JSON 構造が崩れた場合 — パターンがマッチすれば `past_fact`、なければ `null`（probe スキップ）。不正値の場合は loud な warning ログを出力し、prompt drift / モデル劣化を検知できるようにしている。
2. **floor（LLM が null の場合）**: parse に成功して LLM が `null` を出しても、パターンがマッチすれば `past_fact` に引き上げる。`null` だと vector probe が一切走らず、誤判定がそのまま RAG 不発火に直結するため。probe が候補ゼロなら `need = null` に戻るので、誤検知のコストはローカル vector 検索 1 回に留まる。

これにより「不要 probe 削減」という目的を維持しつつ、明示的な過去参照シグナルがある場合は安全網が働く。どちらの安全網が動いたかは `classifier_status` / `fallback_reason` / `original_need_intent` / `intent_floor_applied`（`Gatekeeper.classify()` 返却値 → `debug_info.gatekeeper` → Trace に伝播）で観測できる。なお Deep Search (Layer 2) は `need_intent=past_fact` なら正規表現パターン不一致でも発火する — パターンは fallback 用の安全網であり、LLM が意味的に拾ったケースを二重ゲートで潰さない（`relationship` の常時 Deep 化は Trace のヒット率・遅延を見て判断予定）。LLM 応答からの JSON 抽出は `core/json_extract.py` の `extract_json_str()`（ContextClassifier / StateUpdater / Stage 3 / keyword 抽出で共用）に統一されており、閉じフェンス欠落（トークン切れ）にも耐える。

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
| 5 | **MID-TERM（条件付）** | mid のみ（下記参照） |
| 6 | **RAG（条件付）** | `need` 有時のみ（tier 非依存。下記参照） |
| 7 | **SESSION DIGEST** | 最新の対話の会話圧縮ログ |
| 8 | **TIER INFO** | 現在の思考モード（reflex/mid） |
| 9 | **WEB SEARCH RESULTS**（条件付） | 非Gemini + use_web_search=True 時のみ。Tavily API 経由のWeb検索結果 |
| 10 | **Short Term** | 直近 6 ターンの会話履歴 |

### 🔵 Tier別に追加される情報

#### 【 Tier 1 】 reflex（脊髄反射）
- **追加情報**: mid-term 軸の追加情報は無し。ただし `need` 有り時は tier に関係なく RAG ブロックも注入される（例: 「前に話したあの曲なんだっけ？」が reflex で MemoryProbe にヒット）。
- 挨拶、相槌、「うんわかった」系の短い返事で発動。知識検索を待たず即座に返す。

#### 【 Tier 2 】 mid（中脳・感情系）
- **追加情報**: `use_summarized_mid_term` 設定により切り替わる:
  - `False`（RAWモード）: **MID-TERM MEMORY** （`mid_term.txt` の全文テキスト）
  - `True`（要約モード）: **MID-TERM DIGEST** + **RELATIONSHIP SNAPSHOT** のセット
    （要約ファイルがない場合は RAW にフォールバック）
- 現在の話題に関する質問や、少し前のやり取りの前提を踏まえる会話で発動。

#### 🟣 RAG ブロック（tier 非依存）
- **LONG-TERM MEMORY (RAG)**: `need` が設定されている場合、tier に関係なく注入される。MemoryProbe の candidates から直接構築（追加の LLM 呼び出しなし）。
- MemoryProbe が `status: "no_hit"` で candidates 無しの場合、`need` は `None` のままで RAG ブロックはスキップされる。

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

---

## 5. ストリーミング応答（SSE）

`POST /chat/stream` エンドポイントで Server-Sent Events 形式の逐次応答を返します。
クライアントの体感レイテンシ改善 (TTFB) を目的に、生成されたトークンを順次配信します。

### イベントフォーマット

| event | 順序 | data |
|---|---|---|
| `metadata` | stream 開始直後 | `{tier, need, need_intent, scores, memory_probe_status, search_targets}` |
| `chunk` | 生成中に複数回 | `{text: str}` — 部分テキスト |
| `done` | 生成完了 | `{full_text, sources, session_state, debug_info}` |
| `error` | 例外時 | `{message, recoverable}` |

### Provider 対応状況

| Provider | ネイティブ stream | 経路 |
|---|---|---|
| Gemini | ✅ | `chat_session.send_message_stream()` |
| OpenAI | ✅ | `chat.completions.create(stream=True)` |
| xAI | ✅ | OpenAI SDK + `base_url=api.x.ai/v1` |
| Ollama | ✅ | OpenAI SDK + `base_url=localhost:11434/v1` |

Provider が stream に対応していない場合、`BaseProvider.async_generate_stream()` のデフォルト fallback で `generate()` の結果を 1 チャンクで yield する形に縮退する。

### 設定

`SYSTEM_CONFIG["chat"]["streaming_enabled"]` (デフォルト `True`) で server-side のデフォルト挙動を制御。UI 側は `streaming_enabled` のセッショントグル（チャットヘッダー ⚡ ボタン）でユーザーごとに切り替え可能。

### 並列化

ストリーム開始と同時に `gatekeeper.update_state()` を `asyncio.create_task` で並列起動し、generate / stream とラップ実行することで StateUpdater のレイテンシを隠蔽している。詳細は処理フロー図参照。
