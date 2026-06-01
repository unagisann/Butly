# Discord 連携実装計画

> 作成日: 2026-06-01
> 対象: Butly の Discord adapter 実装。LINE 連携は後続フェーズに回す。

## 目的

Discord から Butly に話しかけられるようにする。

チャット生成・記憶・Gatekeeper・RAG・Sleeptime などの中核処理は、通常の Web チャットと同じ `ButlyRuntime.chat()` 経路を使う。Discord 側では、受信イベントの正規化、instance 解決、返信文の整形、Discord API 送信だけを担当する。

## 基本方針

- Discord 連携は Butly 本体の基本機能ではなく、optional integration / adapter として実装する。
- Butly の中核チャット処理には極力手を入れない。
- Discord からの発話も通常チャットと同じ記憶機構に保存する。
- 外部 ID は `ChatRequest` のメタ情報として扱い、会話本文には混ぜない。
- 返信だけ Discord 向けにやや短めにする。
- まず Discord を完成させ、LINE は adapter パターンが固まった後に実装する。

## 利用ライブラリ

Discord API との接続には `discord.py` を使う。

- Gateway 接続、rate limit、typing indicator、message send などを自前実装しない。
- Butly との接続部分は薄い adapter として自作する。
- 依存は optional にする。
  - 例: `requirements-discord.txt`
  - 将来 `pyproject.toml` に移行する場合は extra dependency にする。

## 想定ディレクトリ

最初は小さく始める。

```text
butly_core/
  external/
    __init__.py
    account_mapping.py
    reply_profiles.py
    message_splitter.py
    discord_adapter.py

run_discord_bot.py
requirements-discord.txt
```

### 役割

| ファイル | 役割 |
| --- | --- |
| `account_mapping.py` | Discord user/channel/guild と Butly instance の対応解決 |
| `reply_profiles.py` | Discord 用の返答スタイル・文字数制約 |
| `message_splitter.py` | Discord の 2000 文字制限に合わせた分割 |
| `discord_adapter.py` | discord.py の bot event / slash command 実装 |
| `run_discord_bot.py` | bot 単体起動用エントリポイント |

## チャット処理フロー

```text
Discord message
  ↓
discord_adapter.py
  ↓
ExternalAccountResolver で instance_name 解決
  ↓
ChatRequest(
  text=message,
  instance_name=resolved_instance,
  source="discord",
  external_user_id=user_id,
  external_channel_id=channel_id,
  metadata={"reply_profile": "discord"}
)
  ↓
runtime.chat(request)
  ↓
通常の ChatService / Gatekeeper / Memory / RAG / Provider
  ↓
DiscordReplyProfile で送信前整形
  ↓
Discord channel.send(...)
```

## 記憶方針

Discord からの会話も、通常チャットと同じように保存する。

- `memory.save_single_turn(request.text, result.text)` は通常どおり実行する。
- `request.text` に Discord user id や channel id は混ぜない。
- debug log には `source="discord"` のみ残す。
- 外部 ID は instance 解決や権限判定にだけ使う。

## 返信方針

Discord は通常 Web チャットより少し短めにする。

### 生成前の style hint

`ChatRequest.metadata` に Discord 用 reply profile を入れる。

```python
metadata={
    "reply_profile": "discord",
    "style_hint": "Discord向けに、通常よりやや簡潔に、数段落以内で返してください。",
    "soft_char_limit": 1400,
    "hard_char_limit": 1900,
}
```

初期実装では、style hint を `ChatService` の context に注入するか、adapter 側でユーザー文に混ぜずに専用 context として渡す。会話記憶本文には含めない。

### 送信前の整形

- Discord の hard limit は 2000 文字。
- Butly 側の hard limit は余白を見て 1900 文字にする。
- soft limit は 1200〜1400 文字程度。
- 1900 文字を超えた場合は複数メッセージに分割する。
- 分割境界は以下の順に選ぶ。
  1. 空行
  2. 改行
  3. 句点
  4. 強制カット

## Instance 解決方針

保存先は `DATA_DIR/external_accounts.json`。

```json
{
  "default_instance": "Butly",
  "discord": {
    "user:123456789012345678": "Jarvis",
    "channel:guild_id:channel_id": "Analyst",
    "guild:guild_id": "Butly"
  }
}
```

### 解決優先順位

```text
user:{user_id}
→ channel:{guild_id}:{channel_id}
→ guild:{guild_id}
→ default_instance
```

最初に一致した instance を採用する。

### 未登録時

- `default_instance` で応答する。
- `default_instance` が存在しない場合はエラーメッセージを返す。
- 将来、未登録ユーザーを拒否したい場合は `default_instance: null` を許容する。

## Instance 変更 UX

Discord では slash command を使う。

### 必須コマンド

```text
/butly current
/butly instances
/butly bind instance:<name> scope:<user|channel|guild>
/butly unbind scope:<user|channel|guild>
```

### コマンド仕様

| コマンド | 動作 |
| --- | --- |
| `/butly current` | 現在この発話で使われる instance と、どの scope で解決されたかを表示 |
| `/butly instances` | 利用可能な Butly instance 一覧を表示 |
| `/butly bind` | 指定 scope に instance を紐づける |
| `/butly unbind` | 指定 scope の紐づけを削除する |

### scope の意味

| scope | 保存キー | 用途 |
| --- | --- | --- |
| `user` | `user:{user_id}` | 自分だけ常に特定 instance を使う |
| `channel` | `channel:{guild_id}:{channel_id}` | チャンネル単位で担当 instance を固定 |
| `guild` | `guild:{guild_id}` | サーバー全体のデフォルトを設定 |

### 推奨初期 UX

最初は channel scope を推奨する。

```text
/butly bind instance:Butly scope:channel
```

