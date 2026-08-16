# LoCoMo Evaluation Web Console

🌐 **日本語** | [English](evaluation_web_console.md)

Butly Web Consoleから、既存のLoCoMo評価と日本語対話A/Bをバックグラウンド
ジョブとして開始・停止・再開し、結果を比較するための仕様。

評価ロジックの正本は引き続き`evals/locomo/`であり、Web APIと画面はCLIを
subprocessとして呼ぶ薄い管理層である。Replay、Sleeptime、QA、checkpoint、
採点の実装をWeb用に複製しない。

## 画面

ホームの`📊`から、次の4セクションを切り替える。選択中のセクションだけを描画し、
非表示フォームのモデル候補や履歴は取得しない。

| セクション | 用途 |
|---|---|
| LoCoMo評価 | Colab Parameters相当のrun設定とモデル割り当て |
| 日本語対話A/B | `intent_gated`と`candidates`の本番対話向け比較 |
| ジョブ | 進捗・phase・ログ、停止、checkpointからの再開 |
| LoCoMo履歴・比較 | 保存済みrunの指標一覧、2〜8 runの比較、問題別delta |

ジョブセクションの2秒更新はStreamlit fragment内だけで行い、評価フォーム全体を
再実行しない。モデル候補は10分キャッシュされ、「モデル一覧を更新」を押した場合
だけ明示的に再取得する。

新規評価フォームは次を扱う。

- `RUN_ID`
- 実行内容: 通常評価、または`retrieval_prep`（Replay/Sleeptime後にQAを省略）
- `RUN_MODE`: `standard` / `stage3-full` / `stage3-source` /
  `stage3-off` / `stage3-on`
- `SOURCE_MEMORY_RUN_ID`
- `QA_MODE`、locale
- datasetから取得したsample IDの複数選択（未選択時は先頭からの件数指定）
- session / questionの全件または上限
- Current Time / Mid-term / Session Digest / RAGのON/OFF
- RAG source、RAW top-k、RAW文字数上限、時間減衰
- 検索方式。既定の`vector`、BM25併用の`hybrid`に加え、
  `dual_query`では元発話とGatekeeperが作った検索文を各15件検索し、
  重複排除・RRF融合した最大25件から通常の上位3件を注入する
- Stage 3 batch size / bootstrap最大カード数
- chat / gatekeeper / summary / knowledge / embeddingの
  Connection→モデル割り当て
- 任意のSemantic Judge用Connection→モデルと出力上限
- 任意のMemory Reranker。推奨のローカルCross-Encoder
  （mMiniLMv2 / GTE multilingual）または比較用LLM Connectionを選び、候補数
  （既定20）、候補本文上限、batch size、任意の関連度しきい値を設定する。
  vector候補を並べ替え、0〜3件を注入する（Hybrid / Dual Queryとは併用不可）
