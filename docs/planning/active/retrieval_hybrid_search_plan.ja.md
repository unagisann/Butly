# 検索改修計画（ハイブリッド検索 / RRF / 近傍展開）

対象: ナレッジカード検索（`ButlyBrain` の Layer 1 / Layer 2 と `MemoryProbe`）
状態: Phase 1 完了。**hybrid は不採用（既定 `vector`）／Phase 1B（常時検索 +
`injection_policy=candidates`）は v27 で採用**（cat1-4 +0.029・cat5 悪化なし。§8 参照）。
残るは既定へ昇格させる範囲の判断と Phase 2 以降
最終更新: 2026-07-28

---

## 0. 背景 — なぜ検索なのか

LoCoMo v26（199問・NanoGPT Qwen3-14B）の実測:

| 条件 | 問数 | スコア |
|---|---:|---:|
| evidence = 1.0（正解の根拠チャンクを取得できた） | 78 (51%) | **0.540** |
| evidence = 0（根拠に届かなかった） | 51 (34%) | **0.186** |

**根拠に届けば 0.54、届かなければ 0.19。** 読み手モデルやプロンプトを触るより、
この 34% を削るほうが効く。全体スコア（cat5 を除く152問）は 0.380 で、
仮に ev=0 の 51 問が ev=1.0 相当まで改善すれば +0.12 前後の余地がある。

### 直前に潰した2つ（この計画の前提）

1. **embedding の task prefix 未適用**（9fa74c8 で修正済み）
   実測で空間の形が直った:

   | | prefix 無し (v22) | prefix 有り (v25) |
   |---|---:|---:|
   | カード同士の cosine | 0.756 | **0.690** |
   | 質問と最良カードの cosine | 0.733 | **0.756** |
   | 差（正なら正常） | **-0.023** | **+0.066** |

2. **Gatekeeper の出力上限 512 で分類器が崩壊**（v25→v26 で 2048 に修正）
   RAG 発火 0.332 → 0.905。

### それでも残る問題

**top1 と top5 の cosine 差は 0.062 → 0.074 とほぼ変わらない。**
107枚のカードが 0.07 の幅に団子で並んでおり、ベクトル単独では正解を上位に
持ち上げきれない。実際 v26 でも:

- `What pets does Melanie have?` → 正解「Two cats and a dog」に届かず 0.00
- `What does Caroline's necklace symbolize?` → 0.00
- `What types of pottery have Melanie and her kids made?` → 0.00

いずれも**固有の語（pets / necklace / pottery）が明確にあるのに沈む**。
ベクトルが最も苦手で、字面一致が最も得意な領域。

### カード存在性から見た改善余地

v26 の provenance（`knowledge_cards.source_files`）で、全カードを取得できた場合の
oracle coverage を確認した:

| 対象 | 完全にカード内 | 一部カード内 | カードに無い |
|---|---:|---:|---:|
| evidence 付き cat1-4（150問） | 124 | 15 | 11 |
| 現在 evidence = 0 の51問 | 34 | 6 | 11 |

現在 evidence = 0 の51問中、**40問は検索改善の対象**で、11問はカード検索だけでは
救えない。また、RAG非発火のcat1-4は14問あり、evidence付き12問の内訳は
完全11・部分1だった。したがって「全質問で検索すること」と「検索結果を注入すること」を
分離し、検索結果を観測した上で注入判定を改善する価値がある。

---

## 1. 目標

| 指標 | 現在 (v26) | Phase 1 目標 | 最終目標（Phase 1-3 合算） |
|---|---:|---:|---:|
| evidence = 0 の割合（cat1-4 の152問） | 34% (51問) | **26% 以下**（40問以下）→ v27 実測 **28%（43問）** | **20% 以下** |
| evidence 平均（**メモリ注入された180問**、cat5込み） | 0.594 | **0.66** | **0.70** |
| evidence 平均（cat1-4 のうち注入された138問） | 0.638 | **0.70** | — |
| overall（cat5 除く152問） | 0.380 | **0.41** → v27 実測 **0.398**（treatment のみなら 0.409） | **0.45** |
| プロンプトトークン（QA、平均） | 1769 | 原則維持（許容上限 **+3%**） | 同左 |
| 検索実行率（`search_execution_rate`） | 0.905 | **1.0** | 1.0 |

**目標値の上限（重要）**: §0 のカード存在性分析より、現在 ev=0 の51問のうち検索で
救えるのは **40問**（残り11問はカードに事実自体が無く Phase 5 待ち）。救えた問が
ev=1.0 群と同じ 0.540 まで上がると仮定した上限は

```
40 × (0.540 - 0.186) / 152 = +0.093  →  overall 0.473
```

つまり **overall 0.45 は「救える40問の約85%を実際に救う」ことを要求する**。
Phase 1 単独の合格ラインとしては上振れなので、Phase 1 は 0.42（=救済21問相当）を
目標とし、0.45 は Phase 1 + Phase 2（episode 埋め込み）+ Phase 3（近傍展開）の
合算目標として扱う。ev=0 割合も同様に Phase 1 は 26%、20% は合算目標。

**evidence の分母に注意**: v26 の 0.594 は「メモリが注入された180問（cat5 を含む）」の
平均。cat1-4 だけに絞ると 0.638、全197問（evidence 定義のある問）だと 0.543。
どの分母かを書かずに比較しない（v25 の誤読の再発防止）。

**非目標**: ISO 日付・冗長回答の是正（採点形式に寄せる改修はしない方針。
[no-benchmark-format-tuning] の判断どおり保留）。リランカーの自前学習。

検索実行率とメモリ注入率は別指標として扱う。全質問を検索しても、無関係な候補を
プロンプトへ常時注入することは目標にしない。

---

## 2. 現状のコード

