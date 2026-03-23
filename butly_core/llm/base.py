"""
base.py
-------
LLM プロバイダーの抽象基底クラス。
全プロバイダー（Gemini, OpenAI, Ollama 等）はこのインターフェースを実装する。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from butly_core.chat.types import Attachment, ChatResponse


class BaseProvider(ABC):
    """LLM プロバイダーの抽象基底クラス"""

    @abstractmethod
    def generate(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> ChatResponse:
        """
        テキスト（+ 添付）を受け取り、LLM から応答を生成する。
        同期メソッド。呼び出し元が非同期コンテキストの場合は
        run_in_threadpool() 等でラップすること。

        将来の非同期版は async_generate() として追加予定。

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

    @abstractmethod
    def summarize(self, conversation_text: str, config: dict) -> str:
        """会話ログの要約生成。同期メソッド。"""
        ...

    @abstractmethod
    def embed(self, text: str) -> Optional[List[float]]:
        """ベクトル埋め込み生成。RAG 検索のインデックス化に使用。"""
        ...

    @abstractmethod
    def classify(self, prompt: str, config: dict) -> str:
        """Gatekeeper の tier 判定用。軽量モデルを使う想定。同期メソッド。"""
        ...

    # --- 非同期メソッド（将来用・デフォルト実装は同期版のラップ） ---

    async def async_generate(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> ChatResponse:
        """非同期版 generate。デフォルトは同期版をスレッドプールで実行。"""
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(self.generate, text, attachments, context)

    async def async_summarize(self, conversation_text: str, config: dict) -> str:
        """非同期版 summarize。デフォルトは同期版をスレッドプールで実行。"""
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(self.summarize, conversation_text, config)

    async def async_embed(self, text: str) -> Optional[List[float]]:
        """非同期版 embed。デフォルトは同期版をスレッドプールで実行。"""
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(self.embed, text)
