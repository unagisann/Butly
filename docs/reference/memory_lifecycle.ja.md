# Butly 記憶ライフサイクル仕様書

🌐 **日本語** | [English](memory_lifecycle.md)

Butly の記憶システムは、会話の鮮度と重要度に応じて複数の層に分かれています。
本文書では、各層の役割・保存タイミング・昇格フロー・オーバーフロー処理・設定パラメータを体系的に説明します。

---

## 全体フロー概要

```
チャット中（毎ターン）
  ↓
[1] short_term_json/  ← 生ターンログ (JSON)
  ↓ short_term_limit 超過時
[2] session_digests/  ← 会話単位の会話圧縮ログ (txt)
  ↓
[3] memory_archive/1_integrated/  ← 中間待機ゾーン

──── Sleeptime 実行時 ────────────────────────────────

[Stage 0] short_term_json/* → 1_integrated/ へ全移動

[Stage 1] 1_integrated JSONs を읽 → テキスト整形
  ├─ [4] mid_term.txt ← RAWログ追記
  ├─ [5] mid_term_digest.txt ← LLMによる日次事実ダイジェスト（差分追記）  ├─ [5b] recent_digest_headlines.json ← LLMによるヘッドライン抽出（最大 4 件）  └─ [6] mid_term_relationship.txt ← LLMによる関係性スナップショット（7日ごと全上書き）

[Stage 2] 1_integrated JSONs → 日付グループ → LLMナレッジ抽出
  └─ [7] butly_memory.db ← 長期記憶ベクトルDB (RAG)
          ↓ 処理後
         memory_archive/2_knowledgeized/{date}/
```

---

## 各記憶層の詳細

### 1. Short-Term JSON（短期記憶ログ）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/short_term_json/session_YYYYMMDD_HHMMSS_ffffff[_NNN].json` |
| **書き込み** | 毎ターン返答後、`memory.save_single_turn()` が自動実行 |
| **形式** | `{"timestamp": "...", "messages": [{"role": "user", "parts": [...]}, {"role": "model", "parts": [...]}]}` |
| **日時指定** | 通常チャットは現在日時。履歴インポートでは `save_single_turn(..., created_at=...)` で元日時をファイル名と `timestamp` の両方へ反映。同一日時が重複した場合は `_001` からの連番を付け、既存ファイルを上書きしない |
| **話者帰属 meta** | 外部入口（Discord / LINE 等）のターンは user メッセージに `meta` を付与: `{"person_id": "...", "display_name": "...", "lane": "direct", "source": "discord", "channel_key": "guild:channel"}`。**meta 欠落時は owner / direct / web と解釈**（後方互換、マイグレーション不要） |
| **上限** | `short_term_limit`（デフォルト 6 ファイル） |
| **オーバーフロー処理** | `memory.maintain_memory()` が溢れた古いファイルをLLMで要約し、session_digests/ に保存後 1_integrated/ へ移動 |
| **Gatekeeper注入** | Short Term ブロック（直近 6 ターン）として常時注入 |

**設定パラメータ:**
```python
SYSTEM_CONFIG["memory"]["short_term_limit"] = 6  # 保持ファイル数
```

> **話者帰属（person_id）:** 外部アカウントは `ButlyRuntime` が `PersonRegistry`
> （`DATA_DIR/persons.json`、`butly_core/external/person_registry.py`）で person_id に解決する。
> 未登録ユーザーには決定的な仮 ID `p_{source}_{hash}` を発行（外部 ID は RAW ログに直接出さない）。
> Sleeptime / maintain_memory / raw_memory_cache の整形時、バッチ内に複数話者が
> いる場合のみ user 発言を `「display_name」:` でラベリングする（1:1 は従来どおり）。
> 詳細は `docs/planning/active/group_context_lanes_plan.ja.md` Phase 1 を参照。

---

