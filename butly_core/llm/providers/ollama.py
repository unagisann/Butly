"""
ollama.py
---------
Ollama LLM プロバイダー。

Phase 1 リファクタ: 共通実装を ``OpenAICompatAdapter`` に集約。
本クラスは Ollama Connection を入れたシムで、Vision 判定 (llava 等)
を override する。

後方互換:
  - ``OllamaProvider()`` 引数なし呼び出し
  - module-level ``_get_client()`` を patch するテスト
  - module-level ``_VISION_MODELS`` 定数
"""

import os
from typing import Optional

from butly_core.llm import _openai_compat as compat
from butly_core.llm.connections import get_connection
from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter

# Vision 対応が確認されているモデルプレフィックス (後方互換のため module-level)
_VISION_MODELS = {"llava", "bakllava", "moondream", "llama3.2-vision", "gemma3"}


def _strip_ollama_prefix(model_name: str) -> str:
    """'ollama/' プレフィックスを除去する (後方互換)。"""
    return model_name.removeprefix("ollama/")


def _get_client():
    """Ollama 用 OpenAI 互換クライアントを生成する (テストパッチポイント)。"""
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai パッケージが必要です。`pip install openai` を実行してください。")

    compat.load_env_file()

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    return openai.OpenAI(api_key="ollama", base_url=base_url)


class OllamaProvider(OpenAICompatAdapter):
    """Ollama Connection を喋る OpenAICompatAdapter のシム。"""

    def __init__(self, default_model_name: Optional[str] = None):
        super().__init__(
            connection=get_connection("ollama"),
            default_model_name=default_model_name,
        )

    def _build_client(self):
        """テスト互換: module-level _get_client() を通す。"""
        return _get_client()

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        # "ollama/llava:13b" → "llava"
        base = _strip_ollama_prefix(model_name).split(":")[0]
        return any(base.startswith(prefix) for prefix in _VISION_MODELS)

    def _log_tag(self) -> str:
        return "OllamaProvider"
