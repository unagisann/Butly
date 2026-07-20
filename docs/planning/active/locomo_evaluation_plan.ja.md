# LoCoMoを用いたButly長期会話記憶評価基盤 実装計画書

> **ステータス: 実装済み（Phase 1〜4 完了、Phase 5 実データ試験のみ未実施）**
> **主な実行環境: Google Colab Pro**
> **設計方針: 評価ロジックは環境非依存のCLIとして実装し、Colabは薄い実行フロントエンドとして扱う**
> **対象リポジトリ: Butly**
> **想定配置: `evals/locomo/`**

---

## 1. 背景

Butlyは、短期会話履歴、Session Digest、Mid-term Digest、Recent Snapshot、Knowledge Card、Glossary、Key Memoryなど、複数の記憶層を持つAIコンパニオン基盤である。

現在は、各機能の保存、検索、API、ストリーミング、プロバイダー切り替えなどに対するソフトウェアテストは充実している。一方で、次のようなAIコンパニオンとしての記憶品質は、継続的かつ定量的に測定できていない。

* 長期間の会話から必要な事実を思い出せるか
* 複数セッションに分散した情報を統合できるか
* 古い情報より更新後の情報を優先できるか
* 発生時期や出来事の前後関係を認識できるか
* 存在しない記憶を捏造せず「分からない」と答えられるか
* Sleeptimeによって必要な情報が適切に知識化されるか
* GatekeeperおよびRAGが必要な場面で発火するか
* ローカルLLMでもButlyの記憶構造を扱えるか

これらを検証するため、長期会話記憶ベンチマークであるLoCoMoをButlyへ接続し、会話投入、Sleeptime、質問応答、採点、成果物保存までを自動化する。

LoCoMo公式データには、複数セッションで構成された会話、各セッションの日時、話者、発言、QA、正解、カテゴリ、根拠となる発言IDが含まれている。

---

## 2. 結論

評価基盤はButlyと同じリポジトリ内に実装するが、通常のチャット処理や正式APIには組み込まない。

以下の責務分離を採用する。

```text
Butly本体
├─ butly_core/
├─ sleeptime.py
└─ 本番用の記憶・会話処理

評価基盤
└─ evals/locomo/
   ├─ LoCoMoデータ変換
   ├─ 会話リプレイ
   ├─ Sleeptime実行制御
   ├─ QA実行
   ├─ 採点
   ├─ チェックポイント
   └─ 評価成果物出力

Colab
└─ evals/locomo/colab/
   └─ CLIを呼び出すだけの薄いNotebook
```

Colab固有処理をButly本体や評価ロジックへ混ぜない。

評価基盤は、将来的に以下でも同じように実行できる構造とする。

* Google Colab Pro
* ローカルWindows PC
* Linuxサーバー
* 将来導入するGPUマシン
* RunPodなどのクラウドGPU

---

## 3. 目的

### 3.1 主目的

LoCoMoの固定会話をButlyへ時系列どおり投入し、各セッション後にSleeptimeを実行したうえで、LoCoMoのQAをButlyへ質問する。

これにより、以下を測定する。

1. 会話ログからの記憶生成品質
2. Knowledge Card、Digest、Snapshotへの情報保持
3. Gatekeeperの記憶要否判定
4. RAG検索の再現率
5. 最終回答の正確性
6. 時間推論および複数セッション推論
7. 誤想起および過剰な記憶利用
8. モデルごとの精度、速度、安定性

### 3.2 副目的

* Butlyの記憶機構を変更した際の回帰テストとして利用する
* ローカルLLMごとの適性を比較する
* Chat、Gatekeeper、Summary、Knowledgeのどこで性能が落ちたか切り分ける
* Sleeptime前後の記憶状態を比較可能にする
* 将来のLongMemEvalやButly独自日本語シナリオ追加の基盤にする

---

## 4. 非ゴール

初期実装では以下を行わない。

* LoCoMoの全マルチモーダル評価
* LoCoMoの画像URLから画像を再取得する処理
* React／Tauriフロントエンドへの評価画面追加
* Streamlitへの評価ボタン追加
* 本番インスタンスを直接使用した評価
* 複数モデルの並列GPU実行
* Judge LLMによる主観的な会話品質評価
* LoCoMo会話そのものをButlyに生成させる評価
* Graph RAGなど未実装機能の追加
* 評価のためだけに通常の記憶挙動を変更すること

