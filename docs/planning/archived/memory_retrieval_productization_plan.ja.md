# 記憶検索・RAW注入の本体反映計画

> **ステータス: 実装完了（2026-09-04）。** LoCoMoで整備したHybrid Evidence Fusionと
> RAW注入設定をButly本体へ反映し、後段rerankerを将来追加できる共通境界まで実装した。
> 起票・完了: 2026-09-04

## 実装結果

- 記憶検索10項目をdefaults／global／instance／effective／originに分けて解決し、
  global・instance別のtyped GET/PATCH APIとatomic persistenceを追加した。
- vector／dual query／hybrid／Evidence Fusionのprimary候補を共通post-rerankerへ渡し、
  未設定identity、不正出力・例外時fail-open、候補列診断を統一した。rerankerは既定OFFのまま。
- 正式Desktop UIへインスタンス別の基本／詳細フォーム、継承解除、RAWの0値、依存fieldの
  disabled表示、backend応答を正とする保存処理を追加した。
- Traceへ実効設定snapshot、RAWの実文字数・上限・ファイル・truncationを残し、
  OpenAPI、生成client、設定例、日英の正本文書、unit／contract／UIテストを同期した。
- グローバル既定`search_mode=vector`とFusion重み`0.70`は維持した。実instanceのopt-inと
  全体既定の昇格判断は、利用者が対象を選んだ後の運用・観測事項として本変更には含めない。

## 0. 結論

本体の検索経路を次の段階へ分離する。

```text
候補生成                 一次順位                    任意の後段処理        最終選択
vector / BM25 / hybrid → Evidence Fusion 70/30 → post-reranker (既定OFF) → top-k注入
```

- Fusionの`base_weight`は**既定0.70**とするが、固定値にはせず設定で変更可能にする。
- RAWの注入元、文字数上限、展開カード数、前後近傍も本体設定と正式UIから変更可能にする。
- `rag_raw_neighbor_radius=1`はUI上では「前後±1」と表示するが、保存値は非負の半径`1`とする。
- 現在の`rag_raw_max_chars`は**文字数上限**であり、トークン上限ではない。UIも
  「RAW最大文字数」と表示し、`memory.max_raw_tokens`（中期RAWキャッシュ用）と混同しない。
- この計画ではrerankerモデルを採用しない。Fusion後の候補列へ安全に差し込める契約、
  診断、fallbackだけを用意し、既定はNoOpのままにする。
- 既存インスタンスの挙動を暗黙に変えない。`search_mode`の全体既定を
  `vector`から`hybrid_evidence_fusion`へ昇格する判断は、本計画の配線完了とは分離する。

これを、検索方式の探索フェーズと本体製品化フェーズの区切りとする。

## 1. 背景

評価経路では次がすでに動作している。

- `hybrid_evidence_fusion`: vector＋BM25の候補をHybrid化し、カードのEpisode / RAW
  passage順位と既定70/30で再融合する。
- `rag_source_mode`: `cards` / `raw` / `both`を選択する。
- `rag_raw_top_k`: RAWへ展開する上位カード数を制御する。
- `rag_raw_max_chars`: 注入するRAW抜粋の文字数予算を制御する。
- `rag_raw_neighbor_radius`: 正確な`source_files`と同じ日付内の前後ファイルを追加する。
- LoCoMo / 日本語対話A/BのWeb UIから、これらの評価条件を変更できる。

一方、本体側は次の状態にある。

1. 設定キー自体は`SYSTEM_CONFIG`とインスタンス`config.json`で解決できるが、正式Desktop
   UIには記憶検索設定画面がない。
2. legacy Streamlitの本体設定画面にも、評価画面と同等の検索・RAW設定フォームはない。
3. 一般rerankerはvector検索経路の中にだけ実装されている。
4. `hybrid` / `hybrid_evidence_fusion`はreranker指定時に`unsupported`でfallbackする。
5. `fused_candidate_ids`と`effective_candidate_ids`はあるが、Fusion後に共通処理を
   差し込むパイプライン境界として統一されていない。