理由:

- チャンネルごとに人格や用途を分けやすい。
- 個人設定より記憶の流れが散らかりにくい。
- guild 全体設定より誤爆範囲が狭い。

### 一時指定

初期実装では後回し。

将来候補:

```text
/butly ask instance:Jarvis message:今日の予定を整理して
@Jarvis 今日の予定を整理して
```

自然文との衝突を避けるため、最初は固定 bind のみで始める。

## メッセージ受信ルール

初期実装では暴発を避けるため、以下のどちらかに限定する。

### 案 A: bot mention された時だけ反応

```text
@Butly 今日どう思う？
```

メリット:

- 誤爆が少ない。
- 既存チャンネルに入れても安全。

### 案 B: 許可チャンネルでは全メッセージに反応

`external_accounts.json` で channel bind されたチャンネルでは全メッセージに反応する。

メリット:

- 専用チャンネルでは自然。

### 初期推奨

案 A から始める。

その後、設定で「bound channel では mention なしでも反応」を追加する。

## 起動方式

初期実装では **別プロセス起動** を推奨する。

```bash
python run_discord_bot.py
```

理由:

- FastAPI の lifecycle と Discord Gateway 接続を混ぜずに済む。
- bot 側の再起動やログ確認が簡単。
- optional integration として切り離しやすい。

ただし `run_discord_bot.py` 内では `ButlyRuntime` を生成し、既存 `DATA_DIR` と同じ場所を見る。

将来、デスクトップアプリからまとめて起動したくなったら、親プロセスが FastAPI と Discord bot を別プロセスとして起動する。

## Secret 管理

`.env` に以下を置く。

```env
DISCORD_BOT_TOKEN=...
```

- `.env.example` には追記済み。
- token は debug log に出さない。
- token 未設定なら bot 起動時に明示エラーで終了する。

## 権限

初期実装では以下を前提にする。

- bot token はローカル環境でのみ使う。
- `/butly bind` / `/butly unbind` は当面誰でも実行可能にしてよい。
- 公開サーバーに入れる場合は、管理者権限または許可ユーザー制限を追加する。

将来追加:

- `DISCORD_ADMIN_USER_IDS`
- `DISCORD_ALLOWED_GUILD_IDS`
- `DISCORD_ALLOWED_CHANNEL_IDS`

## 実装フェーズ

### Phase D1: optional dependency と起動エントリ

- `requirements-discord.txt` を追加。
- `run_discord_bot.py` を追加。
- `.env` から `DISCORD_BOT_TOKEN` を読む。
- token 未設定時は起動失敗にする。

完了条件:

- bot が Discord にログインできる。
- 起動時に ButlyRuntime を生成できる。

### Phase D2: account mapping

- `butly_core/external/account_mapping.py` を追加。
- `external_accounts.json` の読み書きを実装。
- Discord の user/channel/guild scope 解決を実装。
- instance が存在するか検証する。

完了条件:

- user/channel/guild/default の優先順位で instance が解決できる。
- mapping ファイルが無い場合も default で動く。

### Phase D3: slash command

- `/butly current`
- `/butly instances`
- `/butly bind`
- `/butly unbind`

完了条件:

- Discord 上から instance の確認・変更ができる。
- bind/unbind 結果が `external_accounts.json` に保存される。

### Phase D4: message adapter

- bot mention を検知する。
- mention を除去して本文を作る。
- `ChatRequest(source="discord", metadata={"reply_profile": "discord"})` を作る。
- `runtime.chat()` を呼ぶ。
- typing indicator を表示する。
- 応答を channel に送信する。

完了条件:

- Discord から Butly と会話できる。
- 通常チャットと同じ記憶に保存される。

### Phase D5: reply profile / 分割

- `reply_profiles.py` を追加。
- `message_splitter.py` を追加。
- Discord 用 soft/hard limit を適用する。
- 1900 文字超過時は複数メッセージに分割する。

完了条件:

- Discord の 2000 文字制限を超えない。
- Discord 返信が通常 Web チャットよりやや短めになる。

### Phase D6: テスト

- account mapping の単体テスト。
- message splitter の単体テスト。
- Discord event 正規化の単体テスト。
- Discord SDK は mock する。
- 実 Discord への接続は手動スモークテストにする。

完了条件:

- 既存フルテストが通る。
- Discord なし環境でも通常テストが通る。
- optional dependency 未インストールでも Butly 本体は起動できる。

## テスト方針

### 自動テスト対象

- `external_accounts.json` が無い場合の default 解決
- user scope が channel / guild より優先される
- channel scope が guild より優先される
- 存在しない instance が指定された場合のエラー
- bind / unbind の JSON 保存
- 1900 文字以内の分割
- 空行・改行・句点での自然な分割
- mention 除去

### 手動テスト対象

- Discord bot ログイン
- `/butly current`
- `/butly bind`
- bot mention への応答
- typing indicator
- 長文分割送信
- 記憶保存と次ターンでの参照

## 初期スコープ外

- LINE 連携
- Discord streaming 表示
- Discord 添付画像の取り込み
- Discord voice / slash ask の高度化
- 管理 UI
- 複数 bot token
- 公開サーバー向けの厳密な権限管理

## 完了判定

以下を満たしたら Discord 連携の初期版は完了。

- `python run_discord_bot.py` で bot が起動する。
- Discord で bot mention すると Butly が応答する。
- 応答は `ButlyRuntime.chat()` 経由で生成される。
- Discord 会話が通常の記憶機構に保存される。
- `/butly bind` で channel / user / guild に instance を紐づけられる。
- Discord 返信は 2000 文字を超えない。
- optional dependency 未導入でも FastAPI / Streamlit 本体は壊れない。
- フルテストが通る。
