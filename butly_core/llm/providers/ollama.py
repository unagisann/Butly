"""
ollama.py
---------
Ollama LLM プロバイダー。
ローカル Ollama サーバー（OpenAI 互換 API）経由でモデルを利用する。

v3.1: _openai_compat ヘルパーを利用してリファクタ。
"""

import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from butly_core.chat.types import Attachment, ChatResponse
from butly_core.llm.base import BaseProvider
from butly_core.llm import _openai_compat as compat

# Vision 対応が確認されているモデルプレフィックス
_VISION_MODELS = {"llava", "bakllava", "moondream", "llama3.2-vision", "gemma3"}


def _strip_ollama_prefix(model_name: str) -> str:
    """'ollama/' プレフィックスを除去して Ollama API に渡せる形式にする。"""
    return model_name.removeprefix("ollama/")


def _get_client():
    """Ollama 用 OpenAI 互換クライアントを生成する。"""
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai パッケージが必要です。`pip install openai` を実行してください。")

    compat.load_env_file()

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    return openai.OpenAI(api_key="ollama", base_url=base_url)


class OllamaProvider(BaseProvider):
    """Ollama (ローカル LLM) プロバイダー。OpenAI 互換 API を使用。"""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = _get_client()
        return self._client

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        base = _strip_ollama_prefix(model_name).split(":")[0]  # "ollama/llava:13b" → "llava"
        return any(base.startswith(prefix) for prefix in _VISION_MODELS)

    # ==================================================================
    # BaseProvider 必須メソッド
    # ==================================================================

    def summarize(self, conversation_text: str, config: dict) -> str:
        from butly_core.config import SYSTEM_CONFIG
        from butly_core.prompts import PromptLoader

        char_limit = config.get("summary_char_limit", SYSTEM_CONFIG["brain"]["summary_char_limit"])
        loader = PromptLoader()
        # Ollama: SYSTEM_CONFIG のみ参照 (OpenAI と異なり config.agent_name フォールバックなし)
        prompt = loader.get(
            "brain_summarize_conversation",
            agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
            char_limit=char_limit,
            conversation_text=conversation_text,
        )
        try:
            resp = self.client.chat.completions.create(
                model=_strip_ollama_prefix(config.get("model_name", "llama3.2")),
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("temperature", 0.3),
                max_tokens=config.get("generation_config", {}).get("max_output_tokens") or None,
            )
            return resp.choices[0].message.content.strip() if resp.choices else "要約なし"
        except Exception as e:
            print(f"[OllamaProvider] Summarize Error: {e}")
            return "（要約作成に失敗）"

    def embed(self, text: str) -> Optional[List[float]]:
        try:
            model = os.environ.get("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
            resp = self.client.embeddings.create(model=model, input=text)
            return resp.data[0].embedding
        except Exception as e:
            print(f"[OllamaProvider] Embed Error: {e}")
            return None

    def classify(self, prompt: str, config: dict) -> str:
        _gen = config.get("generation_config", {})
        try:
            resp = self.client.chat.completions.create(
                model=_strip_ollama_prefix(config.get("model_name", "llama3.2")),
                messages=[{"role": "user", "content": prompt}],
                temperature=_gen.get("temperature", 0.0),
                max_tokens=_gen.get("max_output_tokens") or None,
            )
            return resp.choices[0].message.content if resp.choices else ""
        except Exception as e:
            print(f"[OllamaProvider] Classify Error: {e}")
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
        model_name = _strip_ollama_prefix(chat_conf.get("model_name", "llama3.2"))

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
        """Ollama (OpenAI 互換) を使ったストリーミング。"""
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
        model_name = _strip_ollama_prefix(chat_conf.get("model_name", "llama3.2"))
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
            log_tag="OllamaProvider",
        ):
            yield event
