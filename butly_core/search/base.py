from abc import ABC, abstractmethod
from typing import List

from butly_core.search.types import SearchResult


class BaseSearchProvider(ABC):
    """検索プロバイダーの抽象基底クラス"""

    @abstractmethod
    def search(self, query: str, max_results: int = 3) -> List[SearchResult]:
        """
        クエリを実行し、検索結果を返す（同期）。

        Parameters
        ----------
        query : str
            検索クエリ文字列。
        max_results : int
            最大取得件数。

        Returns
        -------
        List[SearchResult]
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """API キーが設定済みかなど、利用可能かを返す。"""
        ...