- Embedding prefix 規約（既定 `auto` = モデル名から推定。
  詳細は [memory_lifecycle.ja.md](memory_lifecycle.ja.md#embedding-プロファイルモデル別の入力規約)）
- generation temperatureとGatekeeper出力上限
- 埋め込み不一致の承知実行（`SOURCE_MEMORY_RUN_ID` 指定時のみ表示）

ConnectionとAPIキーは通常のWeb Console設定を共有する。フォームはAPIキー本体を
受け取らず、評価subprocessがBackendプロセスの環境変数を継承する。

新規評価フォームは、最後に開始したWeb評価jobの正規化済みrequestを初期値として
自動的に引き継ぐ。対象はdataset、run mode、再利用元、評価範囲、RAG・検索・Stage 3
設定、モデル割り当て、temperature、出力上限である。job recordは永続化されるため、
StreamlitまたはBackendの再起動後も復元できる。重複を避けるため`RUN_ID`だけは
末尾が`_vNN`なら次番号、それ以外は時刻ベースの候補へ更新する。
`allow_embedding_mismatch`は危険な承知操作なので引き継がず、runごとにOFFへ戻す。

`retrieval_prep`は選択sampleのReplayと各sessionのSleeptimeだけを実行する。
Chat回答、公式採点、Semantic Judgeは呼ばず、選択したLoCoMo質問を
`results/retrieval_questions.json`へatomic保存する。完了runは履歴で
`retrieval_ready`となり、「検索だけ比較」の対象にできる。これにより、例えば
`conv-30`だけで記憶を作り、複数の検索方式を試してからQA費用を使うか判断できる。
採用後は同じrunを`SOURCE_MEMORY_RUN_ID`に選び、正本カードを複製してQAだけを
実行できる。
sample IDは開始時にもBackendでdatasetと照合し、未知IDや重複を拒否する。

LoCoMo履歴の「検索だけ比較（offline retrieval replay）」では`dual_query`、
`reranked`、`evidence_rerank`、`hybrid_evidence_rerank`、
`hybrid_evidence_fusion`を選べる。
回答生成なしで元vector順位とリランク後のRecall@1/3/20、top3のrescue/harm、
完了／fallback、追加レイテンシ、LLM使用時のトークン量を保存・表示する。
検索比較は永続ジョブとしてバックグラウンドで動き、進捗率、現在のモード・問題ID、
直近ログを2秒ごとに更新する。画面を離れても処理は継続し、停止・同条件での再実行も
可能である。完了後はモード別集計に加え、問題別のrescue/harm/fallback、
元vector top3、選択top3、raw score、エラーを表示する。ジョブと
`retrieval_replay.json`はBackend再起動後も復元される。
履歴の「検索比較runの横断比較」では、保存済み成果物がある2〜8 runを選び、
run×modeのRecall@1/3/20と先頭runからのdeltaを表示する。先頭runと末尾runは
共通問題ごとの改善・悪化も確認できる。Sample ID、質問集合、候補数limitが異なる
runは同条件比較とみなさず、UIに警告を表示する。
通常runは`qa_results.jsonl`、`retrieval_prep` runは固定済みの
`retrieval_questions.json`を質問入力として使う。後者の`dual_query`はQA時の保存済み
検索文を持たないため、元runのGatekeeper設定で検索文を生成する。
本評価フォームでRerankerを有効にしたrunは、同じ処理がQA経路にも入り、公式／
Semantic Judgeの回答品質と検索指標を同じrunで確認できる。

本評価フォームの`Search mode`でも`hybrid_evidence_fusion`を選べる。通常hybridの
top N（既定20）だけをEpisode / RAWで遅延再評価し、既定0.70 / 0.30で順位融合して
top 3を注入する。質問vectorは一段目と共有し、run内の永続cacheはvectorとhashだけを
保存する。失敗時は通常hybrid順位へ戻る。履歴／比較にはFusionの完了・fallback、
元hybrid top3からのrescue/harm、p95遅延を表示する。

`evidence_rerank`はvector top N、`hybrid_evidence_rerank`はBM25とvectorを
RRF融合したhybrid top N（既定20）を候補集合とする評価専用モードである。
`hybrid_evidence_fusion`は同じ候補に対し、hybrid順位とEvidence順位の逆数を
既定0.70 / 0.30で重み付き融合する。hybridの有効順位を保持しながら2・3位を
Episode / RAWで補正するモードで、重みはUIから変更できる。offline replayでは
候補比較だけを行い、本評価では同じ共通順位融合を実際のQA検索へ適用する。
各候補に紐づくEpisodeとRAW会話チャンクを
同じembeddingモデルで再採点する。カードごとに最も高い根拠cosine（MaxP）を使って
並べ替え、top 3を選ぶ。初回は文書embeddingの準備フェーズがあり、ベクトルとhashを
`retrieval_cache/evidence_embeddings.sqlite3`へ保存する。モデル、prefix、本文hashが
変われば自動的に別キャッシュになる。質問vectorは一段目と二段目で共有し、同じ質問を
二重にembeddingしない。元runのinstance DB、カード、RAWファイルは変更しない。

外部Embedding Connectionを選んだ場合、Episode / RAW本文は通常のembedding入力として
そのConnectionへ送信される。API keyは通常のConnection認証にだけ使い、キャッシュや
評価成果物には保存しない。SQLiteキャッシュには本文を保存しないが、レビュー用の
`retrieval_replay.json`には選択された根拠の先頭最大600文字を保存・表示する。

`dual_query`は、元発話top15と検索文top15を等重みRRFで融合し、重複排除後の
診断プールを最大25件に制限する。検索文は通常のGatekeeper呼び出しのJSONへ
同時に含めるため、本評価中に生成LLM呼び出しは増えず、embeddingだけが1問2回に
なる。新しいrunのoffline replayはQA時に保存した検索文を再利用する。検索文を
保存していない旧runだけは、元runのGatekeeper設定で問題ごとに1回生成する。
画面にはoriginal／retrieval-query／融合後のRecall@3、top3 rescue/harm、検索文、
両ランキングと融合候補IDを表示する。

Cross-Encoderは質問と文字数上限を適用した候補カードをBackend内で採点し、外部
Connectionへ送らない。比較用LLM engineの場合だけ、Reranker Connectionへ現在の
質問と候補カードのtitle/summary/episode/source_dateを送る。インスタンスDB全体や
APIキーはprompt・評価artifactへ含めず、APIキーは通常のConnection認証にだけ使う。

### 日本語対話A/B

`data/ja_dialogue_ab_prompts_v1.json`には、記憶seed 10件と次の30プロンプトを置く。

- 記憶が回答に必須: 10件
- 記憶が不要な通常会話: 10件
- 記憶が補助的に有効: 10件

runnerはseedをReplayしてSleeptime（任意でStage 3を含む）を一度だけ実行する。
生成済みの同一instanceから、各プロンプトごとに使い捨てcloneを作り、
同じ検索条件で次の2 armを実行する。

#### 既存インスタンスを記憶の種にする

合成seedの代わりに、**実インスタンスを複製して種にできる**。本番の記憶量・
System Instruction・digestをそのまま使うため、トークンや持ち出しの数字が実運用に
近くなる。フォームの「記憶の種」で選ぶか、datasetへ次を書く。

```json
"memory_source": {"type": "instance", "name": "Jarvis"}
```

- 複製元は**読み取りのみ**。run workspace側のコピーだけを操作する
- Sleeptimeは実行しない（カード・digestは複製時点で固定）
- `debug_logs` / `traces` / `*.log`は複製しない
- 保存済みのembeddingベクトルをそのまま使う。別モデルで比較したいときだけ
  「カードを再embedding」をONにする（`--reembed`）
- `embedding_meta`が空のインスタンスは「どのモデル製か不明」と警告を
  `seed_instance.json`へ記録する。検索が生きているかは記憶必須問の結果で確認する

この形式のdatasetは`prompts`をカテゴリ名キーの辞書でも書ける
（`memory_required` / `memory_optional` / `memory_irrelevant`）。
`expected_memory_behavior`を省略するとカテゴリ既定が入り、根拠カードは
`source_card_id`で指せる。

1. `injection_policy=intent_gated`
2. `injection_policy=candidates`

Webフォームでは検索条件を変更できる。前回のjob requestは次回フォームへ引き継ぐ。

- `search_mode`: `vector` / `hybrid` / `dual_query`
- `retrieval_execution`: `always` / `intent_gated`
- `vector_search_threshold`、Deep Searchの有効・無効
- hybrid時のBM25候補数、ベクトル候補数、RRF k、BM25最大DF比
- dual_query時の各query候補数（既定15）、融合pool上限（既定25）、RRF k
- `vector_search_limit`はarm別に指定できる
  （`intent_gated_vector_search_limit` / `candidates_vector_search_limit`）

policyの効果だけを比較するときは両armのlimitを同じにする。本番設定候補どうしを
比較するときは、たとえば`intent_gated=3` / `candidates=2`のようにarm別指定する。
`retrieval_execution=always`を使う。`intent_gated`へ変えると、分類器が記憶不要と
判定した質問では`candidates` armでも検索・注入されない。

各プロンプトは独立しており、先行プロンプトと回答を次のプロンプトへ蓄積しない。
回答はtemperature 0.0を初期値とし、Webフォームから明示変更できる。

自動集計はRAG発火率、検索実行率、平均prompt tokens、latency、記憶必須問の
対象語recall、記憶不要問でのseed固有語の持ち出し率を保存する。後者2つは
機械的なproxyであり、自然さ・過剰な個人化の最終判断は画面の回答差分で行う。

Semantic Judgeを有効にすると、各プロンプトのA/B表示順を入れ替えて2回判定する。
事実の逆転、部分正解、不要な記憶持ち出しを意味で判定し、左右順によって
結果が変わった問は人手確認対象にする。従来の自動proxyは置き換えない。
NanoGPTなどOpenAI互換Connectionでは、プロンプト指示だけでなく
`response_format=json_schema`のstrict schemaで判定JSONを制約する。

成果物はプロンプト単位のatomic JSONなので、停止・失敗後のresumeは完了済みの
`(policy, prompt_id)`を飛ばす。seed生成中に中断した場合は、部分的な記憶を
使わず専用instanceを作り直す。

### 記憶を再利用するrunの埋め込み整合チェック

`SOURCE_MEMORY_RUN_ID` を指定した run（`rerun-qa`）は、元runのカードと
`embedding_blob` をそのまま使う。埋め込みモデルまたは prefix 規約が当時と
変わっていると、保存済みベクトルと検索クエリが**別空間**になり、例外もログも
出ないまま検索だけが壊れる。

そのため `POST /evaluations/jobs` は開始前に、元runの workspace 内 instance DB の
`embedding_meta` を今回の embedding 設定と突き合わせ、食い違えば 400 で弾く。
**次元が一致していても規約が違えば弾く**（例: prefix 導入前に作られたカード）。

承知の上で走らせる場合は `allow_embedding_mismatch: true`（UI の
「埋め込み不一致を承知で実行する」）を渡す。その run の検索指標は比較に使えない。

## ジョブAPI

legacy Web Console用Backendに次を追加する。

| Method | Path | 動作 |
|---|---|---|
| `GET` | `/evaluations/config` | run保存先、dataset候補、run mode、最後の評価request |
| `GET` | `/evaluations/datasets/samples` | datasetを検証し、sample ID・session数・question数を返す |
| `POST` | `/evaluations/jobs` | 新しい評価を開始 |
| `GET` | `/evaluations/jobs` | ジョブ一覧 |
| `GET` | `/evaluations/jobs/{job_id}` | 状態・進捗 |
| `POST` | `/evaluations/jobs/{job_id}/stop` | subprocessと子processを停止 |
| `POST` | `/evaluations/jobs/{job_id}/resume` | 既存runをCLI `resume`で再開 |
| `GET` | `/evaluations/jobs/{job_id}/log` | 末尾ログ |
| `GET` | `/evaluations/runs` | 保存済みrunと主要指標 |
| `GET` | `/evaluations/runs/{run_id}` | 公式採点と現在有効な問題別Semantic判定・レビュー理由 |
| `POST` | `/evaluations/runs/compare` | 2〜8 runの指標・問題別比較 |
| `POST` | `/evaluations/runs/{run_id}/judge` | 既存LoCoMo runをQA再実行なしで意味判定 |
| `POST` | `/evaluations/runs/retrieval-replay/jobs` | 検索だけの比較を永続ジョブとして開始 |
| `GET` | `/evaluations/runs/{run_id}/retrieval-replay` | 保存済み検索比較の集計・問題別結果 |
| `POST` | `/evaluations/runs/retrieval-replay/compare` | 2〜8 runの保存済み検索比較をモード別・問題別に横断比較 |
| `POST` | `/evaluations/runs/retrieval-replay` | 検索比較を同期実行（互換用） |
| `POST` | `/evaluations/dialogue-ab/jobs` | 日本語対話A/Bを開始 |
| `GET` | `/evaluations/dialogue-ab/runs` | 日本語対話A/Bの履歴 |
| `GET` | `/evaluations/dialogue-ab/runs/{run_id}` | policy・プロンプト別結果 |
| `POST` | `/evaluations/dialogue-ab/runs/{run_id}/judge` | 既存A/B runをQA再実行なしで意味判定 |

ローカルLLM/GPUへの過負荷と同一runへの競合書き込みを避けるため、Web APIが
同時に管理するactive jobは1件だけとする。NanoGPTなど外部APIを使う場合も、
まずは同じ制約を維持する。

### 状態

```text
queued → running → completed
                 ├→ failed
                 └→ stopping → stopped

running ── Backend再起動後にPID不在 ─→ interrupted
stopped / failed / interrupted ── resume ─→ running
```

`stop`はcheckpoint済みのunitを取り消さない。再開は既存
`run_config.json`とcheckpointに対して`python -m evals.locomo.cli resume`
を実行する。run directory作成前に停止・失敗したjobは再開できないため、新しい
`RUN_ID`で開始し直す。

Stage3 ON bootstrapが完了証跡を残す前に停止した場合は、既存CLIの安全規則により
resumeが拒否されることがある。その場合は部分nodeを使わず、新しい`RUN_ID`で
ON armを再実行する。

## subprocessと永続状態

ジョブは`sys.executable -m evals.locomo.cli ...`で起動する。
stdout/stderrは直接ログファイルへ追記するため、Streamlitのrerunでは失われない。
Backend再起動後もPIDとprocess作成時刻を照合し、別processへ誤って停止要求を
送らない。

```text
DATA_DIR/eval_runs/
├─ runs/
│  └─ <run_id>/
├─ dialogue_ab/
│  └─ <run_id>/
├─ jobs/
│  ├─ <job_id>.json
│  └─ <job_id>.log
└─ profiles/
   └─ <job_id>.yaml
```

`eval_runs/`はgitignore対象。ジョブJSONと生成profileにはモデル名・Connection ID・
評価条件を保存するが、APIキーは保存しない。

run成果物と履歴・比較画面の既定参照先は`DATA_DIR/eval_runs/runs/`。
環境変数を設定した場合だけ保存・参照先を変更する。

1. `BUTLY_EVALUATION_OUTPUT_DIR`
2. `DATA_DIR/eval_runs/runs/`

日本語対話A/Bは`BUTLY_DIALOGUE_AB_OUTPUT_DIR`、未設定時は
`DATA_DIR/eval_runs/dialogue_ab/`へ保存する。

dataset候補は`BUTLY_LOCOMO_DATASET`、`data/locomo10.json`、合成mini fixtureから
検出する。任意のdatasetを使う場合は、Backendから読める絶対パスをフォームへ入力する。

## 履歴と比較

履歴は保存先直下の`*/run_config.json`を走査する。通常runは`scores.json`があれば
次を表示する。`retrieval_prep` runは選択sample ID、固定質問数、
`retrieval_ready`状態を表示し、採点済みrunとは区別する。

- official overall
- question count、exact match、answer containment
- evidence retrieval rate、**RAG発火率、分類器fallback率**
- 平均latency
- prompt / completion token
- knowledge card作成数、Sleeptime failure
- source run、QA mode、評価範囲

比較APIは先頭runをbaseline、末尾runを主比較対象とし、共通
`(sample_id, question_id)`ごとに`official_score`のdeltaと各predictionを返す。
画面はdelta昇順で表示するため、悪化した問題を先に確認できる。

### Semantic Judge

LoCoMo公式Token F1は`scores.json`の正本として維持し、意味判定は
`semantic_scores.json`と問題単位のatomic artifactへ分離する。日本語回答、
言い換え、事実の逆転、重要要素の欠落を`correct` / `partial` / `incorrect`に分け、
公式スコアとの不一致をレビュー候補として表示する。問題別画面では`partial`、矛盾、
重要情報欠落、low confidence、公式判定とのpossible false positive / false negativeを
理由付きで絞り込める。通常の判定失敗は0点にせず、`partial`として失敗問だけを
resume時に再試行する。ただし明確なunsupported parameter等、全問で再現する
設定契約エラーはAdapterの安全な1回補正後にrunを即時停止する。

`semantic_scores.json`の`question_set_fingerprint`は、現在の`scores.json`にある質問・
参照回答・予測とJudge設定（prompt versionを含む）から毎回再計算する。一致しない
成果物は`stale`として扱い、過去の集計値と問題別判定を画面・reportから隠す。
QA成果物を編集・再生成した場合はSemantic Judgeを再実行する。

Judge用temperatureは対応モデルで0.0に固定し、非対応またはCapability不明の
モデルでは送信せずProvider公式defaultを使う。`reasoning_effort`を明示しなければ、
Capabilityが公開するdefault、reasoning対応だけ既知なら`medium`、Capability不明なら
Provider公式defaultを使う。回答生成と別系統のモデルを選べる。
OpenAI互換ConnectionではDialogue A/BとLoCoMoそれぞれのstrict JSON Schemaを
APIへ渡し、余分なキーや壊れたJSONを生成段階で防ぐ。戻り値は従来どおり
ローカルでも厳格に再検証する。
LoCoMoでは質問・正解・候補回答、対話A/Bではプロンプト・最小の参照事実・
両回答が選択したJudge Connectionへ送信される。インスタンスDB全体は送信しない。
APIキーは判定promptや評価artifactには含めず、通常のConnection認証にだけ使い、
runへ保存しない（remote Connectionでは認証情報としてprovider endpointへ送る）。
Judge設定は評価専用で、実行用instance configへは混入しない。

### 検索指標の読み方

`evidence retrieval rate` は**全問で割った値**である。RAGが発火しなかった問は
0として数えるため、**検索品質が変わらなくても発火率が下がれば一緒に下がる**。
必ず `rag_trigger` と並べて読む。

`classifier fallback rate` が高いrunは、ContextClassifierが空応答やパース失敗で
倒れて `need_intent` が立たず、RAGが丸ごと不発になっている。画面は 0.2 以上の
runを警告表示する。典型的な原因はGatekeeperがthinkingを出すモデル
（Qwen3等）で、`max output tokens` が小さいこと。分類JSONを書く前に上限へ
達して content が空になる。新規評価フォームは、この組み合わせを検知して
開始前に警告する（推奨 2048 以上）。
