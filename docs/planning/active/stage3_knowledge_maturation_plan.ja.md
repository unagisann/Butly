# Stage 3（Knowledge Maturation / memory_nodes）実装計画書: 運用耐性・ブートストラップ・ON/OFF評価

🌐 **日本語** | English（未作成 — 本 `.ja.md` が正本、`.md` は後日ペア化）

> **ステータス: Phase 0〜5 実装済み（2026-07-21、ブランチ `claude/stage3-maturation`）。**
> 残: Phase 5 の実 LoCoMo A/B 実走（導線・profile・検証まで完成、API 実走は未実施）／
> Phase 6（Key Memory 自動反映。A/B ゲート通過後）／Phase 7（node 独立検索。別計画切り出し可）／
> Phase 8（クリーンアップ）／§8 の proposal 参照・承認 API 導線（routers/instances.py。Phase 6 と同時が妥当）。
> 完了条件（§14）を満たしたら `archived` へ移す。
>
> 起票: 2026-07-21 / 想定スパン: 段階的・急がない / 利用者: 自分のみ（破壊的変更可）
>
> 前提: Stage 3 は commit `7411405`（2026-05-25）で **すでに opt-in 実装済み**（デフォルト OFF）。
> 本計画は「未実装を作る」のではなく **「運用に耐える構成へ作り替える」** もの。
>
> 関連:
> [Issue #14](https://github.com/unagisann/Butly/issues/14)（Housekeeper Stage 3 — reflection/generalization/self_model）/
> [memory_lifecycle.ja.md](../../reference/memory_lifecycle.ja.md)（記憶層の正本仕様）/
> [memory_store_normalization_plan.ja.md](memory_store_normalization_plan.ja.md)（A/C層の正規化。Stage 3 は §2 で明示的に非ゴールとされていた別宿題＝本計画）/
> [locomo_evaluation_flow.ja.md](../../reference/locomo_evaluation_flow.ja.md)（評価フロー）

---

## 1. なぜやるか（モチベーション）

Butly の記憶は Stage 1（mid_term 蓄積）・Stage 2（knowledge_cards 生成）まではエピソードを**溜める**層。
Issue #14 が指摘したのは **「溜めるだけでは AI が“成長”しない」** ＝ カード群から現在解釈を蒸留し、
矛盾を解消し、自己モデルへ還元する層（Stage 3）が要る、という主張だった。

Stage 3 は commit `7411405` で `memory_nodes` 層として実装済みだが、**opt-in の実験装置のまま**で、
次の 3 つが運用に耐えない：

1. **被覆の穴（差分方式）**: レビュー対象カードを毎回「利用済み上位＋直近アクセス」から最大 30 件“選び直す”ため、
   同じ高頻度カードが毎晩再投入され、`usage_count=0` かつアクセス窓外のカードは**永遠にレビューされない**（§4）。
2. **ブートストラップ不在**: 既存インスタンス（カード数百〜千）で有効化しても、毎晩 30 件・利用済みのみしか見ないため、
   過去資産の大半が node 化されない。初回一括パスが無い。
3. **効果の未検証**: node が実際に応答品質へ寄与するか、LoCoMo で ON/OFF 比較する導線が無い。

本計画は **(A) content hash 式レビューキューで全カード被覆を保証**し、
**(B) 初回ブートストラップ**を用意し、**(C) reflection（時間減衰）を補強**し、
**(D) self_model 昇格ループを提案＋任意フラグで閉じ**、**(E) LoCoMo ON/OFF 評価**を足す。

設計レビューで、時刻文字列ウォーターマークだけでは失敗判定・再開冪等・同時更新を安全に扱えないことが判明した。
このため本計画では、カード本文の `content_hash`、run ごとのカード状態、DB 適用トランザクションを中核に置く。
監査用時刻は保持するが、再レビュー要否の判定には使わない。

---

## 2. Issue #14 の再解釈（3 概念 → 現行 node 層へのマッピング）

Issue #14 は Stage 3 を reflection / generalization / self_model の 3 語で語る。起票は実装前なので
「未実装」と書かれているが、現状の `memory_nodes` 実装に写像すると **generalization は概ね実装済み・
reflection は部分実装・self_model が本当のギャップ** と整理できる。

| Issue #14 の語 | 現行実装での対応 | 状態 | 本計画での扱い |
|---|---|---|---|
| **generalization（汎化）** | 複数カードから `new_nodes`（現在解釈）を蒸留（[knowledge_maturation.py:251](../../../butly_core/core/knowledge_maturation.py#L251)） | ✅ 実装済み | 被覆をキュー化して**取りこぼしを無くす**（§5） |
| **reflection（振り返り）** | `contradicts` で confidence 減点→`uncertain` 降格、`supersede` で旧解釈置換（[sleeptime.py:1175](../../../sleeptime.py#L1175) / [memory_nodes.py:232](../../../butly_core/core/memory_nodes.py#L232)） | 🟡 部分（矛盾検出のみ。**時間減衰なし**＝一度 active だと永久 active） | **staleness 減衰スイープ**を追加（§7） |
| **self_model（自己モデル更新）** | 昇格条件を満たす node を `memory_node_proposals.json` に出力するのみ（[knowledge_maturation.py:354](../../../butly_core/core/knowledge_maturation.py#L354)）。**Key Memory / 人格へは反映されない** | 🔴 未実装（提案止まり） | **提案＋任意フラグで Key Memory へ自動反映**（§8） |

> **判断（確定済み）**: self_model の自動化は **`subject='user'` の Key Memory human エントリまで**。
> `system_instruction.txt` と persona エントリは自動書き換えしない。

---

## 3. 現状の棚卸し（実装済みベースライン）

### 3.1 データモデル（[database.py:100-176](../../../butly_core/core/database.py#L100-L176)）

| テーブル | 役割 | 主キー / 主なカラム |
|---|---|---|
| `memory_maturation_runs` | 実行ログ | `id`, `instance_name`, `status`, `reviewed/created/linked/superseded_count`, `started/completed_at`, `error`, `metadata_json` |
| `memory_nodes` | 蒸留された現在解釈 | `id`, `kind`(preference/fact/habit/other), `subject`, `topic`, `statement`, `confidence`(0-1), `status`(candidate/active/uncertain/superseded), `superseded_by_node_id`(self-FK), `metadata_json`, `created/updated/last_reinforced_at` |
| `memory_node_sources` | node ↔ card の根拠リンク | PK(`node_id`,`card_id`,`relation`), `relation`(supports/contradicts/context), `confidence`, `note` |

enum は Python 側で validation（[memory_nodes.py:16-42](../../../butly_core/core/memory_nodes.py#L16-L42)）。`kind` は未知値を `other` に正規化、
`status`/`relation` は違反を `ValueError`。マイグレーションは `CREATE TABLE IF NOT EXISTS` ＋ `ALTER TABLE ... ADD COLUMN`
を try/except で囲む方式（[database.py:53-98](../../../butly_core/core/database.py#L53-L98)）。本計画ではこの既存方式を踏襲せず、
`PRAGMA table_info` で存在確認してから追加し、duplicate column 以外の migration エラーを表面化させる。

### 3.2 実行フロー（[sleeptime.py:1009 `stage_3_mature_knowledge`](../../../sleeptime.py#L1009)）

```
process_instance
  └─ _should_run_stage_3(inst_cfg)                 # update_targets.knowledge_maturation ∧ knowledge_maturation_enabled
      └─ stage_3_mature_knowledge(instance_path)
          1. collect_review_cards(window_days=7, max_cards=30, min_usage_count=1)   ← ★差分方式（§4）
          2. start_run
          3. find_nodes([candidate,active,uncertain], limit=200)                    ← existing node 文脈
          4. build_review_prompt → provider.classify (knowledge conf)
          5. parse_llm_output → {link_existing, new_nodes}
          6. apply_link_existing / _reinforce_linked_nodes / apply_new_nodes / mark_uncertain_nodes
          7. collect_promotion_proposals → write memory_node_proposals.json         ← 提案止まり（§8）
          8. complete_run
```

### 3.3 Chat/QA への注入（opt-in・カード随伴）

`knowledge_maturation_enabled=True` のとき、RAG でヒットしたカードに紐づく `status='active'` node を
最大 5 件併走注入（[memory_builder.py:461-475](../../../butly_core/core/gatekeeper/memory_builder.py#L461-L475) /
`_lookup_active_nodes_for_candidates` [:898](../../../butly_core/core/gatekeeper/memory_builder.py#L898)）。
**カードが RAG でヒットしなければ node も見えない**（＝ node は独立検索されない）。QA も通常の
chat→gatekeeper→memory_builder 経路を通るので、この注入は **LoCoMo QA にも効く**（§10 の前提）。

### 3.4 設定キー（[config.py:105-113](../../../butly_core/config.py#L105-L113) / [settings/defaults.py:95-103](../../../butly_core/settings/defaults.py#L95-L103)）

`knowledge_maturation_enabled=False` / `_interval_days=1` / `_window_days=7` / `_max_cards=30` /
`_min_usage_count=1` / `memory_node_{candidate=0.65, active=0.75, promotion=0.85}_threshold` /
`memory_node_promotion_min_sources=2`。

> **要注意（デッドノブ）**: `knowledge_maturation_interval_days` は**どこからも参照されていない**
> （grep 0 件）。＝ Stage 3 は有効時に**毎 sleeptime 走る**。差分方式と相まって「同じ上位カードを毎晩再投入」を助長している。

---

## 4. 差分方式の限界（なぜ運用に耐えないか）

現行 `collect_review_cards`（[knowledge_maturation.py:32](../../../butly_core/core/knowledge_maturation.py#L32)）は毎回、
`usage_count>=1` 上位＋直近アクセスから **最大 30 件を選び“直す”**。レビュー済みを記録しないため次の破綻がある：

| # | 症状 | 根本原因 | 帰結 |
|---|---|---|---|
| 1 | 同じカードを毎晩再投入 | レビュー済みマーカーが無い（毎回 SELECT で選び直す） | LLM コストの空焚き。node が増えず link だけ重複 upsert |
| 2 | 低頻度・未使用カードが**永遠に未レビュー** | `usage_count=0` かつアクセス窓外は候補に入らない | **被覆の穴**。汎化の母集団が偏る |
| 3 | 混雑日の取りこぼし | 1 回 30 件上限・キュー drain 保証なし | 31 件目以降は翌日も選ばれる保証がない |
| 4 | 既存インスタンス有効化で過去資産が死蔵 | 初回一括パスが無い（§6 で解消） | 数百カードでも毎晩 30 件・利用済みのみ |
| 5 | existing node 文脈が `updated_at DESC LIMIT 200` の平坦 cap | バッチと無関係に上位 200 | node 増加で重複 node 乱立（dedup 精度劣化） |

**要点: ストレージやモデルではなく「レビュー対象の選び方」と「成功の記録方法」が壊れている。**
毎回の上位選抜を、content hash で版を識別する永続キューへ置き換える。LLM の成功判定と DB 適用を分離し、
成功した版だけを処理済みにすることで、被覆・冪等・背圧を同時に扱う。

---

## 5. 方針A — content hash 式レビューキュー（中核）

### 5.1 スキーマ追加

`knowledge_cards` に次を追加する。既存カードは migration 時に canonical content から hash を backfill する。
`ALTER TABLE` の失敗を一律に握り潰さず、`PRAGMA table_info` で存在確認してから追加する。
既存カードの `maturation_queued_at` は parse可能な `created_at` をUTCへ正規化し、不能時はmigration開始時刻を使う。
Stage 3 が書く運用時刻は共通helperで固定長UTC `YYYY-MM-DDTHH:MM:SSZ` に正規化する。offset付き表現や小数秒の有無が
混在した既存値もmigration時に同形式へ揃える。

| カラム | 型 | 意味 |
|---|---|---|
| `content_hash` | TEXT | Stage 3 promptへ渡す `title/summary/episode/tags/category/source_date` を正規化して作る SHA-256。カード本文の版識別子 |
| `last_matured_content_hash` | TEXT | 最後に**成功レビュー**した版。NULL または `content_hash` と不一致ならキュー内 |
| `maturation_queued_at` | TEXT | 現在の版がキューへ入った UTC 時刻。公平な FIFO 選択用 |
| `last_matured_at` | TEXT | 最終成功時刻（監査専用。再レビュー判定には使わない） |
| `last_matured_run_id` | TEXT | 最終成功 run の id（追跡用） |

run 単位の再開・監査用に `memory_maturation_run_cards` も追加する。

| カラム | 意味 |
|---|---|
| `run_id`, `card_id`, `content_hash` | run に渡したカード版。複合 UNIQUE |
| `status` | `queued / applied / no_changes / failed / changed_during_run / abandoned` |
| `error` | parse/provider/DB 失敗理由 |
| `diagnostic` | `reviewed_card_ids` 不一致や provider の終了理由など、成否を変えない診断情報 |

queue走査用に `knowledge_cards(is_archived, maturation_queued_at)`、追跡用に
`memory_maturation_run_cards(run_id, status)` の index を追加する。

node 側の embedding（§9 Phase B）で `memory_nodes.embedding_blob BLOB` も足すが、それは後段。

### 5.2 hash の正本化と変更検知

- canonical content は対象フィールドを固定順 JSON（UTF-8、キー順固定、改行・前後空白正規化）にして SHA-256 を計算する。
  `source_date` は出来事の時間的意味に関わるため、選択結果だけでなく
  `build_review_prompt` の `_card_view` にも追加する。`type`、importance、usage、embedding、pin/archiveは
  Stage 3 promptの意味内容ではないためhashにもpromptにも含めない。
- Stage 2 INSERT、`ButlyDatabase.update_card`、将来の本文更新経路は同じ helper で `content_hash` を更新し、
  hash が変わった時だけ `maturation_queued_at` を更新する。
- pin/archive/usage/embedding/type rename など、レビュー対象本文を変えない操作では hash を変えない。
- 現行 Stage 2 はカードを INSERT しており、本文マージは行わない。将来 merge が入っても同じ helper を必須とする。
- audit 時刻は UTC ISO 8601 に統一するが、`updated_at > last_matured_at` の TEXT 比較は行わない。

### 5.3 選択クエリと公平性

runのpreflightで、非アーカイブかつ `content_hash IS NULL` のカードを共通canonical helperにより自己修復backfillし、
`maturation_queued_at` も設定する。hashを生成できない行があればrunを明示的に失敗させ、キューから静かに除外しない。
その完了後に次のクエリを実行する。

```sql
SELECT id, title, summary, episode, tags, category, source_date,
       usage_count, content_hash, maturation_queued_at
FROM knowledge_cards
WHERE COALESCE(is_archived, 0) = 0
  AND content_hash IS NOT NULL
  AND (last_matured_content_hash IS NULL
       OR last_matured_content_hash <> content_hash)
ORDER BY maturation_queued_at ASC,
         COALESCE(usage_count, 0) DESC,
         id ASC
LIMIT :batch_size
```

- **被覆保証**: 最古の未処理版を先に処理する FIFO とし、新規高頻度カードが流入しても既存低頻度カードを追い越し続けない。
- **時刻比較の契約**: `maturation_queued_at` は §5.1 の固定長UTC形式だけを書き込むため、TEXT辞書順が時系列順になる。
  TEXT比較はキュー順序にのみ使い、再レビュー要否は引き続きcontent hashで判定する。
- **保証条件**: worker が継続稼働し、各runで1件以上成功すること。流入が処理能力を上回る場合も個々の既存カードは進むが、
  キュー総数は増えるため、backlog件数・最古待ち時間を観測する。
- `usage_count` は同一 queue 時刻内の tie-break に限定し、被覆より優先しない。

### 5.4 run 開始・結果分類・トランザクション境界

1. instance単位のprocess lockをnon-blockingで取得してStage 3の単一実行を保証し、liveな並行runがあれば新規runを開始しない。
   lock取得後に残っている `running` runは、lockが解放された前processの残骸なので即 `abandoned` として閉じ、
   run_cards も `abandoned` にしてcard版は未処理のまま再選択可能にする。現行の同期実行ではこの回収で十分であり、
   時間だけを基準にlive runをabandonしない。
   `memory_maturation_runs.status` のallow-listにも `abandoned` を追加する。
2. 選択した各カードの **選択時 `content_hash`** を `memory_maturation_run_cards(status='queued')` に保存する。
3. provider層から本文と終了理由を受け取り、`length/max_tokens` など既知のtruncation終了は `truncated_response` として失敗させる。
   `classify() -> str` の互換性は維持し、token usageと同様のcall直後に取得する1-slot completion metadataで受け渡す。
   終了理由を提供しないproviderは、schema-validな完全JSONをparseできた場合のみ受理する。LLM結果を
   `ok / no_changes / truncated_response / empty_response / parse_error / provider_error` に分類する。
   promptの `reviewed_card_ids` は診断用に残すが完全一致を成功条件にせず、不足・余分・未知idは `diagnostic` とlogへ警告する。
   一方、node操作が入力外のcard/node idを参照する場合は副作用を防ぐためschema/参照整合検証で拒否する。
4. `ok` と正当な空配列 `no_changes` の時だけ、node/source/status更新・run counters・カード版スタンプを
   runのstatus/completed_at更新まで含む**同一 SQLite transaction** で commit する。成功時は部分stampせず、投入した全カード版をstampする。
   実装境界は、全repositoryメソッドへ任意connectionを広げるのではなく、1 connectionを所有して途中commitしない
   `MaturationUnitOfWork` または `apply_maturation_batch()` とする。既存の個別repo APIは互換用途に残せる。
5. transaction開始時に全選択カードの `content_hash` を再検証する。1件でも変わっていればbatch全体へnode/sourceを適用せず、
   該当カードを `changed_during_run` としてrunを閉じ、新版をキューへ残す。全件一致時だけ
   `WHERE id=? AND content_hash=?` 付きでスタンプする。
   現行のStage 2→3逐次実行では通常発生しないが、長いLLM呼び出し中のUI/API編集と将来の本文mergeに備える予防的契約である。
6. `truncated_response / empty_response / parse_error / provider_error / DB error` は `failed` として記録し、カード版をスタンプしない。
   `truncated_response / empty_response / parse_error` は通常runでも有限回retry後にbatchを分割し、成功したsub-batchだけを
   それぞれtransaction適用する。retry/splitのLLM呼び出しはrun単位の上限で制限し、上限到達時は残りを未処理で返す。

これにより、LLM後・DB適用中のクラッシュは transaction rollback され、再実行で新規 node が二重作成されない。
提案 JSON は DB commit 後に再生成可能な派生 artifact とし、失敗しても成熟結果自体は巻き戻さない。

### 5.5 existing node 文脈のスコープ化

`knowledge_cards` には `topic/subject/kind` が無いため、それらとの直接一致は使わない。prompt に載せる node は次の順で合成する。

1. バッチ card id と `memory_node_sources` で既に結ばれた active/candidate/uncertain node。
2. カードの `title/tags/category` と node の `topic/statement` の正規化語彙が一致する node。
3. 残枠を直近更新 node で補う。

各集合を node id で dedup し、件数だけでなく prompt 文字数/token予算でも上限を設ける。
`stage3_node_review` prompt の「最近使われたカード」という表現も「レビューキューのカード」へ更新する。
本格解決は §9 Phase B（node 独立検索）。

### 5.6 バッチと背圧

- `knowledge_maturation_batch_size`（既定 40）: 1 batch のカード上限。
- `knowledge_maturation_max_batches_per_run`（既定 1）: 通常運用は 1。追い上げ時のみ増やせる。
- `knowledge_maturation_prompt_max_chars`（既定はモデル設定に合わせて決定）: card＋node 文脈の入力上限。
- `knowledge_maturation_retry_max_calls_per_run`（既定 8）: parse/truncation時のretry＋splitによる追加LLM呼び出し上限。
- JSON安定性が低いモデルでは `knowledge_maturation_batch_size` を小さくできる。bootstrapはさらに §6 の自動縮小を行う。
- 通常の 1 晩 1 run では最大 `batch_size × max_batches_per_run` 件を処理。バックログは複数晩、または §6 で drain。
- 旧 `max_cards`/`window_days`/`min_usage_count`/`interval_days` は**役割終了**（§11 で移行）。

---

## 6. 方針B — 初回ブートストラップ

**目的**: 既存インスタンス（過去カード多数）で有効化した瞬間に、キュー全体を一括で node 化する。

- **トリガー**:
  1. 明示 CLI: `venv/bin/python sleeptime.py stage3-bootstrap --instance <name>`（argparse の新規サブコマンド）。
  2. 自動検知は初期実装に含めない。明示実行の実績とコストを確認後、別判断とする。
- **挙動**: キューが空になるまで `batch_size` 単位で通常レビューを反復。安全上限 `bootstrap_max_cards`（既定 2000）と
  backlog 総数・最古待ち時間・`[Stage3][bootstrap] applied N / failed F / remaining R` を出す。
- **冪等・再開可能**: §5.4 の batch transaction が commit 済みのカード版だけ処理済みになる。途中クラッシュ時は
  transaction 単位で rollback され、再実行は未処理版から続く。
- **弱モデル向け縮小再試行**: §5.4の共通executorを使い、`truncated_response/empty_response/parse_error` は
  有限回retry後、batchを半分に分割して再試行する。
  1件まで縮小しても失敗するカードは、失敗理由を記録して**そのbootstrap invocation内だけ**選択対象から除外する。
  カード版はstampせず次回runでも再試行可能なまま、残りのキュー処理は継続する。`provider_error` も有限回retryし、
  provider全体の継続不能が明白な場合だけ早期終了する。
- **終了状態**: 安全上限・縮小回数・invocation内除外集合により無限ループを防ぐ。失敗カードが残れば全成功とはせず、
  `partial` と失敗一覧・未処理数を返す。弱いモデルの1件の整形失敗でキュー全体を停止させない。
- **LoCoMo との関係**: 精度 A/B では同一 post-Stage 2 clone の ON 側だけに bootstrap を実行する（§10）。

---

## 7. 方針C — reflection 強化（staleness 減衰スイープ）

現状 `last_reinforced_at` は打たれるだけで**減衰に使われていない**＝一度 active になった node は矛盾が来ない限り永久 active。
「振り返り」を機能させるため、run 末に軽量スイープを追加する。ただし実行回数依存の連続減点を避ける。

- `memory_nodes.last_decay_at` を追加し、`last_reinforced_at` と `last_decay_at` の後に経過した
  `node_stale_days` 単位の**未適用期間数**から減衰量を計算する。同じ期間に何度 run しても二重減点しない。
- `active` が閾値割れしたら `active → uncertain`。`uncertain` の長期放置は `metadata.stale=true` とするが削除しない。
- 日時は Stage 3 に注入する `now` を唯一の基準とし、本番は UTC 実時刻、評価は session 時刻を明示的に渡す。
  `datetime.now()` と `CHRONOS_NOW_ENV` の暗黙混在は避ける。
- promotion の「複数日」は `knowledge_cards.source_date` を優先し、欠損時だけ `created_at` の日付へフォールバックする。
  履歴importや同日bootstrapでも、証拠となった出来事の日付を評価できるようにする。
- LLM を追加で呼ばない SQL/repository スイープ。`memory_node_decay_enabled=False` で OFF 可。

---

## 8. 方針D — self_model 昇格ループ（提案＋任意フラグ自動反映）

**確定方針: 提案は常時出力。自動反映は reflection と LoCoMo 評価の後に実装し、opt-in の管理対象エントリだけを扱う。**

- 常時: `collect_promotion_proposals`（[knowledge_maturation.py:354](../../../butly_core/core/knowledge_maturation.py#L354)）＋
  `memory_node_proposals.json` 出力は維持する。全 eligible node を pagination して評価し、現行 `LIMIT 200` を撤去する。
  API/UI から参照・承認できる導線を追加するまでは「人間レビュー導線完成」とみなさない。
- opt-in: `memory_node_auto_apply_enabled=False`（既定）。True のとき、昇格条件
  （active ∧ confidence≥promotion_threshold ∧ supports≥min_sources ∧ 2 日以上に分散）を満たす node を
  `Key_Memory.yaml` の Stage 3 管理エントリへ反映する（既存 [key_memory.py](../../../butly_core/core/key_memory.py) API、atomic write 準拠）。
- **対象制限**: 初期版の自動反映は `subject='user' → target='human'` のみ。agent/persona/不明 subject は提案止まりとし、
  `system_instruction.txt` と `target='persona'` は自動変更しない。
- **promotion ledger**: SQLite に `memory_node_promotions` を追加し、`node_id` UNIQUE、`key_memory_entry_id`、
  `last_applied_content_hash`、`status(active/detached/removed/error)`、promoted/reconciled時刻を持つ。node metadata だけを正本にしない。
- **プロベナンスと冪等**: YAML エントリへ `source_node_id` と `managed_by: stage3` を持たせ、node id で upsert する。
  YAML書き込みとSQLiteを跨ぐため単一transactionとは呼ばず、再実行可能な reconciliation で収束させる。
- **降格・置換**: 元nodeが `uncertain/superseded` になった管理エントリは YAML から除外し ledger に履歴を残す。
  UIで内容が編集された場合は `managed_by` を外して `detached` とし、以後Stage 3は上書き・削除しない。
- **reconciliationの向き**: YAMLに `managed_by: stage3` があり、内容が `last_applied_content_hash` と一致する間だけ、
  node＋ledgerを正としてYAMLを更新・除外する。YAMLの内容変更、`managed_by` の除去、管理entryの手動削除を検出した場合は
  YAML/ユーザー操作を正としてledgerを `detached` にし、entryを再作成・上書きしない。
- **正規化計画との順序**: [memory_store_normalization_plan.ja.md](memory_store_normalization_plan.ja.md) の
  `KeyMemoryStore` 導入後はその Store を経由する。先に本Phaseへ着手する場合も、公開APIを Store へ移行しやすい境界に置く。

---

## 9. 方針E — node 注入の段階拡張（Phase A → B）

**確定方針: 段階的。まず現状のカード随伴を堅牢化し、効果を測ってから独立検索へ。**

- **Phase A（近め・現状堅牢化）**: `_lookup_active_nodes_for_candidates` を維持しつつ、
  `uncertain`/`superseded` 除外の明示・confidence 降順・重複除去・上限を確認（概ね実装済み）。
  `context_levels.rag='off'` と low/mid/high の各体裁を明示テストする。
- **Phase B（後段・独立検索。別 Phase＋評価ゲート）**: `memory_nodes.statement` を embedding
  （`embedding_blob` 追加）し、QA/Chat で **card 検索と node 検索を並走**→マージ注入。
  カード非ヒットでも解釈が届く。実装量が大きいので **§10 の ON/OFF で Phase A の効果を確認してから着手**。

---

## 10. LoCoMo での ON/OFF 評価

**狙い**: 完全に同じ knowledge card 集合・同じQAモデル上で、node 層（Stage 3）の有無だけを変えて QA 精度を比較する。
通常の full replay を2本独立に回すと Stage 2 のLLM出力が揺れ、「同一カード」の条件を満たさないため、
**post-Stage 2 正本を1回だけ作り、OFF/ONへcloneする**方式を正式なA/Bとする。

### 10.1 まず評価runnerの実行経路を完成させる

現行 [SleeptimeRunner](../../../evals/locomo/sleeptime_runner.py) は Stage 1/2 だけを直接呼び、
profile parser も `sleeptime` section を許可しない。このまま `stage3_on` profile を追加しても Stage 3 は走らないため、先に以下を実装する。

- `PROFILE_ROLE_SECTIONS` に `sleeptime` を追加し、profile適用を**再帰マージ**へ変更する。
  `update_targets.knowledge_maturation` だけの上書きで digest/knowledge_cards 等の既定を消さない。
- `SleeptimeRunner` は Stage 2 成功後に `_should_run_stage_3` を評価し、ON の時だけ Stage 3 を呼ぶ。
- `SleeptimeResult` / `sleeptime_log.jsonl` に `stage_3_status`、reviewed/created/linked/superseded、
  failed batch、prompt/completion tokensを追加する。Stage 3 失敗を Stage 2 成功へ紛れ込ませない。
- Stage 3 の `now` は runner の引数で渡す。session直後の実行では session の元日時を使い、QA時だけ設定される
  現行 `CHRONOS_NOW_ENV` に依存しない。resumeでも同じ時刻を復元する。
- `stage3_node_review` prompt をレビューキュー前提へ更新し、英日両localeを同期する。

この経路は「sessionごとにStage 3を走らせられる」統合テスト用に保持するが、正式な精度A/Bは次のclone方式を使う。

### 10.2 同一カードclone A/B

1. baseline source runを `knowledge_maturation=False` で Replay → 全sessionのStage 1/2まで完了する。
2. post-Stage 2 正本instanceを OFF run と ON run へcloneする（既存 `rerun-qa` のmemory reuseを拡張）。
3. 両cloneの `knowledge_cards` について、idと§5.2 canonical content hashの一覧・件数・DB hashを比較し、
   1件でも不一致ならQA前に失敗終了する。
4. OFF cloneはnode生成・注入とも無効のままQAする。
5. ON cloneだけ同じ knowledge model/config で `stage3-bootstrap` を実行し、`knowledge_maturation_enabled=True` でQAする。
6. 両runのQA model、gatekeeper、embedding、質問順、QA mode、locale、context_levels、乱数/temperature設定を固定する。

profileは次の2つを用意する。YAMLの継承機能は現行loaderに無いため、「baselineを継承」とは書かず、
共通設定生成helperまたは完全なprofileを使う。

| profile | Stage 3生成 | active node注入 | 用途 |
|---|---:|---:|---|
| `stage3_off` | OFF | OFF | 同一カードbaseline |
| `stage3_on` | clone後にbootstrap | ON | node層の効果 |

### 10.3 比較メトリクスとゲート

- 主: LoCoMo QA 精度（既存スコアラ）の ON vs OFF。
- 同一性: knowledge card件数・id/content hash集合が完全一致したこと。
- 副: node生成数、source link数、active/candidate/uncertain数、昇格提案数。
- コスト: Stage 3 LLM呼び出し回数・prompt/completion tokens・失敗batch数。
- 自動Key Memory反映はこの評価ではOFFに固定し、node注入だけを測る。
- 悪化、カード不一致、Stage 3失敗率超過のいずれかなら Phase B と自動反映へ進まない。

### 10.4 成果物

- `evals/locomo/profiles/stage3_off.example.yaml` / `stage3_on.example.yaml`。
- 同一source memoryからOFF/ON cloneを作る CLI/runner オプションと、card hash一致検証 artifact。
- `sleeptime_log.jsonl` のStage 3統計と、ON/OFFスコア・コスト・同一性を並べた比較表。
- [locomo_evaluation_flow.ja.md](../../reference/locomo_evaluation_flow.ja.md) / `.md` に実行手順を追記。

---

## 11. 設定キー（新旧対応）

| 旧キー | 新キー | 扱い |
|---|---|---|
| `knowledge_maturation_max_cards` | `knowledge_maturation_batch_size` | 改名（意味変化: 1 batch のカード上限） |
| `knowledge_maturation_window_days` | （廃止） | hash キューで不要 |
| `knowledge_maturation_min_usage_count` | （廃止） | 被覆保証で不要 |
| `knowledge_maturation_interval_days` | （廃止） | 現状デッド。実行頻度はscheduler/CLI側の責務に固定 |
| — | `knowledge_maturation_max_batches_per_run`（既定 1） | 新規（背圧） |
| — | `knowledge_maturation_bootstrap_max_cards`（既定 2000） | 新規（§6 安全上限） |
| — | `knowledge_maturation_prompt_max_chars` | 新規（card＋node prompt予算） |
| — | `knowledge_maturation_retry_max_calls_per_run`（既定 8） | 新規（弱モデル向けretry/splitのコスト上限） |
| — | `memory_node_decay_enabled`（既定 False） | 新規（§7 reflectionゲート） |
| — | `memory_node_stale_days`（既定 30） | 新規（§7 減衰） |
| — | `memory_node_decay_per_period` | 新規（stale期間ごとの減点量） |
| — | `memory_node_auto_apply_enabled`（既定 False） | 新規（§8 自動反映ゲート） |
| 据え置き | `memory_node_{candidate,active,promotion}_threshold` / `_promotion_min_sources` / `knowledge_maturation_enabled` | 変更なし |

旧キーは Phase 2 で読み替え（後方互換フォールバック）→ 全インスタンス移行後に削除。

---

## 12. 段階移行（Phases）

| Phase | 内容 | 破壊性 | 完了の目印 |
|---|---|---|---|
| **0** | 契約確定: canonical content/hash、LLM outcome、transaction境界、注入clock、clone A/Bを仕様化。prompt文言も更新 | なし | 失敗・同時更新・再開の状態遷移テスト表が確定 |
| **1** | キュースキーマ: `content_hash`/成熟hash/queue時刻/run_cardsを追加・backfill。全カード書き手を共通hash helperへ統合 | 追加のみ | 既存カードhashあり・非本文更新でhash不変 |
| **2** | FIFO選択、結果分類、`MaturationUnitOfWork`によるnode/source/stampの単一DB transaction、§5.5 node文脈スコープ化を実装（repo境界変更を含む高工数Phase） | 差分方式撤去 | parse/provider/DB失敗で未処理維持・クラッシュ再試行で重複なし |
| **3** | 明示 `stage3-bootstrap` CLI、進捗・安全上限・縮小再試行・再開を実装 | なし（追加） | 既存カードをdrain・中断再開・単独失敗を隔離してpartial報告 |
| **4** | reflection減衰を `last_decay_at`＋注入clockで実装。support dayを`source_date`基準へ修正 | node confidence変化（opt-in） | 同一期間の再runで二重減点なし |
| **5** | LoCoMo runner/profile/ログをStage 3対応し、同一post-Stage 2 cloneのOFF/ON A/Bを実測 | なし | card hash完全一致＋ON/OFFスコア/コスト表 |
| **6** | Phase 5がゲート通過した場合のみ、promotion ledger＋reconciliationによるKey Memory自動反映（user対象のみ） | opt-inでKey Memory変更 | 降格/置換/中断/UI編集を含め収束・冪等 |
| **7** | （評価が良ければ）Phase B: node 独立検索（§9）。別計画に切り出し可 | 追加 | node検索がカード非ヒット時にも有効 |
| **8** | 正本doc同期、旧キー削除、migration/互換コードのクリーンアップ | クリーンアップ | §14完了条件 |

各 Phase 末で `./scripts/check_before_push.sh` を緑にしてから次へ進む。

---

## 13. リスク・未確定事項

1. **hash書き手の取りこぼし**: 新しいカード更新経路が共通helperを通らないと再レビューされない。run preflightで
   非アーカイブのNULL hashを自己修復し、直接SQLをテスト/grepで監視してrepository以外の本文UPDATEを禁止する。
2. **複数ストアの整合**: Key Memory YAMLとpromotion ledgerは単一transactionにできない。§8のreconciliationと
   `source_node_id`で収束させ、atomicと誤記しない。
3. **評価clock**: QA時だけでなくSleeptime/Stage 3へsession時刻を明示注入し、resume artifactに保存する。
4. **既存 node 文脈の上限**: node 数が数百を超えると prompt が膨らむ。§5.5 の関連link＋語彙＋文字数上限で当面しのぎ、恒久策は §9 Phase B。
5. **LLM 出力品質**: JSON崩れ・空応答・既知truncationはカードを処理済みにせず、status別に観測する。
   `reviewed_card_ids` は警告に留め、有限retry・batch縮小・単独失敗隔離で弱いモデルでもキュー全体を止めない。
6. **公平性と処理能力**: FIFOで個別飢餓は防ぐが、流入超過時はbacklog自体が増える。最古待ち時間を運用指標にする。
7. **コスト**: bootstrapと縮小retryはLLM呼び出しが増える。card上限、prompt予算、retry call上限、進捗、partial終了で
   可視化・制限する。
8. **Issue #14 の語との齟齬**: 起票時「未実装」だが実体は generalization 実装済み。Issue には本計画リンクでコメントし、
   スコープ（self_model が主眼）を明記して更新する。

---

## 14. 完了条件

- [ ] 全非アーカイブカードの**各content hash版**が、成功時に1回だけレビュー済みとなる。本文変更は新hashとして再レビューされる。
- [ ] run preflightが非アーカイブのNULL hashを自己修復し、修復不能な行を黙って除外しない。
- [ ] `no_changes`だけが成功空結果としてstampされ、empty/parse/provider/DB失敗はキューに残る。
- [ ] schema-validな成功応答は投入カード版を全stampし、`reviewed_card_ids` 不一致は診断警告として残る。入力外idを使う操作は拒否される。
- [ ] providerが報告するtruncation、empty/parse/provider/DB失敗はstampされず、終了理由が無いproviderも完全JSONだけを受理する。
- [ ] 2回連続runの2回目は、本文無変更なら `reviewed_card_count == 0`。`0近傍`の曖昧条件は使わない。
- [ ] node/source更新とcard版stampの途中中断を再試行しても、node・source・run counterが重複しない。
- [ ] instance単位lockで並行runを拒否し、前processが残した `running` runだけを `abandoned` として回収する。
- [ ] FIFO被覆テストで、継続する高usage新規流入下でも既存低usageカードが処理される。
- [ ] bootstrapが安全上限・縮小再試行・単独失敗隔離・中断再開を満たし、partial時も未処理/失敗/残数を報告する。
- [ ] reflectionは同じstale期間に複数runしても一度だけ減衰し、閾値割れactiveをuncertainへ降格する。
- [ ] LoCoMo OFF/ONのknowledge card id/content hash集合が完全一致し、同一QAモデルでスコア・コスト表が出る。
- [ ] Phase 5通過後、auto applyがuser nodeだけをKey Memoryへ冪等反映し、降格/置換で除外、手編集後はdetachedとなる。
- [ ] reconciliationは未編集のStage 3管理entryだけをnode/ledgerから更新し、YAMLの編集・管理解除・削除をdetachedとして尊重する。
- [ ] `system_instruction` と persona target は自動反映で不変。
- [ ] `memory_lifecycle.ja.md`/`.md` に Stage 3 の運用（キュー・ブートストラップ・昇格）が反映。
- [ ] `locomo_evaluation_flow.ja.md`/`.md` にclone A/B・clock・Stage 3 logが反映。
- [ ] 旧キー（`max_cards`/`window_days`/`min_usage_count`/`interval_days`）撤去。`./scripts/check_before_push.sh` 緑。

---

## 15. 影響範囲（ファイル）

- 改修:
  [knowledge_maturation.py](../../../butly_core/core/knowledge_maturation.py)（FIFO選択・NULL hash preflight・`_card_view`の`source_date`・
  provider終了理由を含む結果分類・`MaturationUnitOfWork`・減衰・提案）/
  [sleeptime.py](../../../sleeptime.py#L1009)（instance単位process lock・run開始順・縮小再試行付きbootstrap CLI・注入clock・減衰/reconciliation呼び出し）/
  [database.py](../../../butly_core/core/database.py#L53)（card hash/queue列・run_cards・promotion ledger、後段でnode embedding）/
  [memory_nodes.py](../../../butly_core/core/memory_nodes.py)（Unit of Work用の単一connection/commit境界・last_decay・source_date日数・promotion ledger）/
  [key_memory.py](../../../butly_core/core/key_memory.py)（Stage 3管理entry・detached/reconciliation）/
  [routers/instances.py](../../../routers/instances.py)（memory node proposalの参照・承認・却下導線）/
  [config.py](../../../butly_core/config.py#L105) ・ [settings/defaults.py](../../../butly_core/settings/defaults.py#L95)（設定キー §11）
- 改修: [llm/base.py](../../../butly_core/llm/base.py) と `butly_core/llm/providers/`・`protocols/`
  （`classify()` の戻り値互換を保ったcompletion metadata/finish reason受け渡し）
- 改修: [evals/locomo/config.py](../../../evals/locomo/config.py)（`sleeptime` profile＋再帰merge）/
  [evals/locomo/sleeptime_runner.py](../../../evals/locomo/sleeptime_runner.py)（Stage 3実行・clock・統計）/
  [evals/locomo/replay.py](../../../evals/locomo/replay.py)（同一memory clone A/B・hash検証・resume）
- 追加: `evals/locomo/profiles/stage3_off.example.yaml` / `stage3_on.example.yaml`（§10）
- 同期: `butly_core/prompts/locales/{ja,en}/stage3_node_review.txt`
- 同期: [memory_lifecycle.ja.md](../../reference/memory_lifecycle.ja.md) / `.md`、[locomo_evaluation_flow.ja.md](../../reference/locomo_evaluation_flow.ja.md) / `.md`、
  [tests/test_knowledge_maturation.py](../../../tests/test_knowledge_maturation.py)（hash/FIFO/失敗/transaction/clock/bootstrap/decay/reconciliation）と
  `tests/evals/`（profile、Stage 3 runner、同一card clone A/B）
- Issue: [#14](https://github.com/unagisann/Butly/issues/14) に本計画リンクとスコープをコメント
