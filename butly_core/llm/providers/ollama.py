"""
ollama.py
---------
Ollama LLM プロバイダー。
ローカル Ollama サーバー（OpenAI 互換 API）経由でモデルを利用する。
"""

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from butly_core.chat.types import Attachment, ChatResponse
from butly_core.llm.base import BaseProvider

# Vision 対応が確認されているモデルプレフィックス
_VISION_MODELS = {"llava", "bakllava", "moondream", "llama3.2-vision", "gemma3"}


def _get_client():
    """Ollama 用 OpenAI 互換クライアントを生成する。"""
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai パッケージが必要です。`pip install openai` を実行してください。")

    from pathlib import Path
    for env_name in ("APIkey.env", ".env"):
        env_path = Path(__file__).resolve().parents[3] / env_name
        if env_path.exists():
            load_dotenv(env_path)
            break

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
        base = model_name.split(":")[0]  # "llava:13b" → "llava"
        return any(base.startswith(prefix) for prefix in _VISION_MODELS)

    # ==================================================================
    # BaseProvider 必須メソッド
    # ==================================================================

    def summarize(self, conversation_text: str, config: dict) -> str:
        from butly_core.config import SYSTEM_CONFIG
        from butly_core import prompts

        char_limit = config.get("summary_char_limit", SYSTEM_CONFIG["brain"]["summary_char_limit"])
        prompt = prompts.BRAIN_SUMMARIZE_CONVERSATION_PROMPT.format(
            agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
            char_limit=char_limit,
            conversation_text=conversation_text,
        )
        try:
            resp = self.client.chat.completions.create(
                model=config.get("model_name", "llama3.2"),
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("temperature", 0.3),
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
        try:
            resp = self.client.chat.completions.create(
                model=config.get("model_name", "llama3.2"),
                messages=[{"role": "user", "content": prompt}],
                temperature=config.get("generation_config", {}).get("temperature", 0.0),
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

        memory_manager = context.get("memory_manager")
        history = context.get("history", [])
        override_config = context.get("override_config")
        memory_blocks = context.get("memory_blocks")
        rag_results = context.get("rag_results", [])
        use_rag = context.get("use_rag", True)

        # --- system instruction ---
        system_instruction = self._build_system_instruction(
            memory_manager=memory_manager,
            memory_blocks=memory_blocks,
        )

        # --- RAG context ---
        rag_context = ""
        if use_rag and rag_results:
            rag_context = "\n\n[過去の記憶]\n"
            for k in rag_results:
                rag_context += f"・{k['title']}: {k['summary']}\n"
                if k.get("episode"):
                    rag_context += f"  (補足: {k['episode']})\n"
            rag_context += "\n"
        elif use_rag and memory_blocks and memory_blocks.get("rag_context"):
            rag_context = memory_blocks["rag_context"]

        full_prompt = f"{rag_context}\nユーザー: {text}" if rag_context else text

        # --- messages ---
        messages = [{"role": "system", "content": system_instruction}]

        for h in history:
            role = h.get("role", "user")
            parts = h.get("parts", [])
            content = parts[0] if parts else ""
            messages.append({"role": role, "content": str(content)})

        # Ollama vision: base64 画像
        user_content = self._build_user_content(full_prompt, attachments)
        messages.append({"role": "user", "content": user_content})

        # --- API 呼び出し ---
        chat_conf = AI_CONFIG["chat"]
        if override_config and "chat" in override_config:
            chat_conf = {**chat_conf, **override_config["chat"]}

        try:
            resp = self.client.chat.completions.create(
                model=chat_conf.get("model_name", "llama3.2"),
                messages=messages,
                temperature=chat_conf.get("generation_config", {}).get("temperature", 0.7),
            )
            response_text = resp.choices[0].message.content if resp.choices else ""
            return ChatResponse(
                text=response_text or "",
                refs=[dict(k) for k in rag_results] if rag_results else [],
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ChatResponse(text=f"Error: {e}")

    # ==================================================================
    # 内部ユーティリティ
    # ==================================================================

    @staticmethod
    def _build_system_instruction(memory_manager, memory_blocks):
        if memory_blocks is not None:
            from butly_core.core.gatekeeper import build_system_instruction_from_blocks
            return build_system_instruction_from_blocks(
                blocks=memory_blocks,
                memory_manager=memory_manager,
                use_google_search=False,
            )

        from butly_core.config import SYSTEM_CONFIG
        sections = []
        agent_name = SYSTEM_CONFIG["agent"]["agent_name"]
        sys_inst = memory_manager.get_system_instruction() if memory_manager else f"あなたは{agent_name}です。"
        sections.append(f"=== SYSTEM INSTRUCTION ===\n{sys_inst}")

        if memory_manager:
            key_mem = memory_manager.get_key_memory()
            if key_mem:
                sections.append(f"=== KEY MEMORY (根幹記憶) ===\n{key_mem}")
            mid_term = memory_manager.get_mid_term_text_content()
            if mid_term:
                sections.append(f"=== MID-TERM MEMORY (中期記憶) ===\n{mid_term}")
            floating = memory_manager.get_floating_summary()
            if floating:
                sections.append(f"=== FLOATING SUMMARY (未整理記憶) ===\n{floating.strip()}")

        from butly_core.core.chronos import ButlyChronos
        current_time = ButlyChronos().get_system_note()
        sections.append(
            f"=== CURRENT TIME (現在時刻) ===\n{current_time}\n"
            "※上記の時刻情報は文脈理解のための参考情報です。随時日付や時間を読み上げる必要はありません。あくまで自然な対話を最優先してください。"
        )
        return "\n\n".join(sections)

    @staticmethod
    def _build_user_content(text: str, attachments: List[Attachment]):
        """テキスト + 画像を OpenAI 互換形式に変換する。"""
        if not attachments:
            return text

        content = [{"type": "text", "text": text}]
        for att in attachments:
            data_url = f"data:{att.mime_type};base64,{att.data_base64}"
            content.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
        return content
