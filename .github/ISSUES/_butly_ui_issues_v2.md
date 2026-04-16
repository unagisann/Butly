# Butly UI 正常化 + 機能拡張 Issue 一覧

Streamlit Prototype UI の残課題・改善、および SillyTavern / Backyard AI 比較から導出された機能拡張を管理する。

---

## 既存 Issue（UI 正常化）

---

### Issue #1: Context Cache の UI 削除

**Priority**: High（今回の作業に含める）  
**Labels**: `ui`, `cleanup`

#### 概要
Context Cache はデフォルト OFF で、現在のトークン規模（〜1万トークン）では恩恵がない。
設定画面からトグルを削除し、`app.py` の `initialize_system` 内の `prepare_cache` 呼び出しも削除する。
`gemini.py` のキャッシュ関連メソッド自体はコード上残す（将来のエージェント機能用）。

#### タスク
- [ ] `app.py` `render_instance_settings_screen()` から「コンテキストキャッシュ」トグルを削除
- [ ] `app.py` `initialize_system()` 内の `prepare_cache` 呼び出しを削除
- [ ] `cached_content` の戻り値を `initialize_system` から除去（または常に `None`）
- [ ] config のデフォルト値 `use_context_cache: false` は維持

---

### Issue #2: チャット画面 — 直近メッセージの削除と再生成（Swipe）

**Priority**: Medium  
**Labels**: `ui`, `feature`

#### 概要
現在、直近の会話ターン（user + model）をUI上から削除する手段がない。
また、応答の再生成機能もない。
SillyTavern の Swipe（応答を複数生成して選択）や Backyard AI の再生成機能に相当。

#### タスク
- [ ] 直近の model 応答の横に「🗑️ 削除」ボタンを追加
  - `session_state.messages` から末尾の user + model を削除
  - 対応する `short_term_json` ファイルも削除（API エンドポイント追加が必要な場合あり）
- [ ] 直近の model 応答の横に「🔄 再生成」ボタンを追加
  - `session_state.messages` から末尾の model 応答のみ削除
  - 同じ user メッセージを `/chat` に再送信
  - 対応する `short_term_json` の model 応答も更新

#### 注意点
- Streamlit の再描画モデルとの相性に注意（`st.rerun()` のタイミング）
- 再生成時は Gatekeeper の再判定も走るため、tier が変わる可能性がある（これは許容）
- 将来的に Swipe（複数候補の保持と選択）に拡張する余地を残す設計にする

---

### Issue #3: モデルパラメータ設定の拡充

**Priority**: Low  
**Labels**: `ui`, `enhancement`

#### 概要
インスタンス設定画面に `temperature` と `max_tokens` はあるが、`top_p` が UI に露出していない。
また、プロバイダによる対応差を明示する。

#### タスク
- [ ] インスタンス設定画面に `top_p` スライダーを追加
- [ ] `top_k` は Gemini 固有のため、Gemini 選択時のみ表示（または caption で注記）
- [ ] 各パラメータの横に対応プロバイダの注記を追加
  - `temperature`: 全プロバイダ対応
  - `max_tokens`: 全プロバイダ対応
  - `top_p`: 全プロバイダ対応
  - `top_k`: Gemini のみ

#### 対象外（このIssueでは扱わない）
- `reasoning` 設定: プロバイダ横断の統一仕様がないため見送り
- `streaming`: Next.js 移行時に本格対応

---

### Issue #4: Memory 設定の補完

**Priority**: Medium  
**Labels**: `ui`, `config`

#### 概要
中期記憶の要約版（digest / relationship）のサイズ制御が不足している。

#### タスク
- [ ] `mid_term_digest` の最大文字数設定を `config.json` に追加
  - 現在はプロンプト側での制御のみ
  - config 値をプロンプトの `{max_chars}` 変数に渡す形が理想
- [ ] `mid_term_relationship` の最大文字数設定を `config.json` に追加
  - 同上
- [ ] インスタンス設定画面に上記2つの設定項目を追加
  - 既存の「長期記憶 最大文字数」の近くに配置