### 2. Session Digests（会話圧縮ログ）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/session_digests/*.txt`（単一ファイル互換: `session_digest.txt`。旧 `floating_*` パスも読み取り可能） |
| **書き込み** | `memory.maintain_memory()` が short_term 溢れ時に `brain.summarize_conversation()` を呼び出し生成 |
| **形式** | 1 会話 = 1 ファイル（本文は要約のみ）。旧形式（先頭行 `Time: {timestamp}`）は読み込み時に除去 |
| **読み出し形式** | `ButlyMemory.get_session_digest()` が **相対時刻ヘッダー**（例: `--- 約30分前 ---`）付きで結合する。ファイル名や絶対タイムスタンプは LLM が「別会話」と誤認しないよう意図的に出さない |
| **Gatekeeper注入** | SESSION DIGEST ブロックとして全 tier に注入（最新の会話文脈として機能） |
| **ライフサイクル** | Sleeptime 実行時に全ファイルを削除（1_integrated の生JSONが存在するため二重書き込みにならない） |
| **使用モデル** | `AI_CONFIG["summary"]["model_name"]`（低コスト・長文コンテキスト向け） |

> **設計意図:** session_digest は、recent sessions から押し出された会話を圧縮し、直近履歴だけでは足りない流れを補うためのコンテキストです。
> 恒久化は mid_term.txt と butly_memory.db が担います。

---

