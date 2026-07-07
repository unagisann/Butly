"""
test_speaker_attribution.py
---------------------------
話者帰属の書き込み経路 (group_context_lanes_plan Phase 1) の結合テスト。

検証項目:
  - ChatService._build_turn_meta: meta の組み立て / Web は None / 仮 ID フォールバック
  - ButlyRuntime._attach_person: person_id の解決付与とスナップショット優先
  - ButlyMemory.save_single_turn: meta 付き保存と従来形式の後方互換
  - ButlyMemory.maintain_memory: 複数話者バッチの 「display_name」 ラベリング
  - Sleeptime.stage_1_cleanup: 複数話者整形 + 登場回数集計 (persons.json)
  - build_discord_chat_request: guild_id / display_name の伝搬
  - raw_memory_reader.format_sessions: 複数話者時のラベル切替
"""

import json
import os
from pathlib import Path

import pytest

from butly_core.chat.service import _build_turn_meta
from butly_core.chat.types import ChatRequest
from butly_core.core.memory import ButlyMemory
from butly_core.core.raw_memory_reader import format_sessions
from butly_core.external.discord_adapter import build_discord_chat_request
from butly_core.external.person_registry import PERSONS_FILENAME
from butly_core.external.person_registry import OWNER_FALLBACK_PERSON_ID
from butly_core.external.person_registry import provisional_person_id
from butly_core.runtime import ButlyRuntime

TARO_META = {
    "person_id": "p_discord_123",
    "display_name": "たろう",
    "lane": "direct",
    "source": "discord",
    "channel_key": "g1:c1",
}


def _turn_file(ts: str, user_text: str, meta: dict = None) -> dict:
    user_msg = {"role": "user", "parts": [user_text]}
    if meta is not None:
        user_msg["meta"] = meta
    return {
        "timestamp": ts,
        "messages": [user_msg, {"role": "model", "parts": ["了解です"]}],
    }


# ===================================================================
# ChatService._build_turn_meta
# ===================================================================


class TestBuildTurnMeta:
    def test_web_request_returns_none(self):
        """Web UI (外部帰属なし) は meta なし = 従来形式を維持"""
        assert _build_turn_meta(ChatRequest(text="hi")) is None

    def test_discord_request_full_meta(self):
        req = ChatRequest(
            text="hi",
            source="discord",
            external_user_id="123",
            external_channel_id="c1",
            external_guild_id="g1",
            external_display_name="たろう",
            person_id="p_taro",
        )
        meta = _build_turn_meta(req)
        assert meta == {
            "person_id": "p_taro",
            "lane": "direct",
            "source": "discord",
            "display_name": "たろう",
            "channel_key": "g1:c1",
        }

    def test_provisional_fallback_without_person_id(self):
        """runtime での解決が無くても決定的仮 ID で帰属を確保する"""
        req = ChatRequest(text="hi", source="discord", external_user_id="55")
        meta = _build_turn_meta(req)
        assert meta["person_id"] == provisional_person_id("discord", "55")

    def test_line_fallback_is_owner(self):
        """LINE 1:1 は runtime を経由しない場合も owner として保存する"""
        req = ChatRequest(text="hi", source="line", external_user_id="U55")
        meta = _build_turn_meta(req)
        assert meta["person_id"] == OWNER_FALLBACK_PERSON_ID

    def test_channel_key_without_guild(self):
        """DM 等 guild が無い場合は channel_id 単独"""
        req = ChatRequest(
            text="hi",
            source="discord",
            external_user_id="55",
            external_channel_id="c9",
        )
        assert _build_turn_meta(req)["channel_key"] == "c9"


# ===================================================================
# ButlyRuntime._attach_person
# ===================================================================


@pytest.fixture
def runtime(tmp_path) -> ButlyRuntime:
    instances_dir = tmp_path / "butly_core" / "instances"
    instances_dir.mkdir(parents=True)
    return ButlyRuntime(
        data_dir=tmp_path, base_dir=tmp_path, instances_dir=instances_dir
    )


