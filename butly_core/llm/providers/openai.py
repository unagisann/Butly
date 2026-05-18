"""
openai.py
---------
OpenAI (GPT) LLM プロバイダー。
OpenAI API 互換（Azure OpenAI 含む）に対応。

v3.1: _openai_compat ヘルパーを利用してリファクタ。
      reasoning_effort (o1/o3/o4 系) 対応を追加。
"""

import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from butly_core.chat.types import Attachment, ChatResponse
from butly_core.llm.base import BaseProvider
from butly_core.llm import _openai_compat as compat

# Vision 対応が確認されているモデルプレフィックス
_VISION_MODELS = {"gpt-4o", "gpt-4-turbo", "gpt-4-vision", "o1", "o3", "o4"}


def _get_client():
    """OpenAI クライアントを遅延生成する。"""
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai パッケージが必要です。`pip install openai` を実行してください。")

    compat.load_env_file()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI API キーが見つかりません。APIkey.env に OPENAI_API_KEY を設定してください。")

    base_url = os.environ.get("OPENAI_BASE_URL")
    return openai.OpenAI(api_key=api_key, base_url=base_url) if base_url else openai.OpenAI(api_key=api_key)


class OpenAIProvider(BaseProvider):
    """OpenAI API (GPT-4o 等) を使った LLM プロバイダー。"""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = _get_client()
        return self._client

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        return any(model_name.startswith(prefix) for prefix in _VISION_MODELS)

    # ==================================================================
    # BaseProvider 必須メソッド
    # ==================================================================

    def summarize(self, conversation_text: str, config: dict) -> str:
        from butly_core.config import SYSTEM_CONFIG
        from butly_core.prompts import PromptLoader

        char_limit = config.get("summary_char_limit", SYSTEM_CONFIG["brain"]["summary_char_limit"])
        loader = PromptLoader()
        # OpenAI 固有: config.agent_name を優先 → SYSTEM_CONFIG フォールバック
        _agent_name = config.get("agent_name") or SYSTEM_CONFIG["agent"]["agent_name"]
        prompt = loader.get(
            "brain_summarize_conversation",
            agent_name=_agent_name,
            char_limit=char_limit,
            conversation_text=conversation_text,
        )
        try:
            resp = self.client.chat.completions.create(
                model=config.get("model_name", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("temperature", 0.3),
            )
            return resp.choices[0].message.content.strip() if resp.choices else "要約なし"
        except Exception as e:
            print(f"[OpenAIProvider] Summarize Error: {e}")
            return "（要約作成に失敗）"

    def embed(self, text: str) -> Optional[List[float]]:
        try:
            model = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            resp = self.client.embeddings.create(model=model, input=text)
            return resp.data[0].embedding
        except Exception as e:
            print(f"[OpenAIProvider] Embed Error: {e}")
            return None

    def classify(self, prompt: str, config: dict) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=config.get("model_name", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("generation_config", {}).get("temperature", 0.0),
                max_tokens=config.get("generation_config", {}).get("max_output_tokens", 512),
            )
            return resp.choices[0].message.content if resp.choices else ""
        except Exception as e:
            print(f"[OpenAIProvider] Classify Error: {e}")
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
        model_name = chat_conf.get("model_name", "gpt-4o")

        # debug 用 messages preview
        _debug_messages = compat.build_debug_messages(messages)

        try:
            kwargs = compat.build_chat_completion_kwargs(chat_conf, messages, model_name=model_name)
            resp = self.client.chat.completions.create(model=model_name, **kwargs)
            response_text = resp.choices[0].message.content if resp.choices else ""

            result = compat.build_chat_response(response_text, rag_results)
            # v3.1: messages_full → messages_preview に改名 (実態に合わせた命名)
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
        """OpenAI 互換 SDK の stream=True を使ったネイティブストリーミング。"""
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
        model_name = chat_conf.get("model_name", "gpt-4o")
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
            log_tag="OpenAIProvider",
        ):
            yield event
