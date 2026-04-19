"""
test_search_factory.py
----------------------
検索プロバイダーファクトリのユニットテスト。
"""

import os
from unittest.mock import patch

import pytest

from butly_core.search import create_search_provider
from butly_core.search.tavily_provider import TavilySearchProvider
from butly_core.search.ollama_provider import OllamaWebSearchProvider


class TestSearchFactory:
    """create_search_provider のプロバイダー選択テスト。"""

    def test_ollama_chat_with_ollama_key(self):
        """Ollama chat + OLLAMA_WEB_SEARCH_API_KEY → OllamaWebSearchProvider"""
        with patch.dict(os.environ, {"OLLAMA_WEB_SEARCH_API_KEY": "ollama-test"}):
            provider = create_search_provider("ollama/llama3.1")
        assert isinstance(provider, OllamaWebSearchProvider)

    def test_openai_chat_with_tavily_key(self):
        """OpenAI chat + TAVILY_API_KEY → TavilySearchProvider"""
        env = {k: v for k, v in os.environ.items() if k != "OLLAMA_WEB_SEARCH_API_KEY"}
        env["TAVILY_API_KEY"] = "tvly-test"
        with patch.dict(os.environ, env, clear=True):
            provider = create_search_provider("gpt-4o")
        assert isinstance(provider, TavilySearchProvider)

    def test_ollama_chat_without_ollama_key(self):
        """Ollama chat + Ollama key なし → TavilySearchProvider"""
        env = {k: v for k, v in os.environ.items() if k != "OLLAMA_WEB_SEARCH_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            provider = create_search_provider("ollama/llama3.1")
        assert isinstance(provider, TavilySearchProvider)

    def test_empty_model(self):
        """空のモデル名 → TavilySearchProvider"""
        provider = create_search_provider("")
        assert isinstance(provider, TavilySearchProvider)

    def test_no_model_arg(self):
        """引数なし (デフォルト) → TavilySearchProvider"""
        provider = create_search_provider()
        assert isinstance(provider, TavilySearchProvider)

    def test_xai_chat_uses_tavily(self):
        """xAI chat → TavilySearchProvider (xAI はネイティブ検索未対応)"""
        provider = create_search_provider("grok-4.20-reasoning")
        assert isinstance(provider, TavilySearchProvider)
