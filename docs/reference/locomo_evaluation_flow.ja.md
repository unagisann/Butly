# LoCoMo評価のデータ保存・QA実行フロー

🌐 **日本語** | [English](locomo_evaluation_flow.md)

この文書は、LoCoMoの元会話がButlyへどう保存され、いつQAが始まり、
`independent`（独立QA）と`sequential`（継続QA）で何が変わるかを示す。
正本は現行の`evals/locomo/`実装であり、この文書はその処理経路を図解したもの。

## 全体像

評価は「全サンプルの会話を先に保存してから全QA」ではなく、**サンプル単位**で
次の順序を繰り返す。

1. 1サンプル用の正本instanceを作る。
2. 選択された各sessionをReplayし、そのsessionのSleeptimeを完了する。
3. そのサンプルの全sessionが完了したら、選択されたQAを実行する。
4. 次のサンプルへ進む。

```mermaid
flowchart TD
    A["LoCoMo JSON"] --> B["dataset.py<br/>Conversation / Session / Turn / Questionへ変換"]
    B --> C["対象sampleを選択"]
    C --> D["評価専用の正本instanceを作成"]
    D --> E["次のsessionをReplay"]
    E --> F["short_term_jsonへ元会話を保存"]
    F --> G["checkpoint: replayed_sessionsへ追加"]
    G --> H["before_sleeptime snapshot"]
    H --> I["Sleeptime Stage 1 / Stage 2"]
    I --> J["after_sleeptime snapshot"]
    J --> K["checkpoint: sleeptime_completedへ追加"]
    K --> L{"選択sessionが残っているか"}
    L -- Yes --> E
    L -- No --> M["QA基準時刻を最終sessionの翌日に固定"]
    M --> N{"qa_mode"}
    N -- independent --> O["独立QA用の一時cloneで質問"]
    N -- sequential --> P["正本instanceへ順番に質問"]
    O --> Q["qa_results / Trace / checkpointを保存"]
    P --> Q
    Q --> R{"選択questionが残っているか"}
    R -- Yes --> N
    R -- No --> S{"次のsampleがあるか"}
    S -- Yes --> D
    S -- No --> T["採点・summary生成"]
```

## 元会話を最初に保存する経路

### 1. Datasetの解釈

`dataset.py`は公式JSONを読み、次の対応で固定DTOへ変換する。

- `sample_id` → 1つの評価instance
- `conversation.session_N` → 時系列順のsession
- 各発話の`dia_id` → evidence追跡用のID
- `qa` → 後で実行する質問・正解・カテゴリ・evidence
- 画像がある発話 → `blip_caption`を`[Image: ...]`として本文へ追加

### 2. 話者をButlyのroleへ変換

`ReplayAdapter`は話者を次のように固定する。

| LoCoMo | Butly |
|---|---|
| `speaker_a` | `user` |
| `speaker_b` | `assistant` / `model` |

基本はuser発話と直後のassistant発話を1つのButly turnとして保存する。同じroleの
発話が連続した場合は、反対側を空文字にして元の順序と全`dia_id`を失わないようにする。

### 3. `short_term_json`へ保存

Replayしたturnは通常の`ButlyMemory.save_single_turn()`を通る。保存日時には
評価実行時刻ではなく、LoCoMo sessionの元日時を渡す。

```text
LoCoMo turn
  → ReplayAdapter.replay_session()
  → ButlyMemory.save_single_turn(
        user_text,
        assistant_text,
        created_at=LoCoMoの元日時,
        meta=LoCoMo provenance
    )
  → workspace/butly_core/instances/<instance>/short_term_json/session_*.json
```

各JSONには、通常のuser/model本文に加えて次の追跡情報が入る。

- `locomo_sample_id`
- `locomo_session_id`
- `locomo_dialog_id` / `locomo_dialog_ids`
- 元話者と元timestamp
- LoCoMo話者とButly roleの対応
- 評価由来であることを示す`source=eval`

Replay結果のファイル名と`dia_id`対応は`results/replay_log.jsonl`にも保存される。

## 各session後のSleeptime

1 sessionをすべて`short_term_json`へ保存した直後に、そのinstanceへ同期的に
Sleeptimeを実行する。評価instanceの既定では次が有効になる。

- Stage 1: short-term会話のflush、mid-term digest、recent snapshot、
  RAW memory cacheなどの更新
- Stage 2: RAW会話からknowledge cardを抽出し、`butly_memory.db`へ保存
- 無効: key memory更新、knowledge maturation

主要な保存先は次のとおり。

```text
short_term_json/session_*.json
  └─ Sleeptime Stage 1
      ├─ memory_archive/1_integrated/
      ├─ mid_term_digest.txt
      ├─ recent_snapshot.txt
      └─ raw memory cache
          └─ Sleeptime Stage 2
              ├─ butly_memory.db の knowledge_cards
              └─ memory_archive/2_knowledgeized/
```