| 経路 | 実装 | 挙動 |
|---|---|---|
| Layer 1（通常） | `brain.quick_vector_search_diag` | 全カードを取得し、質問文の embedding と cosine。`time_decay` と archive 係数(×0.5)を掛け、MemoryProbe経路では**固定閾値 0.4**を超えたものから上位 `vector_search_limit`(既定3)。関数単体の既定値0.6は実経路では上書きされる |
| Layer 2（deep） | `brain.search_knowledge` → `_search_single_db` | LLM がキーワード抽出 → `title/summary LIKE '%kw%'` で候補を絞る → ヒットが `keyword_hit_threshold`(5) 未満なら**直近100件を無条件で混ぜる** → cosine で並べ替え |
| 呼び出し元 | `MemoryProbe` | `need_intent` が `past_fact` / `relationship` の時だけLayer 1を実行し、Layer 1が空なら条件付きでLayer 2 |

問題点:

- **字面一致のスコアが無い**。`LIKE` は絞り込みのみで、順位付けは cosine 任せ
- **固定閾値 0.4 はモデル依存**。embedding を替えるたびに意味が変わる
- **`fallback_fetch_limit` の「直近100件」は時系列バイアス**。古い正解が入らない
  （コード内の print は「直近50件」と書いてあるが実挙動は `fallback_fetch_limit`=100。
  ログ文言も直す）
- **分類器が検索前のゲートになっている**。`need_intent=null` の誤判定時は、
  関連カードが存在しても検索されない
- カードの埋め込みは `Title + Tags + Summary` のみで **`episode`（所感）が入っていない**
- **件数設定が2セクションに分かれている**。Layer 1 の出力件数は
  `memory_probe.vector_search_limit`(3)、Layer 2 は `brain.search_limit`(3)。
  たまたま同値なので今は表面化しないが、ハイブリッド化で「どちらが効くのか」が
  曖昧になる。§3.5 で経路ごとに固定する

---

## 3. Phase 1 — ハイブリッド検索（BM25 + ベクトル、RRF 融合）★本命

### 3.1 索引

`knowledge_cards` と同じ instance DB に FTS5 の独立テーブルを持つ。

```sql
CREATE VIRTUAL TABLE knowledge_cards_fts USING fts5(
    card_id UNINDEXED,
    title, tags, summary, episode,
    tokenize='trigram'
);
```

- **trigram tokenizer を使う理由**: 形態素解析器なしで日本語が引ける。英語もそのまま
  通る。この環境（SQLite 3.46.1）で日英とも動作確認済み。追加依存ゼロ
  （[SQLite FTS5 trigram仕様](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)）
- **external content（`content='knowledge_cards'`）は使わない**。`knowledge_cards` の
  主キーは TEXT で、external content が要求する INTEGER rowid と結合させると
  再構築時の事故が読みにくい。実体を持たせる（100枚で数百KB、無視できる）
- **同期はトリガで行う**。`INSERT` / `UPDATE` / `DELETE` の3本を
  `knowledge_cards` に張り、書き手（Sleeptime / Stage 3 / migrate）を問わず整合する。
  `UPDATE` は `id/title/tags/summary/episode` の変更時だけ再索引し、embeddingだけの
  migrationでは動かさない
- **既存 DB の backfill**: FTS schema versionと本体/FTSの件数を確認し、不一致または
  version更新時だけ単一トランザクションで再構築する。「FTSが空」の確認だけでは
  部分欠損やtokenizer変更を検出できない
- **version の置き場所**: instance DB 内に `fts_meta(id=1, schema_version, tokenizer,
  card_count, updated_at)` を持つ。`PRAGMA user_version` は他の migration と衝突
  しやすく、tokenizer 名を持てないので使わない。DB クローン（`rerun-qa`）で
  そのまま付いてくる位置に置くのは `embedding_meta` と同じ理由
- **検索経路からの遅延生成**: 索引は `ButlyDatabase._initialize_db` で作るが、
  hybrid を有効化した直後の既存 DB にはまだ無いことがある。検索側は
  `fts_index_ready()` が偽なら一度だけ `ensure_fts_index()` を呼ぶ。対象は
  自分が検索している DB だけなので、A/B の複製元は触らない（R7）
- FTS5/trigramが利用できないPython/SQLiteでは初期化を失敗させず、警告と診断を残して
  `vector`へフォールバックする

### 3.2 クエリ

```
候補A: ベクトル  … 質問文の embedding で cosine 上位 N（vector_candidates 既定20）
候補B: BM25      … 正規化した検索語を FTS5 MATCH → 語境界検証 → df ゲート
                   → bm25() 上位 N（bm25_candidates 既定20）
融合  : RRF      … score = Σ 1/(k + rank_i)   k = 60（標準値）
出力  : 経路ごとの出力上限（§3.5）
```

- **RRF を選ぶ理由**: 順位しか使わないのでスコアのスケールに依存しない。
  BM25とcosineの値を直接正規化する必要がない。ただしベクトル候補の固定閾値を
  残す初期実装では、embeddingモデル依存が完全に消えるわけではない
- **英語・欧文**: NFKC正規化と小文字化後、英数字の語を抽出する。3文字未満と
  stopwordを落とし、各語を完全にquoteしてOR連結する
- **日本語・CJK**: 句読点・空白で分割後、連続文字列から3文字shingleを作って
  OR連結する。最大32語に制限する
- **MATCH式**: ユーザー入力を構文として連結せず、生成した語の `"` を二重化して
  各語をquoteする。SQL値はparameter bindingする
- SQLiteの`bm25()`は**小さい値ほど上位**なので昇順にする。初期column weightは
  `card_id=0 / title=5 / tags=3 / summary=2 / episode=1`。ただし trigram トークン上の
  BM25 は語単位BM25と挙動が違い、この重みは推測値でしかない。**offline replay の
  スイープ対象**（`bm25_weights`）として設定に出す
- 複数instanceを横断する場合は、instanceごとのrankをそのまま融合しない。
  各経路の候補を全instanceから集め、グローバル順位を付けてからRRFする

#### trigram の性質に由来する3つの補正

trigram tokenizer は**純粋な部分文字列一致**であり、語境界も df も見ない。
実測（SQLite 3.46.1）で確認した挙動:

