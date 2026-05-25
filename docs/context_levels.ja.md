# context_levels 仕様書

🌐 **日本語** | [English](context_levels.md)

`context_levels` は、LLMに渡すコンテキストの各要素に `high / mid / low / off` の4段階レベルを設定できる機能です。
プリセット（normal / compact / low / custom）で一括切り替えが可能で、小規模ローカルLLM向けの軽量化や、API利用時のコスト最適化に活用できます。

---

## プリセット一覧

| プリセット | 用途 | 特徴 |
|---|---|---|
| `normal` | API向け・デフォルト | 全セクション `high`。情報量最大 |
| `compact` | 中規模ローカルLLM向け | label_notes / glossary を off、key_memory を low |
| `low` | 7B前後の小規模LLM向け | ヘッダ・注釈なし、各セクションを大幅圧縮 |
| `custom` | 個別設定 | 各要素を任意に組み合わせ |

---

## レベル別出力仕様

### `system_instruction`（性格設定）

| レベル | 出力 |
|---|---|
| `high` | `=== SYSTEM INSTRUCTION ===\n{system_instruction.txt 全文}` |
| `low` | `{system_instruction_low.txt}` ヘッダなし。ファイルがなければ通常版にフォールバック |
| `off` | off不可（最低限の通常版を出力） |

### `key_memory`（根幹記憶）

| レベル | 出力 |
|---|---|
| `high` | `=== KEY MEMORY (根幹記憶) ===\n{Key_Memory.txt 全文}` |
| `low` | `[会話核] {Key_Memory_low.txt}` ヘッダなし。ファイルがなければ通常版にフォールバック |
| `off` | なし |

### `label_notes`（ラベル・注意文）

| レベル | 出力 |
|---|---|
| `high` | `[背景情報]\n※ 情報の優先順位: ...` |
| `low` | なし（off と同じ） |
| `off` | なし |

### `current_time`（現在時刻）

| レベル | 出力 |
|---|---|
| `high` | `=== CURRENT TIME ===\n{時刻}\n※上記の時刻情報は...` |
| `low` | `{時刻のみ1行}` （例: `2026-04-04 21:00 (金)`） |
| `off` | なし |

### `glossary`（共通言語辞書）

| レベル | 出力 |
|---|---|
| `high` | `=== GLOSSARY ===\n{全エントリ}` |
| `low` | なし（off と同じ） |
| `off` | なし |

### `mid_term`（中期記憶）

| レベル | 出力 |
|---|---|
| `high` | `=== MID-TERM DIGEST ===\n※以下は...\n{全文}` + relationship |
| `low` | `[直近の背景]\n{末尾4行}` ヘッダ・注釈なし |
| `off` | なし |

> `low` では文字の途中で切れないよう、行単位で末尾から取得します。

### `rag`（長期記憶 RAG）

| レベル | 出力 |
|---|---|
| `high` | `=== LONG-TERM MEMORY (RAG) ===\n※以下は...\n{全結果}` |
| `low` | `[関連記憶]\n{先頭3行}` ヘッダ・注釈なし |
| `off` | なし |

> `low` では完全な文を維持するよう、行単位で先頭から取得します。

### `session_digest`（会話圧縮ログ）

| レベル | 出力 |
|---|---|
| `high` | `=== SESSION DIGEST ===\n{全文}` |
| `low` | なし（off と同じ。直近会話要約で代替） |
| `off` | なし |

### `tier_info`（Tier情報）

| レベル | 出力 |
|---|---|
| `high` | `=== TIER INFO ===\n現在の思考モード: {tier}\n現在の話題: {topic}` |
| `low` | なし（off と同じ。小規模モデルには意味をなさない） |
| `off` | なし |

### `web_search`（Web検索結果）

| レベル | 出力 |
|---|---|
| `high` | `=== WEB SEARCH RESULTS ===\n{全結果}` |
| `low` | `{結果のみ、最大300文字、ヘッダなし}` |
| `off` | なし |

---

## LOW版ファイルの管理

`system_instruction` と `key_memory` の `low` レベルでは、専用の簡略ファイルを参照します。

### ファイル配置

```
instances/{name}/
├── system_instruction.txt       ← 通常版（既存）
├── system_instruction_low.txt   ← LOW版（簡略）
├── Key_Memory.txt               ← 通常版（既存）
└── Key_Memory_low.txt           ← LOW版（簡略）
```

### フォールバックルール

1. `_low.txt` が存在し、コメント（`#`）以外の行がある → そちらを使用
2. `_low.txt` が存在しないか、コメントのみ → 通常版を使用

> インスタンス作成時に `_low.txt` はコメントのみのテンプレートとして自動生成されます。
> コメントを書き換えずそのままにしておくことで、自動的に通常版へフォールバックします。

