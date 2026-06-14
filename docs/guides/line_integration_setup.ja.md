# LINE 連携セットアップ

[English](line_integration_setup.md) | **日本語**

Butly の LINE adapter を有効化し、LINE の 1:1 トークから Butly と会話するための設定手順です。

## 動作概要

- LINE は既存 FastAPI の `POST /line/webhook` で webhook を受信します。
- 未承認ユーザーには 6 桁の連携コードを返し、Streamlit の「LINE連携」で承認します。
- 8 秒以内の回答は reply、時間がかかる回答は受付 reply 後に push で送信します。
- 初期版は 1:1 トーク専用です。グループ・ルームでは応答しません。

## 前提

- Butly の FastAPI と Streamlit が同じホストで動作すること
- Butly instance が 1 つ以上作成済みであること
- LINE Official Account と Messaging API チャネルを用意できること
- HTTPS webhook を公開するための Cloudflare Tunnel と独自ドメイン

LINE 公式の現在の手順では、Messaging API チャネルは LINE Developers Console から直接作成せず、LINE Official Account を作成して Messaging API を有効化します。

## 1. LINE SDK をインストールする

LINE 連携用の依存は optional です。

```bash
venv/bin/pip install -r requirements-line.txt
```

SDK 未導入または認証情報未設定の場合、`POST /line/webhook` は `503` を返しますが、FastAPI と Streamlit の他機能は通常どおり動作します。

## 2. LINE Official Account と Messaging API を準備する

1. [LINE Official Account Manager](https://manager.line.biz/) で Official Account を作成します。
2. Official Account Manager で Messaging API を有効化します。
3. [LINE Developers Console](https://developers.line.biz/console/) を開き、作成された Messaging API チャネルを選択します。
4. **Basic settings** タブから Channel secret を取得します。
5. **Messaging API** タブで Channel access token を発行します。

LINE は期限を指定できる Channel access token v2.1 を推奨しています。利用する token の更新・失効管理は運用側で行ってください。

Official Account Manager の Greeting messages / Auto-reply messages が有効だと、Butly の回答とは別に自動応答が送られます。混乱を避ける場合は無効化してください。

## 3. `.env` を設定する

Butly の `.env` に取得した値を設定します。

```env
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
```

secret と token はログ・Git・チャットに貼らないでください。漏洩した場合は LINE Developers Console で再発行します。

## 4. FastAPI と Streamlit を起動する

```bash
# Terminal 1: webhook と pairing API
venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000

# Terminal 2: pairing 管理 UI
venv/bin/streamlit run app.py
```

Streamlit の API 接続先は `http://127.0.0.1:8000` のまま使用してください。`/pairing/*` は loopback 専用で、外部・LAN・Tunnel 経由のアクセスを拒否します。

## 5. Cloudflare Tunnel を設定する

`cloudflared` をインストール後、Tunnel と DNS route を作成します。`<...>` は環境に合わせて置き換えてください。

```bash
cloudflared tunnel login
cloudflared tunnel create butly-line
cloudflared tunnel route dns butly-line <LINE_WEBHOOK_HOST>
```

Cloudflare Tunnel の設定ファイルに、`/line/webhook` だけを公開する ingress rule を設定します。

```yaml
tunnel: <TUNNEL_UUID>
credentials-file: /home/<USER>/.cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: <LINE_WEBHOOK_HOST>
    path: ^/line/webhook$
    service: http://127.0.0.1:8000
  - service: http_status:404
```

設定を検証します。

```bash
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://<LINE_WEBHOOK_HOST>/line/webhook
cloudflared tunnel ingress rule https://<LINE_WEBHOOK_HOST>/pairing/pending
```

`/line/webhook` は Butly origin、`/pairing/pending` は catch-all の `http_status:404` に一致する必要があります。その後 Tunnel を起動します。

```bash
cloudflared tunnel run butly-line
```

`/chat`、`/settings/*`、`/pairing/*` を Tunnel から公開しないでください。

## 6. LINE webhook を有効化する

LINE Developers Console の Messaging API タブで、Webhook URL に次を設定します。

```text
https://<LINE_WEBHOOK_HOST>/line/webhook
```

1. **Verify** を実行し、Success になることを確認します。
2. **Use webhook** を有効化します。
3. Messaging API タブの QR code から Official Account を友だち追加します。

## 7. ユーザーを pairing する

1. 未承認ユーザーから Official Account の 1:1 トークへメッセージを送ります。
2. LINE に表示された 6 桁の連携コードを確認します。
3. Streamlit ホーム画面の **LINE連携** を開きます。
4. コードと接続先 Butly instance を確認し、**承認** を押します。
5. LINE から再度メッセージを送り、Butly の回答を確認します。

コードの有効期限は 10 分です。同一ユーザーが有効期限内に再送した場合は同じコードを再利用します。

## 動作確認チェックリスト

- LINE Developers Console の Verify が成功する
- Tunnel から `/line/webhook` 以外へアクセスすると `404` になる
- 未承認ユーザーには連携コードだけが返る
- Streamlit から承認後、通常の回答が返る
- 長い生成では「ただいま考えています。」の後に本回答が届く
- LINE の会話が承認先 instance の記憶へ保存される
- グループ・ルームでは応答しない

## トラブルシューティング

### Verify が `503` になる

`line-bot-sdk`、`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN` を確認し、FastAPI を再起動してください。

### Verify が `400 Invalid LINE signature` になる

Channel secret が対象チャネルのものか確認してください。Proxy が webhook body を変更しても署名検証に失敗します。

### Verify は成功するがメッセージに応答しない

- **Use webhook** が有効か確認する
- Official Account を友だち追加しているか確認する
- 1:1 トークで試す
- FastAPI と Tunnel のログを確認する
- Official Account Manager の自動応答設定を確認する

### Streamlit の LINE連携画面が `403` になる

Streamlit と FastAPI を同じホストで動かし、Streamlit の API 接続先を `http://127.0.0.1:8000` に設定してください。

## 参考

- [LINE: Get started with the Messaging API](https://developers.line.biz/en/docs/messaging-api/getting-started/)
- [LINE: Build a bot](https://developers.line.biz/en/docs/messaging-api/building-bot/)
- [LINE: Verify webhook signature](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)
- [Cloudflare Tunnel configuration file](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)