| クエリ | ヒット | 問題 |
|---|---|---|
| `"cat"` | `catalogs` / `communication` を含むカード | 語境界が無い → 誤爆 |
| `"陶芸"` | **0件** | 2文字は trigram を作れない → 引けない |
| `"melanie"` | 会話中のほぼ全カード | 高DF語だけで候補20枠が埋まる |

1. **語境界の再検証（ASCII 語のみ）**
   FTS がヒットしたカードについて、索引対象テキストへ
   `(?<![a-z0-9])<語>[a-z]{0,3}(?![a-z0-9])` を当て、**語頭一致 + 3文字までの語尾**
   だけを本物のヒットとみなす。`pet`→`pets`、`pottery`→`pottery's` は残り、
   `carpet` / `communication` は落ちる。FTS の後段フィルタなので再現率は
   trigram 素のままから下がらない（誤爆だけを削る）。CJK 語には適用しない
2. **2文字CJK語の LIKE 補助候補**
   3文字未満のCJK語（`陶芸` `記憶` `犬猫`）は FTS では引けないので、
   `knowledge_cards` 側へ `LIKE '%語%'` を投げて**補助候補**として BM25 候補列の
   末尾に付ける（一致語数の多い順）。BM25 スコアは付かないので、RRF には
   「本物のBM25ヒットの後ろの順位」として入る。
   **LoCoMo は英語なのでこの経路は eval で一切検証されない**。日本語での有効性は
   本番 trace（`bm25_short_term_hit_rate`）で別途見る
3. **df ゲート（高DF語だけの候補を落とす）**
   スキャンしたヒット集合から語ごとの df を数え、`df / 総カード数` が
   `bm25_max_df_ratio`（既定0.5）を超える語を「弱い語」とする。
   **弱い語しか一致していないカードは BM25 候補から落とす**。
   `What pets does Melanie have?` で `melanie` だけ一致した無関係カードが
   候補を埋めるのを防ぐ。IDF はスコアを下げるが順位は付けてしまうので、
   スコアではなく候補集合の側で切る。
   ただし件数の少ない DB を守るため `bm25_min_weak_df`（既定5）の床を置く。
   カード3枚で「2枚に出る語」は比率上は高DFだが、ノイズではない

#### RRF スコアの持ち方（実装契約）

既存経路は `all_results.sort(key=lambda x: x["score"])` で**何度も再ソートする**
（`brain.py` の複数instanceマージと `_search_multi_db`）。RRF の順位を cosine で
上書きしないよう、hybrid では次を守る:

- `score` = **RRF スコア**（降順ソートで正しい順序になる値）
- cosine 実値は `vector_score`（decay/archive 適用後）と `raw_score` に退避
- `retrieval_source`(`vector`/`bm25`/`both`) / `vector_rank` / `bm25_rank` /
  `rrf_score` / `matched_terms` を候補 dict に載せる
- `score` の絶対値に依存する下流の閾値判定を作らない（現状も表示・trace のみ）

### 3.3 検索実行と注入判定の分離

Quick Retrievalは`need_intent`に関係なく全質問で実行する。一方、検索候補を
プロンプトへ入れるかは別のpolicyで決める。

1. **Phase 1A**: `injection_policy=intent_gated`
   - 検索は常時実行する
   - 注入条件は現行どおり `need_intent=past_fact/relationship` と候補あり
   - ランキング変更の効果を、注入判定変更と混ぜずに測る
2. **Phase 1B**: 分類器 null の問への注入を実験する
   - `injection_policy=retrieval_assisted`: ベクトルと BM25 の双方が同じカードを
     支持したときだけ昇格（hybrid 専用。vector では発火しない）
   - `injection_policy=candidates`: 候補があれば注入する。**§8 の実測で、検索側の
     どの信号も cat5 の adversarial 問を分離できなかったため、ゲートを作る代わりに
     これを A/B で測る**
   - cat5の誤注入を必ずA/Bで確認する（実測では読み手が耐えている＝リスクは小さい）

初期実装は逐次実行でよい。正しさを確認後、ContextClassifierとQuick Retrievalを
並列化して検索レイテンシを隠す。

### 3.4 「関係ない記憶を注入しない」の担保

RRFは候補の順位付けであり、絶対的な関連性判定には使えない。初期実装では:

- ベクトル側は現行の`vector_search_threshold=0.4`を通過した候補だけを使う
- BM25側は §3.2 の3補正（語境界検証 → df ゲート → 候補数上限）を通ったカードだけ
- **archived の扱いは経路で非対称**。これは意図的:
  - ベクトル側は従来どおり `score *= 0.5` のペナルティを掛けたうえで閾値判定
    （既存挙動を変えない。archived でも高 cosine なら候補に残る）
  - BM25 側は**activeカードが1件も無い場合だけ** archived を候補へ入れる。
    字面一致は archived でも容易に当たるため、ペナルティでは抑えきれない
  - Phase 1 の trace で archived 由来の注入率を見てから、両者を揃えるか判断する
- 両方の候補が空なら結果ゼロ
- 候補があっても、3.3の注入policyを満たさなければプロンプトへ入れない

閾値0.4は当面の後方互換ガードであり、モデル非依存の最終解とはみなさない。
Phase 1のtraceを使って、順位差・分布・BM25一致との組み合わせによる適応的な
relevance gateを後続検討する。

### 3.5 設定

正規設定モデルの`brain` / `memory_probe`へ追加し、互換層の`SYSTEM_CONFIG`にも公開する
（新規コードは`get_settings()`経由で参照）。instance / eval profileから上書き可能で、
`brain`・`memory_probe`とも既に`PROFILE_ROLE_SECTIONS`
（[evals/locomo/config.py](../../../evals/locomo/config.py)）に入っている:

