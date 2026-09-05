"""
xai.py
------
xAI (Grok) LLM プロバイダー。

Phase 1 リファクタ: 共通実装を ``OpenAICompatAdapter`` に集約。
本クラスは XAI Connection を入れたシムで、Vision 判定 (Grok Code Fast 除外)
を override する。

後方互換:
  - ``XaiProvider()`` 引数なし呼び出し
  - module-level ``_get_client()`` を patch するテスト
  - module-level ``_NON_VISION_PREFIXES`` 定数
  - xAI は embedding 非対応 (Connection.embeddings_supported=False 経由で None)
"""

import os
from typing import Optional

from butly_core.llm import _openai_compat as compat
from butly_core.llm.connections import get_connection
from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter

# Vision 非対応モデル: Grok Code Fast はコード特化でテキストのみ
# (後方互換のため module-level に残す)
_NON_VISION_PREFIXES = ("grok-code-fast",)

# xAI デフォルト base_url (後方互換)
_XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"


def _strip_xai_prefix(model_name: str) -> str:
    """'xai/' プレフィックスを除去する (後方互換)。"""
    return model_name.removeprefix("xai/")


def _get_client():
    """xAI 用 OpenAI 互換クライアントを生成する (テストパッチポイント)。"""
    try:
        import openai
    except ImportError:
        raise RuntimeError(
            "openai パッケージが必要です。`pip install openai` を実行してください。"
        )

    compat.load_env_file()

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "xAI API キーが見つかりません。.env または UI から XAI_API_KEY を設定してください。"
        )

    base_url = os.environ.get("XAI_BASE_URL", _XAI_DEFAULT_BASE_URL)
    return openai.OpenAI(api_key=api_key, base_url=base_url)


class XaiProvider(OpenAICompatAdapter):
    """xAI Connection を喋る OpenAICompatAdapter のシム。"""

    def __init__(self, default_model_name: Optional[str] = None):
        super().__init__(
            connection=get_connection("xai"),
            default_model_name=default_model_name,
        )

    def _build_client(self):
        """テスト互換: module-level _get_client() を通す。"""
        return _get_client()

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        base = _strip_xai_prefix(model_name)
        return not any(base.startswith(prefix) for prefix in _NON_VISION_PREFIXES)

    def _log_tag(self) -> str:
        return "XaiProvider"
