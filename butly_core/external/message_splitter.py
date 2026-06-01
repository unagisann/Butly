"""
message_splitter.py
-------------------
プラットフォームの文字数上限に合わせて長文を複数メッセージに分割する。

Discord の hard limit は 2000 文字。余白を見て 1900 文字を既定上限にする。

分割境界は以下の順に「上限内で最後に現れた位置」を選ぶ:
  1. 空行（``\\n\\n``）
  2. 改行（``\\n``）
  3. 句点（``。``）
  4. 強制カット（上限ちょうどで切る）

SDK 非依存の純粋関数。
"""

from __future__ import annotations

from typing import List, Tuple

# Discord の絶対上限（参考値）。既定の hard limit はこれより小さく取る。
DISCORD_HARD_CAP = 2000
DEFAULT_HARD_LIMIT = 1900


def _find_split(remaining: str, limit: int) -> Tuple[str, str]:
    """``remaining`` を上限 ``limit`` 内で (head, rest) に分割する。

    境界優先順位: 空行 → 改行 → 句点 → 強制カット。
    head / rest はまだ前後空白を含むことがある（呼び出し側で trim する）。
    rest は必ず remaining の真の suffix（= 進行が保証される）。
    """
    window = remaining[:limit]

    # 1. 空行
    idx = window.rfind("\n\n")
    if idx > 0:
        return remaining[:idx], remaining[idx:]

    # 2. 改行
    idx = window.rfind("\n")
    if idx > 0:
        return remaining[:idx], remaining[idx:]

    # 3. 句点（。は head 側に含める）
    idx = window.rfind("。")
    if idx >= 0:
        return remaining[: idx + 1], remaining[idx + 1 :]

    # 4. 強制カット
    return remaining[:limit], remaining[limit:]


def split_message(text: str, hard_limit: int = DEFAULT_HARD_LIMIT) -> List[str]:
    """``text`` を各要素が ``hard_limit`` 文字以内になるよう分割する。

    Parameters
    ----------
    text : str
        分割対象。前後の空白は除去される。
    hard_limit : int
        1 メッセージあたりの最大文字数（既定 1900）。

    Returns
    -------
    list[str]
        分割後のメッセージ片。空文字列は含まない。``text`` が空なら ``[]``。
        ``hard_limit`` 以内に収まるなら ``[text]`` の 1 要素。
    """
    if hard_limit < 1:
        raise ValueError("hard_limit must be >= 1")

    text = text.strip()
    if not text:
        return []
    if len(text) <= hard_limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > hard_limit:
        head, rest = _find_split(remaining, hard_limit)
        head = head.rstrip()
        rest = rest.lstrip()
        # _find_split は常に非空の head と真の suffix の rest を返すため前進が保証される。
        # 念のため、万一 head が空になった場合は強制カットで前進させる。
        if not head:
            head = remaining[:hard_limit].rstrip()
            rest = remaining[hard_limit:].lstrip()
        chunks.append(head)
        remaining = rest

    if remaining:
        chunks.append(remaining)
    return chunks