| セクション | キー | 導入時の既定 | 意味 |
|---|---|---|---|
| `brain` | `search_mode` | `"vector"` | `"hybrid"`はevalで先行。有効性確認後に既定を昇格 |
| `brain` | `bm25_candidates` | 20 | BM25側の候補数 |
| `brain` | `vector_candidates` | 20 | ベクトル側の候補数 |
| `brain` | `rrf_k` | 60 | RRFの平滑化定数 |
| `brain` | `bm25_weights` | `{title:5, tags:3, summary:2, episode:1}` | bm25() の column weight。スイープ対象 |
| `brain` | `bm25_max_df_ratio` | 0.5 | この比率を超える語は「弱い語」。弱い語だけの候補を落とす |
| `brain` | `bm25_min_weak_df` | 5 | df がこの件数未満なら弱い語にしない（少件数DBの保護） |
| `brain` | `bm25_scan_limit` | 500 | df 計算と語境界検証のためにスキャンする最大ヒット数 |
| `memory_probe` | `retrieval_execution` | `"always"` | Quick Retrievalを全質問で実行 |
| `memory_probe` | `injection_policy` | `"intent_gated"` | 初期は検索と注入判定を分離して現行条件を維持。`retrieval_assisted`（hybrid専用）/ `candidates`（候補があれば注入）を eval で比較 |

**出力件数はどれが効くのか（経路ごとに固定）**:

| 経路 | 出力件数 | 候補数 |
|---|---|---|
| Layer 1（MemoryProbe / Quick Retrieval） | `memory_probe.vector_search_limit`（既定3） | `brain.vector_candidates` / `bm25_candidates` |
| Layer 2（`brain.search_knowledge` / Deep） | `brain.search_limit`（既定3） | 同上 |

`brain.search_limit` は Layer 1 には効かない。逆に `vector_search_limit` は Layer 2 に
効かない。ハイブリッド化でもこの分担は変えない（既存挙動と同じ）。

`search_mode=vector`ではランキングと注入挙動を従来互換に戻せる。ただし
`retrieval_execution=always`は独立設定なので、完全な旧挙動が必要な場合は
`retrieval_execution=intent_gated`も指定する。

### 3.6 Layer 2 の扱い

`_search_single_db` の `LIKE` + 直近100件フォールバックは**ハイブリッドで置き換える**。
Phase 1ではLLMキーワード抽出を通常検索から外し、3.2の決定論的query builderを使う。
検索語の意味的展開が必要なら、ハイブリッドの実測後に独立機能として再導入する。
これによりLayer 2の追加LLM呼び出しと直近100件fallbackを削除できる。

- **Layer 2 の存在意義**: ハイブリッドでは Layer 1 が既に BM25 を含むので、同じ
  パイプラインを再実行しても結果は変わらない。そこで hybrid の Deep は
  **ベクトル閾値を外した緩いゲート**で走らせる（Layer 1 = 閾値 0.4 通過のみ、
  Layer 2 = 閾値なし + `brain.search_limit`）。Layer 1 が空のときの救済という
  従来の役割はそのまま残る
- **API 互換**: `brain.search_knowledge(keywords, user_query, ...)` のシグネチャは
  変えない（[tests/test_brain_multi_db.py](../../../tests/test_brain_multi_db.py) が
  positional で直接叩いている）。`search_mode=vector` では従来どおり keywords を
  LIKE 絞り込みに使い、`hybrid` では keywords を無視して §3.2 の query builder を
  使う。`keywords=None` を許容する
- **LLM 呼び出しの削除**: `brain.extract_keywords` の実利用は
  [memory_probe.py の `_deep_search_diag`](../../../butly_core/core/gatekeeper/memory_probe.py)
  だけ。hybrid では呼ばない（`vector` では従来どおり呼ぶ）。メソッド自体と
  プロンプトは残す（`vector` モードと外部利用の後方互換）

### 3.7 観測

`debug_info["rag"]["retrieval"]` と trace に、各カードがどちらの候補由来か
（`vector` / `bm25` / `both`）と両者の順位、raw cosine、decay後score、BM25 rank、
RRF score、一致語、注入可否と理由を残す。融合前の**ベクトル単独ランキングと
BM25単独ランキングの card_id 列**も残す（`bm25_rescue_rate` の計算に要る）。

scorerには次を追加する:

| 指標 | 分母 | 定義 |
|---|---|---|
| `search_execution_rate` | 全問 | Quick Retrieval を実行した割合 |
| `retrieval_candidate_rate` | 全問 | 候補が1件以上あった割合 |
| `memory_injection_rate` | 全問 | プロンプトへ実際に注入した割合。**従来の `rag_trigger_rate` と同義**（同値であることを保つため両方出す） |
| `retrieval_recall_at_1/3/20` | oracleカードが存在する問 | 上位k候補の `source_files` が evidence ターンを覆う割合の平均（`evidence_coverage` と同じ計算を、注入カードでなく上位k候補で行う） |
| `bm25_rescue_rate` | oracleカードが存在する問 | 融合後 top3 の coverage > ベクトル単独 top3 の coverage となった割合 |
| `retrieval_latency_ms_p50/p95` | 実行した問 | 検索のみのレイテンシ（embedding 呼び出しを含む） |
| `bm25_short_term_hit_rate` | 実行した問 | 2文字CJK補助候補が候補に入った割合（日本語運用の観測用） |

`rag_trigger_rate` は歴史的に「注入されたか」を測っており、検索実行率ではない。
v26 の 0.905 が実行率の proxy として読めたのは、検索が `need_intent` でゲートされて
いたから。Phase 1 以降は両者が分離するので、**混同しないよう別名で並べる**。

---

## 4. Phase 2 — `episode` を埋め込みに含める

`sleeptime.py` の `content_to_embed` は `Title + Tags + Summary` のみ。一方
`memory_builder` は `episode` をプロンプトに出している。**書く側と探す側が非対称**で、
所感・意味づけは「別の理由でカードが当たったときだけ」見える。

v26 で落ちている質問がまさにこの層:
`What does Caroline's necklace symbolize?` / `Why did Melanie choose to use colors...` /
`How does Melanie prioritize self-care?`

