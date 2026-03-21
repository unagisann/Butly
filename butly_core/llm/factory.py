"""
factory.py
----------
プロバイダーファクトリ。
モデル名に応じて適切な LLM プロバイダーを生成する。
"""

from butly_core.llm.base import BaseProvider


class ProviderFactory:
    """モデル名からプロバイダーインスタンスを生成するファクトリ"""

    @staticmethod
    def create(model_name: str) -> BaseProvider:
        """
        モデル名に基づいて適切なプロバイダーを返す。

        Parameters
        ----------
        model_name : str
            例: "gemini-3-flash-preview", "gpt-4o", etc.

        Returns
        -------
        BaseProvider
            対応するプロバイダーインスタンス。

        Raises
        ------
        NotImplementedError
            未対応のプロバイダーが指定された場合。
        """
        if model_name.startswith("gemini"):
            from butly_core.llm.providers.gemini import GeminiProvider
            return GeminiProvider()

        # 将来拡張: OpenAI, Ollama 等
        raise NotImplementedError(
            f"未対応のモデルです: {model_name}。"
            f"現在対応しているプロバイダー: Gemini（gemini-* モデル）"
        )