関連する既存計画・評価記録:

- [検索改修計画](../active/retrieval_hybrid_search_plan.ja.md)
- [RAW注入の見直し計画](../active/raw_injection_plan.ja.md)
- [RAG評価・改善レポート](../../history/rag_evaluation_report.ja.md)
- [正式フロントエンド移行計画](../active/frontend_migration_plan.ja.md)
- [pydantic-settings設定統合計画](../active/pydantic_settings_plan.ja.md)

## 2. 目標

### 2.1 機能目標

1. 本体でFusionを選択でき、既定0.70の重みを変更できる。
2. RAW注入に関する主要値を、グローバル既定とインスタンス上書きの両方で変更できる。
3. 設定変更は次のチャットターンから反映し、プロセス再起動を要求しない。
4. Fusion後・top-k確定前に任意のrerankerを差し込める。
5. reranker未設定・無効・失敗時は、現在のFusion順位をそのまま使う。
6. Traceと評価成果物から、実際に使われた設定値と各段階の候補順位を確認できる。

### 2.2 安全目標

- 既存設定ファイルに対象キーが無ければ、`defaults.py`の既定値で従来どおり動く。
- `0`を「未指定」と誤認しない。`rag_raw_max_chars=0`と`rag_raw_top_k=0`の既存意味を保つ。
- 不正値・NaN・未知のenumを保存前に拒否し、実行時の暗黙clampに頼らない。
- 設定ファイルはatomic writeし、対象セクション以外を消さない。
- reranker障害でチャット全体を失敗させない。

## 3. 非目標

- LoCoMoに最適なFusion重みを本番既定にすること。
- Fusion 40/60を本番採用すること。初期値は70/30を維持する。
- rerankerモデル、学習データ、最終アルゴリズムを今回決定すること。
- `rag_raw_max_chars`を名前だけ変えてトークン上限として扱うこと。
- `memory.max_raw_tokens`をRAG原文注入の予算へ流用すること。
- BM25の内部column weightやscan実装まで一般ユーザー向けUIへ露出すること。
- LoCoMo評価フォームを本体設定画面として再利用すること。

## 4. 設定モデル

### 4.1 解決順

既存の設定解決順を維持する。

```text
defaults.py
  → user_config.json（グローバル上書き）
  → instances/<name>/config.json（インスタンス上書き）
  → 評価run / リクエスト単位override（通常チャットUIには公開しない）
```

正式UIでは各インスタンス値に「グローバル設定を使う」を設ける。継承へ戻すときは
既定値を書き込まず、インスタンス`config.json`からそのキーを削除する。

### 4.2 初回公開する設定

| UIグループ | 設定キー | 現行既定 | 入力契約 | 備考 |
|---|---|---:|---|---|
| 検索 | `brain.search_mode` | `vector` | `vector` / `hybrid` / `hybrid_evidence_fusion` | Fusionの本体選択を可能にする。既定昇格は別判断 |
| 検索 | `memory_probe.vector_search_limit` | 3 | 整数 1〜10 | 最終注入カード数。候補poolとは別 |
| Fusion | `brain.evidence_fusion_base_weight` | **0.70** | 有限小数 0.0〜1.0、UI step 0.05 | Evidence側は`1 - base_weight`として併記 |
| Fusion | `brain.evidence_raw_chunk_chars` | 1800 | 整数 200〜10000 | 順位計算用RAW passage。注入予算ではない |
| 候補pool | `brain.vector_candidates` | 20 | 整数 3〜100 | 最終注入数以上を必須にする |
| 候補pool | `brain.bm25_candidates` | 20 | 整数 3〜100 | Hybrid/Fusion時だけ有効 |
| RAW注入 | `memory.rag_source_mode` | `cards` | `cards` / `raw` / `both` | raw/both時だけ以下のRAW設定が有効 |
| RAW注入 | `memory.rag_raw_top_k` | 1 | 整数 0〜20 | `0`は全候補。負値の新規保存は拒否 |
| RAW注入 | `memory.rag_raw_max_chars` | 2500 | 整数 0〜50000 | **文字数**。`0`は無制限として警告表示 |
| RAW注入 | `memory.rag_raw_neighbor_radius` | 0 | 整数 0〜10 | `0`=無効、`1`=前後±1 |