#### 注意点
- 現在の `max_mid_term_chars` は RAW 累積テキスト用
- digest と relationship は別ファイルなので、別の設定値が必要
- プロンプト側（`midterm_digest.txt`, `midterm_relationship.txt`）に `{max_chars}` 変数を追加する修正も含む

---

### Issue #5: RAG デバッグにスコア表示を追加

**Priority**: Low  
**Labels**: `ui`, `debug`

#### 概要
Debug モードの「Brain Process」セクションに RAG 検索のスコア（cosine similarity）が表示されていない。

#### タスク
- [ ] `ChatResponse` にスコア情報を含める（`refs` の各要素に `score` フィールド追加）
- [ ] `brain.py` の `search_knowledge` がスコアを返すようにする（既に内部では計算済みのはず）
- [ ] `app.py` の Debug 表示で各 ref のスコアを表示

---

### Issue #6: Sleeptime 管理画面に実行履歴を追加

**Priority**: Low  
**Labels**: `ui`, `enhancement`

#### 概要
Sleeptime の最終実行日時と、最後に生成されたナレッジカードの情報を表示する。

#### タスク
- [ ] Sleeptime 実行完了時にタイムスタンプを保存する仕組みを追加
  - `instance_dir/sleeptime_last_run.json` など
  - 内容: `{"last_run": "2026-03-29T03:00:00", "cards_created": 5}`
- [ ] `render_sleeptime_screen()` に最終実行日時と作成カード数を表示
- [ ] DB ブラウザ画面に最新カードの作成日時を表示（既存の `created_at` フィールドを利用）

---

### Issue #7: Interactions API 残骸の完全クリーンアップ

**Priority**: High（Part A で主要部分は完了済み。残存ファイルの対応）  
**Labels**: `cleanup`

#### 概要
Part A で Interactions API のコードは削除済みだが、既存インスタンスに `last_interaction_id.txt` が残っている可能性がある。

#### タスク
- [ ] マイグレーションスクリプト or Sleeptime に `last_interaction_id.txt` の自動削除を追加
  - 実害はないが、ファイル構成の清潔さのために
- [ ] README のファイル構成から `last_interaction_id.txt` の記載がないことを確認

---

## 新規 Issue（SillyTavern / Backyard AI 比較から導出）

---

### Issue #8: Lorebook（キーワードトリガー型コンテキスト注入）

**Priority**: High（将来の RP 対応・ペルソナ対応の基盤）  
**Labels**: `feature`, `memory`, `architecture`

#### 概要
SillyTavern の World Info / Lorebook に相当する機能。
会話中にキーワードが出現したとき、対応するエントリの内容をプロンプトに自動注入する。
既存の knowledge_cards（RAG = embedding 類似度検索）とは**別の DB / 別のテーブル**として管理する。

Lorebook は「確定的トリガー（キーワード一致）→ 確定的注入」であり、
RAG の「曖昧検索 → スコア上位を注入」とは検索原理が異なる。
**tier に関係なく常に検索をかける（reflex でも）** 点が、既存 RAG（cortex のみ）と決定的に違う。

#### SillyTavern の Lorebook データ構造（参考）
- `keys`: トリガーキーワード（カンマ区切り、正規表現対応）
- `secondary_keys`: AND 条件の副キーワード（任意）
- `content`: 注入テキスト本文
- `comment`: 作成者用メモ（注入されない）
- `insertion_order`: 複数エントリ発火時の優先度
- `position`: プロンプト内の注入位置（before/after system, before/after chat 等）
- `depth`: 注入深度（チャット履歴の何ターン前に挿入するか）
- `scan_depth`: 何ターン分の会話をキーワードスキャン対象にするか
- `case_sensitive`: 大文字小文字の区別
- `match_whole_words`: 単語単位マッチ
- `use_group_scoring`: グループチャット時のスコアリング
- `character_filter`: 特定キャラにのみ発火させるフィルタ
- `enabled`: ON/OFF
- `constant`: 常時注入（キーワード不要で常にプロンプトに含める）
- `delay_until_recursion`: 再帰スキャン時にのみ発火

