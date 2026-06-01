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
    build_discord_chat_request,
)
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
