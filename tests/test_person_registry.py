"""
test_person_registry.py
-----------------------
PersonRegistry (persons.json) のユニットテスト。

検証項目 (group_context_lanes_plan Phase 1):
  - 解決優先順位: aliases 完全一致 → 決定的仮 ID 発行
  - 決定的仮 ID: 同じ入力から常に同じ ID / サニタイズ
  - マージの読み出し時解決 (merged_into チェーン / ループ安全)
  - 登場回数集計 (record_appearances)
  - 壊れた persons.json でも解決が落ちない
"""

import json
from pathlib import Path

import pytest

from butly_core.external.person_registry import (
    OWNER_FALLBACK_PERSON_ID,
    PERSONS_FILENAME,
    PersonRegistry,
    provisional_person_id,
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def registry(data_dir: Path) -> PersonRegistry:
    return PersonRegistry(data_dir)


def _write_persons(data_dir: Path, data: dict) -> None:
    (data_dir / PERSONS_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _read_persons(data_dir: Path) -> dict:
    return json.loads((data_dir / PERSONS_FILENAME).read_text(encoding="utf-8"))


SAMPLE = {
    "persons": {
        "p_yuki": {
            "display_name": "悠希",
            "is_owner": True,
            "aliases": {
                "discord": ["123456789012345678"],
                "line": ["Uaaaabbbbccccdddd"],
            },
            "name_variants": ["悠希", "unagisann"],
        },
        "p_taro": {
            "display_name": "たろう",
            "aliases": {"discord": ["999"]},
        },
    }
}


# ===================================================================
# 決定的仮 ID
# ===================================================================


class TestProvisionalPersonId:
    def test_deterministic(self):
        a = provisional_person_id("discord", "123456789")
        b = provisional_person_id("discord", "123456789")
        assert a == b
        assert a.startswith("p_discord_")
        assert "123456789" not in a

    def test_source_distinguishes(self):
        assert provisional_person_id("discord", "1") != provisional_person_id(
            "line", "1"
        )

    def test_sanitized(self):
        pid = provisional_person_id("Discord ", "U:ab/cd")
        assert pid.startswith("p_discord_")
        assert "U_ab_cd" not in pid

    def test_empty_source_fallback(self):
        assert provisional_person_id("", "42").startswith("p_unknown_")


# ===================================================================
# 解決
# ===================================================================


class TestResolve:
    def test_alias_exact_match(self, data_dir, registry):
        """aliases 完全一致が仮 ID より優先される"""
        _write_persons(data_dir, SAMPLE)
        resolved = registry.resolve("discord", "123456789012345678")
        assert resolved.person_id == "p_yuki"
        assert resolved.display_name == "悠希"
        assert resolved.is_owner is True
        assert resolved.provisional is False

    def test_alias_other_source(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        resolved = registry.resolve("line", "Uaaaabbbbccccdddd")
        assert resolved.person_id == "p_yuki"

    def test_unregistered_gets_provisional(self, data_dir, registry):
        """未登録ユーザーには決定的仮 ID が発行される"""
        _write_persons(data_dir, SAMPLE)
        resolved = registry.resolve("discord", "55555")
        assert resolved.person_id == provisional_person_id("discord", "55555")
        assert resolved.provisional is True
        assert resolved.display_name is None
        assert resolved.is_owner is False

    def test_no_file_still_resolves(self, registry):
        """persons.json が無くても仮 ID で解決できる"""
        resolved = registry.resolve("discord", "1")
        assert resolved.person_id == provisional_person_id("discord", "1")
        assert resolved.provisional is True

    def test_broken_file_still_resolves(self, data_dir, registry):
        (data_dir / PERSONS_FILENAME).write_text("{broken", encoding="utf-8")
        resolved = registry.resolve("discord", "1")
        assert resolved.person_id == provisional_person_id("discord", "1")

    def test_source_case_insensitive(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        resolved = registry.resolve("Discord", "999")
        assert resolved.person_id == "p_taro"


# ===================================================================
# マージ (読み出し時解決)
# ===================================================================


class TestMerge:
    def test_merged_provisional_resolves_to_person(self, data_dir, registry):
        """仮 ID のマージ後、resolve が正式 person を返す（RAW は不変のまま）"""
        _write_persons(data_dir, SAMPLE)
        registry.merge_person(provisional_person_id("discord", "55555"), "p_taro")

        resolved = registry.resolve("discord", "55555")
        assert resolved.person_id == "p_taro"
        assert resolved.display_name == "たろう"

    def test_resolve_person_id_follows_chain(self, data_dir, registry):
        _write_persons(
            data_dir,
            {
                "persons": {
                    "p_a": {"merged_into": "p_b"},
                    "p_b": {"merged_into": "p_c"},
                    "p_c": {"display_name": "C"},
                }
            },
        )
        assert registry.resolve_person_id("p_a") == "p_c"

    def test_merge_moves_aliases(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        registry.merge_person("p_taro", "p_yuki")

        data = _read_persons(data_dir)
        assert data["persons"]["p_taro"]["merged_into"] == "p_yuki"
        assert "aliases" not in data["persons"]["p_taro"]
        assert "999" in data["persons"]["p_yuki"]["aliases"]["discord"]
        # 既存 alias は保持される
        assert "123456789012345678" in data["persons"]["p_yuki"]["aliases"]["discord"]

    def test_merge_to_unregistered_raises(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        with pytest.raises(ValueError):
            registry.merge_person("p_taro", "p_nobody")

    def test_merge_self_raises(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        with pytest.raises(ValueError):
            registry.merge_person("p_taro", "p_taro")

    def test_merge_loop_raises(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        registry.merge_person("p_taro", "p_yuki")
        with pytest.raises(ValueError):
            registry.merge_person("p_yuki", "p_taro")

    def test_merge_sums_stats(self, data_dir, registry):
        data = dict(SAMPLE)
        data["stats"] = {
            "p_taro": {"appearances": 3, "first_seen": "2026-07-01", "last_seen": "2026-07-02"},
            "p_yuki": {"appearances": 5, "first_seen": "2026-07-03", "last_seen": "2026-07-04"},
        }
        _write_persons(data_dir, data)
        registry.merge_person("p_taro", "p_yuki")

        stats = _read_persons(data_dir)["stats"]
        assert "p_taro" not in stats
        assert stats["p_yuki"]["appearances"] == 8
        assert stats["p_yuki"]["first_seen"] == "2026-07-01"
        assert stats["p_yuki"]["last_seen"] == "2026-07-04"


# ===================================================================
# owner / display_name
# ===================================================================


class TestOwnerAndDisplayName:
    def test_owner_person_id(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        assert registry.owner_person_id() == "p_yuki"

    def test_owner_fallback(self, registry):
        assert registry.owner_person_id() == OWNER_FALLBACK_PERSON_ID

    def test_display_name_resolves_merge(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        provisional = provisional_person_id("discord", "55555")
        registry.merge_person(provisional, "p_taro")
        assert registry.display_name(provisional) == "たろう"
        assert registry.display_name("p_unknown") is None


# ===================================================================
# 登場回数集計
# ===================================================================


class TestRecordAppearances:
    def test_creates_stats(self, data_dir, registry):
        registry.record_appearances(
            {
                "p_discord_1": {
                    "count": 2,
                    "first_seen": "2026-07-07T10:00:00",
                    "last_seen": "2026-07-07T12:00:00",
                }
            }
        )
        stats = _read_persons(data_dir)["stats"]
        assert stats["p_discord_1"]["appearances"] == 2
        assert stats["p_discord_1"]["first_seen"] == "2026-07-07T10:00:00"

    def test_accumulates(self, data_dir, registry):
        registry.record_appearances(
            {"p_x": {"count": 1, "first_seen": "2026-07-05", "last_seen": "2026-07-05"}}
        )
        registry.record_appearances(
            {"p_x": {"count": 3, "first_seen": "2026-07-01", "last_seen": "2026-07-08"}}
        )
        stats = _read_persons(data_dir)["stats"]
        assert stats["p_x"]["appearances"] == 4
        assert stats["p_x"]["first_seen"] == "2026-07-01"
        assert stats["p_x"]["last_seen"] == "2026-07-08"

    def test_none_timestamps_ok(self, data_dir, registry):
        registry.record_appearances(
            {"p_y": {"count": 1, "first_seen": None, "last_seen": None}}
        )
        stats = _read_persons(data_dir)["stats"]
        assert stats["p_y"]["appearances"] == 1

    def test_empty_noop(self, data_dir, registry):
        registry.record_appearances({})
        assert not (data_dir / PERSONS_FILENAME).exists()

    def test_resolves_merged_person_before_accumulating(self, data_dir, registry):
        _write_persons(data_dir, SAMPLE)
        provisional = provisional_person_id("discord", "55555")
        registry.merge_person(provisional, "p_taro")

        registry.record_appearances(
            {
                provisional: {
                    "count": 2,
                    "first_seen": "2026-07-07T10:00:00",
                    "last_seen": "2026-07-07T12:00:00",
                }
            }
        )

        stats = _read_persons(data_dir)["stats"]
        assert provisional not in stats
        assert stats["p_taro"]["appearances"] == 2