- `content_to_embed` に `episode` を追加する
- LLMで再要約せず、原文を最大512文字まで決定論的に追加する。`episode`はAIの所感を
  含み得るため、FTS/BM25でもembeddingでも主フィールドより弱く扱う
- Sleeptimeと`migrate_embeddings.py`で共通の`build_card_embedding_text()`を使い、
  書き手ごとのrecipe差を防ぐ
- **全カードの再 embedding が必要**（`migrate_embeddings.py --all`）
- **`embedding_meta` に `text_recipe` を追加する**。現在の指紋は model と profile だけで、
  「何を埋め込んだか」が変わっても検知できない。Phase 2 はまさにそれを変えるので、
  recipe（初期値 `card-v2:title+tags+summary+episode[0:512]`）を指紋に含め、起動時チェックと
  評価コンソールのガードが同じ仕組みで効くようにする
- **触る箇所は3つ**（Phase 2 着手時の作業単位）:
  1. [butly_core/core/embedding_check.py](../../../butly_core/core/embedding_check.py) —
     `embedding_meta` の DDL に列追加 + 突き合わせロジック
  2. [butly_core/llm/embedding_profiles.py](../../../butly_core/llm/embedding_profiles.py)
     の `fingerprint()` — recipe を返す
  3. [app.py](../../../app.py) の `allow_embedding_mismatch` ゲート — recipe 差も対象にする
- **後方互換**: 既存 DB の `text_recipe` は NULL。NULL は `card-v1:title+tags+summary`
  とみなす（不明扱いで全 run を mismatch にしない）。NULL と現行 recipe が食い違う
  ときだけ警告する

---

## 5. Phase 3 — 近傍展開（sibling expansion）

上位 k カードが決まった後、**同じ `source_files` / 同じ `source_date` のカードを候補へ追加**する。
SQL だけで済み、embedding 呼び出しは増えない。

- multi-hop（cat1、v26 で 0.280）と「同じ日の細部」に効く見込み
- per-card `source_files` は 8d9290c で既に入っている（実LLMの申告率98%を実測済み）
- `source_files`重複を強い近傍、`source_date`一致を弱いfallbackとして扱う
- 展開上限は`sibling_limit`（既定2）だが、**最終注入は既存の3カード枠・文字数予算内**。
  siblingは下位カードと入れ替え、追加注入しない
- siblingはサマリのみとし、原文は`rag_raw_top_k`の対象外

---

## 6. Phase 4 — 多言語リランカー

候補20〜30件を cross-encoder で並べ替えて 3 件に絞る。

- 候補: `bge-reranker-v2-m3` / `jina-reranker-v2-base-multilingual`（どちらも ja/en）
- **Butly 本体に重みを持たせない**。Ollama か OpenAI 互換エンドポイント越しに呼ぶ
  形にし、`reranker` を独立した任意ロールとして `AI_CONFIG` に足す
  （未設定なら無効＝現状動作）
- Connection / model / timeoutは`reranker`ロール、enable / candidate数 / 採用数は
  `memory_probe`に置く
- NanoGPT 等の安い LLM で rerank する実装も同じインタフェースで差せる
- **Phase 1 の後に効果を測ってから着手**。RRF だけで 0.07 の団子が解けるなら不要かもしれない

---

## 7. Phase 5 — RAW の embedding（ユーザー案）

カードに存在しない事実を救う唯一の手段。v24 の調査で、`figurine` / `dog face` /
`sunflower` / `summer sounds` は **生ログにあるのにカードDBに1件も無い**ことを確認済み。
さらに v20(79枚) は `sunflower` を持っていたのに v22(103枚) は落としており、
**カード生成には「毎回同じ事実を拾う」保証が無い**。

- RAW ターンをチャンク単位で embedding し、別コレクションとして索引化
- ヒットしたRAWチャンクは、カードへ戻すだけでなく**独立した根拠excerptとして直接注入**
  する。カードに存在しない事実は、カードへ合流するだけでは回答モデルへ届かない
- `source_files`の逆引きで関連カードも補助候補にできるが、RAW provenanceと
  注入文字数予算はカードと別に記録する
- Phase 1を先に実施する。その後、残ったevidence=0のうち「カード自体に無い」割合を
  再測定し、Phase 2/3より先に進めるかを決める

---

## 8. 検証方法

まずLLM回答を生成しない**offline retrieval replay**で、同一質問・同一カードに対する
Recall@k / no-hit を比較する。ここで改善した構成だけを`rerun-qa`へ進める。
実装は `evals/locomo/retrieval_replay.py`:

```bash
# BM25 のみ（embedding 呼び出しなし＝APIキー不要）
venv/bin/python -m evals.locomo.retrieval_replay \
    --run ./eval_runs/runs/qwen3_14b_web_v26 --modes bm25

# ベクトル込みの本比較（質問1件につき embedding 1回）
venv/bin/python -m evals.locomo.retrieval_replay \
    --run ./eval_runs/runs/qwen3_14b_web_v26 --modes vector hybrid \
    --profile ./eval_runs/profiles/<id>.yaml
```

replay は run の DB を一時ディレクトリへ複製してから索引を作るので、
元 run は変更しない（R7）。
QA評価は同一記憶に対して測る（v23で確立した手順）。カードを作り直すと生成分散
（n=199でも±0.02程度）が混ざるため。

| Phase | ベース記憶 | 測り方 |
|---|---|---|
| 1A | `qwen3_14b_web_v25` のworkspaceをrunごとに複製 | `vector` vs `hybrid`をoffline replay後、`intent_gated`固定でQA |
| 1B | 同上 | `intent_gated` vs `retrieval_assisted`。cat5を必ず比較 |
| 2 | 複製workspaceを全件再embedding | 新recipeをmetaへ記録し、mismatch無しを確認してからQA |
| 3 | 同上 | 固定注入予算で`sibling_limit: 0` vs `2` |
| 4 | 同上 | reranker 無効 vs 有効 |

