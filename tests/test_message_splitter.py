"""
test_message_splitter.py
------------------------
Discord 2000 文字制限対応の分割ロジックの単体テスト。
"""

import pytest

from butly_core.external.message_splitter import split_message, DEFAULT_HARD_LIMIT


class TestBasic:
    def test_empty_returns_empty(self):
        assert split_message("") == []
        assert split_message("   \n  ") == []

    def test_short_returns_single(self):
        assert split_message("こんにちは") == ["こんにちは"]

    def test_exactly_at_limit_single(self):
        text = "あ" * 1900
        chunks = split_message(text, hard_limit=1900)
        assert chunks == [text]

    def test_invalid_limit_raises(self):
        with pytest.raises(ValueError):
            split_message("x", hard_limit=0)


class TestBoundaries:
    def test_splits_on_blank_line(self):
        # limit=10。空行で段落が分かれる。
        text = "abcdef\n\nghijkl"
        chunks = split_message(text, hard_limit=10)
        assert chunks == ["abcdef", "ghijkl"]

    def test_prefers_blank_line_over_newline(self):
        text = "aaa\nbbb\n\nccc"
        chunks = split_message(text, hard_limit=10)
        # 上限 10 内で最後の空行（"\n\n"）で切る
        assert chunks[0] == "aaa\nbbb"
        assert chunks[1] == "ccc"

    def test_splits_on_newline(self):
        text = "aaaaa\nbbbbb\nccccc"
        chunks = split_message(text, hard_limit=8)
        for c in chunks:
            assert len(c) <= 8
        assert "".join(chunks).replace("\n", "") == "aaaaabbbbbccccc"

    def test_splits_on_period(self):
        text = "これは一文目です。これは二文目です。"
        chunks = split_message(text, hard_limit=12)
        # 句点で切れ、句点は前側に含まれる
        assert chunks[0].endswith("。")
        for c in chunks:
            assert len(c) <= 12

    def test_forced_cut_when_no_boundary(self):
        text = "x" * 25  # 境界なし
        chunks = split_message(text, hard_limit=10)
        assert chunks == ["x" * 10, "x" * 10, "x" * 5]


class TestInvariants:
    def test_all_chunks_within_limit(self):
        text = ("段落です。" * 200 + "\n\n") * 5
        chunks = split_message(text, hard_limit=200)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 200

    def test_long_text_default_limit(self):
        text = "あ" * 5000
        chunks = split_message(text)
        assert len(chunks) >= 3
        for c in chunks:
            assert len(c) <= DEFAULT_HARD_LIMIT

    def test_no_empty_chunks(self):
        text = "a\n\n\n\nb\n\n\n\nc" * 100
        chunks = split_message(text, hard_limit=15)
        assert all(c.strip() for c in chunks)