`rrf_k`、`time_decay_rate`、BM25の詳細値はAPIのtyped schemaには含められるが、初回の
本体UIでは「開発者向け詳細設定」に隔離する。通常利用者へ一度に全内部値を見せない。

### 4.3 UIでの依存関係

- `search_mode != hybrid_evidence_fusion`ではFusion欄をdisabled表示する。保存値は維持する。
- `rag_source_mode == cards`ではRAW欄をdisabled表示する。保存値は維持する。
- Fusion重みは「Hybrid/Base 70%・Evidence 30%」の両方を表示する。
- 近傍半径は「0（なし）」「±1」「±2」…と表示し、負数を入力させない。
- RAW文字数には推定トークン数を補助表示してもよいが、保存・制限の単位は文字数と明記する。
- 無制限値`0`は通常の数値入力と分け、明示的な「無制限」toggleでのみ設定可能にする。

## 5. 検索パイプライン境界

### 5.1 目標形

検索方式ごとの早期returnを減らし、内部的には次の共通成果物へ揃える。

```text
RetrievalCandidates
├── candidates              # 最終top-kで切る前の候補dict列
├── source_rankings         # vector / BM25 / hybrid
├── primary_ranking         # Fusion適用後、またはbase順位
└── diagnostics
```

共通の最終化処理を1か所に置く。

```python
ranked = primary_ranker.rank(query, candidates)
effective = post_reranker.rerank(query, ranked)  # 未設定ならidentity
selected = effective[:injection_limit]
```

既存`RerankerConfig`と`rerank(query, candidates, top_n)`の契約は再利用する。ただし、
候補本文の作り方（カードのみ／将来のRAW近傍bundle）はreranker本体から分離し、後から
差し替えられる`candidate_text_builder`境界を置く。

### 5.2 診断契約

| フィールド | 意味 |
|---|---|
| `vector_candidate_ids` | vector順位 |
| `bm25_candidate_ids` | BM25順位 |
| `hybrid_candidate_ids` | vector＋BM25融合後の順位 |
| `fused_candidate_ids` | Evidence Fusion適用後、post-reranker前の順位 |
| `reranked_candidate_ids` | post-rerankerが完了した場合の順位 |
| `effective_candidate_ids` | 実際にtop-k選択へ使った最終順位 |

`reranker`診断には`enabled/status/fallback/model_name/latency_ms/selected_count/error`を残す。
失敗時は`fallback=true`とし、`effective_candidate_ids == fused_candidate_ids`を保証する。

### 5.3 互換性

- reranker未設定では、候補順・注入カード・RAW解決結果を変更しない。
- vector＋既存reranker経路も共通最終化処理へ移し、既存設定を維持する。
- `hybrid_evidence_fusion`＋rerankerの`unsupported`分岐と、Web評価側の
  `reranker currently requires search_mode=vector`制約は、共通処理のテスト後に外す。
- 後段rerankerが要求する候補数を確保してからFusionし、最後にtop-kへsliceする。
- 複数instance検索では`source_instance`を候補から失わない。

## 6. API計画

正式Desktop frontendはlegacy `/config`へ直接依存させず、`/api/v1`にtyped resourceを追加する。

### 6.1 グローバル設定

```text
GET   /api/v1/settings/memory-retrieval
PATCH /api/v1/settings/memory-retrieval
```

- GETは`defaults`、保存済みglobal override、effective値を分けて返す。
- PATCHはallowlistされたキーだけを部分更新し、`user_config.json`へatomic writeする。
- 保存成功後にsettings cacheとlegacy互換値を同期し、次ターンから反映する。