`snapshots/<instance>/<session>/before_sleeptime`と`after_sleeptime`は比較・監査用の
コンパクトなsnapshotであり、instance全体のcloneではない。digest、session state、
knowledge card一覧、各保存領域の件数などを記録する。

QAは、対象サンプルの選択sessionがすべて
`sleeptime_completed`へ入った後にだけ始まる。

## QA共通のButly内部経路

両モードとも質問への回答経路は同じで、使用するinstanceだけが異なる。

```mermaid
flowchart LR
    A["LoCoMo Question"] --> B["QARunner<br/>ChatRequestを構築"]
    B --> C["ButlyRuntime.chat()"]
    C --> D["ChatService前処理"]
    D --> E["Chronos + recent history"]
    E --> F["Gatekeeper分類"]
    F --> G["MemoryBlockBuilder"]
    G --> H["RAG<br/>knowledge cards / RAW参照"]
    H --> I["Chat modelで回答生成"]
    F --> J["StateUpdater"]
    I --> K["回答とQA turnをshort_term_jsonへ保存"]
    J --> K
    K --> L["Trace latest.json"]
    L --> M["QARunnerが永続artifactへコピー"]
    M --> N["qa_results.jsonl + traces/<sample>/<question>.json"]
    N --> O["checkpoint.qa_completedを更新"]
```

QA用`ChatRequest`は次の固定ポリシーを使う。

- `use_rag=True`
- Google検索・Web検索は無効
- LoCoMoの質問文は翻訳しない
- QA質問の多数決で`dataset_locale`を判定し、英語datasetには英語、日本語datasetには
  日本語の短答を要求する
- 「現在時刻」は選択sessionの最終日時の翌日に固定

評価instanceのSystem Instructionは、prompt内の全memory sectionを回答根拠として扱う。
特にknowledge card、参照元RAW、active nodeのいずれかが問いへ直接答える場合はその
情報を使い、元会話日時を基準に時制を解釈する。答えがない場合だけ、英語では
`No information available`、日本語では`情報がありません`を返す。英語datasetは
公式互換Porter Token F1、日本語datasetはNFKC + mixed-script文字F1で採点する。
日本語スコアは同じ日本語dataset上の比較用であり、英語公式値とは数値比較しない。
テンプレートの版は
`run_config.json`の`qa_prompt_version`へ保存し、異なる版のスコアを同一条件として
比較しない。

Gatekeeperが`need`を立てた場合、MemoryBlockBuilderがknowledge cardや設定に応じた
RAW参照をpromptへ組み込む。生成後、ChatServiceは質問と回答を通常の会話turnとして
処理対象instanceの`short_term_json`へ保存し、session stateも更新する。

`qa_results.jsonl`の`diagnostics.rag`と質問別Traceには、取得カードの実ID・日付・
参照instance・RAW参照状態を保存する。Stage 3有効時はactive nodeのlookup理由、
紐づくカードID、描画対象、最終Provider prompt内への注入判定も保存するため、
「node未取得」「context levelで除外」「promptへ入ったが回答に未利用」を事後に
区別できる。完全なprompt本文は評価artifactへ複製しない。

純粋ベクトル検索は、instance内のknowledge cardを新旧に関係なくすべて
コサイン類似度の計算対象にする。その後で時間減衰・archive補正・閾値判定を適用し、
上位`limit`件だけをRAG候補として返す。`fallback_fetch_limit`はキーワード検索の
フォールバック専用であり、純粋ベクトル検索の候補範囲を制限しない。
Traceの`memory_probe_layers.vector`では、`fetch_limit: null`が全件検索、
`fetched_count`が実際に評価したカード数を示す。
検索の時間減衰はprofileの`brain.time_decay_rate`で評価run単位に上書きできる。
Colabの既定値`0.0`は新旧カードを意味類似度だけで比較するA/B用であり、
通常インスタンスのシステム既定値は変更しない。

### ハイブリッド検索（`brain.search_mode`）

`brain.search_mode: hybrid`で、FTS5(trigram)のBM25候補とベクトル候補を
RRFで融合する検索へ切り替えられる（既定は`vector`）。profileから
`bm25_candidates` / `vector_candidates` / `rrf_k` / `bm25_weights` /
`bm25_max_df_ratio` / `bm25_min_weak_df` / `bm25_scan_limit`を上書きできる。
hybridの候補dictでは`score`が**RRFスコア**になり、cosineは`vector_score`へ入る。
`retrieval_source`（vector/bm25/both）と両者の順位も残る。

### Gatekeeper検索文とのDual Query（`brain.search_mode: dual_query`）

