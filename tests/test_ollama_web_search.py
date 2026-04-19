"""
test_ollama_web_search.py
-------------------------
OllamaWebSearchProvider のユニットテスト。
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from butly_core.search.ollama_provider import OllamaWebSearchProvider


class TestOllamaWebSearchAvailability:
    """is_available テスト (B7 対応: OLLAMA_WEB_SEARCH_API_KEY)。"""

    def test_available_with_key(self):
        with patch.dict(os.environ, {"OLLAMA_WEB_SEARCH_API_KEY": "ollama-test"}):
            provider = OllamaWebSearchProvider()
            assert provider.is_available() is True

    def test_not_available_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            # clear=True で全部消してしまうので、安全策として直接設定
            env = {k: v for k, v in os.environ.items() if k != "OLLAMA_WEB_SEARCH_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                provider = OllamaWebSearchProvider()
                assert provider.is_available() is False


class TestOllamaWebSearchSearch:
    """search テスト。"""

    @patch("urllib.request.urlopen")
    def test_search_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "results": [
                {"title": "Result 1", "url": "https://example.com/1", "snippet": "Content 1"},
                {"title": "Result 2", "url": "https://example.com/2", "snippet": "Content 2"},
            ]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.dict(os.environ, {"OLLAMA_WEB_SEARCH_API_KEY": "ollama-test"}):
            provider = OllamaWebSearchProvider()
            with patch("butly_core.search.usage_tracker.UsageTracker.increment") as mock_inc:
                results = provider.search("test query")

        assert len(results) == 2
        assert results[0].title == "Result 1"
        assert results[0].url == "https://example.com/1"
        assert results[0].content == "Content 1"
        assert results[0].score is None
        mock_inc.assert_called_once_with("ollama")

    @patch("urllib.request.urlopen")
    def test_search_http_error(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://ollama.com/api/web_search", 500, "Server Error", {}, None
        )

        with patch.dict(os.environ, {"OLLAMA_WEB_SEARCH_API_KEY": "ollama-test"}):
            provider = OllamaWebSearchProvider()
            results = provider.search("test query")

        assert results == []

    def test_search_no_key_returns_empty(self):
        env = {k: v for k, v in os.environ.items() if k != "OLLAMA_WEB_SEARCH_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            provider = OllamaWebSearchProvider()
            results = provider.search("test query")
            assert results == []

    @patch("urllib.request.urlopen")
    def test_search_max_results(self, mock_urlopen):
        """max_results で結果数を制限できる"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "results": [
                {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": f"Content {i}"}
                for i in range(10)
            ]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.dict(os.environ, {"OLLAMA_WEB_SEARCH_API_KEY": "ollama-test"}):
            provider = OllamaWebSearchProvider()
            with patch("butly_core.search.usage_tracker.UsageTracker.increment"):
                results = provider.search("test", max_results=2)

        assert len(results) == 2
