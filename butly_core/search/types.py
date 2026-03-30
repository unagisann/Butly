from pydantic import BaseModel
from typing import Optional


class SearchResult(BaseModel):
    """検索結果 1 件の DTO"""
    title: str
    url: str
    content: str  # スニペット or 本文抜粋
    score: Optional[float] = None  # 関連度スコア（プロバイダーが返す場合）