`dual_query`は、ユーザーの元発話とGatekeeperが同じ分類応答内で作る
`retrieval_query`を独立してベクトル検索する。既定では各top15をカードIDで
重複排除し、等重みRRF（`rrf_k: 60`）で融合した最大25件から、MemoryProbeが
要求した通常の上位3件だけを返す。固有名・日時・否定・関係を維持しつつ、履歴中の
代名詞や省略された主語を解決したstandalone検索文を使う。検索文が空、不正、または
元発話と同一なら2回目のembeddingを省略し、従来の元発話vector順位へ戻る。

設定は`dual_query_candidates`（既定15）、`dual_query_pool_limit`（既定25）、
`rrf_k`。Gatekeeper呼び出しは従来の1回のままで、検索文をJSONの追加フィールドとして
得るため生成LLMコストは増えない。`retrieval_source`は引き続きvector/BM25の
検索方式を表し、どちらのqueryで見つかったかは`query_source`と両queryのrankへ
分離して記録する。

### QAなしの検索比較準備（`workflow=retrieval_prep`）

検索方式を試すためだけに全QAを生成する必要はない。CLIの`run`へ
`--workflow retrieval_prep`を渡すと、選択sampleのReplayとsessionごとの
Sleeptimeを通常どおり完了した後、QA・公式採点・Semantic Judgeを省略する。

```bash
python -m evals.locomo.cli run \
  --dataset /path/to/locomo10.json \
  --output-dir ./eval_runs/runs \
  --run-id retrieval_conv_30 \
  --sample-ids conv-30 \
  --all-sessions \
  --all-questions \
  --workflow retrieval_prep \
  --profile ./eval_runs/profiles/search.yaml
```

選択質問は`results/retrieval_questions.json`へ、sample ID、instance名、category、
evidenceとともにatomic保存する。checkpointは全Replay/Sleeptimeとmanifestが完成した
時点だけ`completed`になる。`resume`は同じworkflowを復元し、完了済みsessionを
再投入せずmanifestを再生成できる。sample IDを複数指定した場合は既定の
`sample_limit=1`より明示IDが優先される。
比較後に本QAへ進む場合は、このrunを`rerun-qa`のsource（Webでは
`SOURCE_MEMORY_RUN_ID`）に指定する。QA先runのworkflowは自動的に`full`へ戻り、
正本カードを変更せずQAだけを実行する。

Run履歴の「検索だけ比較（offline retrieval replay）」から、回答生成なしで
`bm25` / `vector` / `hybrid` / `dual_query` / `reranked` /
`evidence_rerank` / `hybrid_evidence_rerank` /
`hybrid_evidence_fusion`のRecall@1/3/20を比べられる
（Web UIは`POST /evaluations/runs/retrieval-replay/jobs`で永続ジョブを開始する。
従来の同期APIも互換用に残る）。進捗率・現在のモードと問題ID・直近ログは2秒ごとに
更新され、画面を離れても継続する。停止／同条件での再実行、完了後の集計と問題別
rescue/harm/fallback・候補順位・raw scoreの確認も同じUIで行える。結果はrun直下の
`retrieval_replay.json`にも残る。`bm25`はembeddingを呼ばないので即終わるが、
`vector` / `hybrid`は質問1件につきembeddingを1回呼ぶ。`dual_query`は保存済みの
検索文を優先して2回embeddingし、検索文のない旧runではさらに元runのGatekeeperを
1回呼ぶ。`reranked`はvector embeddingに加えて
選択したRerankerで候補を一括採点する。成果物の問題別`details`には質問、evidence、
元のvector順位、実際に選択した0〜3件、全候補のraw score、エラーを残すため、
救済・悪化した問題の確認とモデル別thresholdの校正に使える。通常runでは
`qa_results.jsonl`を読み、`retrieval_prep` runでは上記manifestを読む。manifestには
QA時のGatekeeper検索文がないため、`dual_query`だけはprofileのGatekeeperを問題ごとに
呼ぶ。

保存済み検索比較が2件以上ある場合は、Webの「検索比較runの横断比較」で
run×modeのRecallと先頭runからのdeltaを確認できる。問題別差分は先頭runをbaseline、
末尾runをtargetとして共通質問だけを比較する。Sample ID、質問集合、候補数limitが
一致しないrunには警告が出るため、異なるsampleの結果は汎化傾向の参考値として読み、
同一条件の直接差分とは扱わない。

