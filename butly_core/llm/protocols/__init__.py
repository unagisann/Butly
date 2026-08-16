"""
butly_core.llm.protocols
------------------------
Protocol Adapters: Connection 情報を受け取り、API protocol を抽象化した
具象 Provider クラス群。

- OpenAICompatAdapter: OpenAI 互換 API (OpenAI / xAI / Ollama / Groq 等)
- GeminiNativeAdapter: Google Gemini native SDK
"""

from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter
from butly_core.llm.protocols.gemini_native import (
    GeminiNativeAdapter,
    GeminiNativeRequestAdapter,
)
from butly_core.llm.protocols.openai_chat import (
    OpenAIChatCompletionsRequestAdapter,
)

__all__ = [
    "GeminiNativeAdapter",
    "GeminiNativeRequestAdapter",
    "OpenAIChatCompletionsRequestAdapter",
    "OpenAICompatAdapter",
]
