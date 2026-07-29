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
- `RUN_MODE`: `standard` / `stage3-full` / `stage3-source` /
  `stage3-off` / `stage3-on`
- `SOURCE_MEMORY_RUN_ID`
- `QA_MODE`、locale
- sample / session / questionの全件または上限
- Current Time / Mid-term / Session Digest / RAGのON/OFF
- RAG source、RAW top-k、RAW文字数上限、時間減衰
- Stage 3 batch size / bootstrap最大カード数
- chat / gatekeeper / summary / knowledge / embeddingの
  Connection→モデル割り当て
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

### 日本語対話A/B

`data/ja_dialogue_ab_prompts_v1.json`には、記憶seed 10件と次の30プロンプトを置く。

- 記憶が回答に必須: 10件
- 記憶が不要な通常会話: 10件
- 記憶が補助的に有効: 10件

runnerはseedをReplayしてSleeptime（任意でStage 3を含む）を一度だけ実行する。
生成済みの同一instanceから、各プロンプトごとに使い捨てcloneを作り、
`retrieval_execution=always`のまま次の2 armを実行する。

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

各プロンプトは独立しており、先行プロンプトと回答を次のプロンプトへ蓄積しない。
回答はtemperature 0.0を初期値とし、Webフォームから明示変更できる。

自動集計はRAG発火率、検索実行率、平均prompt tokens、latency、記憶必須問の
対象語recall、記憶不要問でのseed固有語の持ち出し率を保存する。後者2つは
機械的なproxyであり、自然さ・過剰な個人化の最終判断は画面の回答差分で行う。

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
| `POST` | `/evaluations/jobs` | 新しい評価を開始 |
| `GET` | `/evaluations/jobs` | ジョブ一覧 |
| `GET` | `/evaluations/jobs/{job_id}` | 状態・進捗 |
| `POST` | `/evaluations/jobs/{job_id}/stop` | subprocessと子processを停止 |
| `POST` | `/evaluations/jobs/{job_id}/resume` | 既存runをCLI `resume`で再開 |
| `GET` | `/evaluations/jobs/{job_id}/log` | 末尾ログ |
| `GET` | `/evaluations/runs` | 保存済みrunと主要指標 |
| `POST` | `/evaluations/runs/compare` | 2〜8 runの指標・問題別比較 |
| `POST` | `/evaluations/dialogue-ab/jobs` | 日本語対話A/Bを開始 |
| `GET` | `/evaluations/dialogue-ab/runs` | 日本語対話A/Bの履歴 |
| `GET` | `/evaluations/dialogue-ab/runs/{run_id}` | policy・プロンプト別結果 |

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

履歴は保存先直下の`*/run_config.json`を走査する。`scores.json`があれば次を表示する。

- official overall
- question count、exact match、answer containment
- evidence retrieval rate、**RAG発火率、分類器fallback率**
- 平均latency
- prompt / completion token
- knowledge card作成数、Sleeptime failure
- source run、QA mode、評価範囲

比較APIは先頭runをbaseline、末尾runを主比較対象とし、共通
`question_id`ごとに`official_score`のdeltaと各predictionを返す。
画面はdelta昇順で表示するため、悪化した問題を先に確認できる。

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
