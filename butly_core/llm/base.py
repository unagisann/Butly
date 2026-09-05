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

    # ==================================================================
    # トークン実測値 (API usage) の受け渡し
    # ==================================================================
    # 各 provider は API 呼び出し直後に _set_last_token_usage() を呼ぶ。
    # 呼び出し側 (record_llm_call する層) は呼び出し直後に
    # pop_last_token_usage() で取り出して trace / 集計に記録する。
    # 「直後に pop」が前提の 1 スロット方式 (別スレッドの同一 provider
    # 共有は想定しない — ProviderFactory は呼び出しごとに生成する)。

    def _set_last_token_usage(self, usage: Optional[dict]) -> None:
        self._last_token_usage = usage or None

    def pop_last_token_usage(self) -> Optional[dict]:
        usage = getattr(self, "_last_token_usage", None)
        self._last_token_usage = None
        return usage

    # ==================================================================
    # completion metadata (finish reason 等) の受け渡し
    # ==================================================================
    # classify() の戻り値 str 互換を保ったまま、truncation 終了
    # (max_tokens / length) を呼び出し側へ伝えるための 1 スロット方式。
    # token usage と同じく「call 直後に pop」が前提。
    # 提供しない provider / 経路では None のままでよい。

    def _set_last_completion_metadata(self, metadata: Optional[dict]) -> None:
        self._last_completion_metadata = metadata or None

    def pop_last_completion_metadata(self) -> Optional[dict]:
        metadata = getattr(self, "_last_completion_metadata", None)
        self._last_completion_metadata = None
        return metadata

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

        失敗時は **例外を送出する**（None を返さない）。None を返すと呼び出し側で
        レート制限と「正常だがベクトル無し」を区別できず、リトライが効かないまま
        欠損が保存される。`butly_core.llm.errors` の EmbeddingError 系か、
        SDK の例外がそのまま上がる。

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
        """Gatekeeper の tier 判定用。軽量モデルを使う想定。同期メソッド。

        エラー時は例外を送出する（呼び出し側が fallback を判断する）。
        """
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

    async def async_embed(
        self, text: str, config: Optional[dict] = None
    ) -> Optional[List[float]]:
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