class TestAttachPerson:
    def _write_persons(self, tmp_path):
        (tmp_path / PERSONS_FILENAME).write_text(
            json.dumps(
                {
                    "persons": {
                        "p_yuki": {
                            "display_name": "悠希",
                            "is_owner": True,
                            "aliases": {"discord": ["123"]},
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_resolves_registered_alias(self, runtime, tmp_path):
        self._write_persons(tmp_path)
        req = ChatRequest(text="hi", source="discord", external_user_id="123")
        runtime._attach_person(req)
        assert req.person_id == "p_yuki"
        assert req.external_display_name == "悠希"

    def test_snapshot_display_name_wins(self, runtime, tmp_path):
        """adapter のスナップショットはレジストリ登録名より優先"""
        self._write_persons(tmp_path)
        req = ChatRequest(
            text="hi",
            source="discord",
            external_user_id="123",
            external_display_name="ゆきさん",
        )
        runtime._attach_person(req)
        assert req.external_display_name == "ゆきさん"

    def test_unregistered_gets_provisional(self, runtime):
        req = ChatRequest(text="hi", source="discord", external_user_id="999")
        runtime._attach_person(req)
        assert req.person_id == provisional_person_id("discord", "999")

    def test_line_unregistered_maps_to_owner(self, runtime):
        req = ChatRequest(text="hi", source="line", external_user_id="U999")
        runtime._attach_person(req)
        assert req.person_id == OWNER_FALLBACK_PERSON_ID

    def test_web_request_untouched(self, runtime):
        req = ChatRequest(text="hi")
        runtime._attach_person(req)
        assert req.person_id is None


# ===================================================================
# ButlyMemory.save_single_turn (meta 付き保存)
# ===================================================================


class TestSaveSingleTurnMeta:
    def test_meta_saved_on_user_message(self, base_dir):
        mem = ButlyMemory(base_dir, instance_name="99_meta_test")
        file_name = mem.save_single_turn("こんにちは", "はい", meta=TARO_META)

        data = json.loads(
            (mem.short_term_json_dir / file_name).read_text(encoding="utf-8")
        )
        user_msg, model_msg = data["messages"]
        assert user_msg["meta"] == TARO_META
        assert "meta" not in model_msg

    def test_no_meta_keeps_legacy_format(self, base_dir):
        """meta 未指定は従来形式のまま (後方互換の要)"""
        mem = ButlyMemory(base_dir, instance_name="99_meta_test")
        file_name = mem.save_single_turn("こんにちは", "はい")

        data = json.loads(
            (mem.short_term_json_dir / file_name).read_text(encoding="utf-8")
        )
        assert "meta" not in data["messages"][0]

    def test_rapid_turns_do_not_overwrite_each_other(self, base_dir):
        """同一秒内の連続保存でもファイル名が衝突しない"""
        mem = ButlyMemory(base_dir, instance_name="99_meta_test")
        names = [mem.save_single_turn(f"こんにちは{i}", "はい") for i in range(3)]

        assert len(set(names)) == 3
        for file_name in names:
            assert (mem.short_term_json_dir / file_name).exists()


# ===================================================================
# ButlyMemory.maintain_memory (複数話者バッチの整形)
# ===================================================================


class _RecordingBrain:
    def __init__(self):
        self.texts = []

    def summarize_conversation(self, conv_text, override_config=None):
        self.texts.append(conv_text)
        return "要約"


class TestMaintainMemorySpeakers:
    def _write_turns(self, mem, turns):
        """turns: [(name, data)] を古い順の mtime で配置する"""
        for i, (name, data) in enumerate(turns):
            path = mem.short_term_json_dir / name
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            ts = 1_700_000_000 + i * 60
            os.utime(path, (ts, ts))

    def test_multi_speaker_batch_labels(self, base_dir):
        """溢れバッチに複数話者がいれば 「display_name」 でラベリングされる"""
        mem = ButlyMemory(base_dir, instance_name="99_maintain_test")
        turns = [
            ("session_20260707_000001.json", _turn_file("2026-07-07T00:00:01", "外部の発言", TARO_META)),
            ("session_20260707_000002.json", _turn_file("2026-07-07T00:00:02", "オーナーの発言")),
        ] + [
            (f"session_20260707_00000{i}.json", _turn_file(f"2026-07-07T00:00:0{i}", "後続"))
            for i in range(3, 6)
        ]
        self._write_turns(mem, turns)

        brain = _RecordingBrain()
        mem.maintain_memory(brain)  # keep=3 → 古い 2 件が溢れる

        assert len(brain.texts) == 2
        merged = "\n".join(brain.texts)
        assert "「たろう」" in merged
        # owner 発言も複数話者バッチでは 「呼称」 で刻まれる
        assert merged.count("「") >= 2

    def test_single_speaker_batch_unchanged(self, base_dir):
        """1:1 のみのバッチは従来どおりプレフィックスなし"""
        mem = ButlyMemory(base_dir, instance_name="99_maintain_solo")
        turns = [
            (f"session_20260707_00000{i}.json", _turn_file(f"2026-07-07T00:00:0{i}", "発言"))
            for i in range(1, 6)
        ]
        self._write_turns(mem, turns)

        brain = _RecordingBrain()
        mem.maintain_memory(brain)

        assert brain.texts
        assert "「" not in "\n".join(brain.texts)


# ===================================================================
# Sleeptime.stage_1_cleanup (整形 + 登場集計)
# ===================================================================


class TestSleeptimeStage1:
    @pytest.fixture
    def sleeptime_env(self, tmp_path, monkeypatch):
        import sleeptime as sleeptime_mod

        instances_dir = tmp_path / "butly_core" / "instances"
        inst_dir = instances_dir / "t1"
        (inst_dir / "memory_archive" / "1_integrated").mkdir(parents=True)
        (inst_dir / "config.json").write_text(
            json.dumps(
                {
                    "agent_profile": {"ai_name": "Butly"},
                    "user_profile": {"preferred_call": "悠希"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(sleeptime_mod, "BASE_DIR", tmp_path)
        monkeypatch.setattr(sleeptime_mod, "INSTANCES_DIR", instances_dir)

        hk = sleeptime_mod.ButlySleeptime()
        captured = {}
        monkeypatch.setattr(
            hk,
            "_generate_daily_digest",
            lambda instance_path, new_text: captured.update(text=new_text),
        )
        return hk, tmp_path, inst_dir, captured

    def test_multi_speaker_formatting_and_appearances(self, sleeptime_env):
        hk, tmp_path, inst_dir, captured = sleeptime_env
        integrated = inst_dir / "memory_archive" / "1_integrated"
        (integrated / "session_20260707_100000.json").write_text(
            json.dumps(
                _turn_file("2026-07-07T10:00:00", "外部の発言", TARO_META),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (integrated / "session_20260707_110000.json").write_text(
            json.dumps(_turn_file("2026-07-07T11:00:00", "オーナーの発言"), ensure_ascii=False),
            encoding="utf-8",
        )

        hk.stage_1_cleanup("t1")

        text = captured.get("text", "")
        assert "「たろう」: 外部の発言" in text
        assert "「悠希」: オーナーの発言" in text
        assert "Butly: 了解です" in text

        stats = json.loads(
            (tmp_path / PERSONS_FILENAME).read_text(encoding="utf-8")
        )["stats"]
        assert stats["p_discord_123"]["appearances"] == 1
        assert stats["p_discord_123"]["first_seen"] == "2026-07-07T10:00:00"

    def test_owner_only_keeps_legacy_format(self, sleeptime_env):
        """1:1 のみなら従来どおりの整形で、persons.json も作られない"""
        hk, tmp_path, inst_dir, captured = sleeptime_env
        integrated = inst_dir / "memory_archive" / "1_integrated"
        (integrated / "session_20260707_100000.json").write_text(
            json.dumps(_turn_file("2026-07-07T10:00:00", "オーナーの発言"), ensure_ascii=False),
            encoding="utf-8",
        )

        hk.stage_1_cleanup("t1")

        text = captured.get("text", "")
        assert "悠希: オーナーの発言" in text
        assert "「" not in text
        assert not (tmp_path / PERSONS_FILENAME).exists()


# ===================================================================
# Discord adapter
# ===================================================================


class TestDiscordRequestFields:
    def test_guild_and_display_name_propagate(self):
        req = build_discord_chat_request(
            text="hi",
            instance_name="Butly",
            user_id="123",
            channel_id="c1",
            guild_id="g1",
            display_name="たろう",
        )
        assert req.external_guild_id == "g1"
        assert req.external_display_name == "たろう"
        assert req.external_user_id == "123"
        assert req.lane == "direct"

    def test_defaults_stay_none(self):
        req = build_discord_chat_request(
            text="hi", instance_name="Butly", user_id="123", channel_id="c1"
        )
        assert req.external_guild_id is None
        assert req.external_display_name is None


# ===================================================================
# raw_memory_reader.format_sessions
# ===================================================================


class TestFormatSessionsSpeakers:
    def _sessions(self, with_meta: bool):
        meta = TARO_META if with_meta else None
        return [
            {
                "timestamp": "2026-07-07 10:00:00",
                "messages": _turn_file("2026-07-07T10:00:00", "外部の発言", meta)[
                    "messages"
                ],
            },
            {
                "timestamp": "2026-07-07 11:00:00",
                "messages": _turn_file("2026-07-07T11:00:00", "オーナーの発言")["messages"],
            },
        ]

    def test_plaintext_multi_speaker(self):
        text = format_sessions(
            self._sessions(True), format="plaintext", agent_name="Butly", user_name="悠希"
        )
        assert "「たろう」: 外部の発言" in text
        assert "「悠希」: オーナーの発言" in text

    def test_plaintext_single_speaker_unchanged(self):
        text = format_sessions(
            self._sessions(False), format="plaintext", agent_name="Butly", user_name="悠希"
        )
        assert "悠希: 外部の発言" in text
        assert "「" not in text

    def test_compact_multi_speaker(self):
        text = format_sessions(
            self._sessions(True), format="compact", agent_name="Butly", user_name="悠希"
        )
        assert "「たろう」: 外部の発言" in text
        assert "A: 了解です" in text
