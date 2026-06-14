# Discord Integration Setup

**English** | [日本語](discord_integration_setup.ja.md)

This guide enables Butly's Discord adapter so users can chat with Butly from a Discord server or DM.

## How It Works

- The Discord bot runs as `run_discord_bot.py`, separate from FastAPI.
- It responds to bot mentions and uses the standard `ButlyRuntime.chat()` and memory path.
- `/butly` slash commands inspect and change the selected Butly instance.
- Discord image attachments are supported.

## Prerequisites

- A working Butly installation
- At least one existing Butly instance
- A Discord server where you can install a bot
- Access to the [Discord Developer Portal](https://discord.com/developers/applications)

Discord connects outbound through the Gateway, so no webhook or public inbound port is required.

## 1. Install the Discord SDK

The Discord dependency is optional.

```bash
venv/bin/pip install -r requirements-discord.txt
```

## 2. Create a Discord Application and Bot

1. Create a **New Application** in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create a bot user from the application's **Bot** page.
3. Use **Reset Token** or **Copy Token** to obtain the bot token.
4. Enable **Message Content Intent** under **Privileged Gateway Intents**.

Butly needs Message Content Intent to read text following a mention and to process image attachments. Without enabling it in the Developer Portal, the Gateway connection may be rejected or message content may be unavailable.

## 3. Install the Bot to a Server

Create a Guild Install URL from **Installation** or **OAuth2 URL Generator** in the Developer Portal.

Scopes:

- `bot`
- `applications.commands`

Recommended permissions:

- View Channels
- Send Messages
- Read Message History
- Use Application Commands

Open the generated URL and install the bot to your server. Channel-level overrides must also allow the bot to view and send messages.

## 4. Configure `.env`

```env
DISCORD_BOT_TOKEN=your_discord_bot_token_here
```

During development, set a Guild ID to sync slash commands immediately to that server:

```env
DISCORD_DEV_GUILD_ID=your_guild_id_here
```

Enable Discord Developer Mode and right-click the server to copy its Guild ID. Without this value, commands are synced globally and may take time to appear.

Optional settings:

```env
# Respond to every message in channels explicitly bound at channel scope
DISCORD_RESPOND_IN_BOUND_CHANNELS=0

# Log message receipt and mention-detection diagnostics
DISCORD_DEBUG_MESSAGES=0
```

Use `DISCORD_RESPOND_IN_BOUND_CHANNELS=1` only in dedicated channels. Enabling it in a general channel makes the bot respond to conversations that did not explicitly mention it.

## 5. Start the Bot

```bash
venv/bin/python run_discord_bot.py
```

A successful start logs slash-command synchronization and the authenticated bot name. The bot uses the same `DATA_DIR`, instances, and memory data as the main Butly application.

For an always-on installation, manage `run_discord_bot.py` as a separate service from FastAPI, such as with systemd.

## 6. Verify the Connection

Mention the bot in Discord:

```text
@Butly Hello
```

The initial configuration uses `default_instance`. If `external_accounts.json` does not exist, its default is `Butly`.

## Inspecting and Assigning Instances

Available slash commands:

| Command | Purpose |
| --- | --- |
| `/butly current` | Show the currently resolved instance and scope |
| `/butly instances` | List available instances |
| `/butly bind instance:<name> scope:<user\|channel\|guild>` | Assign an instance to a scope |
| `/butly unbind scope:<user\|channel\|guild>` | Remove a scope assignment |

Recommended example:

```text
/butly bind instance:Butly scope:channel
```

Resolution priority:

```text
user → channel → guild → default_instance
```

Assignments are stored in `DATA_DIR/external_accounts.json`.

Currently, `/butly bind` and `/butly unbind` do not enforce an administrator restriction. On public servers, use a dedicated channel and Discord permissions to limit who can use them.

## Verification Checklist

- `run_discord_bot.py` logs in successfully
- `/butly` slash commands appear
- Mentioning the bot produces a Butly response
- `/butly bind` switches to the selected instance
- Long answers are split into multiple Discord messages
- Discord conversations are saved to the selected instance's memory
- Supported image attachments can be included in a question

## Troubleshooting

### `DISCORD_BOT_TOKEN is not set`

Check the token in `.env`. `run_discord_bot.py` exits when the token is missing.

### `PrivilegedIntentsRequired` or Gateway close code `4014`

Enable **Message Content Intent** on the application's Bot page, then restart the bot.

### The bot is online but does not respond to mentions

- Confirm the bot has View Channels and Send Messages permissions
- Confirm the bot itself is mentioned
- Set `DISCORD_DEBUG_MESSAGES=1` and inspect the diagnostic logs
- Use `/butly current` and `/butly instances` to confirm the assigned instance exists

### Slash commands do not appear

Set `DISCORD_DEV_GUILD_ID` during development and restart the bot. Global command sync may take time. Also confirm that the bot was installed with the `applications.commands` scope.

## References

- [Discord: Building your first bot](https://docs.discord.com/developers/quick-start/getting-started)
- [Discord: Bots and companion apps](https://docs.discord.com/developers/bots/overview)
- [Discord: Gateway intents](https://docs.discord.com/developers/events/gateway)
- [Discord: Application commands](https://docs.discord.com/developers/interactions/application-commands)
