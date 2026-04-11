"""
test_raw_memory_reader.py
--------------------------
RawMemoryReader のユニットテスト。
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from butly_core.core.raw_memory_reader import (
    collect_raw_sessions,
    format_sessions,
    build_raw_memory_cache,
    CACHE_FILENAME,
)


def _write_session(directory: Path, filename: str, timestamp: str, messages: list):
    """テスト用セッション JSON を書き込むヘルパー。"""
    directory.mkdir(parents=True, exist_ok=True)
    data = {"timestamp": timestamp, "messages": messages}
    (directory / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _make_messages(user_text: str, model_text: str) -> list:
    return [
        {"role": "user", "parts": [user_text]},
        {"role": "model", "parts": [model_text]},
    ]


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    """テスト用インスタンスディレクトリ。"""
    inst = tmp_path / "instance"
    (inst / "memory_archive" / "1_integrated").mkdir(parents=True)
    (inst / "memory_archive" / "2_knowledgeized").mkdir(parents=True)
    return inst


class TestCollectEmpty:
    def test_collect_empty(self, instance_dir):
        """1_integrated も 2_knowledgeized も空 → 空リスト"""
        result = collect_raw_sessions(instance_dir)
        assert result == []


class TestCollectFromKnowledgeizedOnly:
    def test_collect_from_knowledgeized_only(self, instance_dir):
        """2_knowledgeized にのみ JSON がある → 正しく収集"""
        date_dir = instance_dir / "memory_archive" / "2_knowledgeized" / "2026-04-10"
        _write_session(
            date_dir,
            "session_20260410_210000.json",
            "2026-04-10T21:00:00.000000",
            _make_messages("こんにちは", "こんにちは！"),
        )

        result = collect_raw_sessions(instance_dir, max_tokens=10000)
        assert len(result) == 1
        assert result[0]["filename"] == "session_20260410_210000.json"
        assert result[0]["timestamp"] == "2026-04-10 21:00:00"


class TestCollectFromIntegratedOnly:
    def test_collect_skips_integrated(self, instance_dir):
        """1_integrated にのみ JSON がある → キャッシュ対象外のため空"""
        int_dir = instance_dir / "memory_archive" / "1_integrated"
        _write_session(
            int_dir,
            "session_20260411_100000.json",
            "2026-04-11T10:00:00.000000",
            _make_messages("テスト", "テスト応答"),
        )

        result = collect_raw_sessions(instance_dir, max_tokens=10000)
        assert len(result) == 0


class TestCollectMergedChronological:
    def test_collect_merged_chronological(self, instance_dir):
        """複数日付フォルダ → 時系列順で結合"""
        # 2_knowledgeized (古い)
        date_dir1 = instance_dir / "memory_archive" / "2_knowledgeized" / "2026-04-09"
        _write_session(
            date_dir1,
            "session_20260409_200000.json",
            "2026-04-09T20:00:00.000000",
            _make_messages("古いメッセージ", "古い応答"),
        )

        # 2_knowledgeized (新しい)
        date_dir2 = instance_dir / "memory_archive" / "2_knowledgeized" / "2026-04-11"
        _write_session(
            date_dir2,
            "session_20260411_100000.json",
            "2026-04-11T10:00:00.000000",
            _make_messages("新しいメッセージ", "新しい応答"),
        )

        result = collect_raw_sessions(instance_dir, max_tokens=10000)
        assert len(result) == 2
        # 古い→新しい順
        assert result[0]["filename"] == "session_20260409_200000.json"
        assert result[1]["filename"] == "session_20260411_100000.json"


class TestTokenLimitFileBoundary:
    def test_token_limit_file_boundary(self, instance_dir):
        """max_tokens 超過時にファイル単位で打ち切り"""
        kn_dir = instance_dir / "memory_archive" / "2_knowledgeized"

        # 3つのファイルを作成（各ファイルは十分なトークン数を持つ）
        for i, (ts, text) in enumerate([
            ("20260409_100000", "A" * 500),
            ("20260410_100000", "B" * 500),
            ("20260411_100000", "C" * 500),
        ]):
            date_dir = kn_dir / f"2026-04-{9+i:02d}"
            _write_session(
                date_dir,
                f"session_{ts}.json",
                f"2026-04-{9+i:02d}T10:00:00.000000",
                _make_messages(text, text),
            )

        # 非常に小さいトークン上限を設定
        # 1ファイルは含まれるが3ファイル全ては入らない
        result = collect_raw_sessions(instance_dir, max_tokens=200)
        assert len(result) < 3


class TestTokenLimitReturnsNewest:
    def test_token_limit_returns_newest(self, instance_dir):
        """打ち切り時に最新側のファイルが優先される"""
        kn_dir = instance_dir / "memory_archive" / "2_knowledgeized"

        _write_session(
            kn_dir / "2026-04-09",
            "session_20260409_100000.json",
            "2026-04-09T10:00:00.000000",
            _make_messages("古い" * 100, "古い応答" * 100),
        )
        _write_session(
            kn_dir / "2026-04-11",
            "session_20260411_100000.json",
            "2026-04-11T10:00:00.000000",
            _make_messages("新しい", "新しい応答"),
        )

        result = collect_raw_sessions(instance_dir, max_tokens=50)
        assert len(result) >= 1
        # 最新のファイルが含まれる
        filenames = [r["filename"] for r in result]
        assert "session_20260411_100000.json" in filenames


class TestCollectOnlyKnowledgeized:
    def test_collect_ignores_integrated(self, instance_dir):
        """1_integrated の JSON はキャッシュ対象外"""
        # 1_integrated にデータ配置
        int_dir = instance_dir / "memory_archive" / "1_integrated"
        _write_session(
            int_dir,
            "session_20260411_100000.json",
            "2026-04-11T10:00:00.000000",
            _make_messages("テスト", "応答"),
        )
        # 2_knowledgeized にデータ配置
        kn_dir = instance_dir / "memory_archive" / "2_knowledgeized" / "2026-04-10"
        _write_session(
            kn_dir,
            "session_20260410_210000.json",
            "2026-04-10T21:00:00.000000",
            _make_messages("ナレッジ済み", "応答"),
        )

        result = collect_raw_sessions(instance_dir, max_tokens=10000)
        assert len(result) == 1
        assert result[0]["filename"] == "session_20260410_210000.json"


class TestFormatMarkdown:
    def test_format_markdown(self):
        """markdown 形式の出力検証"""
        sessions = [{
            "timestamp": "2026-04-10 21:00:00",
            "messages": _make_messages("こんにちは", "こんにちは！"),
            "filename": "test.json",
        }]

        result = format_sessions(sessions, format="markdown", agent_name="Jarvis", user_name="悠希")
        assert "### 2026-04-10 21:00:00" in result
        assert "**悠希**: こんにちは" in result
        assert "**Jarvis**: こんにちは！" in result


class TestFormatPlaintext:
    def test_format_plaintext(self):
        """plaintext 形式の出力検証（現行 mid_term.txt と同等フォーマット）"""
        sessions = [{
            "timestamp": "2026-04-10 21:00:00",
            "messages": _make_messages("テスト", "テスト応答"),
            "filename": "test.json",
        }]

        result = format_sessions(sessions, format="plaintext", agent_name="Jarvis", user_name="悠希")
        assert "[2026-04-10 21:00:00] 悠希: テスト" in result
        assert "[2026-04-10 21:00:00] Jarvis: テスト応答" in result


class TestFormatCompact:
    def test_format_compact(self):
        """compact 形式の出力検証"""
        sessions = [{
            "timestamp": "2026-04-10 21:00:00",
            "messages": _make_messages("テスト", "テスト応答"),
            "filename": "test.json",
        }]

        result = format_sessions(sessions, format="compact")
        assert "U: テスト" in result
        assert "A: テスト応答" in result
        # タイムスタンプは含まれない
        assert "2026-04-10" not in result


class TestAgentUserNameSubstitution:
    def test_agent_user_name_substitution(self):
        """agent_name / user_name が正しく反映される"""
        sessions = [{
            "timestamp": "2026-04-10 21:00:00",
            "messages": _make_messages("入力", "出力"),
            "filename": "test.json",
        }]

        result = format_sessions(sessions, format="plaintext", agent_name="Luna", user_name="太郎")
        assert "太郎: 入力" in result
        assert "Luna: 出力" in result


class TestFormatEmpty:
    def test_format_empty_sessions(self):
        """空のセッションリスト → 空文字列"""
        assert format_sessions([], format="plaintext") == ""
        assert format_sessions([], format="markdown") == ""
        assert format_sessions([], format="compact") == ""


# ==================================================================
# build_raw_memory_cache テスト
# ==================================================================


class TestBuildCacheFromKnowledgeized:
    def test_build_cache_from_knowledgeized(self, instance_dir):
        """2_knowledgeized に JSON がある → raw_memory_cache.txt が正しく生成される"""
        date_dir = instance_dir / "memory_archive" / "2_knowledgeized" / "2026-04-10"
        _write_session(
            date_dir,
            "session_20260410_210000.json",
            "2026-04-10T21:00:00.000000",
            _make_messages("こんにちは", "こんにちは！"),
        )

        result = build_raw_memory_cache(
            instance_dir,
            max_tokens=10000,
            injection_format="plaintext",
            agent_name="Jarvis",
            user_name="悠希",
        )

        assert result["sessions"] == 1
        assert result["tokens"] > 0
        assert result["format"] == "plaintext"

        cache_path = instance_dir / CACHE_FILENAME
        assert cache_path.exists()
        content = cache_path.read_text(encoding="utf-8")
        assert "悠希: こんにちは" in content
        assert "Jarvis: こんにちは！" in content


class TestBuildCacheSkipsIntegrated:
    def test_build_cache_skips_integrated(self, instance_dir):
        """1_integrated に JSON があってもキャッシュに含まれない"""
        int_dir = instance_dir / "memory_archive" / "1_integrated"
        _write_session(
            int_dir,
            "session_20260411_100000.json",
            "2026-04-11T10:00:00.000000",
            _make_messages("統合テスト", "統合応答"),
        )

        result = build_raw_memory_cache(instance_dir, max_tokens=10000)
        assert result["sessions"] == 0

        cache_path = instance_dir / CACHE_FILENAME
        assert cache_path.exists()
        assert cache_path.read_text(encoding="utf-8") == ""


class TestCacheEarlyExit:
    def test_cache_early_exit(self, instance_dir):
        """トークン上限到達後、古い日付フォルダは走査しない"""
        kn_dir = instance_dir / "memory_archive" / "2_knowledgeized"
        _write_session(
            kn_dir / "2026-04-09",
            "session_20260409_100000.json",
            "2026-04-09T10:00:00.000000",
            _make_messages("古い" * 200, "古い応答" * 200),
        )
        _write_session(
            kn_dir / "2026-04-11",
            "session_20260411_100000.json",
            "2026-04-11T10:00:00.000000",
            _make_messages("新しい", "新しい応答"),
        )

        result = build_raw_memory_cache(instance_dir, max_tokens=50)
        # 最新のみ含まれるはず
        assert result["sessions"] >= 1

        cache_path = instance_dir / CACHE_FILENAME
        content = cache_path.read_text(encoding="utf-8")
        assert "新しい" in content


class TestCacheFileWritten:
    def test_cache_file_written(self, instance_dir):
        """raw_memory_cache.txt がファイルとして書き出される"""
        result = build_raw_memory_cache(instance_dir, max_tokens=10000)
        cache_path = instance_dir / CACHE_FILENAME
        assert cache_path.exists()
        assert result["sessions"] == 0


class TestFormatChangeRebuild:
    def test_format_change_rebuild(self, instance_dir):
        """format 変更後に再生成で内容が変わる"""
        date_dir = instance_dir / "memory_archive" / "2_knowledgeized" / "2026-04-10"
        _write_session(
            date_dir,
            "session_20260410_210000.json",
            "2026-04-10T21:00:00.000000",
            _make_messages("テスト", "テスト応答"),
        )

        # plaintext で生成
        build_raw_memory_cache(instance_dir, injection_format="plaintext", agent_name="A", user_name="U")
        content_plain = (instance_dir / CACHE_FILENAME).read_text(encoding="utf-8")

        # markdown で再生成
        build_raw_memory_cache(instance_dir, injection_format="markdown", agent_name="A", user_name="U")
        content_md = (instance_dir / CACHE_FILENAME).read_text(encoding="utf-8")

        assert content_plain != content_md
        assert "### 2026-04-10" in content_md
        assert "**U**: テスト" in content_md
