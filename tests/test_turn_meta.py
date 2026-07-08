"""
test_turn_meta.py
-----------------
会話ターン meta の読み出しヘルパのユニットテスト。

検証項目 (group_context_lanes_plan Phase 1):
  - meta 欠落時のデフォルト解釈 (owner / direct / web)
  - lane の正規化 (未知値 → other / 欠落 → direct)
  - 複数話者判定と整形ラベル (複数話者時のみ 「display_name」)
"""

from butly_core.core.turn_meta import (
    OWNER_PERSON_ID,
    effective_meta,
    has_multiple_speakers,
    message_text,
    normalize_lane,
    speaker_label,
    user_label,
)


def _user(text: str = "hi", meta: dict = None) -> dict:
    msg = {"role": "user", "parts": [text]}
    if meta is not None:
        msg["meta"] = meta
    return msg


def _model(text: str = "yes") -> dict:
    return {"role": "model", "parts": [text]}


DISCORD_META = {
    "person_id": "p_discord_123",
    "display_name": "たろう",
    "lane": "direct",
    "source": "discord",
    "channel_key": "g1:c1",
}


# ===================================================================
# meta 欠落時のデフォルト解釈
# ===================================================================


class TestEffectiveMeta:
    def test_missing_meta_defaults(self):
        """meta 欠落 = owner / direct / web と解釈される (後方互換)"""
        meta = effective_meta(_user())
        assert meta["person_id"] == OWNER_PERSON_ID
        assert meta["lane"] == "direct"
        assert meta["source"] == "web"
        assert meta["display_name"] is None
        assert meta["channel_key"] is None

    def test_full_meta_passthrough(self):
        meta = effective_meta(_user(meta=DISCORD_META))
        assert meta["person_id"] == "p_discord_123"
        assert meta["display_name"] == "たろう"
        assert meta["lane"] == "direct"
        assert meta["source"] == "discord"
        assert meta["channel_key"] == "g1:c1"

    def test_owner_person_id_override(self):
        meta = effective_meta(_user(), owner_person_id="p_yuki")
        assert meta["person_id"] == "p_yuki"

    def test_non_dict_meta_ignored(self):
        meta = effective_meta({"role": "user", "parts": ["x"], "meta": "junk"})
        assert meta["person_id"] == OWNER_PERSON_ID


class TestNormalizeLane:
    def test_known_lanes(self):
        assert normalize_lane("direct") == "direct"
        assert normalize_lane("ambient") == "ambient"
        assert normalize_lane("audience") == "audience"

    def test_unknown_becomes_other(self):
        assert normalize_lane("weird") == "other"

    def test_missing_becomes_direct(self):
        assert normalize_lane(None) == "direct"
        assert normalize_lane("") == "direct"
        assert normalize_lane(123) == "direct"


# ===================================================================
# 複数話者判定
# ===================================================================


class TestHasMultipleSpeakers:
    def test_owner_only_false(self):
        assert has_multiple_speakers([_user(), _model(), _user()]) is False

    def test_single_external_false(self):
        msgs = [_user(meta=DISCORD_META), _model(), _user(meta=DISCORD_META)]
        assert has_multiple_speakers(msgs) is False

    def test_external_plus_owner_true(self):
        """meta 付き外部ユーザー + meta 欠落 (owner) で複数話者"""
        assert has_multiple_speakers([_user(meta=DISCORD_META), _user()]) is True

    def test_two_externals_true(self):
        other = dict(DISCORD_META, person_id="p_discord_999", display_name="はな")
        msgs = [_user(meta=DISCORD_META), _user(meta=other)]
        assert has_multiple_speakers(msgs) is True

    def test_model_messages_ignored(self):
        assert has_multiple_speakers([_model(), _model()]) is False

    def test_empty(self):
        assert has_multiple_speakers([]) is False


# ===================================================================
# 整形ラベル
# ===================================================================


class TestLabels:
    def test_speaker_label_display_name(self):
        assert speaker_label(_user(meta=DISCORD_META), "悠希") == "たろう"

    def test_speaker_label_fallback(self):
        assert speaker_label(_user(), "悠希") == "悠希"

    def test_user_label_single_speaker_unchanged(self):
        """1:1 は従来どおり呼称のまま (プレフィックスなし)"""
        assert user_label(_user(meta=DISCORD_META), "悠希", multi_speaker=False) == "悠希"

    def test_user_label_multi_speaker_quoted(self):
        assert (
            user_label(_user(meta=DISCORD_META), "悠希", multi_speaker=True)
            == "「たろう」"
        )

    def test_user_label_multi_speaker_owner_fallback(self):
        """meta の無い発言 (owner) も複数話者バッチでは呼称を 「」 で刻む"""
        assert user_label(_user(), "悠希", multi_speaker=True) == "「悠希」"


class TestMessageText:
    def test_str_part(self):
        assert message_text(_user("こんにちは")) == "こんにちは"

    def test_dict_part(self):
        assert message_text({"role": "user", "parts": [{"text": "t"}]}) == "t"

    def test_empty(self):
        assert message_text({"role": "user", "parts": []}) == ""
        assert message_text({"role": "user"}) == ""
