"""
discord_adapter.py
------------------
Discord（discord.py）と Butly の接続を担う薄い adapter。

責務:
  - 受信メッセージの正規化（bot mention の除去）
  - ``ExternalAccountStore`` による instance 解決
  - ``ChatRequest`` 組み立て（source="discord", reply profile metadata 付与）
  - ``runtime.chat()`` 呼び出し
  - ``message_splitter`` による 2000 文字制限対応の分割送信
  - slash command（/butly current|instances|bind|unbind）

設計上の注意:
  - 本モジュールのトップレベルでは ``discord`` を import しない。
    discord.py を使うコードは ``create_bot()`` 内で遅延 import する。
    これにより discord.py 未インストールでも以下の純粋関数は import・テストできる:
      - ``strip_bot_mention``
      - ``build_discord_chat_request``
  - 外部 ID（user/channel/guild）は instance 解決にのみ使い、会話本文には混ぜない。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from typing import Optional

from butly_core.chat.types import (
    ALLOWED_IMAGE_MIMES,
    MAX_ATTACHMENTS,
    MAX_ATTACHMENT_SIZE,
    Attachment,
    ChatRequest,
)
from butly_core.external.account_mapping import ExternalAccountStore
from butly_core.external.message_splitter import split_message
from butly_core.external.reply_profiles import DISCORD_PROFILE, ReplyProfile

# ----------------------------------------------------------------------
# 純粋ヘルパー（discord 非依存・単体テスト対象）
# ----------------------------------------------------------------------


def strip_bot_mention(content: str, bot_user_id) -> str:
    """メッセージ本文から bot 自身への mention を除去して trim する。

    Discord のメッセージ本文では mention は ``<@123>`` または ``<@!123>``
    （ニックネーム mention）として現れる。両形式を除去する。

    Parameters
    ----------
    content : str
        ``message.content``。
    bot_user_id : int | str
        bot 自身の user id。

    Returns
    -------
    str
        mention を除いた本文（前後空白除去・連続空白は 1 つに圧縮）。
    """
    if not content:
        return ""
    pattern = re.compile(rf"<@!?{re.escape(str(bot_user_id))}>")
    cleaned = pattern.sub(" ", content)
    # mention 除去で生じた連続空白を 1 つに圧縮（改行は保持）
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def is_bot_mentioned(content: str, mentions, bot_user_id) -> bool:
    """Discord message が bot 自身への mention を含むか判定する。"""
    bot_id = str(bot_user_id)
    for mention in mentions or []:
        mention_id = getattr(mention, "id", mention)
        if str(mention_id) == bot_id:
            return True
    if not content:
        return False
    return re.search(rf"<@!?{re.escape(bot_id)}>", content) is not None


def _env_flag(value: Optional[str]) -> bool:
    """環境変数文字列を真偽に変換する（未設定/0/false/no/off/空 → False）。"""
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def should_respond(
    *,
    is_mentioned: bool,
    respond_in_bound_channels: bool,
    channel_is_bound: bool,
) -> bool:
    """このメッセージに反応すべきか判定する（discord 非依存・テスト対象）。

    - mention されていれば常に反応（案A）。
    - opt-in 有効かつ channel scope で bind 済みのチャンネルなら mention 無しでも反応（案B）。
    - それ以外は無視。
    """
    if is_mentioned:
        return True
    if respond_in_bound_channels and channel_is_bound:
        return True
    return False


def discord_image_mime(content_type: Optional[str], filename: Optional[str]) -> Optional[str]:
    """Discord 添付から Butly が受け取れる画像 MIME を推定する。"""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime and filename:
        guessed, _ = mimetypes.guess_type(filename)
        mime = (guessed or "").lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    return mime if mime in ALLOWED_IMAGE_MIMES else None


def discord_attachment_is_image(attachment) -> bool:
    """Discord 添付が Butly 対応画像か判定する。"""
    return (
        discord_image_mime(
            getattr(attachment, "content_type", None),
            getattr(attachment, "filename", None),
        )
        is not None
    )


def build_discord_chat_request(
    *,
    text: str,
    instance_name: str,
    user_id: Optional[str],
    channel_id: Optional[str],
    attachments: Optional[list[Attachment]] = None,
    profile: ReplyProfile = DISCORD_PROFILE,
) -> ChatRequest:
    """Discord 受信内容から内部 ``ChatRequest`` を組み立てる。

    - ``text`` は mention 除去済みの本文。外部 ID は混ぜない。
    - ``source="discord"`` と reply profile metadata（style_hint 等）を付与する。
    - 外部 ID は ``external_user_id`` / ``external_channel_id`` にメタとして保持する。
    """
    return ChatRequest(
        text=text,
        attachments=attachments or [],
        instance_name=instance_name,
        source="discord",
        external_user_id=str(user_id) if user_id is not None else None,
        external_channel_id=str(channel_id) if channel_id is not None else None,
        metadata=profile.to_metadata(),
    )


# ----------------------------------------------------------------------
# bot 構築（discord 依存・遅延 import）
# ----------------------------------------------------------------------


def create_bot(
    runtime,
    store: ExternalAccountStore,
    *,
    profile: ReplyProfile = DISCORD_PROFILE,
    dev_guild_id: Optional[str] = None,
):
    """設定済みの discord client を生成して返す。

    Parameters
    ----------
    runtime : ButlyRuntime
        ``await runtime.chat(request)`` を呼ぶための中核ランタイム。
    store : ExternalAccountStore
        instance 解決・bind/unbind 用。
    profile : ReplyProfile
        返信プロファイル（既定: Discord）。
    dev_guild_id : str | None
        指定すると slash command を当該 guild に即時 sync する（開発・手動テスト用）。
        None の場合は global sync（反映に最大 1 時間かかることがある）。

    Returns
    -------
    discord.Client
        ``client.run(token)`` で起動できる client。

    Raises
    ------
    RuntimeError
        discord.py 未インストール時。
    """
    try:
        import discord
        from discord import app_commands
    except ImportError as e:  # pragma: no cover - 環境依存
        raise RuntimeError(
            "discord.py がインストールされていません。"
            "`pip install -r requirements-discord.txt` を実行してください。"
        ) from e

    intents = discord.Intents.default()
    intents.messages = True
    # メッセージ本文を読むには privileged intent が必要。
    # Discord Developer Portal の Bot 設定で "Message Content Intent" を有効にすること。
    intents.message_content = True
    debug_messages = _env_flag(os.environ.get("DISCORD_DEBUG_MESSAGES"))
    # 案B (opt-in): channel scope で bind したチャンネルでは mention 無しでも反応する。
    respond_in_bound_channels = _env_flag(
        os.environ.get("DISCORD_RESPOND_IN_BOUND_CHANNELS")
    )

    _scope_choices = [
        app_commands.Choice(name="user（自分だけ）", value="user"),
        app_commands.Choice(name="channel（このチャンネル）", value="channel"),
        app_commands.Choice(name="guild（サーバー全体）", value="guild"),
    ]

    def _ids_from_interaction(interaction):
        user_id = str(interaction.user.id) if interaction.user else None
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        channel_id = str(interaction.channel_id) if interaction.channel_id else None
        return user_id, guild_id, channel_id

    async def _send_channel_message(channel, content: str) -> bool:
        try:
            await channel.send(content)
            return True
        except discord.Forbidden as e:
            print(f"[discord] send forbidden: {e}")
        except discord.HTTPException as e:
            print(f"[discord] send failed: {e}")
        return False

    async def _collect_image_attachments(message) -> tuple[list[Attachment], list[str]]:
        collected: list[Attachment] = []
        warnings: list[str] = []
        image_count = 0
        for source in message.attachments or []:
            mime_type = discord_image_mime(source.content_type, source.filename)
            if mime_type is None:
                continue
            image_count += 1
            if image_count > MAX_ATTACHMENTS:
                continue
            if source.size and source.size > MAX_ATTACHMENT_SIZE:
                size_mb = source.size / (1024 * 1024)
                warnings.append(
                    f"`{source.filename}` は大きすぎるため読み込みませんでした"
                    f"（{size_mb:.1f}MB / 上限 {MAX_ATTACHMENT_SIZE // (1024 * 1024)}MB）。"
                )
                continue
            try:
                raw = await source.read()
            except discord.HTTPException as e:
                print(f"[discord] attachment read failed: {e}")
                warnings.append(f"`{source.filename}` の読み込みに失敗しました。")
                continue
            collected.append(
                Attachment(
                    kind="image",
                    mime_type=mime_type,
                    data_base64=base64.b64encode(raw).decode("ascii"),
                    name=source.filename,
                    size=len(raw),
                )
            )
        if image_count > MAX_ATTACHMENTS:
            warnings.append(
                f"画像は最大{MAX_ATTACHMENTS}枚まで読み込みます"
                f"（{image_count}枚中 {len(collected)}枚を使用）。"
            )
        return collected, warnings

    # ---- slash command group: /butly ----
    butly_group = app_commands.Group(
        name="butly", description="Butly の instance を確認・切り替える"
    )

    @butly_group.command(
        name="current", description="この発話で使われる instance と解決 scope を表示"
    )
    async def cmd_current(interaction: "discord.Interaction"):
        user_id, guild_id, channel_id = _ids_from_interaction(interaction)
        resolved = store.resolve_discord(
            user_id=user_id, guild_id=guild_id, channel_id=channel_id
        )
        if resolved is None:
            msg = "このアカウントには instance が割り当てられていません（default 無効）。"
        else:
            exists = "" if resolved.exists else "（⚠ 実在しません）"
            msg = (
                f"現在の instance: **{resolved.instance_name}**{exists}\n"
                f"解決 scope: `{resolved.scope}`"
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @butly_group.command(name="instances", description="利用可能な instance 一覧を表示")
    async def cmd_instances(interaction: "discord.Interaction"):
        names = store.available_instances()
        if names:
            body = "\n".join(f"・{n}" for n in names)
            msg = f"利用可能な instance:\n{body}"
        else:
            msg = "利用可能な instance がありません。"
        await interaction.response.send_message(msg, ephemeral=True)

    @butly_group.command(name="bind", description="指定 scope に instance を紐づける")
    @app_commands.describe(
        instance="紐づける instance 名", scope="紐づける範囲"
    )
    @app_commands.choices(scope=_scope_choices)
    async def cmd_bind(
        interaction: "discord.Interaction",
        instance: str,
        scope: str,
    ):
        user_id, guild_id, channel_id = _ids_from_interaction(interaction)
        try:
            key = store.bind_discord(
                scope=scope,
                instance_name=instance,
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
            )
        except ValueError as e:
            await interaction.response.send_message(f"⚠ {e}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ `{key}` → **{instance}** に紐づけました。", ephemeral=True
        )

    @butly_group.command(name="unbind", description="指定 scope の紐づけを削除する")
    @app_commands.describe(scope="解除する範囲")
    @app_commands.choices(scope=_scope_choices)
    async def cmd_unbind(
        interaction: "discord.Interaction",
        scope: str,
    ):
        user_id, guild_id, channel_id = _ids_from_interaction(interaction)
        try:
            removed = store.unbind_discord(
                scope=scope,
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
            )
        except ValueError as e:
            await interaction.response.send_message(f"⚠ {e}", ephemeral=True)
            return
        if removed:
            msg = f"✅ scope `{scope}` の紐づけを削除しました。"
        else:
            msg = f"scope `{scope}` には紐づけがありませんでした。"
        await interaction.response.send_message(msg, ephemeral=True)

    # ---- client 本体 ----
    class ButlyDiscordClient(discord.Client):
        def __init__(self):
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self):
            self.tree.add_command(butly_group)
            if dev_guild_id:
                guild = discord.Object(id=int(dev_guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(
                    f"[discord] slash commands synced to guild {dev_guild_id} "
                    f"({len(synced)} commands)"
                )
            else:
                synced = await self.tree.sync()
                print(
                    f"[discord] slash commands globally synced ({len(synced)} commands)。"
                    "反映に時間がかかる場合があります。"
                )

        async def on_ready(self):
            print(f"[discord] Logged in as {self.user} (id={self.user.id})")

        async def on_message(self, message: "discord.Message"):
            # 自分・他 bot は無視
            if self.user is None or message.author.id == self.user.id:
                return
            if message.author.bot:
                return
            if debug_messages:
                mention_ids = [str(getattr(m, "id", m)) for m in message.mentions]
                print(
                    "[discord] message received "
                    f"guild={message.guild.id if message.guild else None} "
                    f"channel={message.channel.id} author={message.author.id} "
                    f"content_len={len(message.content or '')} "
                    f"mentions={mention_ids}"
                )
            user_id, guild_id, channel_id = (
                str(message.author.id),
                str(message.guild.id) if message.guild else None,
                str(message.channel.id),
            )

            # 反応判定: mention（案A）or bound channel での opt-in 全反応（案B）
            is_mentioned = is_bot_mentioned(
                message.content, message.mentions, self.user.id
            )
            channel_is_bound = store.has_channel_binding(
                guild_id=guild_id, channel_id=channel_id
            )
            if not should_respond(
                is_mentioned=is_mentioned,
                respond_in_bound_channels=respond_in_bound_channels,
                channel_is_bound=channel_is_bound,
            ):
                if debug_messages:
                    print(
                        "[discord] ignored: not mentioned and channel not auto-respond "
                        f"(bound={channel_is_bound}, optin={respond_in_bound_channels})"
                    )
                return

            text = strip_bot_mention(message.content, self.user.id)

            resolved = store.resolve_discord(
                user_id=user_id, guild_id=guild_id, channel_id=channel_id
            )
            if resolved is None:
                await _send_channel_message(
                    message.channel,
                    "このアカウントには Butly instance が割り当てられていません。"
                )
                return
            if not resolved.exists:
                await _send_channel_message(
                    message.channel,
                    f"instance '{resolved.instance_name}' が見つかりません。"
                    "`/butly instances` で確認のうえ `/butly bind` してください。"
                )
                return
            attachments, attachment_warnings = await _collect_image_attachments(message)
            if not text and attachments:
                text = "添付画像を見てください。"
            if not text and not attachments:
                if attachment_warnings:
                    for warning in attachment_warnings:
                        await _send_channel_message(message.channel, warning)
                    return
                # mention のみ（本文なし）の時だけ促す。bound channel の自動反応では
                # 画像のみ/空メッセージに毎回返さないよう黙ってスルーする。
                if is_mentioned:
                    await _send_channel_message(
                        message.channel,
                        "はい、ご用件をどうぞ。（メンションに続けて話しかけてください）"
                    )
                return
            if debug_messages:
                print(
                    "[discord] handling mention "
                    f"instance={resolved.instance_name} scope={resolved.scope} "
                    f"text_len={len(text)} attachments={len(attachments)}"
                )

            request = build_discord_chat_request(
                text=text,
                instance_name=resolved.instance_name,
                user_id=user_id,
                channel_id=channel_id,
                attachments=attachments,
                profile=profile,
            )

            try:
                async with message.channel.typing():
                    result = await runtime.chat(request)
            except Exception as e:  # noqa: BLE001 - ユーザーには簡潔に通知
                print(f"[discord] chat error: {e}")
                await _send_channel_message(
                    message.channel,
                    f"応答の生成に失敗しました: {str(e)[:150]}"
                )
                return

            chunks = split_message(result.text or "", profile.hard_char_limit)
            if attachment_warnings:
                chunks = attachment_warnings + chunks
            if not chunks:
                chunks = ["（応答が空でした）"]
            for chunk in chunks:
                if not await _send_channel_message(message.channel, chunk):
                    break

    return ButlyDiscordClient()