### 6.2 インスタンス設定

```text
GET   /api/v1/instances/{instance_name}/settings/memory-retrieval
PATCH /api/v1/instances/{instance_name}/settings/memory-retrieval
```

- GETはglobal effective値、instance override、最終effective値、各値のoriginを返す。
- PATCHはインスタンス`config.json`の`brain` / `memory` / `memory_probe`だけを部分更新する。
- `null`は「グローバル設定を使う」への復帰として、該当overrideキーを削除する。
- 他のprofile、モデル、prompt設定を上書きしない。
- 未知キーは422、範囲外・非有限値も422、存在しないinstanceは404とする。

OpenAPI snapshotと生成TypeScript clientを同じ変更で更新する。

## 7. UI計画

正式Desktop UIのインスタンス設定に「記憶検索」セクションを追加する。

### 基本設定

- 検索方式
- 最終注入カード数
- 注入内容（カード／RAW／両方）
- RAW最大文字数
- RAWへ展開するカード数
- RAW近傍（なし／±1／±2…）

### 詳細設定

- Fusion Hybrid/Base重み
- Evidence passage chunk文字数
- vector候補数
- BM25候補数
- 有効値と継承元の表示

各入力は「保存」までローカルstateに保持する。保存後はbackendから再取得し、正規化後の
effective値を表示する。失敗時に楽観表示を残さない。

legacy Streamlit本体UIは移行期間の互換経路として同じAPIを呼ぶ薄いフォームを追加してよいが、
設定ロジックとvalidationを`app.py`へ複製しない。正式Desktop UIを正とする。

## 8. 実装フェーズ

### Phase 1: typed設定契約

- API用のrequest/response schemaと共通validationを定義する。
- defaults、global、instance、effective、originを返せるresolverを作る。
- `0`を保持するpartial updateと、`null`によるinherit復帰を実装する。
- `user_config.json.example`へ全公開キーと説明を追加する。

**完了条件:** UIなしでAPI round-tripでき、既存configを壊さない。

### Phase 2: Fusion後post-reranker境界

- vector / hybrid / Fusionの候補生成と最終top-k sliceを分離する。
- 共通`post-reranker`適用処理を追加する。
- 未設定identity、例外時Fusion fallback、診断保存を実装する。
- Fusionとの組み合わせを拒否しているbackend validationを解除する。
- rerankerモデルは既定OFFのままにする。

**完了条件:** rerankerなしの既存Fusion結果が完全一致し、fake rerankerでFusion後の順序を
変更できる。

### Phase 3: 本体API・正式Desktop UI

- versioned APIのGET/PATCHを追加する。
- OpenAPIと生成clientを更新する。
- instance設定画面へ基本／詳細フォームを追加する。
- 継承、依存fieldのdisabled表示、無制限警告、保存エラーを実装する。
- 必要ならlegacy Streamlitを同じAPIへ接続する。

**完了条件:** ファイルを直接編集せず、UIから各設定を変更・継承へ復帰できる。

### Phase 4: 観測とrollout

- Chat Traceへ実効設定snapshotを追加する。
- Fusion / rerankerの各順位、fallback、RAW文字数、ファイル数、truncationを確認可能にする。
- 既存instanceで設定なし／明示的に現行値を保存した場合の同値性を確認する。
- 対象instanceだけFusionをopt-inし、通常対話で関連性・遅延・不要記憶注入を確認する。
- `search_mode`の全体既定昇格は、この観測結果を別の判断記録として決める。

**完了条件:** 本体利用で設定の由来・実効値・検索結果を追跡でき、既存利用者へ暗黙の挙動変更がない。

## 9. テスト計画

### 設定・API

- 既定値としてFusion weight `0.70`を返す。
- global overrideよりinstance overrideが優先される。
- `0`が欠落扱いにならず、`rag_raw_max_chars=0` / `rag_raw_top_k=0`として保存される。
- `null`でinstance overrideだけが削除される。
- 不正enum、NaN、範囲外、候補数とtop-kの矛盾を422にする。
- PATCHしても無関係な設定セクションと秘密情報を保持する。
- 書き込み失敗時に既存JSONを壊さない。