`evidence_rerank`は既存のTitle / Tags / Summary embeddingによるvector top N、
`hybrid_evidence_rerank`はBM25とvectorをRRF融合したhybrid top N（既定20）を取り、
`hybrid_evidence_fusion`も同じ候補集合を取り、hybrid / Evidence順位の逆数を
既定0.70 / 0.30で融合する。重みはWeb UIまたは
`--evidence-fusion-base-weight`で変更できる。
候補カードのEpisodeと紐づくRAW会話を同じembedding空間へ追加し、
カードごとの最大cosine（MaxP）で並べ替えてtop 3を選ぶ評価専用モードである。
RAWは既定1800文字・180文字overlapで分割し、EpisodeもRAWもない旧カードだけSummaryへ
fallbackする。文書embeddingは初回にまとめて準備し、質問embeddingとともに
`retrieval_cache/evidence_embeddings.sqlite3`へ保存する。キーにはmodel、profile、
query/document prefix、本文hashを含むため、設定や本文変更後に古いvectorを再利用しない。
質問vectorは一段目のSummary検索と二段目の根拠採点で共有し、同じ質問への重複呼び出しを
避ける。キャッシュはhashとvectorだけを持ち、元runのinstance DB、カード、RAWは
変更しない。

外部Embedding ConnectionではEpisode / RAW本文が通常のembedding入力として送られる。
API keyはConnection認証にだけ使われ、cacheや成果物へ保存しない。一方、問題別レビューを
可能にするため`retrieval_replay.json`には選択根拠のpreview（最大600文字）を保存する。
集計とUIはembedding準備の進捗、cache hit/miss/write、完了／fallback、追加latency、
各モードの一段目top3からのrescue/harmを分けて表示する。

Webコンソールの「検索設定（Dual Query / Hybrid / Reranker）」から
`search_mode` / `retrieval_execution` / `injection_policy` を選べる。
`hybrid`のときだけ`bm25_candidates` / `vector_candidates` / `rrf_k` /
`bm25_max_df_ratio`の入力欄が出て、profile YAMLの`brain`・`memory_probe`
セクションへ書き込まれる（`vector` runのprofileにBM25キーは残さない）。
`dual_query`では各query候補数・pool上限・RRF kをprofileへ書く。run履歴と比較表には
検索文生成率、original/rewrite Recall@3、rescue/harmも並ぶ。

### Memory Reranker（`reranker`）

任意の`reranker` profileセクションを有効にすると、純粋vector検索の上位候補
（既定20件）を並べ替え、最大3件を通常のRAG候補として注入する。推奨経路は
生成を行わない`cross_encoder`で、質問と各カードの`title` / `summary` /
`episode` / `source_date`をペアにして1バッチで関連度スコアへ変換する。

許可済みモデルは日本語対応の
`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`と
`Alibaba-NLP/gte-multilingual-reranker-base`。依存は通常起動に含めず、使う環境だけ
`pip install -r requirements-reranker.txt`で導入する。モデルは初回利用時に読み込み、
process内で再利用する。`score_threshold`を設定した場合は条件を満たすカードが
なければ0枚を返し、Deep Searchで迂回せず注入を見送る。raw scoreは確率とは
限らないため、しきい値はoffline評価でモデルごとに校正する。

```yaml
reranker:
  enabled: true
  engine: cross_encoder
  model_name: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
  candidate_limit: 20
  max_candidate_chars: 1600
  batch_size: 20
  device: auto
  score_threshold: null
```

旧`llm` engineも比較再現用に残す。こちらは候補を不透明ラベル付きJSONとして
Connectionへ送り、厳格JSON Schemaとローカル検証を通す。不正応答・接続失敗時は
どちらのengineも元のvector順位へフォールバックする。

v1は`search_mode: vector`専用で、Hybrid / Dual Queryとの同時利用はWeb/APIで拒否する。
無効または未設定なら従来挙動と同一で、本番instanceでも同じ任意セクションを使える。
診断には`vector_candidate_ids`（元順位）と`effective_candidate_ids`（注入順位）、
engine、完了／fallback、追加レイテンシ、LLMの場合はusageを別々に保存する。

検索の実行と注入判定は`memory_probe.retrieval_execution`（既定`always`）と
`memory_probe.injection_policy`（既定`intent_gated`）で独立に制御する。
`always`では`need_intent=null`の問でもQuick Retrievalが走るため、
**`rag_trigger_rate`はもう検索実行率ではない**。実行率は
`search_execution_rate`、注入率は`memory_injection_rate`（`rag_trigger_rate`と同値）
を見る。

scorerが出す検索系の指標:

