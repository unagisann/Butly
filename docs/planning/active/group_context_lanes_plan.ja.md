# 話者帰属とコンテキストレーン 設計計画書

> ステータス: Phase 1 実装済み（2026-07-08）。Phase 2 以降は未着手
>   （フロントエンド土台・observability との兼ね合いで後回し、§8 参照）。
> 作成日: 2026-07-07
> 位置づけ: 多人数コンテキスト（Discordグループ等）に向けた記憶基盤の拡張計画。
> 本文書は設計のインテークスナップショットであり、着手時は GitHub issue を正とする。
> 本文書内の Phase 番号は GitHub issue 番号とは無関係。

---

## 0. 背景と目的

Butly の北極星を以下のように定式化する。

> **Butly は一つの人格であり、1対1・グループ・どの環境でも、出会った人間を個別に憶えている。文脈（チャンネル・状況）は人格が出入りする部屋にすぎない。**

Discord アダプタは既に稼働しており、グループチャンネルで複数の人間が Butly に話しかける状況は現実に発生しうる。しかし現状:

- 保存される会話ターンに **話者の帰属情報がない**（全員が role="user" に潰れる）
- 外部 ID は instance 解決後に破棄され、記憶に到達しない
- 非メンション発言（部屋の中の他人同士の会話）は **知覚すらされない**

検索・注入・見せ方は後からいくらでも作り直せるが、**書き込み時に失われた帰属は復元不可能**。
よって本計画の中核は「読み出しの高度化」ではなく「**書き込み時に person_id / lane を刻むこと**」である。

### スコープ内
- 人物レジストリ（person_id とエイリアス管理）
- 会話ターンへの話者帰属の刻印
- レーン（direct / ambient）の導入と Discord での ambient 知覚
- ambient_context の context_levels 統合
- Sleeptime のレーン別固化（direct=従来 / ambient=感想カード）
- provenance タグ（firsthand / overheard）

### スコープ外（やらない）
- audience レーンの実装（VTuber 的コメント処理）— enum 予約のみ
- 多人数 relationship graph
- 検索時の person_id フィルタ（データが溜まってから）
- ambient のリアルタイム LLM 要約（Sleeptime 夜間バッチで足りる）
- LINE のグループ対応（LINE は 1:1 スコープを維持）
- 過去ログへの帰属の遡及付与（原理的に不可能。諦める）

---

## 1. 設計原則

1. **書き込み時帰属**: person_id と lane はメッセージ保存時に刻む。後段のすべて（検索・カード・注入）はこれを前提にできるが、逆は成立しない。
2. **知覚レベル原則**（「要約の要約禁止」の一般化）: Sleeptime の入力は、そのレーンにおける知覚レベルの記録とする。direct の知覚原本は逐語ログ、ambient の知覚原本も RAW ログだが、**固化の出力は感想（印象）でよい**。機械的な多段圧縮（要約の要約）は引き続き禁止。
3. **圧縮はビュー、保存は RAW**: プロンプト注入用のローリング表示は使い捨て。DB / ファイルには常に lane タグ付き RAW を残す（監査証跡・実証主義の担保）。
4. **本文は汚さない**: 外部 ID・person_id を会話本文（text）には混ぜない方針は維持。帰属は**構造化メタデータ**として持つ。ただし「保存後に破棄」の現行方針は本計画で改訂する（§6）。
5. **enum 最小化 + other フォールバック**: Stage 3 と同じ流儀。lane / provenance / kind に未知値が来ても壊れない。
6. **劣化戦略**: 弱い環境（lite プロファイル）では ambient を off にする。「雑踏で耳が遠くなる」だけで人格は壊れない。
7. **データ駆動の閾値**: adoption gate の N、開示抑制の強度、ambient 注入のデフォルトレベルは、実データが溜まるまで決めない。

---

## 2. 用語とデータモデル

### 2.1 person_id

人物を一意に示す内部 ID。**ユーザー名・表示名はキーにしない**（変更されうる・プラットフォーム間で不一致）。

- 形式: `p_` prefix + 短い識別子（例: `p_yuki`）
- オーナー（悠希）は `is_owner: true` の登録済み person
- 未登録の外部ユーザーは**決定的な仮 ID を自動発行**する: `p_{source}_{hash}`（例: `p_discord_9d589f7b1b7c3a11`）。手動登録を待たずに帰属を確保するため。後から正式 person にマージ可能（エイリアス統合）。外部 ID は RAW ログに直接出さない。

