# LINE Integration Setup

**English** | [日本語](line_integration_setup.ja.md)

This guide enables Butly's LINE adapter so users can chat with Butly in a one-to-one LINE conversation.

## How It Works

- LINE webhooks are received by the existing FastAPI endpoint at `POST /line/webhook`.
- Unapproved users receive a six-digit pairing code, which an administrator approves from the Streamlit **LINE Integration** screen.
- Answers completed within eight seconds use the Reply API. Slower answers send an acknowledgement first, followed by the completed answer through the Push API.
- The initial release supports one-to-one chats only. Group and multi-person room events are ignored.

## Prerequisites

- Butly FastAPI and Streamlit running on the same host
- At least one existing Butly instance
- A LINE Official Account with Messaging API enabled
- A Cloudflare Tunnel and domain for publishing the HTTPS webhook

Under LINE's current setup flow, create a LINE Official Account first and enable Messaging API from the Official Account Manager. Messaging API channels can no longer be created directly in the LINE Developers Console.

## 1. Install the LINE SDK

The LINE dependency is optional.

```bash
venv/bin/pip install -r requirements-line.txt
```

Without the SDK or credentials, `POST /line/webhook` returns `503`, while the rest of FastAPI and Streamlit continues to work normally.

## 2. Prepare the Official Account and Messaging API

1. Create an Official Account in [LINE Official Account Manager](https://manager.line.biz/).
2. Enable Messaging API for the account.
3. Open the generated Messaging API channel in the [LINE Developers Console](https://developers.line.biz/console/).
4. Copy the Channel secret from **Basic settings**.
5. Issue a Channel access token from the **Messaging API** tab.

LINE recommends a Channel access token v2.1 with a user-specified expiration. Token rotation and revocation remain an operational responsibility.

Greeting messages and auto-reply messages from Official Account Manager may be sent in addition to Butly's responses. Disable them if you want Butly to be the only responder.

## 3. Configure `.env`

Add the credentials to Butly's `.env`:

```env
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
```

Never put the secret or token in logs, Git, or chat messages. Reissue compromised credentials from the LINE Developers Console.

## 4. Start FastAPI and Streamlit

```bash
# Terminal 1: webhook and pairing API
venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000

# Terminal 2: pairing administration UI
venv/bin/streamlit run app.py
```

Keep Streamlit's API endpoint set to `http://127.0.0.1:8000`. The `/pairing/*` endpoints are loopback-only and reject LAN, external, and Tunnel requests.

## 5. Configure Cloudflare Tunnel

After installing `cloudflared`, create a Tunnel and DNS route. Replace all `<...>` placeholders.

```bash
cloudflared tunnel login
cloudflared tunnel create butly-line
cloudflared tunnel route dns butly-line <LINE_WEBHOOK_HOST>
```

Configure ingress so only `/line/webhook` is published:

```yaml
tunnel: <TUNNEL_UUID>
credentials-file: /home/<USER>/.cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: <LINE_WEBHOOK_HOST>
    path: ^/line/webhook$
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Validate the rules:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://<LINE_WEBHOOK_HOST>/line/webhook
cloudflared tunnel ingress rule https://<LINE_WEBHOOK_HOST>/pairing/pending
```

`/line/webhook` must match the Butly origin, while `/pairing/pending` must match the `http_status:404` catch-all. Then start the Tunnel:

```bash
cloudflared tunnel run butly-line
```

Do not publish `/chat`, `/settings/*`, or `/pairing/*` through the Tunnel.

## 6. Enable the LINE Webhook

In the Messaging API tab of the LINE Developers Console, set the Webhook URL to:

```text
https://<LINE_WEBHOOK_HOST>/line/webhook
```

1. Run **Verify** and confirm it succeeds.
2. Enable **Use webhook**.
3. Add the Official Account as a friend using the QR code on the Messaging API tab.

## 7. Pair a User

1. Send a message to the Official Account from an unapproved user's one-to-one chat.
2. Note the six-digit pairing code returned by LINE.
3. Open **LINE Integration** on the Streamlit home screen.
4. Confirm the code, choose a Butly instance, and click **Approve**.
5. Send another LINE message and confirm that Butly responds.

Pairing codes expire after ten minutes. Repeated messages from the same user reuse the active code.

## Verification Checklist

- LINE Developers Console Verify succeeds
- Tunnel requests outside `/line/webhook` return `404`
- Unapproved users receive only a pairing code
- Approved users receive normal Butly responses
- Slow generations send an acknowledgement followed by the final answer
- LINE conversations are saved to the approved instance's memory
- Group and multi-person room messages receive no response

## Troubleshooting

### Verify returns `503`

Check `line-bot-sdk`, `LINE_CHANNEL_SECRET`, and `LINE_CHANNEL_ACCESS_TOKEN`, then restart FastAPI.

### Verify returns `400 Invalid LINE signature`

Confirm the Channel secret belongs to the selected channel. Signature validation also fails if a proxy modifies the webhook body.

### Verify succeeds, but messages receive no response

- Confirm **Use webhook** is enabled
- Confirm the Official Account has been added as a friend
- Test from a one-to-one chat
- Check FastAPI and Tunnel logs
- Check Official Account Manager auto-reply settings

### Streamlit's LINE Integration screen returns `403`

Run Streamlit and FastAPI on the same host and set Streamlit's API endpoint to `http://127.0.0.1:8000`.

## References

- [LINE: Get started with the Messaging API](https://developers.line.biz/en/docs/messaging-api/getting-started/)
- [LINE: Build a bot](https://developers.line.biz/en/docs/messaging-api/building-bot/)
- [LINE: Verify webhook signature](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)
- [Cloudflare Tunnel configuration file](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)