初期MVPでは、テキスト会話とQA評価に限定する。

---

## 5. 評価方式

## 5.1 Replay Mode

初期実装では、LoCoMoの会話をモデルに再生成させず、原文を固定会話としてButlyへ投入する。

```text
LoCoMo Session 1
↓
日時・話者情報を保持してButlyへ保存
↓
Sleeptime実行
↓
LoCoMo Session 2
↓
Sleeptime実行
↓
以降繰り返し
↓
LoCoMo QAをButlyへ質問
↓
回答・検索結果・Trace・記憶状態を保存
↓
自動採点
```

Replay Modeでは、元会話の内容が変化しないため、LoCoMo公式の正解およびevidenceを利用できる。

### 5.2 Generated Conversation Mode

将来拡張として、LoCoMoの一方の話者だけを入力し、もう一方の返答をButlyに生成させるモードを検討する。

ただし、生成された会話はLoCoMo原文から分岐するため、公式QAスコアとは分離する。

Generated Conversation Modeでは、主に以下を評価する。

* 会話の自然さ
* 人格一貫性
* 記憶の自然な利用
* 不要な記憶の持ち出し
* 発言矛盾
* 関係性の継続
* 長期間の会話による人格崩壊

このモードは初期MVPには含めない。

---

## 6. 本体側に必要な最小変更

評価コードの大部分は`evals/locomo/`へ配置する。ただし、実際のButlyを安全に隔離環境で動かすため、以下の本体改善が必要である。

## 6.1 ButlySleeptimeへのパス注入

現在の`ButlyRuntime`は`data_dir`、`base_dir`、`instances_dir`を外部から受け取れるため、評価用の一時環境を構築しやすい。

一方、`ButlySleeptime`はモジュール位置を基準とする`BASE_DIR`および`INSTANCES_DIR`への依存が残っている。

次のように任意のパスを渡せる構造へ変更する。

```python
class ButlySleeptime:
    def __init__(
        self,
        base_dir: Optional[Path] = None,
        instances_dir: Optional[Path] = None,
    ):
        self.base_dir = (
            Path(base_dir)
            if base_dir is not None
            else Path(__file__).resolve().parent
        )
        self.instances_dir = (
            Path(instances_dir)
            if instances_dir is not None
            else self.base_dir / "butly_core" / "instances"
        )
```

Sleeptime内部のグローバル`BASE_DIR`／`INSTANCES_DIR`参照は、可能な範囲で`self.base_dir`／`self.instances_dir`へ移行する。

### 要件

* 引数未指定時の既存動作を変えない
* 既存APIおよびStreamlitの挙動を変えない
* 評価環境から一時ディレクトリを指定できる
* 既存テストを維持する
* 新しい回帰テストを追加する

### 実装状況（2026-07-10）

`ButlySleeptime(base_dir=None, instances_dir=None)`を追加し、Stage 1〜3、
DBバックアップ、人物登場集計を注入先へ統一した。未指定時は従来の
プロジェクトルートを使用する。隔離パスを検証する回帰テストを追加済み。

## 6.2 会話保存日時の注入

LoCoMoには各セッションの日時が含まれている。

しかし現在の`save_single_turn()`は、実行時の`datetime.now()`を保存する。

短時間で数週間分の会話を再生すると、Butly上ではすべて数分以内の出来事として保存され、時間推論の評価が成立しない。

後方互換を維持しながら、任意日時を指定可能にする。

```python
def save_single_turn(
    self,
    user_text: str,
    model_text: str,
    meta: Optional[dict] = None,
    created_at: Optional[datetime] = None,
):
    created_at = created_at if created_at is not None else datetime.now()
```

### 要件

* `created_at`未指定時は現在日時を使う
* ファイル名とJSON内timestampの両方へ指定日時を反映する
* 同一秒・同一日時の衝突を回避する
* 既存呼び出し元の変更を不要とする
* 任意日時保存のテストを追加する

### 実装状況（2026-07-10）

`ButlyMemory.save_single_turn(..., created_at=None)`を追加した。指定日時は
ファイル名とJSON内`timestamp`へ反映し、同一日時が重複した場合は
`_001`からの連番を付けて既存ターンの上書きを防ぐ。未指定時の挙動は従来どおり。
`./scripts/check_before_push.sh`は1128件成功、integration 7件除外で完了した。

## 6.3 評価用の直接会話保存