### 2.2 人物レジストリ（persons.json）

`DATA_DIR/persons.json`。external_accounts.json と同じ流儀（JSON 手編集 + atomic write、SQLite 化は後回し）。

```json
{
  "persons": {
    "p_yuki": {
      "display_name": "悠希",
      "is_owner": true,
      "aliases": {
        "discord": ["123456789012345678"],
        "line": ["Uxxxxxxxxxxxxxxxx"]
      },
      "name_variants": ["悠希", "unagisann"]
    }
  }
}
```

- 解決順: `(source, external_user_id)` → aliases 完全一致 → hit なければ仮 ID 発行
- LINE ペアリング機構は既に (source, external_id) → 本人 の対応を持っているため、レジストリの入力源として統合可能（実装時に確認）
- 仮 ID から正式 person へのマージ: `merge_person(from_id, to_id)` — RAW ログは書き換えず、レジストリに `merged_into` を記録し**読み出し時に解決**する（RAW 不変の原則）

### 2.3 lane

メッセージの知覚経路。

| lane | 意味 | 保存先 | 応答生成 |
| --- | --- | --- | --- |
| `direct` | Butly 宛の発話（メンション・DM・1:1・名前呼び） | short_term_json（従来経路） | する |
| `ambient` | 部屋の中の他人同士の会話 | ambient_log/（新設、§3.2） | しない |
| `audience` | コメント欄（**予約のみ、実装しない**） | — | — |
| その他 | 未知値は `other` として direct 相当で安全側に倒す | | |

### 2.4 provenance

**派生物（ナレッジカード・感想カード）に付与する**出所タグ。メッセージ自体には付けない（lane から導出できるため。冗長フィールドを持たない）。

| provenance | 由来 |
| --- | --- |
| `firsthand` | direct レーンの会話から抽出 |
| `overheard` | ambient レーンから抽出 |
| その他 | `other` フォールバック |

想起時、`overheard` 由来は断定調を避けヘッジ付きで出す（「〜って言ってた気がする」）。プロンプト側の指示で実現し、追加機構は作らない。

### 2.5 short_term_json のスキーマ拡張

現行:
```json
{"timestamp": "...", "messages": [{"role": "user", "parts": [...]}, {"role": "model", "parts": [...]}]}
```

拡張（メッセージ単位に optional な `meta` を追加）:
```json
{"timestamp": "...",
 "messages": [
   {"role": "user", "parts": [...],
    "meta": {"person_id": "p_discord_123", "display_name": "たろう",
             "lane": "direct", "source": "discord",
             "channel_key": "guild_id:channel_id"}},
   {"role": "model", "parts": [...]}
 ]}
```

- **後方互換**: `meta` 欠落時は `person_id=owner / lane=direct / source=web` とみなす（過去ログ・Web チャットはこの規則で正しく解釈される）。マイグレーション不要。
- `display_name` は**その時点のスナップショット**。表示・Sleeptime 整形用。同一性の判定には使わない。
- `channel_key` は状況フレームデータ（CURRENT TIME と同格）。「チャンネルは窓」の原則どおり記憶の分離キーにはしないが、ambient のスコープ判定（§4.2）に必要。

---

## 3. レーン設計

### 3.1 direct レーン

従来の会話経路そのもの。変更は「meta が付くこと」のみ。

宛先判定（Discord アダプタ内、既存の `is_bot_mentioned` を拡張）:
1. bot メンション → direct
2. DM / 1:1 → direct
3. bot への reply → direct
4. **名前呼び**（instance の呼称文字列が本文に含まれる）→ direct に昇格 ＝ カクテルパーティー効果。呼称リストは instance 設定に持つ（表記ゆれは name_variants で吸収）
5. 上記以外 → ambient（capture 有効チャンネルのみ、§3.2）

### 3.2 ambient レーン（新設・知覚の追加）

**現状の Discord アダプタは非メンション発言を無視しており、ambient は「保存の分岐」ではなく「知覚の新設」である。** 以下の性質を持つ:

