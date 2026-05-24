"""
factory.py
----------
プロバイダーファクトリ。

入力形式:
  - str: 旧 API。例 "gpt-4o", "gemini-3.5-flash", "ollama/llama3.2"
         model_name の prefix から Connection を推定する。
  - dict: 新 API。例 {"connection": "openai", "model_name": "gpt-4o"}
         旧形式 {"model_name": "gpt-4o"} も連動。
  - ModelRef: 新 API (型安全)。

内部処理:
  1. ``normalize_model_ref`` で ``ModelRef`` に統一
  2. ``connection_id`` から ``Connection`` を引く
  3. ``Connection.protocol`` ごとに対応 Adapter をインスタンス化

旧 API 経路 (str) は Phase 2 以降も維持する (Brain 等が多用)。
"""

from butly_core.llm.base import BaseProvider
from butly_core.llm.connections import get_connection, try_get_connection
from butly_core.llm.model_registry import ModelRef, normalize_model_ref


class ProviderFactory:
    """ModelRef / dict / str から Provider を生成するファクトリ。"""

    @staticmethod
    def create(model) -> BaseProvider:
        """Provider を生成する。

        Parameters
        ----------
        model : str | dict | ModelRef
            - str: 旧 API。"gpt-4o" など。
            - dict: 新 API。{"connection": "openai", "model_name": "gpt-4o"} or
                   旧形式 {"model_name": "gpt-4o"} (connection は推定)。
            - ModelRef: 型安全な参照。

        Raises
        ------
        ValueError
            model が空 / connection 推定不能の場合。
        NotImplementedError
            未対応 protocol が指定された場合。
        KeyError
            connection_id が registry に未登録の場合。
        """
        try:
            ref = normalize_model_ref(model)
        except ValueError as e:
            # 互換のため、旧来は NotImplementedError を投げていた
            raise NotImplementedError(
                f"未対応のモデルです: {model}。詳細: {e}。"
                f"対応 connection: google (gemini-*), openai (gpt-*/o1/o3/o4/text-embedding-*), "
                f"xai (grok-*/xai/*), ollama (ollama/*)。"
                f"その他の OpenAI 互換 provider は LLM_CONNECTIONS で connection を定義してください。"
            ) from e

        connection = get_connection(ref.connection_id)

        if connection.protocol == "openai_compat":
            # 既存テスト互換のため、built-in 4 種は providers/*.py のシムを通す
            # (module-level _get_client パッチポイントを温存)。
            if connection.id == "openai":
                from butly_core.llm.providers.openai import OpenAIProvider

                return OpenAIProvider(default_model_name=ref.model_name)
            if connection.id == "xai":
                from butly_core.llm.providers.xai import XaiProvider

                return XaiProvider(default_model_name=ref.model_name)
            if connection.id == "ollama":
                from butly_core.llm.providers.ollama import OllamaProvider

                return OllamaProvider(default_model_name=ref.model_name)
            # user 定義 connection (Groq 等) はそのまま adapter を使う
            from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter

            return OpenAICompatAdapter(
                connection=connection,
                default_model_name=ref.model_name,
            )

        if connection.protocol == "gemini_native":
            from butly_core.llm.providers.gemini import GeminiProvider

            return GeminiProvider(default_model_name=ref.model_name)

        raise NotImplementedError(
            f"Unsupported protocol: {connection.protocol!r} for connection {connection.id!r}"
        )
