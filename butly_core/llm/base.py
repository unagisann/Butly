"""
base.py
-------
LLM プロバイダーの抽象基底クラス。
全プロバイダー（Gemini, OpenAI, Ollama 等）はこのインターフェースを実装する。
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional

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
    def embed(self, text: str, config: Optional[dict] = None) -> Optional[List[float]]:
        """ベクトル埋め込み生成。RAG 検索のインデックス化に使用。

        Parameters
        ----------
        text : str
        config : dict | None
            Optional. `{"model_name": "text-embedding-3-large", ...}` のような
            role config 辞書。指定があればその model_name で embed する。
            未指定なら Provider/Connection の default を使う。
        """
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

    async def async_embed(self, text: str, config: Optional[dict] = None) -> Optional[List[float]]:
        """非同期版 embed。デフォルトは同期版をスレッドプールで実行。"""
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(self.embed, text, config)

    async def async_generate_stream(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        ストリーミング応答生成。チャンクを逐次 yield する。

        各 yield は dict で、type に応じてペイロード形式が変わる:
          - {"type": "chunk", "text": str}              : 部分テキスト
          - {"type": "done",  "full_text": str,
             "sources": list, "debug": dict}            : 終端 + 最終メタデータ
          - {"type": "error", "message": str}           : 終端エラー

        デフォルト実装は非ストリーミングの async_generate() の結果を
        1 チャンクで yield する fallback。Provider が override 推奨。
        """
        result = await self.async_generate(text, attachments, context)
        if result.text:
            yield {"type": "chunk", "text": result.text}
        yield {
            "type": "done",
            "full_text": result.text or "",
            "sources": result.sources or [],
            "debug": result.debug_info or {},
        }