- **チャンネル単位のオプトイン**。`external_accounts.json` に `ambient_capture` フラグを追加（default: false）。プライバシーと従量課金の両面から、明示的に有効化したチャンネルだけ聴く。
- ambient メッセージは **LLM を一切呼ばない**。runtime.chat() を経由せず、保存のみ（コストゼロ・遅延ゼロ）。
- 保存先: `instances/{name}/ambient_log/{YYYY-MM-DD}.jsonl`（1 行 = 1 メッセージ、meta 付き）。short_term_json に混ぜない — short_term は毎ターン逐語注入されるため、ambient を混ぜるとプロンプトが雑談で膨張する。
- ローテーション: Sleeptime 処理済みの日次ファイルは `memory_archive/` 配下へ移動（監査証跡として保持。SQLite/テキストの容量は無視できる）。

### 3.3 audience レーン

実装しない。lane enum に値として存在を認めるだけ（コメントで予約を明記）。マイグレーション不要で将来追加できることが確認できていれば十分。

---

## 4. 既存機構への写像

### 4.1 Gatekeeper

- 宛先判定（§3.1）は **adapter 層**で行う。Gatekeeper には手を入れない — Gatekeeper は軽量判断層であり、重くする変更はしない（既定原則）。
- 将来 ambient の文脈が tier 判定に効くと分かったら、その時に `ambient_present: bool` 程度のヒントを classify 入力に足す（今はやらない）。

### 4.2 context_levels — `ambient_context` セクション新設

context_levels に 1 セクション追加するだけで注入制御が完結する（新機構不要）。

| レベル | 出力 |
| --- | --- |
| `high` | `=== AMBIENT (周囲の会話) ===` + 同一 channel_key の直近 N 行（`「表示名」: 発言` 形式、行単位トリム） |
| `low` | `[周囲] {直近の話題を1行}` ヘッダなし |
| `off` | なし |

- 注入は**応答対象メッセージと同じ channel_key の ambient のみ**。別の部屋の雑談は注入しない（状況フレームの一貫性）。
- v1 は RAW 行の切り出しのみ（LLM 要約なし）。ローリング LLM 要約は必要性が実証されてから。
- プリセット: `normal` = high（ただし ambient データが存在する時のみ出力）、`compact` / `low` = off。**lite 環境は自動的に「耳が遠い」**。
- 生成した注入ビューは保存しない（原則 3）。

### 4.3 Sleeptime — レーン別固化

| | direct | ambient |
| --- | --- | --- |
| Stage 1（digest） | 従来どおり | **対象外**（mid_term を汚さない） |
| Stage 2（knowledge card） | 従来どおり + provenance=`firsthand` | **感想カード生成**（新プロンプト）+ provenance=`overheard` |
| エピソードカード | 従来どおり | **作らない**（傍聴は当事者エピソードではない） |
| 人物カード（Stage 3） | kind=`person` の更新に寄与 | kind=`person` の更新に寄与（overheard 重み） |

**感想カード**: Sleeptime が ambient_log の RAW（前日分）を読み、「周りでこんなことがあった」という一人称の印象として固化する。
- 入力は必ず RAW（注入用ビューを食べない ＝ 多段圧縮の構造的排除）
- 出力先: 既存 `knowledge_cards` テーブル（新テーブルは作らない）。`provenance` カラムを 1 本追加（ALTER TABLE、default `firsthand` で既存カード無傷）
- 分量の目安: 1 日 1 チャンネルあたり最大 1 枚。盛り上がりがなければ 0 枚（「特筆なし」を許可するプロンプトにする）

**多人数会話の整形**: Sleeptime が 1_integrated / ambient_log を整形する際、**複数話者が存在する場合のみ**各発言に `「display_name」:` プレフィックスを付ける。帰属を刻んでも Sleeptime の LLM に見えなければ意味がないため、これは Phase 1 の必須項目。1:1（オーナーのみ）の場合は従来どおり無プレフィックス（既存プロンプトの挙動を変えない）。

### 4.4 Stage 3 との関係

- 人物カードは Stage 3 の knowledge card に `kind="person"` を足す形で相乗り。**本計画は新サブシステムを建てない**。
- 本計画の Phase 1（帰属書き込み）は Stage 3 に**依存しない**し、先行して問題ない。むしろ Stage 3 の人物カード生成が始まる前に帰属付きデータが溜まっているほど良い。
- `memory_edges`（bi-temporal）が入った際、person_id はエッジの端点候補になる。今は設計メモに留める。