Replay Modeでは、LoCoMoに含まれる双方の発言を固定データとして保存する。

モデルに返答を生成させず、Butlyの正式な記憶形式へ会話を投入する小さなAPIまたは評価用アダプターを用意する。

本番APIへLoCoMo専用エンドポイントは追加しない。

候補は以下のいずれかとする。

1. `ButlyMemory.save_single_turn()`を評価アダプターから直接呼ぶ
2. 汎用的な`import_turn()`メソッドを本体へ追加する
3. 評価用Storeを経由して正式JSON形式を書き込む

初期実装では1を優先する。ただし話者、日時、dialog IDなどのメタデータを保持する必要がある場合は、汎用的なインポート機能として設計する。

---

## 7. ディレクトリ構成

```text
evals/
├─ __init__.py
└─ locomo/
   ├─ __init__.py
   ├─ README.md
   ├─ cli.py
   ├─ config.py
   ├─ dataset.py
   ├─ adapter.py
   ├─ workspace.py
   ├─ replay.py
   ├─ sleeptime_runner.py
   ├─ qa_runner.py
   ├─ scorer.py
   ├─ artifacts.py
   ├─ checkpoint.py
   ├─ report.py
   ├─ profiles/
   │  ├─ full_local.example.yaml
   │  └─ fixed_memory_models.example.yaml
   └─ colab/
      └─ butly_locomo_eval.ipynb

tests/
└─ evals/
   ├─ test_locomo_dataset.py
   ├─ test_locomo_adapter.py
   ├─ test_locomo_replay.py
   ├─ test_locomo_scorer.py
   ├─ test_locomo_checkpoint.py
   └─ fixtures/
      └─ mini_locomo.json
```

---

## 8. コンポーネント責務

## 8.1 `dataset.py`

LoCoMo JSONを読み込み、内部標準DTOへ変換する。

主なDTO:

```python
@dataclass
class LocomoTurn:
    dialog_id: str
    speaker: str
    text: str
    timestamp: datetime
    image_caption: str | None = None

@dataclass
class LocomoSession:
    session_id: str
    timestamp: datetime
    turns: list[LocomoTurn]

@dataclass
class LocomoQuestion:
    question_id: str
    question: str
    answer: str
    category: int
    evidence: list[str]

@dataclass
class LocomoConversation:
    sample_id: str
    speaker_a: str
    speaker_b: str
    sessions: list[LocomoSession]
    questions: list[LocomoQuestion]
```

### 要件

* セッション番号順に並べる
* 日時文字列を`datetime`へ変換する
* 画像ターンでは初期MVPとして`blip_caption`をテキストへ変換可能にする
* 不正データは具体的なエラーにする
* 元データを変更しない

## 8.2 `workspace.py`

評価実行ごとに隔離されたButlyデータディレクトリを作成する。

例:

```text
eval_runs/
└─ qwen3_14b_20260710_203000/
   ├─ workspace/
   │  └─ butly_core/instances/locomo_sample_0/
   ├─ results/
   ├─ traces/
   ├─ snapshots/
   ├─ checkpoints/
   └─ run_config.json
```

### 要件

* 本番の`butly_core/instances`を使用しない
* run ID単位で完全に隔離する
* 評価終了後も成果物を保持する
* `--clean`指定時のみ削除する
* 途中再開が可能な構造にする

## 8.3 `adapter.py`

LoCoMoの話者とButlyのuser／assistant表現を対応付ける。

Replay Modeでは、LoCoMo会話を2発言単位または時系列ターン単位でButlyへ保存する。

### 注意点

LoCoMoの会話は、必ずしもuser→assistantの単純なペアとは限らない可能性がある。

そのため、内部では全ターンの順序を保持し、Butlyの現在の保存形式へ変換する規則を明示する。

変換時には以下をメタデータとして保存可能にする。

* `locomo_sample_id`
* `locomo_session_id`
* `locomo_dialog_id`
* `original_speaker`
* `original_timestamp`
* `source = "eval"`
* `lane = "direct"`

評価メタデータが通常プロンプトへ不用意に注入されないよう注意する。

### Phase 2で固定した変換規則（2026-07-10）

`speaker_a`をButlyの`user`、`speaker_b`を`assistant`へ割り当てる。
通常の交互発話は2発言を1ファイルへ保存する。同一話者の連続発言や
`speaker_b`開始時は、反対側を空文字として保存し、元発言順と全dialog IDを保持する。
必須metaに加えて、ペア内の`locomo_dialog_ids`、`speaker_roles`、各発言の
role対応を`locomo_turns`へ保存する。評価用metaの未知フィールドは通常の
プロンプト整形では参照されない。

