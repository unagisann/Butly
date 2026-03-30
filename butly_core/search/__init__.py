from butly_core.search.types import SearchResult
from butly_core.search.tavily_provider import TavilySearchProvider


def create_search_provider():
    """設定に基づいて検索プロバイダーを生成する（将来の差し替えポイント）"""
    return TavilySearchProvider()