### 4.5 adoption gate（人物カードの昇格閾値）

全登場人物にカードを作ると不特定多数で破綻する。**N 回以上 / M 日以上にわたって登場した person のみ** kind=`person` カードに昇格させる。
- N, M は**実データが溜まってから決定**（importance score 飽和の教訓と同じ手順）
- それまでは Sleeptime に集計だけ仕込む: person_id ごとの登場回数・初出/最終日時を記録（persons.json 内 or 軽量テーブル）。**集計は Phase 1 から動かし、判断は保留**する

---

## 5. 安全設計

### 5.1 誤帰属の固定化対策

ambient 記憶には本人による訂正ループがない（第三者は Butly の記憶を見ない）。
- RAW ログを監査証跡として恒久保持（§3.2）
- 感想カードに provenance=`overheard` を刻み、想起時はヘッジ表現（§2.4）
- person マージで判明した誤同定は読み出し時解決で吸収（§2.2）

### 5.2 開示事故の防止

「B さんが転職を考えているらしい」を A さんとの会話で自発的に開示するのは事故。
- v1 の対策: overheard 由来のカードは、**会話相手がその情報の主体本人でない場合、自発的想起の優先度を下げる**方針を設計メモとして明記
- 実装（検索スコアへの反映・抑制の強度）は**実データ待ち**。閾値なしで機構だけ先に作らない

### 5.3 プライバシー

- persons.json は外部 ID を含むためローカル管理（既存の external_accounts.json と同等の扱い）
- debug log に外部 ID を残さない現行方針は維持。person_id（内部 ID）は debug log 可
- ambient capture はオプトイン（§3.2）。他人の会話を聴く機能である以上、既定 off は思想的にも必須

---

## 6. 既存方針の改訂（明文化）

`external_chat_design_decisions.ja.md`（アーカイブ済み）の以下の方針を改訂する:

| 旧方針 | 新方針 |
| --- | --- |
| 外部 ID は instance 解決に使った後、保持不要なら破棄してよい | (source, external_user_id) は person_id に解決し、**構造化メタデータとして保存経路まで貫通させる** |
| 会話記憶に外部 ID を混ぜない | **本文に混ぜない点は維持**。meta フィールドに person_id / lane / channel_key を持つ |

改訂理由: 話者帰属は書き込み時にしか確保できず、多人数コンテキスト対応の前提条件であるため。

---

## 7. 実装フェーズ

TDD 順序（テスト → 実装）。各 Phase 完了時にフルテスト回帰を確認する。

### Phase 1: 人物レジストリ + 書き込み時帰属（最優先・今のトリガーはこれ）— ✅ 実装済み (2026-07-08)

実装メモ:
- person 解決は `ButlyRuntime._attach_person()`（全入口の共通チョークポイント）で実施。
  adapter は外部 ID + display_name スナップショットを `ChatRequest` に載せるだけ。
- meta 組み立ては `ChatService._build_turn_meta()`、読み出し側の後方互換規則は
  `butly_core/core/turn_meta.py` に集約。
- 複数話者プレフィックスは「整形バッチ内に 2 人以上の話者がいる場合のみ」適用
  （Sleeptime Stage 1 / Stage 2・maintain_memory・raw_memory_cache の 4 経路）。
- 登場回数集計は Sleeptime Stage 1 が `persons.json` の `stats` に記録（判断は保留のまま）。
- LINE ペアリングと persons.json の統合（§9）は未実施のまま先送り。LINE は現行 1:1
  スコープのため、未登録 alias は owner として扱う。ペアリングは instance 割り当てのみを担い、
  レジストリは aliases（手編集）+ 決定的仮 ID で自立して機能する。

Discord グループで発言が潰れて保存される問題は**現在進行形のデータ損失**であり、他のすべてに先行する。

- `butly_core/external/person_registry.py` 新設（純粋ロジック・SDK 非依存、account_mapping.py と同じ流儀）
  - 解決 / 仮 ID 発行 / マージ（読み出し時解決）/ 登場回数集計