| 指標 | 分母 | 内容 |
|---|---|---|
| `search_execution_rate` | 全問 | Quick Retrievalを実行した割合 |
| `retrieval_candidate_rate` | 全問 | 候補が1件以上あった割合 |
| `memory_injection_rate` | 全問 | promptへ注入した割合（=`rag_trigger_rate`） |
| `retrieval_recall_at_1/3/20` | oracleカードがある問 | 上位k候補の`source_files`がevidenceターンを覆う割合 |
| `vector_only_recall_at_3` | 同上 | BM25を外した対照値 |
| `bm25_rescue_rate` | 同上 | 融合top3がベクトル単独top3を上回った割合 |
| `reranker_completion_rate` / `reranker_fallback_rate` | Reranker実行問 | 構造化判定の完了率／元vector順位へ戻した割合 |
| `reranker_rescue_rate_at_3` / `reranker_harm_rate_at_3` | oracleカードがあり判定完了した問 | リランク後top3が元vector top3より改善／悪化した割合 |
| `reranker_latency_ms_p50/p95` | Reranker実行問 | Reranker呼び出しだけの追加レイテンシ |
| `evidence_fusion_completion_rate` / `evidence_fusion_fallback_rate` | Fusion実行問 | Episode/RAW採点の完了率／通常hybrid順位へ戻した割合 |
| `evidence_fusion_rescue_rate_at_3` / `evidence_fusion_harm_rate_at_3` | oracleカードがありFusion完了した問 | Fusion top3が元hybrid top3より改善／悪化した割合 |
| `evidence_fusion_latency_ms_p50/p95` | Fusion実行問 | Evidence採点を含むFusion追加レイテンシ |
| `evidence_fusion_cache_hit_rate` | Fusionのquery/document embedding参照 | run内永続cacheで再利用できた割合 |
| `retrieval_latency_ms_p50/p95` | 実行した問 | 検索のみのレイテンシ（embedding呼び出しを含む） |
| `bm25_short_term_hit_rate` | 実行した問 | 2文字CJK語のLIKE補助候補が入った割合 |

`evidence_retrieval_rate`は**注入されたカード**で測るので注入policyの影響を受ける。
ランキング自体の良し悪しは`retrieval_recall_at_k`で見る。

`brain.search_mode: hybrid_evidence_fusion`は通常のhybrid top-N（既定20）だけを
Episode/RAWのMaxP cosineで再評価し、hybrid 0.70 / Evidence 0.30の逆順位を融合して
上位3件を注入する。質問vectorは一段目と共有し、文書vector/hashだけをrun単位の
SQLite cacheへ保存する。RAW本文は保存しない。Evidence読込・embedding・cacheの
いずれかが失敗した問は回答処理を止めず、元hybrid順位へfallbackする。

評価profileの`context_levels.levels`では、`current_time`、`mid_term`、
`session_digest`、`rag`をそれぞれ`high` / `'off'`にできる。RAGを完全に止める
場合は、prompt注入を止める`rag: 'off'`に加えて`brain.use_rag: false`で検索自体も
止める。Colab Parametersセルはこの4項目をbooleanで公開し、生成YAMLへ両方の
RAG設定を正しく書く。手書きYAMLの`off`はYAML 1.1でbooleanに解釈されるため、
文字列`'off'`としてquoteする。

`chat`、`gatekeeper`、`summary`、`knowledge`は、それぞれ独立した
`generation_config.temperature`を持てる。chatは最終回答、gatekeeperは検索判断、
summaryとknowledgeはSleeptime中のdigest/card生成へ作用する。

## 同じカードでQAだけ再実行

`rerun-qa`は、既存runの正本instanceを新しいrunへ複製し、ReplayとSleeptimeを
skipしてQAだけを0問目から実行する。

```bash
python -m evals.locomo.cli rerun-qa \
  --source-run ./eval_runs/qwen3_14b_colab_v16 \
  --dataset /path/to/locomo10.json \
  --output-dir ./eval_runs \
  --run-id qwen3_14b_colab_v16_no_time \
  --all-questions \
  --profile /path/to/qa-ablation.yaml
```

安全条件と性質は次のとおり。

- 元runは`qa_mode=independent`でなければならない。独立QAだけが正本instanceを
  post-Sleeptimeのまま不変に保つ。
- datasetのSHA-256、対象sessionのReplay/Sleeptime checkpoint、正本の
  `short_term_json`が空であることを検証する。
- 元runには書き込まない。カードDBを含むinstanceとReplay/Sleeptime logを
  新runへcopyし、新しいQA結果・Trace・checkpointを作る。
- copyしたinstanceのLoCoMo回答用System Instructionは現在の
  `qa_prompt_version`へ更新する。カード・RAW・active nodeは変更しない。
- `run_config.json`の`memory_reused_from_run_id`がカードの出所を示す。
- QA-only再実行ではchat/gatekeeper temperatureとcontext切替は変化するが、
  summary/knowledge temperatureは既に完成したdigest/cardを作り直さないため
  結果へ作用しない。
- datasetを移動した場合だけ`--dataset`で新しい場所を指定できる。元run manifestの
  SHA-256と一致しないファイルは拒否する。
- 再利用runのresume時にmemory checkpointが欠落・不完全なら、二重投入を避けるため
  Replay/Sleeptimeへfallbackせず停止する。

