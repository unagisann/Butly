"""
test_session_digest_format.py
-------------------------------
SESSION DIGEST のヘッダー形式 (相対時間化) のテスト。
ファイル名や絶対タイムスタンプは LLM ノイズになるため除去し、
代わりに「約N時間前」のような相対表現に置き換える。
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from butly_core.core.memory import (
    _parse_session_filename_timestamp,
    _strip_legacy_time_line,
    _format_relative_time,
)


class TestParseSessionFilenameTimestamp:
    def test_with_extension(self):
        dt = _parse_session_filename_timestamp("session_20260517_174748.txt")
        assert dt == datetime(2026, 5, 17, 17, 47, 48)

    def test_without_extension(self):
        dt = _parse_session_filename_timestamp("session_20260101_000000")
        assert dt == datetime(2026, 1, 1, 0, 0, 0)

    def test_with_microseconds(self):
        dt = _parse_session_filename_timestamp("session_20260101_000000_123456.json")
        assert dt == datetime(2026, 1, 1, 0, 0, 0)

    def test_invalid_format_returns_none(self):
        assert _parse_session_filename_timestamp("not_a_session.txt") is None
        assert _parse_session_filename_timestamp("session_bad.txt") is None
        assert _parse_session_filename_timestamp("") is None

    def test_invalid_date_returns_none(self):
        # 13月99日のような不正値
        assert _parse_session_filename_timestamp("session_20261399_999999.txt") is None


class TestStripLegacyTimeLine:
    def test_strips_first_line(self):
        text = "Time: 2026-05-17T17:47:48.102965\nここから本文\n本文続き"
        cleaned = _strip_legacy_time_line(text)
        assert cleaned == "ここから本文\n本文続き"

    def test_no_time_line_unchanged(self):
        text = "本文だけ\n複数行"
        assert _strip_legacy_time_line(text) == text

    def test_time_only(self):
        text = "Time: 2026-05-17T17:47:48"
        assert _strip_legacy_time_line(text) == ""


class TestFormatRelativeTime:
    @pytest.fixture
    def now(self):
        return datetime(2026, 5, 17, 22, 0, 0)

    def test_just_now(self, now):
        assert _format_relative_time(now - timedelta(seconds=10), now) == "たった今"

    def test_minutes(self, now):
        assert _format_relative_time(now - timedelta(minutes=30), now) == "約30分前"

    def test_one_hour(self, now):
        assert _format_relative_time(now - timedelta(minutes=75), now) == "約1.2時間前"

    def test_under_two_hours(self, now):
        assert _format_relative_time(now - timedelta(minutes=90), now) == "約1.5時間前"

    def test_hours(self, now):
        assert _format_relative_time(now - timedelta(hours=4, minutes=10), now) == "約4時間前"

    def test_days(self, now):
        assert _format_relative_time(now - timedelta(days=2, hours=5), now) == "約2日前"


class TestGetSessionDigestIntegration:
    """ButlyMemory.get_session_digest() の出力フォーマットを検証"""

    @pytest.fixture
    def memory(self, tmp_path, monkeypatch):
        from butly_core.core.memory import ButlyMemory
        monkeypatch.setattr(ButlyMemory, "_load_config", lambda self: {"memory": {}})
        return ButlyMemory(instance_name="test", base_dir=tmp_path)

    def test_outputs_relative_time_header(self, memory, tmp_path):
        """新形式ファイルから相対時間ヘッダーで出力される"""
        # 30 分前に書かれたファイルとして作成
        f = memory.session_digest_dir / "session_20260517_213000.txt"
        f.write_text("これは要約です", encoding="utf-8")
        # mtime も合わせる
        ts = datetime(2026, 5, 17, 21, 30, 0).timestamp()
        os.utime(f, (ts, ts))

        result = memory.get_session_digest()
        # ファイル名 / Time: の絶対表記は出ない
        assert "session_20260517" not in result
        assert "Time:" not in result
        # 相対表現が出る
        assert "前 ---" in result or "たった今" in result
        assert "これは要約です" in result

    def test_strips_legacy_time_prefix(self, memory, tmp_path):
        """旧形式 (Time: ... を含む) ファイルでも Time: 行は出力されない"""
        f = memory.session_digest_dir / "session_20260517_180000.txt"
        f.write_text("Time: 2026-05-17T18:00:00.123\n本文ここから", encoding="utf-8")

        result = memory.get_session_digest()
        assert "Time: 2026-05-17" not in result
        assert "本文ここから" in result

    def test_multiple_files_ordered_chronologically(self, memory):
        """複数ファイル: 古い → 新しい順に出力 (mtime 昇順)"""
        f1 = memory.session_digest_dir / "session_20260517_100000.txt"
        f1.write_text("古い要約", encoding="utf-8")
        os.utime(f1, (datetime(2026, 5, 17, 10).timestamp(),) * 2)

        f2 = memory.session_digest_dir / "session_20260517_200000.txt"
        f2.write_text("新しい要約", encoding="utf-8")
        os.utime(f2, (datetime(2026, 5, 17, 20).timestamp(),) * 2)

        result = memory.get_session_digest()
        assert result.index("古い要約") < result.index("新しい要約")