- `ChatRequest` → `save_single_turn` へ meta を貫通させる
- short_term_json / 1_integrated の meta 対応（後方互換読み出し込み）
- Sleeptime 整形の複数話者プレフィックス対応（§4.3）
- テスト: レジストリ解決優先順位 / 決定的仮 ID / meta 欠落時のデフォルト解釈 / 複数話者整形 / マージの読み出し時解決

**完了条件**: Discord グループで複数人が Butly に話しかけたとき、各ターンに正しい person_id が刻まれ、Sleeptime の digest に話者名が現れる。既存 1:1 チャットの挙動・既存テストに変化がない。

### Phase 2: ambient 知覚（Discord）

- `external_accounts.json` に `ambient_capture` フラグ追加
- Discord アダプタ: 非メンション発言の ambient_log/ への保存（LLM 呼び出しなし・応答なし）
- 名前呼び → direct 昇格の判定追加
- テスト: capture off 時に何も起きない / on 時に JSONL 追記 / 名前呼び昇格 / メンションは従来どおり

**完了条件**: opted-in チャンネルの雑談が ambient_log に溜まり、Butly の応答挙動・コストが一切変わらない。

### Phase 3: ambient_context 注入

- context_levels に `ambient_context` セクション追加（high / low / off、プリセット反映）
- MemoryBlockBuilder: 同一 channel_key の直近 N 行を切り出して注入
- テスト: レベル別出力 / channel_key スコープ / データ不在時の無出力 / low プリセットで off

**完了条件**: グループチャンネルで Butly に話しかけたとき、直前の周囲の会話を踏まえた応答ができる。1:1 チャットには何も注入されない。

### Phase 4: Sleeptime レーン別固化

- ambient_log を入力とする感想カード生成（新プロンプト、locales/ja に追加）
- `knowledge_cards` に `provenance` カラム追加（ALTER TABLE / default `firsthand`）
- direct 由来カードへの provenance=`firsthand` 付与
- 処理済み ambient_log の memory_archive/ 移動
- テスト: 感想カードの provenance / 「特筆なし」時の 0 枚 / RAW 入力の担保（ビュー非依存）/ 既存カードの無傷確認

**完了条件**: 夜間 Sleeptime 後、前日の ambient から感想カードが生成され、RAG で想起可能。既存の direct 経路の固化に変化がない。

### Phase 5: データ駆動の調整（実データが溜まってから着手）

- adoption gate の N / M 決定と kind=`person` 昇格（Stage 3 実装後）
- overheard カードの想起重み・開示抑制の実装
- ambient 注入レベルのデフォルト調整
- 必要なら persons.json → SQLite 移行

**着手条件**: Phase 2 の ambient データが数週間分蓄積し、SQLite エクスポート分析で傾向が読めること。

---

## 8. 優先順位と他計画との関係

- **Phase 1 のみ即時着手を推奨**（データ損失の停止。小さく、Stage 3 非依存）
- Phase 2〜4 は observability 整備・Stage 3 との兼ね合いで順番を決める。本計画が observability より先行する理由はない
- Phase 5 は意図的に非スケジュール（データ待ち）

---

## 9. 未決事項（実データ / 実装時判断）

| 項目 | 決定タイミング |
| --- | --- |
| adoption gate の N / M | ambient + 帰属データ数週間分の分析後 |
| 感想カードに embedding を付けるか（RAG 対象にするか） | Phase 4 実装時。v1 は付ける想定だが、ノイズ源になるなら外す |
| 名前呼び判定の表記ゆれ範囲 | Phase 2 実装時（instance 呼称リストの粒度） |
| LINE ペアリングと persons.json の統合方法 | Phase 1 実装時に既存コード確認 |
| ambient 注入のデフォルト行数 N | Phase 3 で仮置き → Phase 5 で調整 |

---

## 10. 完了判定（計画全体）

- Discord グループで複数の人間と自然に会話でき、後日「〇〇さんが言ってた△△」を正しい人物に帰属して想起できる
- 傍聴した会話が翌日「周りでこんなことあったな」という感想として記憶に残り、断定ではなくヘッジ付きで語られる
- lite プロファイルでは ambient が透過的に無効化され、人格・1:1 体験に影響がない
- 全 RAW ログが lane / person_id 付きで監査可能な状態で残っている
- 既存の 1:1 チャット・LINE 1:1・フルテストに回帰がない