Colabでは`SOURCE_MEMORY_RUN_ID`へ元run IDを設定し、`RUN_ID`を新しい値に変える。
空文字のままなら通常のReplay → Sleeptime → QAを行う。sample/session範囲は元runの
カード集合に固定され、question範囲だけを変更できる。

## Stage 3（memory_nodes）の ON/OFF 評価

Stage 3 の効果測定は「完全に同じ knowledge card 集合の上で node 層の有無だけを
変える」clone A/B を正式手順とする（Stage 2 の LLM 出力揺れを排除するため、
full replay を 2 本回す方式は使わない）。

```bash
# 1. baseline source run（Stage 3 は既定 OFF のまま Replay/Sleeptime まで完了）
python -m evals.locomo.cli run --dataset ... --output-dir ./eval_runs \
  --run-id stage3-source --qa-mode independent --all-questions

# 2. OFF clone: 同一カードで node 無し QA
python -m evals.locomo.cli rerun-qa --source-run ./eval_runs/stage3-source \
  --run-id stage3-off --profile evals/locomo/profiles/stage3_off.example.yaml

# 3. ON clone: カード同一性検証 → stage3-bootstrap でキュー drain → node 注入 QA
python -m evals.locomo.cli rerun-qa --source-run ./eval_runs/stage3-source \
  --run-id stage3-on --stage3-bootstrap \
  --profile evals/locomo/profiles/stage3_on.example.yaml
```

Colab NotebookではParametersセルをフォーム表示し、`RUN_ID`、
`SOURCE_MEMORY_RUN_ID`、評価範囲、主要パスを右側の入力欄から変更できる。
`RUN_MODE`は次のプルダウンから選ぶ。

| `RUN_MODE` | 動作 |
|---|---|
| `standard` | 通常評価。source IDが空ならReplayから実行し、指定時は従来どおりカードを再利用してQAだけ再実行 |
| `stage3-full` | source不要の単一run。各セッションでReplay → Stage 2 → Stage 3を実行し、そのrunで生成したnodeを最終QAへ注入する。実運用相当の統合評価であり、同一カードA/Bではない |
| `stage3-source` | 正式A/Bのpost-Stage 2正本を作成。Stage 3は明示OFF、source IDは空、`QA_MODE=independent`必須 |
| `stage3-off` | sourceの同一カードをcloneし、node無しでQA。source ID必須 |
| `stage3-on` | 同じsourceをcloneし、`--stage3-bootstrap`とnode注入を自動で有効化してQA。source ID必須 |

OFF/ONには異なる`RUN_ID`と同じ`SOURCE_MEMORY_RUN_ID`を設定する。
ローカルモデル向けに`STAGE3_BATCH_SIZE`、安全上限として
`STAGE3_BOOTSTRAP_MAX_CARDS`もフォームから変更できる。

同じパラメータはButly Web Consoleの`📊`画面からも設定できる。Web版は
Notebookの評価ロジックを移植せず、生成profileと引数を既存CLIへ渡す。
CLIの進捗を永続ログから表示し、停止後はcheckpointからresumeできる。保存済み
runの主要指標と問題別deltaも画面上で比較できる。API・状態遷移・保存先の詳細は
[LoCoMo Evaluation Web Console](evaluation_web_console.ja.md)を参照。

性質と artifact:

- clone 直後に `knowledge_cards` の id と canonical content hash
  （`butly_core/core/card_content.py` の §5.2 正規化）を source と照合し、
  1 件でも不一致なら QA 前に失敗終了する。結果は run 直下の
  `card_identity.json` に記録される（OFF/ON とも同じ source と照合するため、
  両者のカード集合は推移的に同一）。
- `--stage3-bootstrap` は ON 側専用。bootstrap の統計は
  `results/stage3_bootstrap_log.jsonl` に出力され、`status=completed` 以外
  （partial 含む）は A/B の腕として無効なので失敗終了する。bootstrap 後にも
  カード集合の不変を再検証する。
- ON bootstrap中にColabが切断され、全sampleの完了証跡
  `card_identity.json`が確定していない場合、`resume`は部分nodeのままQAへ進まず
  明示的に失敗する。新しい`RUN_ID`でON armを再実行する。
- bootstrap の clock は QA と同じ「最終会話日の翌日」を注入する。
- node 注入は `memory.knowledge_maturation_enabled=true`（stage3_on profile）
  で有効になる。自動 Key Memory 反映はこの評価では常に OFF。
- 比較指標: QA スコア（既存 scorer）、`card_identity.json` の件数・digest、
  `stage3_bootstrap_log.jsonl` の node 生成数・失敗数・LLM 呼び出し数。

### per-session で Stage 3 を走らせる統合テスト経路