**なぜ v25 の workspace か**: v26 は v25 の記憶に対する `rerun-qa`
（`run_config.json` の `memory_reused_from_run_id: qwen3_14b_web_v25`）。
つまり §0 の実測値はすべて v25 のカード107枚に対するもので、v25 workspace を
複製すれば v26 と同一記憶で比較できる。

**見る指標**: overall（cat5 除く）だけでなく、
`retrieval_recall_at_1/3/20` / `MRR` / `evidence（分母を明記）` / `ev=0 の問数` /
`search_execution_rate` / `memory_injection_rate` / `prompt_tokens` / 検索latencyを
必ず並べる。evidence は分母（全問 / 注入問 / cat1-4）で値が変わるので、
分母抜きの数字は読めない（v25 の誤読の再発防止）。

`allow_embedding_mismatch`でrecipe差を通してはならない。Phase 2では元runを変更せず、
複製workspaceをmigrateし、全カードの`embedding_meta.text_recipe`と実設定が一致してから
評価する。

### Phase 1B の QA A/B 結果（v27、2026-07-28）— **採用**

`qwen3_14b_web_v27` = v25記憶の rerun-qa。v26 との profile 差分は
`memory_probe: {retrieval_execution: always, injection_policy: candidates}` **のみ**
（Stage 3 設定・モデル・temperature・context levels は同一）。

| 指標 | v26 | v27 | 差 |
|---|---:|---:|---:|
| official overall（199問） | 0.4815 | **0.4949** | +0.0133 |
| cat1-4 overall（152問） | 0.3804 | **0.3979** | +0.0174 |
| **cat5（47問）** | 0.8085 | **0.8085** | **±0.0** |
| ev=0 の問数（cat1-4） | 51 | **43** | **-8** |
| ev=1.0 の問数（cat1-4） | 78 | **86** | +8 |
| evidence_retrieval_rate | 0.543 | 0.604 | +0.061 |
| rag_trigger_rate / search_execution_rate | 0.905 / — | 1.000 / 1.000 | +0.096 |
| prompt_tokens 平均 | 1769 | 1846 | **+4.3%** |

**注入が変わった問だけを見る（temperature 0.7 の生成分散と分離）**:

| グループ | n | スコア平均差 | 改善 / 悪化 |
|---|---:|---:|---|
| 注入が変わった（treatment） | 19 | **+0.2845** | 9 / 1 |
| └ cat1-4 | 14 | **+0.3147** | 8 / 1 |
| └ cat5 | 5 | **+0.2000** | 1 / **0** |
| 注入が変わらない（= 生成分散のみ） | 180 | -0.0153 | 17 / 26 |

cat1-4 の +0.0174 は **treatment +0.0290 / ノイズ -0.0116** の合成。
**真の効果は +0.029**（0.380 → 0.409 相当）で、事前見積もり +0.024 を上回った。

読み取れたこと:

- ev=0 が 51 → 43 と、事前に特定した8問がそのまま救われた（予測どおり）
- **cat5 は悪化ゼロ**。新たに記憶が入った5問は 1問改善・0問悪化で、
  `What does Melanie's necklace symbolize?`（主語すり替えの adversarial 問）も
  記憶を渡された状態で 1.00 を維持した。cat5 全体が ±0 なのは、注入が変わって
  いない42問側の生成分散（-1問）と相殺されたため
- prompt_tokens の増加は**注入が変わった19問に集中**（+684/問、合計 +13.0k）。
  残り180問は +12.7/問。全体平均 +4.3% は §8 の予算 +3% を超えるが、
  「記憶が届いていなかった問に記憶を入れた分」であって全問一律の膨張ではない
- latency の +8.0秒/問は、**注入が変わっていない180問側が +1.48M ms** を占める
  ＝ NanoGPT 側の変動。今回の変更由来ではない

### Phase 1A の A/B 結果（2026-07-27）— **hybrid は既定へ昇格しない**

v26 workspace（カード107枚 / oracle カードがある cat1-4 139問）で、embedding を
1回だけ回してランキングをキャッシュし、融合方式だけを差し替えて比較した:

| strategy | @1 | @3 | @20 | hit@3 | hit@20 |
|---|---:|---:|---:|---:|---:|
| **vector only** | 0.4862 | **0.6906** | 0.8363 | **107** | 126 |
| bm25 only | 0.4101 | 0.5270 | 0.7302 | 79 | 113 |
| RRF 1:1（実装した既定） | 0.4466 | 0.6457 | 0.8405 | 99 | 127 |
| RRF 2:1（vector 優位） | 0.4742 | 0.6577 | 0.8363 | 100 | 126 |
| RRF 3:1 | 0.4814 | 0.6523 | 0.8363 | 100 | 126 |
| cascade（vector top3 を固定して BM25 を後ろへ） | 0.4862 | 0.6906 | 0.8189 | 107 | 125 |

**top3 の相補性**: both 74 / vector のみ 33 / **BM25 のみ 5** / どちらも外し 27。

読み方:

- BM25 が top3 で単独救済するのは **5問**、対して vector 単独は 33問。等重み RRF は
  その5問を取りに行って **8問を落とす**（107→99）。重みを 3:1 まで振っても
  vector 単独に届かない
- k=20 ではほぼ互角（0.8363 → 0.8405）。BM25 が候補プールへ足す情報は 1問分
- **§8 の昇格条件1（Recall@3 が +5pt）を満たすどころか -4.5pt。よって既定は
  `vector` のまま**。hybrid の実装は残し、日本語運用・別 embedding での再評価に備える
- BM25 が救った5問には計画書 §0 が挙げた `What types of pottery have Melanie and
  her kids made?` が含まれる。**狙った問は実際に直るが、代償のほうが大きい**

### 本当のボトルネックは検索順位ではなかった

| 対象（oracle 139問） | coverage | hit |
|---|---:|---:|
| v26 で実際に注入されたカード | 0.6331 | 99 |
| **常時検索（`retrieval_execution=always`）の vector top3** | **0.6906** | **107** |