### 3. Memory Archive / 1_integrated（一時中間ゾーン）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/memory_archive/1_integrated/` |
| **書き込み** | ① `maintain_memory()` がオーバーフロー時に移動 ② Sleeptime Stage 0 が short_term_json/* を全移動 |
| **内容** | short_term_json と同じ生 JSON ファイル |
| **役割** | Stage 1（mid_term 更新）と Stage 2（ナレッジ化）の両方がここから読む |
| **後処理** | Stage 2 完了後、`2_knowledgeized/{date}/` へ移動 |

---

### 4. mid_term.txt（中期記憶 RAW ログ）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/mid_term.txt` |
| **書き込み** | Sleeptime Stage 1 (`stage_1_cleanup`) が 1_integrated JSON をテキスト整形して追記 |
| **上限** | `max_mid_term_chars`（デフォルト 30,000 文字） |
| **オーバーフロー** | 先頭から溢れた分を `memory_archive/3_log/archive_long_term.txt` に追記後、最新部分のみ残す |
| **Gatekeeper注入** | `use_summarized_mid_term = False`（RAWモード）の場合に mid tier へ MID-TERM MEMORY ブロックとして注入 |
| **形式** | `[YYYY-MM-DD HH:MM:SS] {role_label}: {text}` の行形式テキスト |

**設定パラメータ:**
```python
SYSTEM_CONFIG["memory"]["max_mid_term_chars"] = 30000
SYSTEM_CONFIG["memory"]["use_summarized_mid_term"] = True  # True で要約注入、False で RAW注入
```

---

### 5. mid_term_digest.txt（事実ダイジェスト）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/mid_term_digest.txt` |
| **書き込み** | Sleeptime Stage 1 内 `_generate_daily_digest()` — 当日の生ログを LLM で事実抽出し差分追記 |
| **入力** | 当日の生ログテキスト（`new_text`）のみ。要約の要約は絶対にしない |
| **入力チャンク分割** | `digest_max_input_chars` が設定されている場合、日付ヘッダ `[YYYY-MM-DD ...]` を区切りにチャンク分割し、各チャンクごとにLLMへ送信→結果を結合 |
| **上限** | `max_digest_chars`（デフォルト 8,000 文字） |
| **オーバーフロー** | `memory_archive/3_log/archive_digest.txt` に追記後、最新部分のみ残す |
| **Gatekeeper注入** | `use_summarized_mid_term = True`（要約モード）の場合に mid tier へ MID-TERM DIGEST ブロックとして注入 |
| **スキップ条件** | `new_text` が 200 文字未満、または `generate_mid_term_summaries = False` |
| **使用モデル** | `AI_CONFIG["summary"]["model_name"]`（Flash Lite 系） |

**設定パラメータ:**
```python
SYSTEM_CONFIG["memory"]["generate_mid_term_summaries"] = True
SYSTEM_CONFIG["memory"]["max_digest_chars"] = 8000
# config.json > sleeptime セクション:
digest_max_input_chars = 0   # 1回あたりの最大入力文字数。0=無制限
```

---

### 5b. recent_digest_headlines.json（最近のヘッドライン）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/recent_digest_headlines.json` |
| **書き込み** | Sleeptime Stage 1 内 `_generate_recent_headlines()` — ダイジェストから LLM で最大 4 件のヘッドラインを抽出 |
| **入力** | `mid_term_digest.txt` の末尾（最大 10,000 文字） |
| **形式** | `{"type": "topic" or "event", "headline": "20〜40文字の要約"}` の JSON 配列 |
| **使用先** | `Gatekeeper.__init__()` がヘッドラインを読み込み ContextClassifier に渡しスコアリングに使用 |
| **ライフサイクル** | Sleeptime 実行のたびに上書き |
| **使用モデル** | `AI_CONFIG["summary"]["model_name"]`（Flash Lite 系） |

---

### 6. mid_term_relationship.txt（関係性スナップショット）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/mid_term_relationship.txt` |
| **書き込み** | Sleeptime Stage 1 内 `_update_relationship_if_due()` — 7 日間隔で全上書き |
| **入力** | `mid_term_digest.txt`（蓄積された事実ダイジェスト）— 日々の断片は使わない |
| **更新頻度** | `relationship_update_interval_days`（デフォルト 7 日）以上経過した場合のみ更新 |
| **Gatekeeper注入** | `use_summarized_mid_term = True` の場合に mid tier へ RELATIONSHIP SNAPSHOT ブロックとして注入 |
| **スキップ条件** | `mid_term_digest.txt` が 200 文字未満、またはインターバル未達 |
| **使用モデル** | `AI_CONFIG["knowledge"]["model_name"]`（高推論 Pro 系） |
| **設計意図** | 関係性は緩やかに変化するため毎日書き換えると不安定になる。週次程度が適切 |

**設定パラメータ:**
```python
SYSTEM_CONFIG["memory"]["relationship_update_interval_days"] = 7
```

---

### 7. butly_memory.db（長期記憶ベクトルDB）

| 項目 | 内容 |
|---|---|
| **場所** | `instances/{name}/butly_memory.db` |
| **書き込み** | Sleeptime Stage 2 (`stage_2_knowledgeize`) — 日付グループ単位でLLMがナレッジカードを抽出しINSERT |
| **入力** | 1_integrated の生 JSON を日付ごとにまとめたテキスト |
| **入力チャンク分割** | `knowledge_max_input_chars` が設定されている場合、JSONファイル単位で分割。「次のファイルを追加すると上限超過→ここまでで1チャンク」として処理 |
| **カード粒度** | 1枚につき1つの主要な記憶単位（出来事・決定・状態変化・継続状態・関係性の進展）。同じ出来事の補足事実は同居できるが、独立した質問への答えになる別イベント・別時点・別の根拠ファイル群は分割する。ファイル境界は保存上の区切りであり、1ファイル=1カードにはしない |
| **スキップ機能** | `skip_knowledge_generation = true` の場合、Stage 2 を完全にスキップ。RAWデータは 1_integrated に保持され、後日高性能モデルで一括処理可能 |
| **スキーマ** | `knowledge_cards` テーブル（下記参照） |
| **Embedding** | `title + tags + summary` を `AI_CONFIG["embedding"]["model_name"]` で埋め込み → BLOB保存。埋め込み前に **embedding プロファイル**の文書側 prefix を付与する（下記） |
| **検索** | `ButlyBrain.search_memories()` がクエリ埋め込みとコサイン類似度でリランキング。クエリ側には**クエリ用 prefix** を付与する |
| **Gatekeeper注入** | `need` が設定された時のみ（tier 非依存）、`MemoryProbe` の candidates から RAG ブロックを構築し LONG-TERM MEMORY として注入。注入ソースは `memory.rag_source_mode` で制御: `"cards"`（既定・カードのみ）/ `"raw"`（当時の会話原文のみ）/ `"both"`（カード + 原文）。raw/both では各カードの `source_files` から RAW 会話 JSON を遅延逆引きし、原文抜粋を合計 `memory.rag_raw_max_chars` 文字（既定 2500、0=無制限、超過ファイルは greedy skip）まで注入する（parent-document retrieval — カード=検索インデックス、事実の根拠=原文）。原文を展開するカード数は `memory.rag_raw_top_k`（既定 1＝最上位カードの原文のみ、残りはサマリ。0/負値で全カード）で絞る。解決不能時はカード注入にフォールバック |
| **後処理** | 処理済み JSON は `memory_archive/2_knowledgeized/{date}/` へ移動 |
| **バックアップ** | `butly_core/db_backups/` にローテーション保存（世代数: `backup.generations`） |

**knowledge_cards テーブルの主なカラム:**

| カラム | 型 | 内容 |
|---|---|---|
| `id` | TEXT | `{db_type}_{YYYYMMDD}_{連番}` 形式の一意ID |
| `type` | TEXT | インスタンス名（db_type） |
| `category` | TEXT | LLMが付与したカテゴリ |
| `title` | TEXT | 出来事のタイトル |
| `tags` | TEXT | 検索用タグ（カンマ区切り） |
| `summary` | TEXT | 事実の要約。箇条書き・複数行可。原文の5W1Hを保持し、相対時間表現は会話タイムスタンプ基準の絶対日付へ変換して記録 |
| `episode` | TEXT | エピソード詳細（AIの所感。簡潔なら複数文可） |
| `ai_importance` | REAL | AIにとっての重要度 (0-1) |
| `humanity_importance` | REAL | 人類にとっての重要度 (0-1) |
| `embedding_blob` | BLOB | float32 バイト列（コサイン類似度検索用） |
| `source_date` | TEXT | 元会話の日付 (YYYY-MM-DD)。検索の time decay はこの「出来事の古さ」を基準に計算（無い旧カードは `created_at` にフォールバック） |
| `source_files` | TEXT | そのカードの主要な記憶単位を直接支える RAW ファイル名の JSON 配列。`memory_archive/2_knowledgeized/{date}/` 配下の元会話へ遡及するためのポインタで、`rag_source_mode` が raw/both のとき RAG 原文注入の逆引きに使う。Stage 2 が抽出モデルにカードごとの根拠ファイルを申告させ、チャンク内に実在する名前だけを採用する（幻覚・特定不能時はチャンク全ファイルへフォールバック）。カード単位に絞れるほど RAG の原文注入量が小さくなる |
| `content_hash` | TEXT | Stage 3 prompt に渡す意味内容（title/summary/episode/tags/category/source_date）を正規化した SHA-256。カード本文の**版識別子**。本文を書く経路（Stage 2 INSERT / `update_card` / `register_knowledge`）は共通 helper（`butly_core/core/card_content.py`）で必ず更新する |
| `last_matured_content_hash` | TEXT | Stage 3 が最後に**成功レビュー**した版。NULL または `content_hash` と不一致ならレビューキュー内 |
| `maturation_queued_at` | TEXT | 現在の版がキューへ入った固定長 UTC 時刻（`YYYY-MM-DDTHH:MM:SSZ`）。FIFO 選択の順序キー |
| `last_matured_at` / `last_matured_run_id` | TEXT | 最終成功時刻と run id（監査専用。再レビュー要否の判定には使わない） |

---

### 8. memory_nodes（Stage 3 / Knowledge Maturation・opt-in）

Stage 1/2 がエピソードを「溜める」層なのに対し、Stage 3 はカード群から
**現在解釈（memory_nodes）を蒸留**する層。既定 OFF
（`memory.knowledge_maturation_enabled=False` かつ
`sleeptime.update_targets.knowledge_maturation=False`）。

| 項目 | 内容 |
|---|---|
| **テーブル** | `memory_nodes`（kind/subject/topic/statement/confidence/status/last_decay_at）、`memory_node_sources`（node↔card の supports/contradicts/context リンク）、`memory_maturation_runs`（実行ログ）、`memory_maturation_run_cards`（run に投入したカード版と結果） |
| **レビューキュー** | content hash 式。非アーカイブかつ `last_matured_content_hash` が NULL または `content_hash` と不一致のカードが対象。`maturation_queued_at` 昇順の FIFO で全カード被覆を保証（usage は同時刻内の tie-break のみ）。本文変更は新 hash として自動で再キューされる |
| **実行フロー** | instance 単位 process lock（non-blocking flock）→ 前 process が残した `running` run を `abandoned` 回収 → preflight（NULL hash の自己修復 backfill。不能なら run 失敗）→ batch 選択 → LLM（`stage3_node_review` prompt）→ 結果分類 → **単一 SQLite transaction** で node/source 更新・run counters・カード版 stamp・run 完了を commit |
| **結果分類** | `ok` / `no_changes`（正当な空結果。stamp する）/ `truncated_response`（provider の finish_reason）/ `empty_response` / `parse_error` / `provider_error`（stamp せずキューに残す）。retryable 失敗は有限 retry → batch 半分割 → 1 件隔離。追加 LLM 呼び出しは `knowledge_maturation_retry_max_calls_per_run` で制限 |
| **同時更新防御** | transaction 開始時に全カードの `content_hash` を再検証。1 件でも変わっていれば batch 全体を適用せず `changed_during_run`。新版はキューに残る |
| **bootstrap** | `venv/bin/python sleeptime.py stage3-bootstrap --instance <name> [--max-cards N]`。キューが空になるまで batch を反復（安全上限 `knowledge_maturation_bootstrap_max_cards`=2000）。失敗カードは invocation 内だけ隔離し、`partial` として失敗一覧・残数を報告。transaction 単位で冪等・再開可能 |
| **reflection（減衰）** | `memory_node_decay_enabled=True` のとき run 末に SQL スイープ。`last_reinforced_at`/`last_decay_at` 基準の未適用 stale 期間数（`memory_node_stale_days`=30 単位）× `memory_node_decay_per_period`=0.05 を減点。同一期間の再 run で二重減点しない。active が `memory_node_active_threshold` 割れで uncertain 降格、uncertain の 2 期間以上放置は `metadata.stale=true`（削除しない） |
| **昇格提案** | active ∧ confidence≥`memory_node_promotion_threshold` ∧ supports≥`memory_node_promotion_min_sources` ∧ 複数日（`source_date` 優先）の node を `memory_node_proposals.json` へ出力（全 eligible node を pagination）。Key Memory への自動反映は未実装（計画 Phase 6） |
| **Chat/QA 注入** | `knowledge_maturation_enabled=True` のとき、RAG でヒットしたカードに紐づく `status='active'` node を最大 5 件併走注入（カード非ヒット時は node も見えない）。Traceと`debug_info.rag.active_nodes`にはlookup理由、候補、紐づくカードID、最終prompt内の実在判定を保存する |
| **設定キー** | `knowledge_maturation_batch_size`=40 / `_max_batches_per_run`=1 / `_prompt_max_chars`=40000 / `_retry_max_calls_per_run`=8 / `_bootstrap_max_cards`=2000。旧 `max_cards` はインスタンス設定に残っていれば batch_size として読み替え。旧 `window_days`/`min_usage_count`/`interval_days` は廃止 |

---

## Sleeptime の実行フロー詳細

Sleeptime は手動トリガーまたはスケジュール実行され、全インスタンスに対して以下の処理を順次実行します。
通常運用では従来どおりプロジェクトルートを使用する。隔離実行では
`ButlySleeptime(base_dir=..., instances_dir=...)` を指定でき、Stage 1〜3、
DBバックアップ、人物登場集計の読み書き先が注入したパスへ統一される。

```
ButlySleeptime.run()
  ↓
  process_instance(instance_path) ← 各インスタンスに対して
    ├── stage_1_cleanup(instance_path)
    │     ├── [Stage 0] short_term_json/* → 1_integrated/ へ全移動
    │     ├── [Step 1] 1_integrated JSON をテキスト整形
    │     ├── [Step 2] session_digests/* を削除（一時コンテキストのクリア）
    │     ├── [Step 3] mid_term.txt に追記（オーバーフロー時は 3_log へアーカイブ）
    │     ├── [Step 4] _generate_daily_digest() → mid_term_digest.txt 差分追記
    │     │          └── ★ digest_max_input_chars 超過時は日付ヘッダ区切りでチャンク分割
    │     ├── [Step 5] _generate_recent_headlines() → recent_digest_headlines.json（最大 4 見出し）
    │     └── [Step 6] _update_relationship_if_due() → mid_term_relationship.txt（7日間隔）
    │
    ├── stage_2_knowledgeize(instance_path, db_type)
    │     ├── ★ skip_knowledge_generation チェック（true ならスキップ）
    │     ├── 1_integrated JSON を日付でグループ化
    │     ├── ファイル単位でチャンク分割（knowledge_max_input_chars で制御）
    │     ├── チャンクごとに ask_gemini_to_summarize() → knowledge_cards 生成
    │     ├── 各カードに embedding + content_hash 生成 → butly_memory.db に INSERT
    │     └── 処理済み JSON → 2_knowledgeized/{date}/ へ移動
    │
    └── stage_3_mature_knowledge(instance_path)  ← opt-in（§8 参照。既定 OFF）
          ├── process lock → abandoned 回収 → preflight backfill
          ├── FIFO batch 選択 → LLM レビュー → 結果分類
          ├── 単一 transaction で node/source/版 stamp/run 完了を commit
          ├── （opt-in）staleness 減衰スイープ
          └── memory_node_proposals.json 出力
```

---

## チャット中のリアルタイム処理

Sleeptime とは独立して、チャット中にも以下の処理が行われます。

```
1. ユーザー発言受信
2. ChatService → Gatekeeper.classify()（tier 判定）
3. MemoryBlockBuilder.build()（メモリブロック構築）
       ↓ 各ブロックのソース
       SYSTEM INSTRUCTION   ← system_instruction.txt
       KEY MEMORY           ← Key_Memory.txt
       MID-TERM DIGEST      ← mid_term_digest.txt (要約モード)
       MID-TERM MEMORY      ← mid_term.txt (RAWモード)
       CURRENT TIME         ← システム時刻
       LONG-TERM (RAG)      ← butly_memory.db (need 有時のみ・tier 非依存)
       SESSION DIGEST     ← session_digests/*.txt
       TIER INFO            ← tier 文字列
       SHORT TERM           ← short_term_json/*.json (直近 6 ターン)
4. Brain.generate() で応答生成
5. memory.save_single_turn() → short_term_json/ に保存
6. memory.maintain_memory() → short_term_limit 超過チェック
       超過していれば: 古いファイルをLLMで要約 → session_digests/ + 1_integrated/ へ
```

---

## アーカイブ構造まとめ

```
instances/{name}/
├── short_term_json/           # ① アクティブな生ターンログ
├── session_digests/        # ② 短期溢れ時の会話圧縮ログ（一時）
├── session_digest.txt       # ② 旧方式（互換性維持）
├── mid_term.txt               # ④ 中期記憶 RAW（最新 30,000 文字）
├── mid_term_digest.txt        # ⑤ 事実ダイジェスト（最新 8,000 文字）
├── mid_term_relationship.txt  # ⑥ 関係性スナップショット（7日ごと更新）
├── Key_Memory.txt             # 不変の根幹記憶（手動編集）
├── system_instruction.txt     # AI 人格定義（手動編集）
├── session_state.json         # Gatekeeper セッション状態
├── recent_digest_headlines.json  # 最近の会話ヘッドライン（Gatekeeper 入力）
├── glossary.yaml              # Glossary / Lorebook（term / aliases / category / status / priority）
├── debug_logs/                # ChatService の debug 自動保存
│   ├── latest.json            # 最新ターン（上書き）
│   └── history/               # 直近 20 ターン（ローテーション）
├── butly_memory.db            # ⑦ 長期記憶ベクトルDB
└── memory_archive/
    ├── 1_integrated/          # ③ Sleeptime 処理待ち生 JSON
    ├── 2_knowledgeized/       # ナレッジ化済み JSON（日付フォルダ）
    │   └── {YYYY-MM-DD}/
    └── 3_log/
        ├── archive_long_term.txt  # mid_term.txt のオーバーフロー
        └── archive_digest.txt     # mid_term_digest.txt のオーバーフロー
```

---

## 使用モデル一覧

| 処理 | モデル設定キー | 用途 |
|---|---|---|
| チャット応答生成 | `AI_CONFIG["chat"]["model_name"]` | Brain の主応答 |
| 会話要約 (session_digest) | `AI_CONFIG["summary"]["model_name"]` | 短期溢れ時の会話圧縮ログ |
| 事実ダイジェスト生成 | `AI_CONFIG["summary"]["model_name"]` | mid_term_digest 生成 |
| 関係性スナップショット | `AI_CONFIG["knowledge"]["model_name"]` | mid_term_relationship 生成 |
| ヘッドライン抽出 | `AI_CONFIG["summary"]["model_name"]` | recent_digest_headlines.json 生成 |
| ナレッジカード抽出 | `AI_CONFIG["knowledge"]["model_name"]` | Stage 2 RAG DB への抽出 |
| 埋め込みベクトル生成 | `AI_CONFIG["embedding"]["model_name"]` | knowledge_cards.embedding_blob |

---

## Embedding プロファイル（モデル別の入力規約）

検索用 embedding モデルの多くは、クエリ側と文書側で別の prefix を要求する。付け忘れると
埋め込みが 1 つの円錐に潰れ、cosine の識別力が失われる（実測: prefix 無しの nomic では
**カード同士の cosine 平均 0.756 > 質問と正解カードの cosine 平均 0.733** ＝無関係なカード
同士のほうが近く、ランキングが機能しない）。

`butly_core/llm/embedding_profiles.py` がモデル名から規約を引く。

| プロファイル | クエリ側 | 文書側 | 対象モデル |
|---|---|---|---|
| `nomic` | `search_query: ` | `search_document: ` | nomic-embed-text v1/v1.5 |
| `e5` | `query: ` | `passage: ` | multilingual-e5-* 等 E5 系 |
| `bge-instruct` | instruction 文 | なし | bge-large/base/small-en |
| `qwen3-embedding` | instruction 文 | なし | Qwen3-Embedding |
| `bge-m3` / `gemini` / `openai` / `mxbai` | なし | なし | prefix 不要のモデル |
| `plain` | なし | なし | 規約不明のモデル（既定） |

**解決順序**（`resolve_profile`）:

1. `embedding.query_prefix` / `embedding.document_prefix` の明示指定（未知モデル向けの脱出ハッチ）
2. `embedding.profile` の明示指定（プロファイル ID。`"plain"` で無効化）
3. `embedding.profile` 未指定 or `"auto"` → `model_name` から推定
4. どれにも当たらなければ `plain`

設定例（instance config / eval profile どちらでも可）:

```json
"embedding": {
  "connection": "local_embedding",
  "model_name": "nomic-embed-text",
  "profile": "auto"
}
```

**モデル差し替え時の保護**: 埋め込みを書いた側は `embedding_meta` テーブル（instance DB 内、1行）に
`model_name` / `profile` / `dim` を記録する。起動時チェック（`embedding_check.log_startup_check`）が
現在の設定と突き合わせ、食い違えば警告する。**次元が同じでも規約が変われば別空間**になる
（例: nomic を prefix 無しから有りへ）ため、次元だけでなくプロファイルも比較する。
差し替えたら `python migrate_embeddings.py --all` で再生成する。
| Tier 判定 | `AI_CONFIG["gatekeeper"]["model_name"]` | ContextClassifier 3スコア出力 |
