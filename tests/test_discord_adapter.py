"""
test_discord_adapter.py
-----------------------
discord_adapter の純粋ヘルパー（discord SDK 非依存）の単体テスト:
  - strip_bot_mention: メンション除去（<@id> / <@!id>）
  - build_discord_chat_request: ChatRequest 組み立て（source / metadata / 外部 ID）

discord.py 未インストールでも import・実行できることが前提。
"""

from butly_core.external.discord_adapter import (
    strip_bot_mention,
    is_bot_mentioned,
    discord_image_mime,
    discord_attachment_is_image,
    build_discord_chat_request,
    should_respond,
    _env_flag,
    create_bot,
)
from butly_core.chat.types import Attachment
from butly_core.external.reply_profiles import DISCORD_PROFILE


class TestStripBotMention:
    def test_leading_mention(self):
        assert strip_bot_mention("<@123> こんにちは", 123) == "こんにちは"

    def test_nickname_mention(self):
        assert strip_bot_mention("<@!123> やあ", 123) == "やあ"

    def test_mention_in_middle(self):
        assert strip_bot_mention("ねえ <@123> 元気？", 123) == "ねえ 元気？"

    def test_accepts_str_id(self):
        assert strip_bot_mention("<@123> hi", "123") == "hi"

    def test_keeps_other_mentions(self):
        # bot=123 のみ除去、他ユーザー(999)の mention は残す
        assert strip_bot_mention("<@999> <@123> hi", 123) == "<@999> hi"

    def test_no_mention_just_trims(self):
        assert strip_bot_mention("  ただの文章  ", 123) == "ただの文章"

    def test_empty(self):
        assert strip_bot_mention("", 123) == ""

    def test_only_mention_becomes_empty(self):
        assert strip_bot_mention("<@123>", 123) == ""


class TestIsBotMentioned:
    def test_detects_parsed_mention_object(self):
        class Mention:
            id = 123

        assert is_bot_mentioned("hi", [Mention()], 123)

    def test_detects_raw_mention_when_mentions_empty(self):
        assert is_bot_mentioned("<@123> hi", [], 123)

    def test_detects_raw_nickname_mention(self):
        assert is_bot_mentioned("<@!123> hi", [], "123")

    def test_rejects_other_mentions(self):
        assert not is_bot_mentioned("<@999> hi", [], 123)


class TestDiscordImageMime:
    def test_accepts_allowed_content_type(self):
        assert discord_image_mime("image/png", "x.bin") == "image/png"

    def test_normalizes_jpg(self):
        assert discord_image_mime("image/jpg", "x.jpg") == "image/jpeg"

    def test_guesses_from_filename(self):
        assert discord_image_mime(None, "screen.webp") == "image/webp"

    def test_rejects_unsupported_image(self):
        assert discord_image_mime("image/gif", "x.gif") is None

    def test_attachment_is_image(self):
        class Source:
            content_type = None
            filename = "photo.jpeg"

        assert discord_attachment_is_image(Source())


class TestShouldRespond:
    def test_mention_always_responds(self):
        assert should_respond(
            is_mentioned=True, respond_in_bound_channels=False, channel_is_bound=False
        )

    def test_bound_channel_responds_when_optin(self):
        assert should_respond(
            is_mentioned=False, respond_in_bound_channels=True, channel_is_bound=True
        )

    def test_bound_channel_ignored_when_optout(self):
        assert not should_respond(
            is_mentioned=False, respond_in_bound_channels=False, channel_is_bound=True
        )

    def test_unbound_channel_ignored_even_with_optin(self):
        assert not should_respond(
            is_mentioned=False, respond_in_bound_channels=True, channel_is_bound=False
        )

    def test_no_mention_no_optin_ignored(self):
        assert not should_respond(
            is_mentioned=False, respond_in_bound_channels=False, channel_is_bound=False
        )


class TestEnvFlag:
    def test_truthy(self):
        assert _env_flag("1")
        assert _env_flag("true")
        assert _env_flag("YES")
        assert _env_flag("on")

    def test_falsy(self):
        assert not _env_flag(None)
        assert not _env_flag("")
        assert not _env_flag("0")
        assert not _env_flag("false")
        assert not _env_flag(" off ")


class TestBuildChatRequest:
    def test_basic_fields(self):
        req = build_discord_chat_request(
            text="今日の予定を整理して",
            instance_name="Jarvis",
            user_id="100",
            channel_id="300",
        )
        assert req.text == "今日の予定を整理して"
        assert req.instance_name == "Jarvis"
        assert req.source == "discord"
        assert req.external_user_id == "100"
        assert req.external_channel_id == "300"

    def test_metadata_carries_reply_profile(self):
        req = build_discord_chat_request(
            text="hi", instance_name="Butly", user_id="1", channel_id="2"
        )
        assert req.metadata["reply_profile"] == "discord"
        assert req.metadata["style_hint"] == DISCORD_PROFILE.style_hint
        assert req.metadata["hard_char_limit"] == DISCORD_PROFILE.hard_char_limit

    def test_text_not_polluted_with_ids(self):
        """外部 ID は本文に混ざらない（記憶汚染防止）。"""
        req = build_discord_chat_request(
            text="ふつうの発話", instance_name="Butly", user_id="100", channel_id="300"
        )
        assert "100" not in req.text
        assert "300" not in req.text

    def test_none_ids(self):
        req = build_discord_chat_request(
            text="hi", instance_name="Butly", user_id=None, channel_id=None
        )
        assert req.external_user_id is None
        assert req.external_channel_id is None

    def test_int_ids_coerced_to_str(self):
        req = build_discord_chat_request(
            text="hi", instance_name="Butly", user_id=100, channel_id=300
        )
        assert req.external_user_id == "100"
        assert req.external_channel_id == "300"

    def test_attachments_are_carried(self):
        attachment = Attachment(
            mime_type="image/png",
            data_base64="aGVsbG8=",
            name="screen.png",
            size=5,
        )
        req = build_discord_chat_request(
            text="見て",
            instance_name="Butly",
            user_id="100",
            channel_id="300",
            attachments=[attachment],
        )
        assert len(req.attachments) == 1
        assert req.attachments[0].name == "screen.png"


def test_create_bot_registers_choice_commands_when_discord_installed():
    """discord.py が注釈評価で app_commands を見失わないことの回帰テスト。"""
    import pytest

    pytest.importorskip("discord")

    class DummyStore:
        pass

    bot = create_bot(runtime=object(), store=DummyStore())

    assert bot is not None