#### Butly への統合方針（設計メモ）
1. **保存先**: `butly_memory.db` 内に `lorebook_entries` テーブルを新設
   - knowledge_cards とは完全に別テーブル
   - ペルソナ対応時に instance 単位で持つので、既存の per-instance DB に収まる
2. **検索タイミング**: `memory_builder.py` の `build()` 内で、tier 判定**後**・RAG 検索**前**に実行
   - 全 tier（reflex 含む）で常にスキャン
   - スキャン対象: user_input + 直近 N ターンの会話テキスト（scan_depth で制御）
3. **注入位置**: `build_context_prefix()` に新セクション `=== LOREBOOK ===` を追加
   - RAG の前に配置（Lorebook は確定情報、RAG は参考情報）
4. **最小 MVP**: keys（カンマ区切り）+ content + enabled のみ
   - position / depth / regex / recursion 等は将来拡張

#### タスク
- [ ] `lorebook_entries` テーブル設計（最小: id, keys, content, enabled, insertion_order, created_at, updated_at）
- [ ] `database.py` に Lorebook CRUD メソッド追加
- [ ] `memory_builder.py` にキーワードスキャン + 注入ロジック追加
- [ ] `build_context_prefix()` に `=== LOREBOOK ===` セクション追加
- [ ] API エンドポイント追加（`/lorebook/{instance_name}/entries`）
- [ ] UI: Lorebook 管理画面（エントリ一覧・追加・編集・削除・ON/OFF トグル）

#### 将来拡張（この Issue では扱わない）
- secondary_keys（AND 条件）
- 正規表現キーワード
- position / depth 制御（注入位置の柔軟化 → Issue #13 と連携）
- character_filter（グループチャット時）
- constant エントリ（常時注入）
- SillyTavern Lorebook JSON のインポート機能
- delay_until_recursion（再帰スキャン）

---

### Issue #9: Character Card V2 インポート

**Priority**: Medium  
**Labels**: `feature`, `integration`

#### 概要
SillyTavern / Backyard AI エコシステムの Character Card（V2 仕様: PNG 画像にメタデータ埋め込み）を
Butly のインスタンスとしてインポートする機能。
ローカル LLM ユーザーが既存のキャラ資産をそのまま Butly で使える。

#### Character Card V2 のフィールド → Butly マッピング

| Card V2 フィールド | Butly 対応先 |
|---|---|
| `name` | config.json `agent.name` / Key_Memory の AI名 |
| `description` | system_instruction.txt に統合 |
| `personality` | system_instruction.txt に追記 |
| `scenario` | system_instruction.txt に追記（またはセッション初期コンテキスト） |
| `first_mes` | 初回チャット時の AI 応答として使用 |
| `mes_example` | system_instruction.txt の few-shot セクションに追記 |
| `creator_notes` | インポート時に表示のみ（注入しない） |
| `system_prompt` | system_instruction.txt の先頭に配置 |
| `post_history_instructions` | build_context_prefix の末尾に注入（Author's Note 相当） |
| `tags` | config.json のメタデータとして保存 |
| `character_book` (embedded lorebook) | → Issue #8 の lorebook_entries にインポート |

#### タスク
- [ ] PNG メタデータ（tEXt チャンク）からの Character Card JSON 抽出
- [ ] フィールドマッピング + インスタンス自動生成ロジック
- [ ] embedded lorebook の自動インポート（Issue #8 の lorebook_entries へ）
- [ ] UI: インポート画面（ファイルアップロード → プレビュー → 確認 → 生成）
- [ ] Backyard AI の `.byaf` 形式対応は将来検討

#### 注意点
- `description` と `personality` の統合方法はユーザーに選択させる（結合 or 分離）
- `system_prompt` がある場合、既存の personality テンプレートとの競合処理
- avatar 画像の保存先を決める必要がある（instance_dir/avatar.png 等）

---

### Issue #10: ユーザーペルソナ（User Persona）