差の8問は**すべて「分類器が null で検索が走らなかった」問**（注入ミスではない）。
現在スコア平均 0.062。つまり ev=0 の主因はランキングではなく**検索前のゲート**で、
BM25 なしで（`search_mode=vector` のまま）取り返せる。

### 注入ゲートは作れない（実測）

`need_intent=null` の19問（cat1-4 が14、cat5 が5）に対し、注入してよいかを
検索側の信号で判定できるか試した:

| ゲート候補 | 結果 |
|---|---|
| cosine 絶対値 | cat1-4（当たり）0.696–0.840 に対し cat5 は 0.706–0.762。**完全に重なる** |
| 順位差（top1 - top2） | 平均 0.044 vs 0.020。分布は重なりゲートにならない |
| BM25 一致（vector top3 ∩ BM25 top-k） | **cat5 5問すべてが一致**。分離ゼロ |

LoCoMo cat5 は実在する話題の主語・属性だけを差し替えて作られている
（例: 実在するのは Caroline のネックレスなのに `What does Melanie's necklace
symbolize?`）。**検索側の信号では原理的に見分けられない**。

一方、誤注入の実害は小さいことも実測できた:

| cat5 47問 | accuracy |
|---|---:|
| 既に記憶が注入されている 42問 | **0.810** |
| 注入されていない 5問 | 0.800 |

読み手は無関係な記憶を渡されても "No information" を維持できている。
したがって `injection_policy` に **`candidates`（候補があれば注入）** を追加し、
ゲートを作る代わりに A/B で是非を測る。

### Phase 1A 実装直後の offline 実測（BM25 単独・参考）

v26 の workspace（カード107枚）に対し、**BM25 単独**（ベクトル・RRF なし）で
`retrieval_recall` を測った。LLM 呼び出しなし・embedding なしの純オフライン計測:

| 対象 | 結果 |
|---|---|
| oracle カードがある cat1-4（139問）の BM25 top3 coverage | 0.527（現行の注入カード基準 0.633 と比較して単独では低い） |
| **現在 ev=0 かつ oracle あり（40問）の救済数** | top1: **6** / top3: **11** / top5: 12 / top20: **21** |

読み方:

- BM25 単独 top3 で 11問（40問中）が根拠へ到達する。注入枠3のまま RRF で融合
  すればベクトル側の当たりも残るので、**Phase 1 の現実的な救済は 11〜13問**。
  overall は 0.380 + 11×0.354/152 ≈ **0.406**
- したがって §1 の Phase 1 目標 0.42（=21問救済）はまだ強気。**まず 0.41 を
  当面の合格線**とし、0.42 は Phase 2（episode 埋め込み）の寄与込みで狙う
- 一方 **top20 では 21問**に届く。順位付けさえ良ければ倍にできるということで、
  Phase 4（リランカー）の期待値は当初の想定より高い
- `melanie`（df 51/107 = 0.476）は df ゲートの閾値 0.5 をわずかに下回り「弱い語」に
  ならなかった。実会話では主役名の df が 0.5 前後に来るので、`bm25_max_df_ratio` は
  スイープ対象として扱う

### Phase 1を既定化する判定

`search_mode=hybrid`を本番既定へ昇格する条件:

1. oracleカードが存在する問の`retrieval_recall_at_3`がvectorより5ポイント以上改善
2. cat1-4のevidence=0が10問以上減る（51→41以下。§1のPhase 1目標は40問以下）
3. cat5 accuracyの低下が2ポイント以内
4. 平均prompt tokensは現状比+3%以内
5. warm時の検索p95が1秒以内。**この p95 は embedding 呼び出しを含む**ので、
   FTS 単体の時間とは別に `retrieval_latency_ms_p95` と embedding 呼び出し時間を
   分けて記録する（p95 の主因が FTS でないなら FTS を責めても仕方がない）

満たさない場合も`hybrid`機能は残し、既定は`vector`のまま原因をtraceで調べる。

---

## 9. リスク

| # | 内容 | 対応 |
|---|---|---|
| R1 | trigram FTS の索引サイズ。カード本文の約3倍 | 100枚で数百KB。1万枚でも数十MB で許容。実測してから判断 |
| R2 | BM25 が固有名詞に強い反面、助詞・stopwordでノイズが増える | 欧文stopword除去、CJK 3文字shingle最大32語、field weight、固定候補数で制御。offline Recall@kで確認 |
| R2a | **trigram は語境界を見ない**。`cat`→`catalogs`/`communication`、`pet`→`carpet` が当たる（実測） | §3.2 の語境界再検証（語頭一致＋語尾3文字まで）で誤爆だけ削る。効かなければ英語用に unicode61 の第2 FTS を追加検討 |
| R2b | **2文字のCJK語は trigram で引けない**（`陶芸` は0件、実測）。日本語の内容語は2文字が多い。しかも**LoCoMoは英語なのでevalで表面化しない** | 2文字語は `LIKE` 補助候補として BM25 候補列の末尾へ。既知の制約として §10 に明記し、本番 trace の `bm25_short_term_hit_rate` で観測 |
| R2c | 高DF語（会話の主役名など）だけで候補20枠が埋まる | `bm25_max_df_ratio`（既定0.5）を超える語しか一致しない候補を落とす |
| R3 | 既存 instance DB への FTS backfill 中の書き込み競合 | `_initialize_db` 内（既存のschema migrationと同じタイミング）で実施。WAL下の単一トランザクション |
| R4 | Stage 3 のノード注入との相互作用 | ノードはカード検索の結果に紐づくため、ハイブリッドで上位が変われば注入されるノードも変わる。A/B は Stage 3 の設定を固定して行う |
| R5 | 本番の `time_decay_rate` は 0.003 だが eval は 0.0。**decay とハイブリッドの相互作用は未評価** | decayはベクトル順位へ適用。BM25の明示的な字面一致は古い事実も救うためdecayしない。archiveはactive候補が無い場合だけ使い、本番設定で別途確認 |
| R6 | 配布環境のSQLiteにFTS5/trigramが無い | 起動時にcapabilityを検査し、hybrid指定でも警告付きvector fallback。診断へ理由を残す |
| R7 | A/B用backfillが元runのworkspaceを変更する | `rerun-qa`の複製先だけを初期化・backfillし、source runを不変に保つテストを追加 |
| R8 | RRF の順位が下流の `score` 再ソートで壊れる（`brain.py` は複数箇所で `score` 降順に並べ直す） | hybrid では `score` に RRF スコアを載せ、cosine は `vector_score` / `raw_score` へ退避（§3.2 の実装契約）。回帰テストで順序を固定 |
| R9 | 目標値（overall 0.45 / ev=0 20%）が Phase 1 単独では届きにくい | §1 のとおり Phase 1 は 0.42 / 26%、0.45 / 20% は Phase 1-3 合算目標として分離 |