## 8.4 `replay.py`

指定された会話とセッションを順番に投入する。

主な処理:

1. 評価用インスタンス作成
2. 必要なモデル設定を適用
3. LoCoMoセッション投入
4. セッション後の成果物保存
5. Sleeptime実行
6. Sleeptime後の成果物保存
7. チェックポイント更新
8. 次セッションへ進行

### オプション

```text
--sample-ids
--sample-limit
--session-limit
--start-session
--run-sleeptime-per-session
--run-sleeptime-at-end
--resume
--stop-after-replay
```

## 8.5 `sleeptime_runner.py`

評価対象インスタンスに対してSleeptimeを同期実行する。

評価ではバックグラウンドAPI経由ではなく、処理完了を待てる直接呼び出しを使う。

### 記録項目

* 開始時刻
* 終了時刻
* 実行時間
* Stage 1成功／失敗
* Stage 2成功／失敗
* 作成されたKnowledge Card数
* Digest更新有無
* Recent Snapshot更新有無
* エラー
* リトライ回数

## 8.6 `qa_runner.py`

全セッション投入後、LoCoMoの質問をButlyへ送信する。

質問は通常の`ButlyRuntime.chat()`を通し、実際の以下の処理を利用する。

* Gatekeeper
* MemoryProbe
* MemoryBlockBuilder
* RAG
* Chat Provider
* Trace
* debug log

`ButlyRuntime`はHTTPルーターに依存せず、CLIや外部入口から直接チャットを実行できる構造になっている。

### 質問時の設定

初期値:

```text
use_rag = true
use_google_search = false
use_web_search = false
source = "api"
```

外部Web検索はLoCoMoの正解を汚染するため無効にする。

## 8.7 `scorer.py`

LoCoMo公式評価コードを参考に、カテゴリごとの採点を行う。

公式実装は、単一回答、複数回答、時間質問、情報なし質問などで採点方法を分けている。

初期MVPでは以下を実装する。

* 正規化済みToken F1
* Exact Match
* Answer containment
* No-information accuracy
* カテゴリ別平均
* 全体平均