**Priority**: Medium  
**Labels**: `feature`, `persona`

#### 概要
SillyTavern では「ユーザー側」も複数ペルソナを持て、キャラごとに自動切り替えできる。
Butly の Key_Memory には user_name / nickname があるが、
ユーザーの詳細な自己記述をプロンプトに注入する仕組みがない。

コンパニオン用途でも「仕事モードの自分」「趣味モードの自分」の切り替えは自然なニーズ。

#### 現状の Key_Memory 構造
```
AI Name: ジャービス
User Name: 悠希
Nickname: ゆうき
```

#### 拡張案
```
butly_core/
├── user_personas/
│   ├── default.txt      ← デフォルトのユーザー記述
│   ├── work.txt         ← 仕事モード用
│   └── hobby.txt        ← 趣味モード用
```

- `build_context_prefix()` に `=== USER PERSONA ===` セクションを追加
- KEY_MEMORY の直後、MID-TERM の前に配置
- インスタンスごとに「どのユーザーペルソナを使うか」を config で指定
- SillyTavern のようにキャラ↔ペルソナの自動バインドも将来対応

#### タスク
- [ ] ユーザーペルソナのファイル構造と読み込みロジック設計
- [ ] `memory_builder.py` に user_persona 注入を追加
- [ ] `build_context_prefix()` に `=== USER PERSONA ===` セクション追加
- [ ] config.json に `user_persona` フィールド追加
- [ ] UI: ユーザーペルソナ管理画面（作成・編集・切り替え）
- [ ] UI: インスタンス設定画面にユーザーペルソナ選択ドロップダウン追加

---

### Issue #11: TTS 統合（音声合成）

**Priority**: Low（RP 対応時に優先度上昇）  
**Labels**: `feature`, `multimedia`

#### 概要
SillyTavern は ElevenLabs / XTTS / Silero / AllTalk / システム TTS 等を統合。
Backyard AI もボイスチャット対応。Butly には音声入出力がない。

Raspi 5 でローカル TTS はリソース的に厳しいため、
外部 API or ネットワーク上の別マシンへの委譲が現実的。

#### 検討すべき方式
- **Piper TTS**（ローカル、軽量、Raspi で動作報告あり）
- **XTTS v2**（ローカル、高品質だが GPU 推奨）
- **ElevenLabs / OpenAI TTS**（クラウド API）
- **VOICEVOX**（ローカル、日本語に強い、ただし Raspi 対応は要検証）

#### タスク
- [ ] TTS プロバイダ抽象化設計（BaseTTSProvider）
- [ ] 最小 MVP: OpenAI TTS API or Piper での実装
- [ ] AI 応答テキストの音声再生（Streamlit `st.audio` で出力）
- [ ] config.json に TTS 設定セクション追加
- [ ] UI: TTS ON/OFF トグルと音声プロバイダ選択

#### 関連
- Issue #12（表情・スプライト）と連携し、口パク同期等も将来検討

---

### Issue #12: 表情・スプライト表示（SessionState mood 連動）

**Priority**: Low（RP 対応時に優先度上昇）  
**Labels**: `feature`, `multimedia`, `ui`

#### 概要
SillyTavern はキャラの感情に応じてスプライト画像を自動切替し、
さらに Live2D / VRM アニメーションもサポートする。
Butly の SessionState には `mood` フィールドがあるため、
そこからスプライト切替に繋げるパスは設計上自然。

#### 最小 MVP
1. instance_dir に `sprites/` フォルダを追加
   - `neutral.png`, `happy.png`, `sad.png`, `angry.png`, `thinking.png` 等
2. SessionState の `mood` 値からスプライト画像を選択
3. チャット画面の AI アバター部分に表示

#### タスク
- [ ] スプライト画像のフォルダ構造と命名規則を決定
- [ ] mood → スプライト名のマッピング設定（config or 固定ルール）
- [ ] `app.py` のチャット描画部分にスプライト表示ロジック追加
- [ ] Character Card V2 インポート時にアバター画像を default sprite として保存

