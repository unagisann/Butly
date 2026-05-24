import os
from typing import List

from butly_core.search.base import BaseSearchProvider
from butly_core.search.types import SearchResult


class TavilySearchProvider(BaseSearchProvider):
    """Tavily Search API を使用した検索プロバイダー"""

    def __init__(self):
        self.api_key = os.environ.get("TAVILY_API_KEY", "")

    def search(self, query: str, max_results: int = 3) -> List[SearchResult]:
        if not self.is_available():
            print("[TavilySearch] API key not configured")
            return []

        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=self.api_key)
            response = client.search(
                query=query,
                max_results=max_results,
                search_depth="basic",
                include_answer=False,
            )

            results = []
            for item in response.get("results", []):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        content=item.get("content", ""),
                        score=item.get("score"),
                    )
                )

            from butly_core.search.usage_tracker import UsageTracker

            UsageTracker.increment("tavily")

            return results

        except Exception as e:
            print(f"[TavilySearch] Search Error: {e}")
            return []

    def is_available(self) -> bool:
        return bool(self.api_key)
