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

import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from butly_core.chat.types import Attachment, ChatResponse
from butly_core.llm import _openai_compat as compat
from butly_core.llm.base import BaseProvider
from butly_core.llm.canonical import CanonicalGenerationRequest, request_from_config
from butly_core.llm.capabilities import get_capability_resolver
from butly_core.llm.connections import Connection
from butly_core.llm.errors import EmbeddingError, EmbeddingNotSupported
from butly_core.llm.model_registry import ModelRef
from butly_core.llm.protocols.openai_chat import (
    OpenAIChatCompletionsRequestAdapter,
    canonical_messages_from_openai,
)

logger = logging.getLogger(__name__)


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
        self._capability_resolver = get_capability_resolver()
        self._request_adapter = OpenAIChatCompletionsRequestAdapter()

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

    def _canonical_request(
        self,
        *,
        model_name: str,
        messages: list[dict[str, Any]],
        config: Optional[dict[str, Any]],
        default_temperature: Any,
        default_max_output_tokens: Any = None,
        stream: bool = False,
        purpose: str = "chat",
        reasoning_effort_policy: str = "provider_default",
    ) -> CanonicalGenerationRequest:
        kwargs: dict[str, Any] = {}
        if default_max_output_tokens is not None:
            kwargs["default_max_output_tokens"] = default_max_output_tokens
        return request_from_config(
            model=ModelRef(self.connection.id, model_name),
            messages=canonical_messages_from_openai(messages),
            config=config,
            default_temperature=default_temperature,
            stream=stream,
            purpose=purpose,
            reasoning_effort_policy=reasoning_effort_policy,
            **kwargs,
        )

    def _complete_canonical(self, request: CanonicalGenerationRequest):
        capabilities = self._capability_resolver.resolve(
            self.connection,
            request.model,
        )
        kwargs = self._request_adapter.build_kwargs(request, capabilities)
        kwargs["model"] = request.model.model_name
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception as error:
            correction = self._request_adapter.correction_for_error(
                error,
                request,
            )
            if correction is None:
                raise

        corrected_capabilities = capabilities.overlay(correction)
        corrected_kwargs = self._request_adapter.build_kwargs(
            request,
            corrected_capabilities,
        )
        corrected_kwargs["model"] = request.model.model_name
        logger.info(
            "retrying one request after unsupported-parameter correction: "
            "connection=%s model=%s",
            self.connection.id,
            request.model.model_name,
        )
        response = self.client.chat.completions.create(**corrected_kwargs)
        try:
            self._capability_resolver.record_observed(request.model, correction)
        except Exception:
            # Provider応答は成功しているため、ローカルcache障害で失敗へ
            # 巻き戻さない。次回に同じ安全な補正が再実行されるだけでよい。
            logger.warning(
                "provider capability observation could not be saved",
                exc_info=True,
            )
        return response

    # ==================================================================
    # BaseProvider 必須メソッド
    # ==================================================================

    def summarize(self, conversation_text: str, config: dict) -> str:
        from butly_core.config import SYSTEM_CONFIG
        from butly_core.prompts import PromptLoader, resolve_prompt_locale

        char_limit = config.get(
            "summary_char_limit",
            SYSTEM_CONFIG["brain"]["summary_char_limit"],
        )
        locale = config.get("locale") or resolve_prompt_locale()
        loader = PromptLoader(
            locale=locale,
            allow_user_overrides=config.get(
                "allow_user_prompt_overrides",
                True,
            ),
        )
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
            # Canonical RequestからCapabilityに応じたProvider payloadへ変換する。
            request = self._canonical_request(
                model_name=model,
                messages=messages,
                config=config,
                default_temperature=0.3,
                purpose="summary",
            )
            resp = self._complete_canonical(request)
            self._set_last_token_usage(compat.extract_token_usage(resp))
            if resp.choices:
                return resp.choices[0].message.content.strip()
            return "No summary" if locale != "ja" else "要約なし"
        except Exception as e:
            print(f"[{self._log_tag()}] Summarize Error: {e}")
            if locale != "ja":
                return "(Failed to create summary)"
            return "（要約作成に失敗）"

    def embed(self, text: str, config: Optional[dict] = None) -> Optional[List[float]]:
        if not self.connection.embeddings_supported:
            raise EmbeddingNotSupported(
                f"[{self._log_tag()}] {self.connection.display_label} does not "
                f"provide embedding API. Use another provider for embedding."
            )
        # 例外は握り潰さずそのまま送出する。レート制限を None に変えると
        # 呼び出し側のリトライに届かず、ベクトル無しで保存されてしまう。
        model = self._resolve_embedding_model(config)
        resp = self.client.embeddings.create(model=model, input=text)
        self._set_last_token_usage(compat.extract_token_usage(resp))
        if not resp.data:
            raise EmbeddingError(
                f"[{self._log_tag()}] embeddings.create returned no data "
                f"(model={model})"
            )
        return resp.data[0].embedding

    def classify(self, prompt: str, config: dict) -> str:
        """分類呼び出し。エラー時は例外を送出する。

        握りつぶして "" を返すと、呼び出し側で provider エラーと
        「本当に空の応答」を区別できない。
        """
        model = self._resolve_chat_model(config)
        messages = [{"role": "user", "content": prompt}]
        # classifyもchat/summaryと同じCanonical経路を使う。
        request = self._canonical_request(
            model_name=model,
            messages=messages,
            config=config,
            default_temperature=0.0,
            default_max_output_tokens=512,
            purpose=str(config.get("_purpose") or "classify"),
            reasoning_effort_policy=str(
                config.get("_reasoning_effort_policy") or "provider_default"
            ),
        )
        resp = self._complete_canonical(request)
        self._set_last_token_usage(compat.extract_token_usage(resp))
        finish_reason = (
            getattr(resp.choices[0], "finish_reason", None) if resp.choices else None
        )
        self._set_last_completion_metadata(
            {"finish_reason": finish_reason} if isinstance(finish_reason, str) else None
        )
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
            request = self._canonical_request(
                model_name=model_name,
                messages=messages,
                config=chat_conf,
                default_temperature=0.7,
                purpose="chat",
            )
            resp = self._complete_canonical(request)
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
            self._set_last_token_usage(token_usage)
            if token_usage:
                result.debug_info["token_usage"] = token_usage
            return result
        except Exception as e:
            raise RuntimeError("Provider generation failed") from e

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

        request = self._canonical_request(
            model_name=model_name,
            messages=messages,
            config=chat_conf,
            default_temperature=0.7,
            stream=True,
            purpose="chat",
        )
        capabilities = self._capability_resolver.resolve(
            self.connection,
            request.model,
        )
        request_kwargs = self._request_adapter.build_kwargs(
            request,
            capabilities,
        )
        request_kwargs["model"] = model_name

        pending_correction = None

        def _correct_parameter_error(error, _current_kwargs):
            nonlocal pending_correction
            correction = self._request_adapter.correction_for_error(
                error,
                request,
            )
            if correction is None:
                return None
            pending_correction = correction
            corrected = self._request_adapter.build_kwargs(
                request,
                capabilities.overlay(correction),
            )
            corrected["model"] = model_name
            return corrected

        def _record_correction() -> None:
            if pending_correction is not None:
                self._capability_resolver.record_observed(
                    request.model,
                    pending_correction,
                )

        async for event in compat.async_chat_completion_stream(
            client=self.client,
            model=model_name,
            messages=messages,
            chat_conf=chat_conf,
            debug_data=debug_data,
            log_tag=self._log_tag(),
            request_kwargs=request_kwargs,
            parameter_error_handler=_correct_parameter_error,
            on_success=_record_correction,
        ):
            yield event

    # ==================================================================
    # 内部ユーティリティ
    # ==================================================================

    def _log_tag(self) -> str:
        return f"OpenAICompat/{self.connection.id}"
