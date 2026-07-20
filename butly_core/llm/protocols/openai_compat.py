"""
openai_compat.py (protocols)
----------------------------
OpenAI 互換 protocol を喋る Provider 全ての共通実装。

OpenAI / xAI / Ollama / Groq / Together / DeepInfra etc. はこの 1 クラスで
カバーできる (Connection を入れ替えるだけ)。

責務:
  - `Connection` を保持し、SDK クライアントを必要時に生成
  - `_openai_compat.py` のヘルパー群 (messages 構築/chat completion/stream) を委譲
  - role に応じた model_name 解決 (config > default_model_name)
  - embed model 解決 (config > default > env > built-in default)
  - Connection.embeddings_supported=False のときは embed が None を返す
  - Connection.model_name_strip_prefix で API 渡す前の正規化
"""

from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from butly_core.chat.types import Attachment, ChatResponse
from butly_core.llm import _openai_compat as compat
from butly_core.llm.base import BaseProvider
from butly_core.llm.connections import Connection


class OpenAICompatAdapter(BaseProvider):
    """OpenAI 互換 API を喋る Provider の汎用実装。

    Parameters
    ----------
    connection : Connection
        接続情報 (base_url, api_key_env, prefix 除去ルール等を含む)。
    default_model_name : str, optional
        config で model_name が指定されないときのフォールバック。
    """

    def __init__(
        self,
        connection: Connection,
        default_model_name: Optional[str] = None,
    ):
        self.connection = connection
        self.default_model_name = default_model_name
        self._client = None

    # ==================================================================
    # クライアント生成 (subclass で override 可能)
    # ==================================================================

    @property
    def client(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self):
        """OpenAI 互換 SDK クライアントを生成する。

        subclass で override すれば、テストの module-level `_get_client` パッチ
        ポイントなどを温存できる。
        """
        try:
            import openai
        except ImportError:
            raise RuntimeError(
                "openai パッケージが必要です。`pip install openai` を実行してください。"
            )

        compat.load_env_file()

        kwargs: dict[str, Any] = {}

        base_url = self.connection.resolve_base_url()
        if base_url:
            kwargs["base_url"] = base_url

        if self.connection.api_key_env:
            api_key = self.connection.resolve_api_key()
            if not api_key:
                raise RuntimeError(
                    f"API キーが見つかりません。{self.connection.api_key_env} "
                    f"({self.connection.display_label}) を設定してください。"
                )
            kwargs["api_key"] = api_key
        else:
            # auth 不要 connection (Ollama 等) でも SDK は api_key 必須なのでダミーを渡す
            kwargs["api_key"] = "no-auth"

        if self.connection.extra_headers:
            kwargs["default_headers"] = dict(self.connection.extra_headers)

        return openai.OpenAI(**kwargs)

    # ==================================================================
    # Vision (subclass で override 推奨)
    # ==================================================================

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        """既定では vision を許可。各 Provider shim で適切に override する。"""
        return True

    # ==================================================================
    # モデル名解決ヘルパー
    # ==================================================================

    def _strip(self, model_name: str) -> str:
        return self.connection.strip_model_prefix(model_name)

    def _resolve_chat_model(
        self,
        config: Optional[dict],
        fallback: Optional[str] = None,
    ) -> str:
        m = (
            (config or {}).get("model_name")
            or self.default_model_name
            or fallback
            or "gpt-4o-mini"
        )
        return self._strip(m)

    def _to_chat_completion_conf(
        self,
        config: dict,
        *,
        default_temperature: float = 0.7,
    ) -> dict:
        """summarize / classify の flat config を build_chat_completion_kwargs 用の
        chat_conf 形式 (model_name + generation_config) に正規化する。

        summarize は `temperature` を flat に置く流儀。
        classify は `generation_config.temperature` 流儀。両方を吸収する。
        """
        gen = dict(config.get("generation_config") or {})

        # flat な temperature を generation_config 側に持ち込む
        if "temperature" not in gen and "temperature" in config:
            gen["temperature"] = config["temperature"]
        gen.setdefault("temperature", default_temperature)

        # reasoning_effort が flat か generation_config 内かを吸収
        reasoning_effort = config.get("reasoning_effort") or gen.get("reasoning_effort")

        normalized: dict[str, Any] = {
            "model_name": config.get("model_name"),
            "generation_config": gen,
        }
        if reasoning_effort is not None:
            normalized["reasoning_effort"] = reasoning_effort

        return normalized

    def _resolve_embedding_model(self, config: Optional[dict]) -> str:
        """embed model を優先順位に従って解決する。

        config["model_name"] > self.default_model_name > env (legacy)
        > connection.default_embedding_model > "text-embedding-3-small"
        """
        if config and config.get("model_name"):
            return self._strip(config["model_name"])
        if self.default_model_name:
            return self._strip(self.default_model_name)
        if self.connection.embedding_model_env:
            env_val = os.environ.get(self.connection.embedding_model_env)
            if env_val:
                return env_val
        if self.connection.default_embedding_model:
            return self.connection.default_embedding_model
        return "text-embedding-3-small"

    # ==================================================================
    # BaseProvider 必須メソッド
    # ==================================================================

    def summarize(self, conversation_text: str, config: dict) -> str:
        from butly_core.config import SYSTEM_CONFIG
        from butly_core.prompts import PromptLoader

        char_limit = config.get(
            "summary_char_limit",
            SYSTEM_CONFIG["brain"]["summary_char_limit"],
        )
        loader = PromptLoader()
        _agent_name = config.get("agent_name") or SYSTEM_CONFIG["agent"]["agent_name"]
        prompt = loader.get(
            "brain_summarize_conversation",
            agent_name=_agent_name,
            char_limit=char_limit,
            conversation_text=conversation_text,
        )
        try:
            model = self._resolve_chat_model(config)
            messages = [{"role": "user", "content": prompt}]
            # reasoning model (o1/o3/o4) を summary に割り当てても動くよう
            # build_chat_completion_kwargs に通す
            normalized_conf = self._to_chat_completion_conf(
                config,
                default_temperature=0.3,
            )
            kwargs = compat.build_chat_completion_kwargs(
                normalized_conf,
                messages,
                model_name=model,
            )
            kwargs["model"] = model
            resp = self.client.chat.completions.create(**kwargs)
            return (
                resp.choices[0].message.content.strip() if resp.choices else "要約なし"
            )
        except Exception as e:
            print(f"[{self._log_tag()}] Summarize Error: {e}")
            return "（要約作成に失敗）"

    def embed(self, text: str, config: Optional[dict] = None) -> Optional[List[float]]:
        if not self.connection.embeddings_supported:
            print(
                f"[{self._log_tag()}] {self.connection.display_label} does not "
                f"provide embedding API. Use another provider for embedding."
            )
            return None
        try:
            model = self._resolve_embedding_model(config)
            resp = self.client.embeddings.create(model=model, input=text)
            return resp.data[0].embedding
        except Exception as e:
            print(f"[{self._log_tag()}] Embed Error: {e}")
            return None

    def classify(self, prompt: str, config: dict) -> str:
        """分類呼び出し。エラー時は例外を送出する。

        握りつぶして "" を返すと、呼び出し側で provider エラーと
        「本当に空の応答」を区別できない。
        """
        model = self._resolve_chat_model(config)
        messages = [{"role": "user", "content": prompt}]
        # classify でも reasoning model 対応のため build_chat_completion_kwargs 経由
        normalized_conf = self._to_chat_completion_conf(
            config,
            default_temperature=0.0,
        )
        kwargs = compat.build_chat_completion_kwargs(
            normalized_conf,
            messages,
            model_name=model,
        )
        kwargs["model"] = model
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content if resp.choices else ""

    def generate(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> ChatResponse:
        from butly_core.config import AI_CONFIG

        override_config = context.get("override_config")
        history = context.get("history", [])
        rag_results = context.get("rag_results", [])

        system_instruction = compat.resolve_system_instruction(context)
        context_prefix = compat.resolve_context_prefix(context)

        position = compat.resolve_position(context)
        user_content = compat.build_user_content(text, attachments)
        messages = compat.build_messages(
            system_instruction=system_instruction,
            context_prefix=context_prefix,
            history=history,
            user_content=user_content,
            position=position,
        )

        chat_conf = compat.merge_chat_config(AI_CONFIG["chat"], override_config)
        # default_model_name (factory が ChatService の選択をキャプチャしたもの) を
        # AI_CONFIG / instance_config より優先する。
        # request.model_name で per-request 切替された場合の整合性確保。
        model_name = self._strip(
            self.default_model_name or chat_conf.get("model_name") or "gpt-4o"
        )

        _debug_messages = compat.build_debug_messages(messages)

        try:
            kwargs = compat.build_chat_completion_kwargs(
                chat_conf,
                messages,
                model_name=model_name,
            )
            resp = self.client.chat.completions.create(model=model_name, **kwargs)
            response_text = resp.choices[0].message.content if resp.choices else ""

            result = compat.build_chat_response(response_text, rag_results)
            result.debug_info = {
                "messages": _debug_messages,
                "messages_preview": _debug_messages,
                # 全文（テキストのみ）: prompt_full 表示とトークン概算に使う
                "messages_full": compat.build_full_messages(messages),
                "raw_response": response_text,
            }
            # API 実測のトークン数（llama.cpp 含む OpenAI 互換が返す）
            token_usage = compat.extract_token_usage(resp)
            if token_usage:
                result.debug_info["token_usage"] = token_usage
            return result
        except Exception as e:
            import traceback

            traceback.print_exc()
            return ChatResponse(text=f"Error: {e}")

    # ==================================================================
    # ストリーミング
    # ==================================================================

    async def async_generate_stream(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        from butly_core.config import AI_CONFIG

        override_config = context.get("override_config")
        history = context.get("history", [])

        system_instruction = compat.resolve_system_instruction(context)
        context_prefix = compat.resolve_context_prefix(context)
        position = compat.resolve_position(context)
        user_content = compat.build_user_content(text, attachments)
        messages = compat.build_messages(
            system_instruction=system_instruction,
            context_prefix=context_prefix,
            history=history,
            user_content=user_content,
            position=position,
        )

        chat_conf = compat.merge_chat_config(AI_CONFIG["chat"], override_config)
        # default_model_name 優先 (generate と同じ理由)
        model_name = self._strip(
            self.default_model_name or chat_conf.get("model_name") or "gpt-4o"
        )
        _debug_messages = compat.build_debug_messages(messages)

        debug_data = {
            "messages": _debug_messages,
            "messages_preview": _debug_messages,
            "messages_full": messages,
        }

        async for event in compat.async_chat_completion_stream(
            client=self.client,
            model=model_name,
            messages=messages,
            chat_conf=chat_conf,
            debug_data=debug_data,
            log_tag=self._log_tag(),
        ):
            yield event

    # ==================================================================
    # 内部ユーティリティ
    # ==================================================================

    def _log_tag(self) -> str:
        return f"OpenAICompat/{self.connection.id}"
