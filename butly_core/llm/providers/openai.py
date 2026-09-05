"""
openai.py
---------
OpenAI (GPT) LLM プロバイダー。

Phase 1 リファクタ: 共通実装を ``OpenAICompatAdapter`` に集約。
本クラスは OpenAI Connection を入れた薄シム + Vision 判定 override のみ。

後方互換:
  - ``OpenAIProvider()`` 引数なし呼び出し
  - module-level ``_get_client()`` を patch するテスト
  - module-level ``_VISION_MODELS`` 定数
"""

import os
from typing import Optional

from butly_core.llm import _openai_compat as compat
from butly_core.llm.connections import get_connection
from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter

# Vision 対応が確認されているモデルプレフィックス (後方互換のため module-level に残す)
_VISION_MODELS = {"gpt-4o", "gpt-4-turbo", "gpt-4-vision", "o1", "o3", "o4"}


def _get_client():
    """OpenAI クライアントを遅延生成する (テストパッチポイント)。"""
    try:
        import openai
    except ImportError:
        raise RuntimeError(
            "openai パッケージが必要です。`pip install openai` を実行してください。"
        )

    compat.load_env_file()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OpenAI API キーが見つかりません。.env に OPENAI_API_KEY を設定してください。"
        )

    base_url = os.environ.get("OPENAI_BASE_URL")
    return (
        openai.OpenAI(api_key=api_key, base_url=base_url)
        if base_url
        else openai.OpenAI(api_key=api_key)
    )


class OpenAIProvider(OpenAICompatAdapter):
    """OpenAI Connection を喋る OpenAICompatAdapter のシム。"""

    def __init__(self, default_model_name: Optional[str] = None):
        super().__init__(
            connection=get_connection("openai"),
            default_model_name=default_model_name,
        )

    def _build_client(self):
        """テスト互換: module-level _get_client() を通す。"""
        return _get_client()

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        return any(model_name.startswith(prefix) for prefix in _VISION_MODELS)

    def _log_tag(self) -> str:
        return "OpenAIProvider"
