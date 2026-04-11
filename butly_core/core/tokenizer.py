"""
tokenizer.py
-------------
tiktoken cl100k_base によるトークンカウント。
OpenAI 以外のモデルでは近似値（±10%程度）として使用。
"""

import tiktoken

_encoder = None


def count_tokens(text: str) -> int:
    """tiktoken cl100k_base でトークン数をカウント（近似値）"""
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return len(_encoder.encode(text))
