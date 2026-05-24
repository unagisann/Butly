"""
ollama_provider.py
------------------
Ollama Cloud の Web Search API を使用した検索プロバイダー。
https://ollama.com/api/web_search を REST で叩く。

v3.1: Phase 1 — web_search のみ (web_fetch は Phase 2 以降)。
"""

import json
import os
import urllib.request
import urllib.error
from typing import List

from butly_core.search.base import BaseSearchProvider
from butly_core.search.types import SearchResult


class OllamaWebSearchProvider(BaseSearchProvider):
    """Ollama Cloud の Web Search API プロバイダー。

    環境変数 OLLAMA_WEB_SEARCH_API_KEY で認証する。
    """

    _ENDPOINT = "https://ollama.com/api/web_search"

    def __init__(self):
        self.api_key = os.environ.get("OLLAMA_WEB_SEARCH_API_KEY", "")

    def search(self, query: str, max_results: int = 3) -> List[SearchResult]:
        if not self.is_available():
            print("[OllamaWebSearch] API key not configured")
            return []

        try:
            payload = json.dumps({"query": query}).encode("utf-8")
            req = urllib.request.Request(
                self._ENDPOINT,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            results = []
            for item in data.get("results", [])[:max_results]:
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        content=item.get("snippet", ""),
                        score=None,  # Ollama API は score を返さない
                    )
                )

            from butly_core.search.usage_tracker import UsageTracker

            UsageTracker.increment("ollama")

            return results

        except Exception as e:
            print(f"[OllamaWebSearch] Search Error: {e}")
            return []

    def is_available(self) -> bool:
        return bool(self.api_key)