---

## プロンプト構成例

### normal プリセット（API向け・フル情報）

```
[system message]
=== SYSTEM INSTRUCTION ===
私はユーザーに寄り添い、心地よい対話を提供する存在である。
目的はユーザーが安心して考え、行動できるよう支えること。
丁寧で柔らかい口調を用い、温かさと親しみを大切にする。
（...全文...）

=== KEY MEMORY (根幹記憶) ===
AI名: ルナ
ユーザー名: ばとりー
呼称: マスター

[user message - context prefix]
[背景情報]
※ 以下は現在の会話を補助する背景情報です。
※ 情報の優先順位: 直近の会話履歴 > ...

=== CURRENT TIME (現在時刻) ===
2026-04-04 21:00 (金)
※上記の時刻情報は文脈理解のための参考情報です。

=== GLOSSARY ===
- ルナ: AI名。親しみやすく温かい対話スタイル。

=== MID-TERM DIGEST (中期記憶・事実ダイジェスト) ===
※以下はAIの主観的な記憶です。
（...全文...）

=== RELATIONSHIP SNAPSHOT (関係性スナップショット) ===
（...全文...）

=== SESSION DIGEST ===
直前の会話で天気の話をしました。

=== TIER INFO ===
現在の思考モード: mid
現在の話題: 趣味の開発とAIへの関心について
```

---

### low プリセット（小規模LLM向け・最小限）

```
[system message]
ユーザーに寄り添い、心地よい対話を提供する。温かく丁寧な口調。
[会話核] AI名: ルナ / ユーザー名: 悠希（マスター） / 役割: 思考を拡張するパートナー

[user message - context prefix]
2026-04-04 21:00 (金)

[直近の背景]
- 悠希とAIルナによる「Butly」プラットフォーム上の対話テストを実施。
- 趣味の開発とローカルLLMの最適化について議論中。
- AI開発が趣味でリフレッシュを図っている。
- 宮崎県在住、年度初めの多忙な時期。

[関連記憶]
- Streamlit環境でのUI実装において、Selectboxとカスタム入力を並べる方針で合意。
- DEBUGモードの実装が完了。
- OllamaモデルのTemplate設定が不正だった問題を解決。

[会話履歴 - 直近ターン]
[現在のユーザー入力]
```

---

## Gatekeeper OFF 時の tier 分岐

`low` プリセット使用時は Gatekeeper OFF を推奨します（UIに警告メッセージを表示）。
Gatekeeper OFF 時の動作は以下の通りです。

| Gatekeeper | RAG | tier | 説明 |
|---|---|---|---|
| ON | — | 動的判定 | 従来通り。LOWモード時も Gatekeeper ON 可 |
| OFF | ON | `mid` (固定) | `need="rag_search"` を立てて常時 RAG 検索を実施 |
| OFF | OFF | `mid` (固定) | RAG 無し |

---

## 設定方法

### インスタンス設定画面（app.py）

1. インスタンス設定画面を開く
2. 「🧩 コンテキスト注入設定」セクションで「プリセット」を選択
3. `low` 選択時は Gatekeeper 設定で無効化を推奨
4. 「Custom」選択時は詳細設定で各要素を個別に変更可能
5. 「順序設定」でセクションの並び順を変更可能

### config.json（直接編集）

```json
{
  "context_levels": {
    "preset": "low",
    "levels": {
      "system_instruction": "low",
      "key_memory": "low",
      "label_notes": "off",
      "current_time": "low",
      "glossary": "off",
      "mid_term": "low",
      "rag": "low",
      "session_digest": "off",
      "tier_info": "off",
      "web_search": "high"
    },
    "order": {
      "system_instruction": ["system_instruction", "key_memory"],
      "context_prefix": ["label_notes", "current_time", "glossary", "mid_term", "rag", "session_digest", "tier_info", "web_search"]
    },
    "system_instruction_position": "top"
  }
}
```

### 後方互換性

旧形式の `context_order` を持つ `config.json` は、初回チャット時に自動的に `context_levels` に変換されます（`preset: "custom"`, 旧ON要素 → `high`, 旧OFF要素 → `off`）。

---

## 将来拡張

- **`mid` レベルの実装**: 13B〜30B クラス向けの中間出力（ヘッダあり・注釈なし等）
- **`glossary low`**: ユーザー入力と文字一致検索で3件程度を抽出
- **`_low.txt` 自動生成**: 要約モデルで簡略版を生成する確認フロー
- **コンテキスト予算の動的制御**: モデルのコンテキスト長を検知して自動プリセット選択
- **追加プリセット**: `ultra-low`（3Bクラス）、`api-optimized`（コスト最適化）等
