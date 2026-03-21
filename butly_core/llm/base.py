"""
base.py
-------
LLM プロバイダーの抽象基底クラス。
全プロバイダー（Gemini, OpenAI, Ollama 等）はこのインターフェースを実装する。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from butly_core.chat.types import Attachment, ChatResponse


class BaseProvider(ABC):
    """LLM プロバイダーの抽象基底クラス"""

    @abstractmethod
    async def generate(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> ChatResponse:
        """
        テキスト（+ 添付）を受け取り、LLM から応答を生成する。

        Parameters
        ----------
        text : str
            ユーザーのテキスト入力（RAG コンテキスト付きの完全なプロンプト）。
        attachments : List[Attachment]
            添付ファイル（画像等）のリスト。
        context : Dict[str, Any]
            brain / memory / history / config 等のコンテキスト情報。
            キー例:
              - "brain": ButlyBrain インスタンス
              - "memory_manager": ButlyMemory インスタンス
              - "history": 会話履歴
              - "cached_content": キャッシュ
              - "override_config": インスタンス設定
              - "memory_blocks": Gatekeeper 記憶ブロック
              - "use_google_search": bool

        Returns
        -------
        ChatResponse
            LLM の応答。
        """
        ...

    @staticmethod
    @abstractmethod
    def supports_vision(model_name: str) -> bool:
        """
        指定モデルが画像入力（vision）に対応しているかを返す。

        Parameters
        ----------
        model_name : str
            モデル名（例: "gemini-3-flash-preview"）。

        Returns
        -------
        bool
        """
        ...
