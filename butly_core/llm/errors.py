"""
errors.py
---------
LLM 呼び出し層の例外。

Provider の embed() は「値を返せなかった」ことを None ではなく例外で伝える。
None を返すと 429 (RESOURCE_EXHAUSTED) が正常系として扱われ、
``sleeptime._robust_api_call`` の指数バックオフに届かないまま
embedding_blob が NULL のカードが保存される（静かな欠損）。
"""

from __future__ import annotations


class EmbeddingError(RuntimeError):
    """embedding を生成できなかった。"""


class EmbeddingNotSupported(EmbeddingError):
    """この connection は embedding API を提供していない（リトライ不能）。"""


class EmbeddingUnavailable(EmbeddingError):
    """リトライを尽くしても embedding を取得できなかった。

    呼び出し側は「ベクトル無しで保存する」のではなく、書き込み自体を
    諦めて入力を再試行対象として残すこと。
    """