#### 将来拡張
- LLM による感情分類（SillyTavern の Classify 拡張に相当）
- Live2D / VRM 対応（Next.js 移行後が現実的）

---

### Issue #13: 動的プロンプト注入位置の制御（Author's Note）

**Priority**: Low  
**Labels**: `feature`, `architecture`

#### 概要
SillyTavern の Author's Note に相当する機能。
プロンプトの任意の深さ（depth）に動的に情報を挿入できる。
「今のシーンの雰囲気」「物語の方向性」等をリアルタイムに変更可能。

現在の `build_context_prefix()` の注入順序は固定:
```
[ラベル + 注意文] → CURRENT TIME → MID-TERM → RAG → FLOATING → TIER INFO
```

#### 拡張案
- インスタンスごとに `authors_note.txt` を持てるようにする
- UI で内容をリアルタイム編集可能
- 注入位置（depth）を設定可能にする（SillyTavern は会話履歴の N ターン前に挿入）
- Character Card V2 の `post_history_instructions` のインポート先としても機能

#### タスク
- [ ] `authors_note.txt` のファイル管理と読み込みロジック
- [ ] `build_context_prefix()` に注入位置制御ロジック追加
- [ ] config.json に `authors_note_depth` フィールド追加
- [ ] UI: チャット画面からアクセスできる Author's Note 編集パネル

---

### Issue #14: 記憶 DB の UI 分離（通常記憶 vs Lorebook）

**Priority**: Medium（Issue #8 と同時に対応）  
**Labels**: `ui`, `memory`

#### 概要
Issue #8 で Lorebook テーブルを追加した後、
設定画面で「通常記憶（knowledge_cards）」と「Lorebook」を明確に分離して管理する。
将来的に、チャット時にどの記憶ソースを使うか選択可能にする。

#### UI 構成案
```
設定画面
├── 📚 記憶管理
│   ├── 🧠 ナレッジカード（既存の DB ブラウザ）
│   │   - RAG 検索（embedding 類似度）で使用
│   │   - cortex tier でのみ検索
│   └── 📖 Lorebook（新規）
│       - キーワードトリガーで使用
│       - 全 tier で常時検索
│       - エントリ一覧・追加・編集・削除・ON/OFF
├── ⚙️ 記憶ソース設定（将来）
│   ├── [✓] ナレッジカード（RAG）を使用
│   ├── [✓] Lorebook を使用
│   └── [✓] Mid-term summary を使用
```

#### タスク
- [ ] DB ブラウザ画面のタブ分離（ナレッジカード / Lorebook）
- [ ] Lorebook 専用の CRUD UI
- [ ] 記憶ソース ON/OFF 設定（config.json に `memory_sources` セクション追加）
- [ ] memory_builder.py で config の記憶ソース設定を参照

---

### Issue #15: RP 用設定画面（ロールプレイモード）

**Priority**: Low（ペルソナ対応の基盤整備後）  
**Labels**: `feature`, `ui`, `roleplay`

#### 概要
コンパニオン用途とロールプレイ用途で必要な設定が異なる。
RP 用の設定画面を別タブまたはモードとして用意する。

#### コンパニオンモード vs RP モードで異なる設定

| 設定項目 | コンパニオン | RP |
|---|---|---|
| Lorebook | 任意 | ほぼ必須 |
| Author's Note | 不要 | 必須（シーン制御） |
| ユーザーペルソナ | シンプル（名前程度） | 詳細（キャラ設定） |
| Swipe / 再生成 | あると便利 | 必須 |
| 表情・スプライト | あると良い | ほぼ必須 |
| グループチャット | 不要 | あると良い |
| 記憶の永続化 | 重要（年単位） | チャット単位 |
| SessionState | 自動更新 | 手動制御もほしい |
| first_mes (初回メッセージ) | 不要 | 必須 |

