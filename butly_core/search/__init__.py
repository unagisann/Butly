import os

from butly_core.search.types import SearchResult
from butly_core.search.tavily_provider import TavilySearchProvider
from butly_core.search.ollama_provider import OllamaWebSearchProvider


def create_search_provider(
    chat_model: str = "",
) -> TavilySearchProvider | OllamaWebSearchProvider:
    """設定に基づいて検索プロバイダーを生成する。

    v3.1: Ollama chat + OLLAMA_WEB_SEARCH_API_KEY → OllamaWebSearchProvider。
    それ以外 → TavilySearchProvider。
    """
    if chat_model.startswith("ollama/") and os.environ.get("OLLAMA_WEB_SEARCH_API_KEY"):
        return OllamaWebSearchProvider()
    return TavilySearchProvider()
