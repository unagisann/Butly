# 外部チャット連携 設計決定メモ

> **ステータス: 実装済み・アーカイブ（2026-06-14）**
>
> Discord / LINE adapter 実装前の設計判断を記録した資料です。一部の方針は
> 後続の安全要件で変更されています。現在の挙動は現行コード、テスト、
> [Discord セットアップ](../../guides/discord_integration_setup.ja.md)、
> [LINE セットアップ](../../guides/line_integration_setup.ja.md)を優先してください。

> 作成日: 2026-06-01
> 対象: `external_chat_preflight_plan.ja.md` の Phase 5 / Phase 6（方針決定フェーズ）
> 位置づけ: ここで決めた方針は Discord / LINE adapter を実装するときの前提とする。
> この文書の Phase 5 / 6 は「決定の明文化」であり、実際の解決ロジックや push 送信機構は
> adapter 実装時（外部連携本体）に作る。本文書ではコードを追加しない。

## 土台整備（Phase 1〜4, 7, 8）の実装状況

外部連携に入る前の土台整備は実装済み。

| Phase | 内容 | 実装 |
| --- | --- | --- |
| 1 | `ButlyRuntime`（`butly_core/runtime.py`） | 済 |
| 2 | `routers/chat.py` を Runtime 経由に。添付変換 helper 抽出 | 済 |
| 3 | `ChatService.execute()/execute_stream()` 前処理を `_prepare_chat_context` に共通化 | 済 |
| 4 | `ChatRequest` に `source` / `external_user_id` / `external_channel_id` 追加 | 済 |
| 7 | `.env.example` に Discord / LINE secret 追記 | 済 |
| 8 | Runtime 単体テスト（`tests/test_runtime.py`） | 済 |

外部 adapter からは次の薄い呼び出しだけで応答を得られる。

```python
request = ChatRequest(
    text=user_text,
    instance_name=instance_name,
    source="discord",                 # "web" | "discord" | "line" | "cli" | "api"
    external_user_id=external_user_id, # 記憶本文には混ぜない（メタ情報）
    external_channel_id=channel_id,
)
result = await deps.runtime.chat(request)
```

---

## Phase 5: インスタンス解決方針

「誰が話しかけたら、どの Butly instance が応答するか」を一意に決める。

### 対応表の保存場所

- `DATA_DIR/external_accounts.json`（最初は単一 JSON ファイル。SQLite 化は後回し）。
- 起動時に読み込み、変更は手動編集 → 再読み込み。管理 UI は後回し（後回しでよい項目に該当）。
- ファイルが存在しない／壊れている場合はデフォルト instance にフォールバックし、起動は止めない。

### ファイル形式（案）

source ごとに名前空間を分け、キーは文字列で持つ。

```json
{
  "default_instance": "00_master",
  "discord": {
    "user:123456789012345678": "00_master",
    "channel:guild_id:channel_id": "10_work",
    "guild:guild_id": "00_master"
  },
  "line": {
    "user:Uxxxxxxxxxxxxxxxx": "00_master",
    "group:Cyyyy:Uxxxx": "20_family"
  }
}
```

### 解決キーと優先順位

外部イベントを正規化したあと、粒度の細かいキーから順にフォールバックする。

- **Discord**: `user:{user_id}` → `channel:{guild_id}:{channel_id}` → `guild:{guild_id}` → `default_instance`
- **LINE**: `group:{group_id}:{user_id}` → `user:{user_id}` → `default_instance`
  （1:1 トークには group_id がないため `user:` を使う）

最初に一致した instance を採用する。一致が無ければ `default_instance`。

### 未登録ユーザーの挙動

- `default_instance`（既定 `00_master`）で応答する。拒否はしない。
- 将来、未登録ユーザーをブロックしたい場合は `default_instance` を `null` にして
  「受け付けない」を表現できる余地を残す（adapter 側で `null` を弾く）。

### ユーザー ID / チャンネル ID の取り扱い方針

- `external_user_id` / `external_channel_id` は `ChatRequest` のメタフィールドに載せるだけ。
  **会話記憶に保存する本文（`request.text`）には混ぜない**（実装済の方針）。
- debug log に残すのは `source` のみ（`ChatService` 実装済）。
  外部 ID は個人情報なので debug log には残さない。ルーティングのトレースが必要に
  なった場合のみ、短縮ハッシュ（先頭数桁）を別途検討する。
- instance 解決に使った後の外部 ID は、保持する必要がなければ adapter 層で破棄してよい。

---

## Phase 6: 外部連携の返信制約を吸収する方針

Discord と LINE は返信 API の性質が違うため、生成が長くても破綻しない形にする。
**外部 adapter は最初から streaming を必須にせず、`runtime.chat()`（非ストリーミング・全文）を前提**にする。

### Discord

- まず通常応答（`channel.send` / `interaction` への follow-up）で始める。
- 生成中は任意で typing indicator（`channel.typing()`）を出す。
- 長文は **2000 文字**（Discord のメッセージ上限）を目安に分割送信する。
  分割は段落（空行）→ 文 → 強制カットの順で境界を選ぶ。
- streaming は後回し。必要になったら `runtime.chat_stream()` を逐次 edit で反映する案を検討。

### LINE

- reply token は 1 回限り・短時間有効。生成が間に合わないケースを前提にする。
- 方針: **即時 reply で軽い受付 → 生成完了後に push message で本回答**。
  - 受付段階で loading animation（`chat.loading.start`）を使えるなら併用してよい。
- 長文は **5000 文字**（LINE のテキストメッセージ上限）を目安に分割し、複数メッセージで push する。
- push を使うため、対象ユーザー／グループの ID は reply 時点で確保しておく。

### タイムアウト時 / 失敗時の文言（共通の目安）

- 受付（生成中）: 「ただいま考えています。少しお待ちください。」
- 生成失敗: 「申し訳ありません、応答の生成に失敗しました。」
- タイムアウト（規定時間内に終わらない）: 受付メッセージを出した上で、完了後に本回答を送る
  （LINE は push、Discord は通常送信）。

### 長文分割の共通ルール

1. プラットフォーム上限（Discord 2000 / LINE 5000）を chunk 上限にする。
2. まず段落境界（空行）で割る。
3. 段落が上限を超える場合は文境界（。/ 改行）で割る。
4. それでも超える場合は上限文字数で強制カット。
5. 各 chunk は順序を保って送る。

---

## adapter 実装に入る前提（再掲）

- `runtime.chat()` が存在する … 済
- `/chat` が Runtime 経由で動く … 済
- 外部ユーザーと instance の対応方針が決まっている … 本文書 Phase 5
- 返信制約・長文分割・タイムアウト方針が決まっている … 本文書 Phase 6
- secret の置き場所が決まっている … `.env`（`DISCORD_BOT_TOKEN` / `LINE_CHANNEL_SECRET` / `LINE_CHANNEL_ACCESS_TOKEN`）
- フルテストが通る … 済

`execute_stream()` の共通化と WebSocket の Runtime 化は土台整備で実施済みのため、
LINE まで広げる前提条件は満たしている。
