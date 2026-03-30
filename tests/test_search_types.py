"""
test_search_types.py
--------------------
SearchResult DTO のユニットテスト。
API キー不要。
"""

import pytest

from butly_core.search.types import SearchResult


class TestSearchResult:
    """SearchResult DTO のテスト"""

    def test_basic_creation(self):
        """基本的なインスタンス生成"""
        result = SearchResult(
            title="Test Title",
            url="https://example.com",
            content="Some content",
        )
        assert result.title == "Test Title"
        assert result.url == "https://example.com"
        assert result.content == "Some content"
        assert result.score is None

    def test_with_score(self):
        """score 付きでインスタンス生成"""
        result = SearchResult(
            title="Test",
            url="https://example.com",
            content="Content",
            score=0.95,
        )
        assert result.score == 0.95

    def test_serialization(self):
        """Pydantic の dict 変換"""
        result = SearchResult(
            title="Title",
            url="https://example.com",
            content="Content",
            score=0.8,
        )
        d = result.model_dump()
        assert d["title"] == "Title"
        assert d["url"] == "https://example.com"
        assert d["content"] == "Content"
        assert d["score"] == 0.8

    def test_deserialization(self):
        """dict から SearchResult を復元"""
        data = {
            "title": "Restored",
            "url": "https://example.com/restore",
            "content": "Restored content",
            "score": 0.7,
        }
        result = SearchResult(**data)
        assert result.title == "Restored"
        assert result.score == 0.7
