"""
test_tavily_provider.py
-----------------------
TavilySearchProvider のユニットテスト。
API キー不要 — 実際の API 呼び出しはモックで代替。
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from butly_core.search.tavily_provider import TavilySearchProvider
from butly_core.search.types import SearchResult


class TestIsAvailable:
    """is_available() のテスト"""

    def test_available_with_key(self):
        """API キーが設定されていれば True"""
        provider = TavilySearchProvider()
        provider.api_key = "tvly-test-key"
        assert provider.is_available() is True

    def test_unavailable_without_key(self):
        """API キーが空なら False"""
        provider = TavilySearchProvider()
        provider.api_key = ""
        assert provider.is_available() is False

    def test_unavailable_with_none(self):
        """API キーが None 相当なら False"""
        with patch.dict(os.environ, {}, clear=True):
            provider = TavilySearchProvider()
            provider.api_key = ""
            assert provider.is_available() is False


class TestSearch:
    """search() のテスト"""

    def test_returns_empty_when_unavailable(self):
        """API キー未設定時は空リストを返す"""
        provider = TavilySearchProvider()
        provider.api_key = ""
        results = provider.search("test query")
        assert results == []

    @patch("butly_core.search.tavily_provider.TavilySearchProvider.is_available", return_value=True)
    @patch("butly_core.search.usage_tracker.UsageTracker.increment")
    def test_search_with_mock_response(self, mock_increment, mock_available):
        """モック API レスポンスで正常系を検証"""
        mock_response = {
            "results": [
                {
                    "title": "Result 1",
                    "url": "https://example.com/1",
                    "content": "First result content",
                    "score": 0.95,
                },
                {
                    "title": "Result 2",
                    "url": "https://example.com/2",
                    "content": "Second result content",
                    "score": 0.85,
                },
            ]
        }

        mock_client = MagicMock()
        mock_client.search.return_value = mock_response

        with patch("tavily.TavilyClient", return_value=mock_client):
            provider = TavilySearchProvider()
            provider.api_key = "tvly-test-key"
            results = provider.search("test query", max_results=2)

        assert len(results) == 2
        assert results[0].title == "Result 1"
        assert results[0].url == "https://example.com/1"
        assert results[0].content == "First result content"
        assert results[0].score == 0.95
        assert results[1].title == "Result 2"
        mock_increment.assert_called_once()

    @patch("butly_core.search.tavily_provider.TavilySearchProvider.is_available", return_value=True)
    def test_search_handles_exception(self, mock_available):
        """API エラー時は空リストを返す"""
        with patch("tavily.TavilyClient", side_effect=Exception("API Error")):
            provider = TavilySearchProvider()
            provider.api_key = "tvly-test-key"
            results = provider.search("test query")

        assert results == []

    @patch("butly_core.search.tavily_provider.TavilySearchProvider.is_available", return_value=True)
    @patch("butly_core.search.usage_tracker.UsageTracker.increment")
    def test_search_empty_results(self, mock_increment, mock_available):
        """API が空の結果を返した場合"""
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}

        with patch("tavily.TavilyClient", return_value=mock_client):
            provider = TavilySearchProvider()
            provider.api_key = "tvly-test-key"
            results = provider.search("test query")

        assert results == []