## 10. 決定事項

1. Phase 1導入時の既定は`vector`。evalの昇格条件を満たしてから`hybrid`を既定にする
2. Quick Retrievalは全質問で実行し、注入policyと分離する
3. Phase 1Aは`intent_gated`、Phase 1Bで`retrieval_assisted`を評価する
4. `episode`は再要約せず、原文を最大512文字までembeddingへ追加する
5. embedding本文は共通helperで作り、`text_recipe`をfingerprintへ含める
6. rerankerは独立した任意のAIロールにする
7. siblingは注入枠を増やさず、既存3カード枠内で入れ替える
8. RAWヒットはカード経由だけでなく、独立した根拠excerptとして直接注入する
9. **BM25候補には3段の補正を必ず通す**（語境界検証 / df ゲート / 候補数上限）。
   trigram の素の挙動をそのまま順位付けに使わない
9b. **hybrid は既定にしない**（2026-07-27 の A/B 結果）。RRF は vector の良い順位を
   BM25 候補で薄め、@3 で -4.5pt。機能はフラグの奥に残し、日本語運用・別 embedding で
   再評価する
9c. **注入ゲートは検索側の信号では作れない**（cosine / 順位差 / BM25 一致がいずれも
   cat5 と分離しない）。`candidates` policy で A/B し、読み手の耐性で判断する
10. **2文字CJK語は既知の制約**。LIKE 補助候補で最低限救うが、日本語の字面一致は
    LoCoMo では検証されない。既定を `hybrid` へ昇格しても、この点は
    「英語で検証済み・日本語は未検証」と扱う
11. hybrid の `score` は RRF スコア。cosine は `vector_score` / `raw_score` に退避する
12. 出力件数は Layer 1 = `memory_probe.vector_search_limit`、Layer 2 = `brain.search_limit`
    で固定（§3.5）
13. §1 の目標は Phase 1 単独（0.42 / ev=0 26%）と合算（0.45 / 20%）に分ける

---

## 11. 進め方

Phase 1だけで独立して価値があり、他はPhase 1の結果を見てから優先度を決める。

1. ~~Phase 1A（FTS5 / RRF / 常時検索 / intent-gated注入）を実装~~ 完了
2. ~~offline retrieval replayでvectorとhybridを比較~~ 完了 → **hybrid は @3 で -4.5pt。既定は `vector` のまま**
3. ~~Phase 1B を QA で A/B する~~ 完了（v27）。cat1-4 の真の効果 **+0.029**、
   ev=0 は 51→43、cat5 悪化なし → **eval では採用**
4. **次**: 既定へ昇格させる範囲を決める。eval profile は `candidates` を既定にしてよいが、
   本番（対話）では全ターンにカードが入るため、**雑談ターンのトークン増と
   ふるまいの変化は LoCoMo では測れていない**。本番既定は保留し、日本語の
   対話サンプルでトークン実測とふるまい確認をしてから判断する
5. 残ったevidence=0（43問）を「カード内にある / カードに無い」に再分類
6. **Phase 4（リランカー）の優先度を上げる**。vector は @3 で 0.6906 だが
   @20 では 0.8363 まで届いており、順位付けだけで19問ぶんの余地がある。
   Phase 2（episode）と合わせて次の候補
7. Phase 5（RAWのembedding）は「カードに無い」問の割合を再測定してから判断

### Phase 1 で追加するテスト

`./scripts/check_before_push.sh`（＝ `pytest -m "not integration"`）で緑になること。

- query builder: NFKC/小文字化、3文字未満の除去、stopword、CJK 3文字shingle、
  最大語数、`"` の二重化（`he said "hi"` のような入力で MATCH 構文が壊れない）
- 語境界検証: `cat` が `catalogs` を拾わない / `pet` が `pets` を拾う
- df ゲート: 全カードに出る語しか一致しない候補が落ちる
- 2文字CJK: LIKE 補助候補として拾えている
- RRF: 既知の2つのランキングから期待順が出る。`score` が RRF スコアである
- FTS 同期: INSERT / UPDATE（本文列のみ）/ DELETE のトリガ、embedding だけの
  UPDATE で再索引が走らないこと
- backfill: 件数不一致・version不一致で再構築、一致時は何もしない
- capability 欠落時（FTS5/trigram なし）に hybrid 指定でも例外を投げず vector へ落ちる
- `search_mode=vector` で従来の順位・注入挙動が変わらない（回帰）
- R7: `rerun-qa` の複製先を初期化しても source run の DB が変わらない

### 更新する docs

- [docs/reference/coding_conventions.ja.md](../../reference/coding_conventions.ja.md) — 該当なしなら不要
- `docs/reference/` の検索・記憶まわりの仕様、
  [docs/reference/locomo_evaluation_flow.ja.md](../../reference/locomo_evaluation_flow.ja.md)
  （新 scorer 指標）
- [docs/reference/FILE_STRUCTURE.ja.md](../../reference/FILE_STRUCTURE.ja.md)（新モジュール）
- 英語版 `*.md` は日本語版に追従（最低でも `*.ja.md`）