Notebookの`stage3-full`（または通常の `run` に `stage3_on` profile）では、
sourceをcloneせず、profile の `sleeptime` セクションが
再帰マージで適用され、SleeptimeRunner が Stage 2 成功後に Stage 3 を実行する。
Stage 3 の clock には session の元日時を注入する（QA 時だけ設定される
`BUTLY_CHRONOS_NOW` に依存しない）。`sleeptime_log.jsonl` には
`stage_3_status` / `stage_3_reviewed_cards` / `stage_3_created_nodes` /
`stage_3_linked_sources` / `stage_3_failed_cards` / `stage_3_llm_calls` /
`stage_3_prompt_tokens` / `stage_3_completion_tokens` が Stage 1/2 と分離して
記録され、Stage 3 の失敗が Stage 2 の成功に紛れ込まない。
この経路は、実運用と同じく記憶蓄積中にnodeを生成して最終QAで利用する統合評価用。
Stage 3単独の正式な精度 A/B は上記clone方式を使う。

## `independent`: 独立QA

目的は、**すべての質問を完全に同じpost-Sleeptime状態から評価すること**。

```mermaid
flowchart TD
    A["正本instance<br/>全session + Sleeptime完了状態 P0"]
    A --> B["一時領域へbaselineを1回copytree"]
    B --> C["Question 1前:<br/>baseline → active instanceへcopytree"]
    C --> D["新しいRuntimeでQ1を実行"]
    D --> E["Q1/Answerはactiveだけを変更"]
    E --> F["結果・Traceをrun_dirへ永続化"]
    F --> G["Question 2前:<br/>activeを削除し baselineから再copy"]
    G --> H["新しいRuntimeでQ2を実行"]
    H --> I["Q2はQ1を一切見ない"]
    I --> J["以後、各質問でresetを繰り返す"]
    J --> K["全質問後、一時領域を削除"]
    A -. "QAでは変更されない" .-> K
```

一時領域の概念的な構成は次のとおり。

```text
/tmp/butly-locomo-qa-<instance>-*/
├─ baseline/<instance>/                 # 正本から最初に1回コピー
└─ active/butly_core/instances/<instance>/
                                          # 各質問の直前にbaselineから再作成
```

重要な性質:

- 正本instanceはQAで変更されない。
- Q1の質問・回答・session stateはQ2へ渡らない。
- 各質問でinstance cacheも持ち越さないよう、新しいRuntimeを作る。
- QA中の会話保存先は一時active instanceであり、次のresetで破棄される。
- `qa_results.jsonl`と質問別Traceだけはrun directoryへ永続化される。
- コストは正本→baselineの初回コピーに加え、質問ごとのbaseline→activeコピー。

このモードはバージョン間の回答性能比較に向く。質問順序による有利・不利を除去できる。

セッション数による影響を切り分ける場合は、モデル・profile・質問範囲を固定し、
まず`--session-limit 3 --question-limit 10`、次に
`--all-sessions --question-limit 10`を別run IDで実行する。前者は旧評価条件との
比較、後者は全会話を記憶したときの検索スケーリング確認になる。

## `sequential`: 継続QA

目的は、**実運用のようにQA会話を同じinstanceへ蓄積して耐久性を測ること**。

```mermaid
flowchart TD
    A["正本instance<br/>全session + Sleeptime完了状態 P0"]
    A --> B["Q1直前のrecovery pointを作成"]
    B --> C["正本instanceでQ1を実行"]
    C --> D["Q1/Answer・session stateを正本へ保存<br/>状態 P1"]
    D --> E["結果保存 → checkpoint更新 → recovery point削除"]
    E --> F["Q2直前のrecovery pointを作成"]
    F --> G["状態P1の正本instanceでQ2を実行"]
    G --> H["Q2はQ1/Answerをrecent historyとして参照可能<br/>状態 P2"]
    H --> I["結果保存 → checkpoint更新 → 次の質問"]
```

重要な性質:

- ReplayとSleeptimeに使った正本instanceへ、そのままQAを保存する。
- Q1の質問・回答・session stateがQ2以降へ残る。
- QA間に明示的なSleeptimeは実行しない。
- ChatServiceの通常処理として`short_term_json`保存とmemory maintenanceは行われる。
- 各質問前に正本instance、`qa_results.jsonl`の長さ、Traceを含むrecovery pointを作る。
- 中断時にcheckpoint未commitの質問があれば、resume時に質問前へrollbackする。

このモードは、100回程度の質問が連続したときの履歴汚染、topic/session stateの変化、
RAG判断のドリフトなどを含む運用耐久評価に向く。

## 2モードの比較

| 観点 | `independent` | `sequential` |
|---|---|---|
| 各質問の開始状態 | 毎回同じpost-Sleeptime baseline | 直前QAまで蓄積した状態 |
| QAの保存先 | `/tmp`の一時active instance | run directoryの正本instance |
| 前の質問・回答 | 見えない | recent historyとして見える |
| 正本instance | QAでは不変 | QAごとに変化 |
| Runtime | 質問ごとに新規 | 同じRuntimeを継続利用 |
| QA間Sleeptime | なし | なし |
| 主な用途 | モデル・バージョン比較 | 実運用・連続耐久試験 |
| 主な追加コスト | 質問ごとのinstance copy | 質問ごとのrecovery copy |

