# Discord 連携セットアップ

[English](discord_integration_setup.md) | **日本語**

Butly の Discord adapter を有効化し、Discord サーバーや DM から Butly と会話するための設定手順です。

## 動作概要

- Discord bot は FastAPI とは別プロセスの `run_discord_bot.py` で起動します。
- Bot へのメンションに反応し、通常の `ButlyRuntime.chat()` と記憶機構を使用します。
- `/butly` slash command で使用する Butly instance を確認・変更できます。
- Discord の画像添付にも対応します。

## 前提

- Butly の通常セットアップが完了していること
- Butly instance が 1 つ以上作成済みであること
- Bot を追加できる Discord サーバーがあること
- [Discord Developer Portal](https://discord.com/developers/applications) を利用できること

Discord は Gateway へ outbound 接続するため、Webhook やポート公開は不要です。

## 1. Discord SDK をインストールする

Discord 連携用の依存は optional です。

```bash
venv/bin/pip install -r requirements-discord.txt
```

## 2. Discord Application と Bot を作成する

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** を作成します。
2. Application の **Bot** ページで Bot user を作成します。
3. **Reset Token** または **Copy Token** から bot token を取得します。
4. **Privileged Gateway Intents** で **Message Content Intent** を有効化します。

Butly はメンション後の本文と画像添付を読むため、Message Content Intent が必要です。Intent を Developer Portal 側で有効にせず起動すると、Gateway 接続が拒否されたり本文を取得できなかったりします。

## 3. Bot をサーバーへインストールする

Developer Portal の **Installation** または **OAuth2 URL Generator** で Guild Install 用 URL を作成します。

Scopes:

- `bot`
- `applications.commands`

推奨権限:

- View Channels
- Send Messages
- Read Message History
- Use Application Commands

生成された URL を開き、対象サーバーへ Bot を追加します。チャンネル固有の権限設定でも、Bot がチャンネルを閲覧・送信できることを確認してください。

## 4. `.env` を設定する

```env
DISCORD_BOT_TOKEN=your_discord_bot_token_here
```

開発時は Guild ID を設定すると slash command が対象サーバーへ即時同期されます。

```env
DISCORD_DEV_GUILD_ID=your_guild_id_here
```

Guild ID は Discord の Developer Mode を有効にし、サーバーを右クリックしてコピーできます。未指定の場合は global command として同期され、反映に時間がかかる場合があります。

任意設定:

```env
# channel scope で bind 済みのチャンネルでは、メンションなしでも全メッセージに反応
DISCORD_RESPOND_IN_BOUND_CHANNELS=0

# 受信・mention 判定の診断ログを出力
DISCORD_DEBUG_MESSAGES=0
```

`DISCORD_RESPOND_IN_BOUND_CHANNELS=1` は専用チャンネルだけで使用してください。一般チャンネルで有効にすると、Bot が意図しない会話にも応答します。

## 5. Bot を起動する

```bash
venv/bin/python run_discord_bot.py
```

正常に起動すると、slash command の同期結果とログインした Bot 名がログに表示されます。Bot は Butly 本体と同じ `DATA_DIR`、instance、記憶データを使用します。

常設運用では systemd などで `run_discord_bot.py` を FastAPI とは別サービスとして管理してください。

## 6. 疎通確認する

Discord で Bot をメンションして話しかけます。

```text
@Butly こんにちは
```

初期状態では `default_instance` が使用されます。`external_accounts.json` が無い場合の既定値は `Butly` です。

## Instance の確認と割り当て

利用可能な slash command:

| Command | Purpose |
| --- | --- |
| `/butly current` | 現在解決される instance と scope を表示 |
| `/butly instances` | 利用可能な instance 一覧を表示 |
| `/butly bind instance:<name> scope:<user\|channel\|guild>` | scope に instance を割り当て |
| `/butly unbind scope:<user\|channel\|guild>` | scope の割り当てを削除 |

推奨例:

```text
/butly bind instance:Butly scope:channel
```

解決優先順位:

```text
user → channel → guild → default_instance
```

割り当ては `DATA_DIR/external_accounts.json` に保存されます。

現在 `/butly bind` と `/butly unbind` に管理者制限はありません。公開サーバーでは専用チャンネルと Discord 権限で利用者を制限してください。

## 動作確認チェックリスト

- `run_discord_bot.py` が Bot としてログインする
- `/butly` slash command が表示される
- Bot へのメンションで Butly が応答する
- `/butly bind` 後、指定 instance が使用される
- 長文回答が複数の Discord メッセージに分割される
- Discord の会話が対象 instance の記憶へ保存される
- 対応画像を添付して質問できる

## トラブルシューティング

### `DISCORD_BOT_TOKEN が未設定` と表示される

`.env` の token を確認してください。`run_discord_bot.py` は token 未設定時に終了します。

### `PrivilegedIntentsRequired` または Gateway close code `4014`

Developer Portal の Bot ページで **Message Content Intent** を有効化し、Bot を再起動してください。

### Bot は online だがメンションへ応答しない

- Bot に View Channels / Send Messages 権限があるか確認する
- Bot 自身を正しくメンションしているか確認する
- `DISCORD_DEBUG_MESSAGES=1` で判定ログを確認する
- 割り当て先 instance が存在するか `/butly current` と `/butly instances` で確認する

### slash command が表示されない

開発時は `DISCORD_DEV_GUILD_ID` を設定して再起動してください。global sync は反映に時間がかかる場合があります。また、Bot のインストール時に `applications.commands` scope が含まれているか確認してください。

## 参考

- [Discord: Building your first bot](https://docs.discord.com/developers/quick-start/getting-started)
- [Discord: Bots and companion apps](https://docs.discord.com/developers/bots/overview)
- [Discord: Gateway intents](https://docs.discord.com/developers/events/gateway)
- [Discord: Application commands](https://docs.discord.com/developers/interactions/application-commands)