LoCoMo公式互換スコアとButly独自の補助指標は別フィールドで保存する。
[公式評価実装](https://github.com/snap-research/locomo/blob/main/task_eval/evaluation.py)
に合わせ、カテゴリ1はカンマ区切りの複数回答F1、カテゴリ2〜4は
正規化・stemming後のToken F1、カテゴリ3は正解のセミコロン以前を採点対象、
カテゴリ5は情報なしを示す所定表現の判定とする。Exact MatchとAnswer
containmentは比較・デバッグ用の追加指標であり、公式F1とは呼ばない。

追加でButly固有指標を計算する。

* RAG発火率
* 正解時のRAG発火率
* 不正解時のRAG発火率
* evidenceを含むカードの取得率
* 取得カード数
* 誤ったカードの注入数
* 平均レイテンシ
* p50／p95レイテンシ
* Gatekeeper tier分布
* need_intent分布
* Sleeptime生成カード数

## 8.8 `artifacts.py`

各工程の成果物を保存する。

### 保存対象

```text
run_config.json
environment.json
dataset_manifest.json
replay_log.jsonl
sleeptime_log.jsonl
qa_results.jsonl
scores.json
summary.md
errors.jsonl
```

各セッション後に以下のスナップショットを保存する。

```text
snapshots/
└─ locomo_sample_0/
   └─ session_001/
      ├─ before_sleeptime/
      └─ after_sleeptime/
```

対象:

* `mid_term_digest.txt`
* `recent_snapshot.txt`
* `recent_digest_headlines.json`
* `session_state.json`
* Knowledge Card一覧
* short-termファイル数
* archiveファイル数
* Trace
* debug log

APIキーや秘匿情報は成果物へ保存しない。

## 8.9 `checkpoint.py`

Colab切断やランタイム上限に備え、途中再開を可能にする。

チェックポイント例:

```json
{
  "run_id": "qwen3_14b_20260710_203000",
  "sample_id": "conv-0",
  "completed_sessions": ["session_1", "session_2"],
  "sleeptime_completed_for": ["session_1", "session_2"],
  "qa_completed": 0,
  "status": "replaying"
}
```

### 要件

* セッション完了ごとに保存する
* Sleeptime完了後に保存する
* QAは一定件数ごとに保存する
* `--resume`で続きから再開する
* 二重投入を防ぐ
* Google Drive上でも破損しにくいatomic writeを使う

---

## 9. モデル評価プロファイル

結果を解釈しやすくするため、2種類のプロファイルを用意する。

## 9.1 Full Local Profile

1つのローカルLLMを可能な限り複数ロールへ使用する。

```yaml
name: full_local
chat:
  connection: colab_local
  model_name: model-name

gatekeeper:
  connection: colab_local
  model_name: model-name

summary:
  connection: colab_local
  model_name: model-name

knowledge:
  connection: colab_local
  model_name: model-name

embedding:
  connection: local_embedding
  model_name: embedding-model
```

目的:

> そのモデルを中心にButly全体を運用できるか

## 9.2 Fixed Memory Pipeline Profile

Summary、Knowledge、Embedding、Gatekeeperを固定し、Chatモデルだけ変更する。

```yaml
name: fixed_memory_pipeline
chat:
  connection: evaluation_target
  model_name: target-model

gatekeeper:
  connection: fixed
  model_name: fixed-model

summary:
  connection: fixed
  model_name: fixed-model

knowledge:
  connection: fixed
  model_name: fixed-model

embedding:
  connection: fixed
  model_name: fixed-embedding
```

目的:

> 会話モデル単体の回答能力を比較する

初期実装はFull Local Profileを優先する。ただし設定構造は両方を扱えるようにする。

---

## 10. CLI仕様案

最小実行例:

```bash
python -m evals.locomo.cli run \
  --dataset data/locomo10.json \
  --profile evals/locomo/profiles/full_local.yaml \
  --qa-mode independent \
  --locale en \
  --sample-limit 1 \
  --session-limit 3 \
  --question-limit 10 \
  --output-dir /content/drive/MyDrive/butly-evals
```

全LoCoMo（全sample・全session・全question）:

```bash
python -m evals.locomo.cli run \
  --dataset data/locomo10.json \
  --profile evals/locomo/profiles/full_local.yaml \
  --qa-mode independent \
  --locale en \
  --all-samples \
  --all-sessions \
  --all-questions \
  --output-dir ./eval_runs
```

再開:

```bash
python -m evals.locomo.cli resume \
  --run-dir /content/drive/MyDrive/butly-evals/qwen3_14b_20260710
```

採点のみ再実行:

```bash
python -m evals.locomo.cli score \
  --run-dir ./eval_runs/qwen3_14b_20260710
```

レポート生成:

```bash
python -m evals.locomo.cli report \
  --run-dir ./eval_runs/qwen3_14b_20260710
```

---

## 11. Colab対応方針

Colab Proを初期の主要実行環境としてサポートする。

ただし評価コード本体はColabへ依存させない。

Notebookの責務は次に限定する。

1. Google Driveをマウント
2. Butlyをcloneまたはpull
3. 必要なPython依存関係をインストール
4. Hugging Face認証情報を環境変数から読み込む
5. ローカルLLMサーバーを起動
6. 評価プロファイルを作成
7. CLIを実行
8. 結果をDriveへ保存
9. サマリーを表示

Notebook内に以下を直接実装しない。

* LoCoMo解析
* Sleeptime制御
* QA実行
* 採点
* チェックポイント
* Butly記憶ファイルの直接操作
* レポート生成ロジック

### Colabのモデルサーバー

初期候補:

* llama.cpp server
* vLLM
* Ollama

Butly側から既存のOpenAI互換Connectionとして利用できる方式を優先する。

特定のサーバー実装へ評価基盤を固定しない。

---

## 12. 実装フェーズ

## Phase 0: 仕様固定

* LoCoMo公式データ構造を確認
* Replay Modeの話者変換規則を決定
* QAカテゴリと採点方法を整理
* 評価用成果物形式を決定
* ミニFixtureを作成

### 完了条件

* 1会話、2セッション、5問程度のFixtureがある
* 期待される処理フローが文書化されている
* 評価データを本番データから隔離する方針が確定している

## Phase 1: 本体の評価可能化

**実装済み（2026-07-10）**

* `ButlySleeptime`へパス注入
* `save_single_turn()`へ日時注入
* 必要な回帰テスト追加
* 既存動作が変わらないことを確認

### 完了条件

* 一時ディレクトリ上でRuntimeとSleeptimeを実行できる
* 任意日時の会話を保存できる
* 通常起動時の挙動に変更がない
* 既存テストがすべて通る

## Phase 2: ミニReplay Runner

**実装済み（2026-07-10）**

* Fixture読込
* 評価用Workspace作成
* 会話リプレイ
* セッション単位Sleeptime
* 最終QA
* JSONL出力

### 実装状況

* 公式スキーマ互換の合成Fixture（1会話・2セッション・5カテゴリQA）を追加
* typed DTOとparserを追加。公式日時形式、数値/null answer、画像caption、
  question ID補完、具体的な入力エラーへ対応
* run ID単位のWorkspaceを追加。本番instances tree配下への出力を拒否し、
  既存runは`clean=True`指定時のみ削除
* `speaker_a=user` / `speaker_b=assistant`のReplayAdapterを追加し、元日時と
  LoCoMo metaを`save_single_turn()`経由で保存
* 2セッションそれぞれでStage 1/2を同期実行し、Knowledge Card、Digest、
  前後スナップショットを保存
* 最終QAを`ButlyRuntime.chat()`で実行。RAG有効、Google/Web検索無効、
  Traceを質問単位で保存
* `replay_log.jsonl` / `sleeptime_log.jsonl` / `qa_results.jsonl`、
  `dataset_manifest.json` / `environment.json`を出力
* Fake Providerによる外部API不要の縦断テストを含む21件を追加
* `./scripts/check_before_push.sh`完了。1149件成功、integration 7件除外、
  dependency check正常

### 完了条件

* 2〜3セッションを完全自動実行できる
* セッション間でSleeptimeが完了する
* QA回答とTraceを保存できる
* 同じ入力で再現可能な評価成果物を生成できる

## Phase 3: 採点とレポート

**実装済み（2026-07-11）**

* F1／EM／No-information採点
* カテゴリ別集計
* Butly固有指標
* Markdownサマリー
* エラー一覧

### 実装状況

* `scorer.py`: 公式評価と同じ正規化（カンマ除去・小文字化・句読点除去・
  a/an/the/and除去）とPorter stemming後のToken F1。カテゴリ1はカンマ区切り
  複数回答の平均max F1、カテゴリ3はセミコロン以前のみ採点、カテゴリ5は
  「no information available」「not mentioned」の判定。EM／containmentは
  補助指標として別フィールドに保存
* `stemming.py`: nltk非依存のPorter (1980) 実装。nltk拡張との稀な差分は
  docstringと`scores.json`の`stemming`フィールドに明記
* Butly固有指標: RAG発火率（正解時／不正解時の内訳つき）、取得カード数、
  レイテンシ mean/p50/p95、tier・need_intent分布、Sleeptime生成カード数・
  失敗数、evidence取得率（データセット指定時のみのトークン重複
  ヒューリスティック）
* `report.py`: `scores.json`から`summary.md`を生成（カテゴリ別表、低スコア
  質問一覧、ライセンス表記）。`errors.jsonl`はreplay/sleeptime/qaログの
  失敗行を集約
* CLI: `run`は採点・レポートまで一括実行（`--skip-scoring`で無効化）。
  `score` / `report`で再実行可能
* `sleeptime_runner.py`が参照していた`ButlySleeptime`のprivateメソッドは
  `get_instance_config()` / `should_update()`としてpublic化し、本体・
  既存テスト含め全呼び出し元を更新

### 完了条件

* 1コマンドで評価と採点が完了する
* `scores.json`と`summary.md`が生成される
* 不正解例を追跡できる
* RAG発火と取得カードを確認できる

## Phase 4: Colab Notebook

**実装済み（2026-07-11、Colab実機での手順確認は未実施）**

* Driveマウント
* セットアップ
* モデルサーバー起動
* CLI実行
* 再開
* 結果表示

### 実装状況

* `checkpoint.py`: セッション完了・Sleeptime完了・QA 1問ごとにatomic write
  で保存。run ID照合と破損検出つき。途中中断されたセッションはmetaの
  `locomo_sample_id` / `locomo_session_id`一致でshort-termから破棄して
  全体を再投入するため、二重投入は発生しない
* `replay.py`に`resume_evaluation(run_dir)`を追加。`run_config.json`から
  `ReplayConfig`を復元し、checkpointの続きから実行（resume時は`clean`を
  常に無効化）
* CLI `resume --run-dir`を追加。独立QAは使い捨てcloneを再生成し、逐次QAは
  各問直前のinstance・QA結果offset・Traceをdurable復元点へ保存する。
  QA書き込みとcheckpoint更新の間で落ちた場合はresume時に未commit分を
  rollbackしてから再実行する。採点時の`(sample_id, question_id)`単位の
  最新レコード採用も旧artifact互換の防御として残す
* `--profile <yaml>`対応: chat/gatekeeper/summary/knowledge/embeddingの
  ロール別セクションを評価インスタンスconfigへ適用。
  `profiles/full_local.example.yaml` / `fixed_memory_models.example.yaml`を同梱
* profile top-level `locale`とCLI `--locale`に対応（CLI > profile > `en`）。
  `--qa-mode independent|sequential`、sample/session/questionごとの
  `--*-limit` / `--all-*`を追加
* `colab/butly_locomo_eval.ipynb`: Driveマウント→clone→依存インストール→
  HFトークン→llama.cppサーバー起動→`colab_local` Connection登録→CLI実行→
  resume→summary表示のみ。ParametersセルでQA mode、locale、3軸の全件／
  上限制御を選択し、評価ロジックはNotebookに置かない

### 完了条件

* 新規Colabランタイムから手順どおり実行できる（※実機確認はPhase 5で実施）
* ランタイム切断後にDrive上のチェックポイントから再開できる
* Notebook固有コードが評価ロジックへ混入していない

## Phase 5: LoCoMo実データ試験

最初は以下に限定する。

```text
1 conversation
3 sessions
10 questions
1 model
```

成功後に段階的に増やす。

```text
1 conversation
全sessions
全questions
1 model
```

その後、別モデルを1つずつ評価する。

---

## 13. テスト方針

## 13.1 Unit Test

* LoCoMo JSONの解析
* セッション順序
* 日時変換
* 話者変換
* QAカテゴリ変換
* F1／EM採点
* 情報なし採点
* チェックポイント
* atomic write
* Resume時の重複防止

## 13.2 Integration Test

外部APIとGPUを使わないFake Providerで以下を確認する。

* Replay
* Sleeptime
* QA
* 成果物出力
* Resume
* エラー時の途中保存

## 13.3 Optional GPU Test

integrationマーカーを付け、通常CIから除外する。

* 実モデル起動
* 1セッション
* 1回Sleeptime
* 1問QA
* 結果保存

---

## 14. 評価結果の基本形式

QA結果例:

```json
{
  "run_id": "qwen3_14b_20260710_203000",
  "profile": "full_local",
  "sample_id": "conv-0",
  "question_id": "qa-001",
  "question": "What did Caroline decide to study?",
  "expected_answer": "nursing",
  "prediction": "She decided to study nursing.",
  "category": 1,
  "evidence": ["D17"],
  "exact_match": 0.0,
  "token_f1": 1.0,
  "answer_contained": true,
  "tier": "mid",
  "need": "past_fact",
  "rag_triggered": true,
  "retrieved_card_ids": [
    "episode_20260710_001"
  ],
  "retrieved_card_titles": [
    "Caroline decided to study nursing"
  ],
  "latency_ms": 2840,
  "error": null
}
```

実行サマリー例:

```json
{
  "model": "qwen3-14b-q4",
  "samples": 1,
  "sessions": 12,
  "questions": 65,
  "overall_f1": 0.71,
  "no_information_accuracy": 0.80,
  "rag_trigger_rate": 0.76,
  "evidence_retrieval_rate": 0.63,
  "average_latency_ms": 3110,
  "p95_latency_ms": 6240,
  "knowledge_cards_created": 84,
  "sleeptime_failures": 0
}
```

---

## 15. 注意事項・リスク

### 15.1 元日時を保持しないと時間評価が無効になる

全会話を現在日時で保存しないこと。

### 15.2 原文Replayと生成会話を混ぜない

生成会話は公式QAの前提を壊すため、別モード・別スコアとして扱う。

### 15.3 Sleeptimeのモデル失敗を回答モデルの失敗と混同しない

成果物にロール別モデル名と各処理の成功状態を必ず記録する。

### 15.4 外部Web検索を無効にする

Web検索で正解を取得すると、長期記憶評価ではなく外部検索評価になる。

### 15.5 本番記憶を使用しない

評価用Workspaceは必ず隔離し、本物のButlyインスタンスを変更しない。

### 15.6 英語性能と記憶性能が混ざる

LoCoMoは英語中心のため、結果にはモデルの英語能力が含まれる。

将来的に一部シナリオを日本語化し、Butly独自日本語評価セットを追加する。

### 15.7 Colab切断

セッション単位のチェックポイントとDrive保存を必須にする。

### 15.8 評価時の記憶汚染

QAへの回答自体が次のQA用記憶へ保存されると、後続質問へ影響する可能性がある。

初期実装では次のいずれかを採用し、挙動を明示する。

* QA回答を記憶へ保存しない評価モード
* 各QA前に同じ最終Workspaceから複製する
* QAを順番に実行するが、評価質問をSleeptimeへ含めない

推奨は、最終Replay完了時点のWorkspaceをQAごとに複製し、各質問を独立評価する方式である。

現在は`--qa-mode independent`を既定とし、各QAを同じ最終Replay /
post-Sleeptime状態から独立実行する。`--qa-mode sequential`では従来どおり
先行QAをshort-termとsession stateへ残し、実運用の連続質問耐久を測る。
どちらのmodeでもQAターンをSleeptimeへは投入しない。sample/session/question
の各範囲は`--*-limit`と`--all-*`を排他的に選択し、全LoCoMo評価では3軸すべて
の`--all-*`を明示する。localeはCLI、profile、英語既定値の順に解決し、
checkpoint resumeでもrun開始時の値を維持する。localeはButly内部prompt /
memory出力を選び、LoCoMo質問・正解は翻訳しない。公式Token F1との互換性を
保つためQA回答も英語に固定する。日本語版ベンチマークには翻訳済みdatasetと
日本語対応scorerを別途用意する。評価instanceでは再現性のためローカル
`user_prompts.json` overrideを無効化する。

### 15.9 データセットのライセンス

LoCoMo公式リポジトリは
[CC BY-NC 4.0](https://github.com/snap-research/locomo/blob/main/LICENSE.txt)
で公開されている。公式データ本体は
Butlyリポジトリへ同梱せず、利用者が取得したファイルをCLIへ渡す。
テスト用`mini_locomo.json`は公式会話の抜粋ではなく、同じスキーマを持つ
合成データとして作成する。評価レポートと利用手順には出典・ライセンスを明記する。

---

## 16. 完了条件

初期バージョンは、以下をすべて満たした場合に完了とする。

* LoCoMoデータを読み込める
* 任意の会話およびセッション数を選択できる
* 元日時を保持して会話を投入できる
* 評価用データが本番環境から隔離される
* 各セッション後にSleeptimeを同期実行できる
* Sleeptime前後の記憶状態を保存できる
* LoCoMo QAをButlyRuntime経由で実行できる
* 回答、Trace、RAG結果、取得カードを保存できる
* F1、EM、情報なし精度を計算できる
* カテゴリ別と全体のスコアを出力できる
* セッション単位で途中再開できる
* Colab Pro上でCLIを実行できる
* Google Driveへ成果物を保存できる
* Colab固有処理が評価コアへ混入していない
* 既存のButly機能とテストを壊していない

---

## 17. Codexへの実装指示

この計画を一括実装しないこと。

以下の順序で、小さなPRまたはコミット単位に分割する。

### 最初の作業

1. 現行の`ButlySleeptime`、`ButlyRuntime`、`ButlyMemory`、`InstanceManager`を調査する
2. Phase 1の本体変更案を提示する
3. 既存挙動を変えないテストを先に追加する
4. パス注入と日時注入だけを実装する
5. 全テストを実行する

### 次の作業

6. `tests/evals/fixtures/mini_locomo.json`を作る
7. `dataset.py`とDTOを実装する
8. 一時Workspace上でReplayできる最小Runnerを実装する
9. Sleeptimeを1回実行する
10. QAを1問実行し、結果をJSONへ保存する

### その後

11. 採点
12. チェックポイント
13. レポート
14. Colab Notebook

実装中に本計画と現行コードが矛盾した場合、無理に計画どおり変更せず、差異、影響、代替案を報告すること。

評価専用コードを本番チャット経路へ混入させないこと。

Notebookをロジックの正本にしないこと。

既存のOpenAI互換Provider、ModelRef、Connection、ButlyRuntimeを再利用し、LoCoMo専用のモデル呼び出し処理を新規に作らないこと。
