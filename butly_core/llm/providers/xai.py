"""
xai.py
------
xAI (Grok) LLM プロバイダー。
OpenAI SDK + base_url="https://api.x.ai/v1" で Chat Completions を利用する。

Phase 1: Chat Completions のみ（Vision 対応、embedding は別プロバイダーへフォールバック）
Phase 2 で Agent Tools API（X Search / Web Search / Code Execution）を追加予定。
"""

import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from butly_core.chat.types import Attachment, ChatResponse
from butly_core.llm.base import BaseProvider
from butly_core.llm import _openai_compat as compat

# Vision 非対応モデル: Grok Code Fast はコード特化でテキストのみ
_NON_VISION_PREFIXES = ("grok-code-fast",)

# xAI デフォルト base_url
_XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"


def _strip_xai_prefix(model_name: str) -> str:
    """'xai/' プレフィックスを除去する。"""
    return model_name.removeprefix("xai/")


def _get_client():
    """xAI 用 OpenAI 互換クライアントを生成する。"""
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai パッケージが必要です。`pip install openai` を実行してください。")

    compat.load_env_file()

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "xAI API キーが見つかりません。APIkey.env または UI から XAI_API_KEY を設定してください。"
        )

    base_url = os.environ.get("XAI_BASE_URL", _XAI_DEFAULT_BASE_URL)
    return openai.OpenAI(api_key=api_key, base_url=base_url)


class XaiProvider(BaseProvider):
    """xAI (Grok) プロバイダー。OpenAI 互換 Chat Completions API を使用。"""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = _get_client()
        return self._client

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        base = _strip_xai_prefix(model_name)
        return not any(base.startswith(prefix) for prefix in _NON_VISION_PREFIXES)

    # ==================================================================
    # BaseProvider 必須メソッド
    # ==================================================================

    def summarize(self, conversation_text: str, config: dict) -> str:
        from butly_core.config import SYSTEM_CONFIG
        from butly_core.prompts import PromptLoader

        char_limit = config.get("summary_char_limit", SYSTEM_CONFIG["brain"]["summary_char_limit"])
        loader = PromptLoader()
        _agent_name = config.get("agent_name") or SYSTEM_CONFIG["agent"]["agent_name"]
        prompt = loader.get(
            "brain_summarize_conversation",
            agent_name=_agent_name,
            char_limit=char_limit,
            conversation_text=conversation_text,
        )
        try:
            resp = self.client.chat.completions.create(
                model=_strip_xai_prefix(config.get("model_name", "grok-4-1-fast-non-reasoning")),
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("temperature", 0.3),
            )
            return resp.choices[0].message.content.strip() if resp.choices else "要約なし"
        except Exception as e:
            print(f"[XaiProvider] Summarize Error: {e}")
            return "（要約作成に失敗）"

    def embed(self, text: str) -> Optional[List[float]]:
        """xAI は embedding API を提供していないため None を返す。

        embedding は Gemini / OpenAI 等の別プロバイダーを使うこと。
        """
        print("[XaiProvider] xAI does not provide embedding API. Use another provider for embedding.")
        return None

    def classify(self, prompt: str, config: dict) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=_strip_xai_prefix(config.get("model_name", "grok-4-1-fast-non-reasoning")),
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("generation_config", {}).get("temperature", 0.0),
                max_tokens=config.get("generation_config", {}).get("max_output_tokens", 512),
            )
            return resp.choices[0].message.content if resp.choices else ""
        except Exception as e:
            print(f"[XaiProvider] Classify Error: {e}")
            return ""

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

        # --- system instruction / context prefix ---
        system_instruction = compat.resolve_system_instruction(context)
        context_prefix = compat.resolve_context_prefix(context)

        # --- messages 構築 ---
        position = compat.resolve_position(context)
        user_content = compat.build_user_content(text, attachments)
        messages = compat.build_messages(
            system_instruction=system_instruction,
            context_prefix=context_prefix,
            history=history,
            user_content=user_content,
            position=position,
        )

        # --- API 呼び出し ---
        chat_conf = compat.merge_chat_config(AI_CONFIG["chat"], override_config)
        _gen = chat_conf.get("generation_config", {})
        model_name = _strip_xai_prefix(chat_conf.get("model_name", "grok-4-1-fast-non-reasoning"))

        # debug 用 messages preview
        _debug_messages = compat.build_debug_messages(messages)

        try:
            resp = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=_gen.get("temperature", 0.7),
                max_tokens=_gen.get("max_output_tokens") or None,
            )
            response_text = resp.choices[0].message.content if resp.choices else ""

            result = compat.build_chat_response(response_text, rag_results)
            result.debug_info = {
                "messages": _debug_messages,
                "messages_preview": _debug_messages,
                "raw_response": response_text,
            }
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
        """xAI (OpenAI 互換) を使ったストリーミング。"""
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
        model_name = _strip_xai_prefix(chat_conf.get("model_name", "grok-4-1-fast-non-reasoning"))
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
            log_tag="XaiProvider",
        ):
            yield event