## evidence=0 の内訳（`analyze-evidence`）

`evidence_coverage` は「注入されたカードが根拠ターンを含むか」なので、0 のときに
検索経路のどこで落ちたかまでは分からない。`analyze-evidence` は scorer が
`scores.json` に書いた `search_executed` / `oracle_available` / `recall_at_20` /
`recall_at_3` を上流から順に見て、**最初に落ちた場所ひとつ**へ分類する。

```bash
venv/bin/python -m evals.locomo.cli analyze-evidence --run-dir eval_runs/runs/<run-id>
venv/bin/python -m evals.locomo.cli analyze-evidence \
  --run-dir eval_runs/runs/<run-id> --baseline eval_runs/runs/<比較対象>
```

| 分類 | 条件 | 効く打ち手 |
|---|---|---|
| `no_search` | `search_executed` が false | Gatekeeper / `need_intent` |
| `no_card` | `oracle_available` が false（根拠ターンを含むカードが1枚も無い） | カード化（Sleeptime 抽出） |
| `not_in_candidates` | `recall_at_20` が 0 | embedding / 検索クエリ |
| `rank_below_injection` | `recall_at_20` > 0 かつ `recall_at_3` が 0 | ランキング / リランカー |
| `dropped_after_ranking` | `recall_at_3` > 0 なのに `evidence_coverage` が 0 | 注入 policy / 注入枠 |
| `unclassified` | 判定に要るキーが `scores.json` に無い | `score` を再実行する |

- category 5（adversarial）は「No information」が正解なので、根拠を引けないこと
  自体は失敗ではない。既定では分母から外し、件数だけ参考表示する
  （`--include-adversarial` で含める）。
- 分類は排他。1問が複数の原因を持っていても二重計上しない。
- 検索指標が入る前の run は全問 `unclassified` になる。キーの欠損と `false` を
  区別しているので、古い run が `no_search` に化けることはない。
- `scores.json` しか読まないので run の再実行は不要。`--json` で機械可読出力、
  `--list N` で bucket ごとの question_id 表示件数を変えられる。

## Run directoryと永続artifact

```text
<output-dir>/<run-id>/
├─ run_config.json
├─ dataset_manifest.json
├─ environment.json
├─ workspace/
│  └─ butly_core/instances/<instance>/   # 正本instance
├─ checkpoints/
│  ├─ checkpoint.json
│  └─ sequential_qa/                     # sequential実行中だけのrecovery point
├─ snapshots/
│  └─ <instance>/<session>/
│     ├─ before_sleeptime/
│     └─ after_sleeptime/
├─ results/
│  ├─ replay_log.jsonl
│  ├─ sleeptime_log.jsonl
│  └─ qa_results.jsonl
└─ traces/
   └─ <sample>/<question-id>.json
```

独立QAの一時instanceはこのtree外のOS temp directoryに作られ、全質問後に削除される。

## CLI / Colabの進捗表示

`run`、`resume`、`rerun-qa`は、長いモデル呼び出しの開始前と完了後に、次の形式で進捗を
stderrへ即時出力する。Colabの実行セルは子プロセスのstderrをそのまま表示するため、
Notebook側で出力を読み取る処理は不要。

```text
[LoCoMo  41.2%] [11/24] sleeptime | conv-26 session_6 completed
```

全体率は経過時間の予測ではなく、完了した処理件数による簡易指標。

- Replay 1 session = 1 unit
- Sleeptime 1 session = 1 unit
- QA 1 question = 1 unit
- 上記を0〜90%へ換算
- 採点完了で96%、`summary.md`生成完了で100%

各unitは同じ重みなので、モデルやsession長によって実時間とのずれは生じる。
`resume`ではcheckpoint済みunitを初期完了数へ含めるため、表示は中断前の位置に近い
割合から再開する。進捗はstderr、従来の最終結果JSONはstdoutの最終行に出力する。

## Checkpointの読み方

`checkpoints/checkpoint.json`は、各サンプルについて次を記録する。

- `replayed_sessions`: 元会話の`short_term_json`保存までcommit済み
- `sleeptime_completed`: そのsessionのSleeptimeとafter snapshotまでcommit済み
- `qa_completed`: 結果保存後にcommit済みの質問数
- `status`: `replaying` / `qa` / `completed`

したがって、あるsessionが`replayed_sessions`にはあるが`sleeptime_completed`にない場合、
そのsessionは「Replay済み、Sleeptime実行中または未完了」を意味する。QAはまだ始まらない。