#### UI 構成案
```
インスタンス設定
├── 基本設定（既存）
├── 🎭 RP 設定（新規タブ）
│   ├── Author's Note 編集
│   ├── Scenario 設定
│   ├── First Message 設定
│   ├── Lorebook リンク
│   ├── 表情マッピング設定
│   └── 出力フォーマット設定（アクション記法 *...* 等）
```

#### タスク
- [ ] RP 設定タブの UI 設計
- [ ] config.json に `mode: "companion" | "roleplay"` フィールド追加
- [ ] モードに応じた UI 表示の出し分けロジック
- [ ] RP モード固有の設定項目の実装（Author's Note, Scenario, First Message）

#### 前提 Issue
- Issue #8（Lorebook）
- Issue #10（ユーザーペルソナ）
- Issue #13（Author's Note）

---

### Issue #16: チャット分岐（Chat Bookmarks）

**Priority**: Low  
**Labels**: `feature`, `ui`

#### 概要
SillyTavern ではチャットの任意の地点に Bookmark を打ち、そこから分岐チャットを作成できる。
コンパニオン用途では優先度低いが、RP 用途では「もしあの時こう答えていたら」を試せる。

#### タスク
- [ ] short_term_json の特定ターンからの分岐コピー機能
- [ ] 分岐チャットの一覧・選択 UI
- [ ] memory_builder が参照するチャット履歴の切り替え

---

## 優先順位まとめ

| Priority | Issue | 内容 | カテゴリ |
|----------|-------|------|---------|
| **High** | #1 | Context Cache UI 削除 | UI 正常化 |
| **High** | #7 | Interactions API 残骸クリーンアップ | UI 正常化 |
| **High** | #8 | Lorebook（キーワードトリガー型コンテキスト注入） | 新機能 |
| **Medium** | #2 | チャット — 削除・再生成（Swipe） | UI 正常化 |
| **Medium** | #4 | Memory 設定補完（digest/relationship サイズ制御） | UI 正常化 |
| **Medium** | #9 | Character Card V2 インポート | 新機能 |
| **Medium** | #10 | ユーザーペルソナ | 新機能 |
| **Medium** | #14 | 記憶 DB の UI 分離（通常記憶 vs Lorebook） | 新機能 |
| **Low** | #3 | モデルパラメータ拡充（top_p 等） | UI 正常化 |
| **Low** | #5 | RAG スコア表示 | UI 正常化 |
| **Low** | #6 | Sleeptime 実行履歴 | UI 正常化 |
| **Low** | #11 | TTS 統合 | 新機能 |
| **Low** | #12 | 表情・スプライト（mood 連動） | 新機能 |
| **Low** | #13 | Author's Note（動的注入位置制御） | 新機能 |
| **Low** | #15 | RP 用設定画面 | 新機能 |
| **Low** | #16 | チャット分岐（Bookmarks） | 新機能 |

---

## 依存関係

```
#8 Lorebook ──────┬──→ #14 記憶DB UI分離
                  ├──→ #15 RP設定画面
                  └──→ #9 Character Card V2（embedded lorebook）

#10 ユーザーペルソナ ──→ #15 RP設定画面

#13 Author's Note ────→ #15 RP設定画面
                  ────→ #9 Character Card V2（post_history_instructions）

#2 再生成（Swipe）────→ #16 チャット分岐

#12 表情スプライト ──→ #11 TTS（口パク同期）
```

---

## SillyTavern の機能で意図的に見送ったもの

| 機能 | 見送り理由 |
|---|---|
| グループチャット（複数AI同時会話） | Phase C のペルソナ/エージェント階層で別アプローチ |
| Visual Novel Mode | RP 特化。Next.js 移行後に検討 |
| STscript / マクロ | パワーユーザー向け。Butly の設計思想と距離がある |
| Regex フィルタ | 有用だが優先度低。必要時に個別対応 |
| 拡張マーケットプレイス | コミュニティ規模が異なる。当面は本体に統合 |
| Data Bank（ベクトル検索用テキストファイル格納） | Butly は knowledge_cards + embedding_blob で対応済み |
| Talkativeness / Natural Order | グループチャット機能を見送っているため不要 |
