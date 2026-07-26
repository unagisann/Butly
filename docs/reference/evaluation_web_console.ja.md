# LoCoMo Evaluation Web Console

🌐 **日本語** | [English](evaluation_web_console.md)

Butly Web Consoleから、既存のLoCoMo評価CLIをバックグラウンドジョブとして
開始・停止・再開し、過去runのスコアを比較するための仕様。

評価ロジックの正本は引き続き`evals/locomo/`であり、Web APIと画面はCLIを
subprocessとして呼ぶ薄い管理層である。Replay、Sleeptime、QA、checkpoint、
採点の実装をWeb用に複製しない。

## 画面

ホームの`📊`から、次の3タブを開く。

| タブ | 用途 |
|---|---|
| 新規評価 | Colab Parameters相当のrun設定とモデル割り当て |
| ジョブ | 進捗・phase・ログ、停止、checkpointからの再開 |
| 履歴・比較 | 保存済みrunの指標一覧、2〜8 runの比較、問題別delta |

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
| `GET` | `/evaluations/config` | run保存先、dataset候補、run mode |
| `POST` | `/evaluations/jobs` | 新しい評価を開始 |
| `GET` | `/evaluations/jobs` | ジョブ一覧 |
| `GET` | `/evaluations/jobs/{job_id}` | 状態・進捗 |
| `POST` | `/evaluations/jobs/{job_id}/stop` | subprocessと子processを停止 |
| `POST` | `/evaluations/jobs/{job_id}/resume` | 既存runをCLI `resume`で再開 |
| `GET` | `/evaluations/jobs/{job_id}/log` | 末尾ログ |
| `GET` | `/evaluations/runs` | 保存済みrunと主要指標 |
| `POST` | `/evaluations/runs/compare` | 2〜8 runの指標・問題別比較 |

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
├─ jobs/
│  ├─ <job_id>.json
│  └─ <job_id>.log
└─ profiles/
   └─ <job_id>.yaml
```

`eval_runs/`はgitignore対象。ジョブJSONと生成profileにはモデル名・Connection ID・
評価条件を保存するが、APIキーは保存しない。

run成果物の既定保存先は次の優先順位で決まる。

1. `BUTLY_EVALUATION_OUTPUT_DIR`
2. 開発checkoutに`docs/temp/`が存在する場合は`docs/temp/`
3. それ以外は`DATA_DIR/eval_runs/runs/`

dataset候補は`BUTLY_LOCOMO_DATASET`、`data/locomo10.json`、合成mini fixtureから
検出する。任意のdatasetを使う場合は、Backendから読める絶対パスをフォームへ入力する。

## 履歴と比較

履歴は保存先直下の`*/run_config.json`を走査する。`scores.json`があれば次を表示する。

- official overall
- question count、exact match、answer containment
- evidence retrieval rate
- 平均latency
- prompt / completion token
- knowledge card作成数、Sleeptime failure
- source run、QA mode、評価範囲

比較APIは先頭runをbaseline、末尾runを主比較対象とし、共通
`question_id`ごとに`official_score`のdeltaと各predictionを返す。
画面はdelta昇順で表示するため、悪化した問題を先に確認できる。