### 検索パイプライン

- rerankerなしのvector / hybrid / Fusion順位が変更前と一致する。
- fake rerankerが`fused_candidate_ids`を入力として受け取る。
- reranker成功時だけ`reranked_candidate_ids`が最終順位になる。
- reranker失敗・空結果・不正出力時はFusion順位へ戻る。
- final top-kより深い候補poolをrerankerへ渡せる。
- 複数instance候補の`source_instance`を保持する。

### RAW注入

- `cards`ではRAWファイルを読まない。
- `raw` / `both`で文字数、top-k、近傍半径のeffective値が反映される。
- `neighbor_radius=0`と未指定が同じ結果になる。
- `neighbor_radius=1`で同日±1だけを追加する。
- 正確なprovenanceが近傍より先に文字数予算へ入る。
- 文字数0、top-k 0、truncation、欠損ファイルの既存意味を保つ。

### Frontend

- APIのeffective値とoriginを初期表示する。
- inactive fieldがdisabledになる。
- 「グローバル設定を使う」で`null` PATCHを送る。
- 0、0.70、±1の表示と送信値が一致する。
- backendエラー時に保存成功表示を出さない。

push前は`./scripts/check_before_push.sh`を唯一の正として実行し、frontend工程がSKIPされた場合は
CIの`windows-desktop.yml`でlint / typecheck / test / build / cargo fmtを確認する。

## 10. 文書更新

実装と同じ変更で最低限以下を更新する。

- `docs/reference/configuration.ja.md` / `.md`
- `docs/reference/memory_lifecycle.ja.md` / `.md`
- `docs/reference/FILE_STRUCTURE.ja.md` / `.md`
- `docs/reference/frontend_chat.ja.md` / `.md`、または新しい正式設定画面の仕様書
- `user_config.json.example`
- OpenAPI snapshotと生成client
- 本計画書のステータスと`docs/planning/README.ja.md`

評価UI固有の設定は`docs/reference/evaluation_web_console.ja.md`へ残し、本体設定との違いを明記する。

## 11. リスクと対策

| リスク | 対策 |
|---|---|
| LoCoMo最適値を日常会話へ持ち込む | 既定70/30を維持し、重み変更は明示設定に限定 |
| RAW増加で関連性とtoken使用量が悪化 | 文字数上限・top-k・近傍を独立設定し、Traceへ実量を記録 |
| 「文字数」と「トークン数」の混同 | UI・API・docsで単位を明記し、`max_raw_tokens`と別物として扱う |
| 設定が増え、利用者が迷う | 基本／詳細を分け、inactive fieldをdisabled表示 |
| 0を未指定扱いして無制限設定が消える | `exclude_unset`と明示的な`None`判定を使う |
| reranker障害で会話が失敗する | fail-openでFusion順位へ戻し、診断へ理由を残す |
| 既存instanceへ挙動変更が入る | 欠落キーはdefaultsへfallbackし、default昇格を別判断にする |

## 12. この計画での区切り

以下が揃った時点で、Hybrid / Fusion / RAW注入の本体反映を完了として本計画をarchiveする。

1. Fusion weight 0.70を初期値として、グローバル・インスタンス別に変更できる。
2. RAW source、最大文字数、展開カード数、前後近傍を本体UIから変更できる。
3. 実効値と継承元がAPI・UI・Traceで確認できる。
4. Fusion後rerankerの差し込み口があり、未設定時の結果が現在と完全一致する。
5. rerankerモデルは未採用・既定OFFで、将来の別計画へ分離されている。
6. 正式仕様、設定例、OpenAPI、テストが同期している。

この後のreranker選定、RAW近傍bundleを使う候補本文生成、multi-hop集合選択は、
この共通境界を前提とした独立計画として扱う。
