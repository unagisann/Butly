"""
gemini.py
---------
Gemini LLM プロバイダー。
brain.py から移管された Gemini 固有ロジック（チャット / 検索リトライ / 要約 / 埋め込み等）を一元管理する。
attachments → Gemini parts への変換もここで完結。
"""

import asyncio
import base64
import os
import re
import tempfile
from typing import Any, AsyncGenerator, Dict, List, Optional

from google import genai
from google.genai import types
from dotenv import load_dotenv

from butly_core.chat.types import (
    Attachment,
    ChatResponse,
    INLINE_SIZE_PER_FILE,
    INLINE_SIZE_TOTAL,
)
from butly_core.llm.base import BaseProvider


# 明示的な非対応リスト（vision 非対応が判明したモデルのみ追加）
VISION_UNSUPPORTED_MODELS = {
    "gemini-3.1-flash-lite",          # テキスト特化軽量モデル（stable）
    "gemini-3.1-flash-lite-preview",  # テキスト特化軽量モデル（旧 preview 名・後方互換）
}


def _get_client() -> genai.Client:
    """Gemini API クライアントを取得する（API キー自動解決）。"""
    from pathlib import Path
    env_files = ["APIkey.env", ".env"]
    for env_name in env_files:
        env_path = Path(__file__).resolve().parents[3] / env_name
        if env_path.exists():
            load_dotenv(env_path)
            break
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Gemini API キーが見つかりません。APIkey.env に GEMINI_API_KEY を設定してください。")
    return genai.Client(api_key=api_key)


class GeminiProvider(BaseProvider):
    """
    Gemini API を使った LLM プロバイダー。

    画像入力は 2 経路:
      - 小さい画像（1枚2MB以下 & 合計8MB以下）: inline data
      - 大きい画像: Files API 経由
    """

    def __init__(self):
        self._client = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = _get_client()
        return self._client

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        return model_name not in VISION_UNSUPPORTED_MODELS

    # ==================================================================
    # BaseProvider 必須メソッド
    # ==================================================================

    def summarize(self, conversation_text: str, config: dict) -> str:
        """会話ログの要約を生成する。"""
        from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
        from butly_core.prompts import PromptLoader

        # config から model_name / temperature を取得（brain.summarize_conversation がマージ済み）
        model_name = config.get("model_name", AI_CONFIG["summary"]["model_name"])
        temperature = config.get("temperature", AI_CONFIG["summary"]["generation_config"].get("temperature", 0.3))
        char_limit = config.get("summary_char_limit", SYSTEM_CONFIG["brain"]["summary_char_limit"])
        safety_settings = AI_CONFIG["summary"].get("safety_settings")

        loader = PromptLoader()
        prompt = loader.get(
            "brain_summarize_conversation",
            agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
            char_limit=char_limit,
            conversation_text=conversation_text,
        )

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    safety_settings=safety_settings,
                ),
            )
            return response.text.strip() if response.text else "要約なし"
        except Exception as e:
            print(f"[GeminiProvider] Summarize Error: {e}")
            return "（要約作成に失敗）"

    def embed(self, text: str, config: Optional[dict] = None) -> Optional[List[float]]:
        """Gemini Embedding API でベクトルを生成する。

        優先順位: config["model_name"] > AI_CONFIG["embedding"]["model_name"]
        """
        from butly_core.config import AI_CONFIG
        model_name = (config or {}).get("model_name") or AI_CONFIG["embedding"]["model_name"]
        try:
            response = self.client.models.embed_content(
                model=model_name,
                contents=text,
            )
            return response.embeddings[0].values
        except Exception as e:
            print(f"[GeminiProvider] Embed Error: {e}")
            return None

    def classify(self, prompt: str, config: dict) -> str:
        """軽量モデルでテキスト分類 / キーワード抽出を行う（同期）。"""
        try:
            response = self.client.models.generate_content(
                model=config["model_name"],
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=config["generation_config"].get("temperature", 0.0),
                    max_output_tokens=config["generation_config"].get("max_output_tokens", 512),
                    safety_settings=config.get("safety_settings"),
                ),
            )
            return response.text if response.text else ""
        except Exception as e:
            print(f"[GeminiProvider] Classify Error: {e}")
            return ""

    def generate(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> ChatResponse:
        """
        テキスト（+ 添付画像）を Gemini API で処理する。

        context に含まれるキー:
          - brain, memory_manager, history, override_config
          - memory_blocks, use_google_search
          - rag_results: ChatService が構築した RAG 検索結果リスト
          - use_rag: bool
        """
        brain = context["brain"]
        memory_manager = context["memory_manager"]
        history = context.get("history", [])
        override_config = context.get("override_config")
        memory_blocks = context.get("memory_blocks")
        use_google_search = context.get("use_google_search", False)
        rag_results = context.get("rag_results", [])
        use_rag = context.get("use_rag", True)
        context_order = context.get("context_order")
        context_levels = context.get("context_levels")

        # --- 画像 parts ---
        image_parts = []
        if attachments:
            image_parts = self._convert_attachments_to_parts(attachments)
            print(f"[GeminiProvider] 画像 parts 構築完了: {len(image_parts)}枚")

        # --- RAG コンテキスト文字列を構築 ---
        # RAG は build_context_prefix() で会話コンテキストに注入されるため、
        # ここでは full_prompt に結合しない
        full_prompt = text

        # --- キーワード（レスポンス用） ---
        keywords = []
        if use_rag and rag_results:
            # extract_keywords の結果は ChatService 側で呼ばれているが、
            # ここでは refs 用に rag_results を流用
            pass

        # --- チャット実行 ---
        try:
            if use_google_search:
                response_text, sources = self._try_search_with_retry(
                    full_prompt, image_parts, history,
                    memory_manager, override_config, memory_blocks,
                    context_order=context_order,
                    context_levels=context_levels,
                )
            else:
                chat_session = self._start_chat(
                    history=history,
                    memory_manager=memory_manager,
                    override_config=override_config,
                    use_google_search=False,
                    memory_blocks=memory_blocks,
                    context_order=context_order,
                    context_levels=context_levels,
                )
                prompt_parts = [full_prompt] + image_parts
                print("[GeminiProvider] Sending message to Gemini API...")
                response = chat_session.send_message(prompt_parts)
                print("[GeminiProvider] Got response from Gemini API!")
                response_text, sources, _ = self._extract_response(response)

            # debug_info 構築（Gemini 固有）
            from butly_core.core.gatekeeper import (
                build_system_instruction_from_blocks,
                build_context_prefix,
            )
            _debug_sys_inst = build_system_instruction_from_blocks(
                blocks=memory_blocks,
                memory_manager=memory_manager,
                use_google_search=use_google_search,
                context_order=context_order,
                context_levels=context_levels,
            )
            _debug_ctx_prefix = build_context_prefix(
                blocks=memory_blocks,
                memory_manager=memory_manager,
                use_google_search=use_google_search,
                context_order=context_order,
                context_levels=context_levels,
            )

            result = ChatResponse(
                text=response_text,
                keywords=keywords,
                refs=[dict(k) for k in rag_results] if rag_results else [],
                sources=sources if sources else [],
            )
            result.debug_info = {
                "system_instruction": (_debug_sys_inst[:500] + "...") if len(_debug_sys_inst) > 500 else _debug_sys_inst,
                "system_instruction_full": _debug_sys_inst,
                "context_prefix": (_debug_ctx_prefix[:500] + "...") if len(_debug_ctx_prefix) > 500 else _debug_ctx_prefix,
                "context_prefix_full": _debug_ctx_prefix,
                "history_count": len(history),
                "user_input": full_prompt,
                "raw_response": response_text,
            }
            return result
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ChatResponse(
                text=f"Error: {e}",
                keywords=keywords,
                refs=[dict(k) for k in rag_results] if rag_results else [],
                sources=[],
            )

    # ==================================================================
    # ストリーミング
    # ==================================================================

    async def async_generate_stream(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Gemini ネイティブの send_message_stream を使ったストリーミング応答。

        制約: use_google_search=True の場合は grounding metadata が stream で
        正しく組めないので非ストリーミングにフォールバックする。
        """
        use_google_search = context.get("use_google_search", False)
        if use_google_search:
            # Google Search 経路は非 stream 一括返却
            result = await self.async_generate(text, attachments, context)
            if result.text:
                yield {"type": "chunk", "text": result.text}
            yield {
                "type": "done",
                "full_text": result.text or "",
                "sources": result.sources or [],
                "debug": result.debug_info or {},
            }
            return

        memory_manager = context["memory_manager"]
        history = context.get("history", [])
        override_config = context.get("override_config")
        memory_blocks = context.get("memory_blocks")
        context_order = context.get("context_order")
        context_levels = context.get("context_levels")

        image_parts = []
        if attachments:
            image_parts = self._convert_attachments_to_parts(attachments)

        # debug data を事前構築 (done event で返す)
        from butly_core.core.gatekeeper import (
            build_system_instruction_from_blocks,
            build_context_prefix,
        )
        _debug_sys_inst = build_system_instruction_from_blocks(
            blocks=memory_blocks,
            memory_manager=memory_manager,
            use_google_search=False,
            context_order=context_order,
            context_levels=context_levels,
        )
        _debug_ctx_prefix = build_context_prefix(
            blocks=memory_blocks,
            memory_manager=memory_manager,
            use_google_search=False,
            context_order=context_order,
            context_levels=context_levels,
        )

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _producer():
            full_text = ""
            try:
                chat_session = self._start_chat(
                    history=history,
                    memory_manager=memory_manager,
                    override_config=override_config,
                    use_google_search=False,
                    memory_blocks=memory_blocks,
                    context_order=context_order,
                    context_levels=context_levels,
                )
                prompt_parts = [text] + image_parts
                print("[GeminiProvider] Streaming start")
                for chunk in chat_session.send_message_stream(prompt_parts):
                    chunk_text = ""
                    try:
                        if chunk.text:
                            chunk_text = chunk.text
                    except (ValueError, AttributeError):
                        if chunk.candidates:
                            try:
                                for part in chunk.candidates[0].content.parts:
                                    if hasattr(part, "text") and part.text:
                                        chunk_text += part.text
                            except (AttributeError, IndexError):
                                pass
                    if chunk_text:
                        full_text += chunk_text
                        loop.call_soon_threadsafe(
                            queue.put_nowait, {"type": "chunk", "text": chunk_text}
                        )
                print("[GeminiProvider] Streaming done")
                loop.call_soon_threadsafe(queue.put_nowait, {
                    "type": "done",
                    "full_text": full_text,
                    "sources": [],
                    "debug": {
                        "system_instruction": (_debug_sys_inst[:500] + "...") if len(_debug_sys_inst) > 500 else _debug_sys_inst,
                        "system_instruction_full": _debug_sys_inst,
                        "context_prefix": (_debug_ctx_prefix[:500] + "...") if len(_debug_ctx_prefix) > 500 else _debug_ctx_prefix,
                        "context_prefix_full": _debug_ctx_prefix,
                        "history_count": len(history),
                        "user_input": text,
                        "raw_response": full_text,
                    },
                })
            except Exception as e:
                import traceback
                traceback.print_exc()
                loop.call_soon_threadsafe(queue.put_nowait, {
                    "type": "error", "message": str(e),
                })

        producer_future = loop.run_in_executor(None, _producer)

        try:
            while True:
                event = await queue.get()
                yield event
                if event["type"] in ("done", "error"):
                    break
        finally:
            try:
                await producer_future
            except Exception:
                pass

    # ==================================================================
    # Gemini 固有ユーティリティ
    # ==================================================================

    def _filter_hallucinations(self, text: str) -> str:
        """Gemini が誤って出力する内部注釈テキストを除外する。"""
        if not text:
            return text
        unwanted_patterns = [
            r"No citation needed( as it's a personal response)?\.?",
            r"\[No citation needed\]",
            r"Citation not needed\.?",
        ]
        filtered = text
        for pattern in unwanted_patterns:
            filtered = re.sub(pattern, "", filtered, flags=re.IGNORECASE)
        return filtered.strip()

    def _extract_response(self, response):
        """レスポンスからテキストとソースを抽出する。"""
        sources = []
        response_text = ""
        finish_reason = None

        if response.candidates:
            finish_reason = response.candidates[0].finish_reason

        # Grounding Metadata
        try:
            if response.candidates and response.candidates[0].grounding_metadata:
                metadata = response.candidates[0].grounding_metadata
                if metadata.grounding_chunks:
                    for chunk in metadata.grounding_chunks:
                        if chunk.web:
                            sources.append({
                                "title": chunk.web.title or "Google Search",
                                "url": chunk.web.uri,
                            })
        except Exception as e:
            print(f"[GeminiProvider] Grounding Extraction Error: {e}")

        # テキスト抽出
        try:
            if response.text:
                response_text = response.text
        except (ValueError, AttributeError):
            pass

        if not response_text and response.candidates:
            try:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        response_text += part.text
            except (AttributeError, IndexError):
                pass

        response_text = self._filter_hallucinations(response_text)
        return response_text, sources, finish_reason

    def _merge_config(self, base_conf, override_conf):
        """設定辞書のディープマージ。"""
        if not override_conf:
            return base_conf
        merged = base_conf.copy()
        for key, value in override_conf.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                merged[key] = self._merge_config(merged[key], value)
            else:
                merged[key] = value
        return merged

    # ------------------------------------------------------------------
    # チャットセッション管理
    # ------------------------------------------------------------------

    def _start_chat(
        self,
        history=None,
        memory_manager=None,
        override_config=None,
        use_google_search=False,
        memory_blocks=None,
        context_order=None,
        context_levels=None,
    ):
        """チャットセッションを開始する。"""
        from butly_core.config import AI_CONFIG, SYSTEM_CONFIG

        if history is None:
            history = []

        chat_conf = AI_CONFIG["chat"]
        if override_config and "chat" in override_config:
            chat_conf = self._merge_config(chat_conf, override_config["chat"])

        # History → types.Content 変換
        history_contents = []
        for h in history:
            if isinstance(h, dict):
                role = h.get("role")
                parts = [
                    types.Part(text=p) if isinstance(p, str) else types.Part(text=str(p))
                    for p in h.get("parts", [])
                ]
                history_contents.append(types.Content(role=role, parts=parts))
            else:
                history_contents.append(h)

        # Tools
        tools_config = None
        if use_google_search:
            tools_config = [{"google_search": {}}]

        generate_config = types.GenerateContentConfig(
            temperature=chat_conf["generation_config"].get("temperature"),
            top_p=chat_conf["generation_config"].get("top_p"),
            top_k=chat_conf["generation_config"].get("top_k"),
            max_output_tokens=chat_conf["generation_config"].get("max_output_tokens"),
            safety_settings=chat_conf.get("safety_settings"),
            tools=tools_config,
            system_instruction=None,
        )

        model_name = chat_conf["model_name"]

        from butly_core.core.gatekeeper import (
            build_system_instruction_from_blocks,
            build_context_prefix,
        )
        base_instruction = build_system_instruction_from_blocks(
            blocks=memory_blocks,
            memory_manager=memory_manager,
            use_google_search=use_google_search,
            context_order=context_order,
            context_levels=context_levels,
        )

        # 可変コンテキストを history の先頭に注入
        context_prefix = build_context_prefix(
            blocks=memory_blocks,
            memory_manager=memory_manager,
            use_google_search=use_google_search,
            context_order=context_order,
            context_levels=context_levels,
        )
        if context_prefix:
            prefix_content = types.Content(
                role="user",
                parts=[types.Part(text=context_prefix)]
            )
            history_contents = [prefix_content] + history_contents

        generate_config.system_instruction = base_instruction

        chat = self.client.chats.create(
            model=model_name,
            config=generate_config,
            history=history_contents,
        )
        return chat

    # ------------------------------------------------------------------
    # 内部: 画像変換
    # ------------------------------------------------------------------

    def _convert_attachments_to_parts(self, attachments: List[Attachment]) -> list:
        """Attachment リストを Gemini の Part リストに変換する。"""
        parts = []
        total_size = 0
        temp_files = []

        try:
            for i, att in enumerate(attachments):
                img_bytes = base64.b64decode(att.data_base64)
                file_size = len(img_bytes)
                total_size += file_size

                if file_size <= INLINE_SIZE_PER_FILE and total_size <= INLINE_SIZE_TOTAL:
                    part = types.Part.from_bytes(data=img_bytes, mime_type=att.mime_type)
                    parts.append(part)
                    print(f"[GeminiProvider] 画像{i+1}: inline ({file_size / 1024:.0f}KB)")
                else:
                    part, tmp_path = self._upload_via_files_api(img_bytes, att, i)
                    parts.append(part)
                    if tmp_path:
                        temp_files.append(tmp_path)
        finally:
            for tmp in temp_files:
                try:
                    os.unlink(tmp)
                    print(f"[GeminiProvider] 一時ファイル削除: {tmp}")
                except Exception as e:
                    print(f"[GeminiProvider] 一時ファイル削除失敗: {tmp} - {e}")

        return parts

    def _upload_via_files_api(self, img_bytes: bytes, att: Attachment, index: int) -> tuple:
        """Files API で画像をアップロードし Part を返す。"""
        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        ext = ext_map.get(att.mime_type, ".bin")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(img_bytes)

            uploaded = self.client.files.upload(file=tmp_path, config={"mime_type": att.mime_type})
            print(f"[GeminiProvider] 画像{index+1}: Files API ({len(img_bytes) / 1024:.0f}KB) → {uploaded.name}")
            return uploaded, tmp_path
        except Exception as e:
            print(f"[GeminiProvider] Files API アップロードエラー: {e}")
            raise

    # ------------------------------------------------------------------
    # 内部: Google 検索リトライ
    # ------------------------------------------------------------------

    def _try_search_with_retry(
        self,
        full_prompt: str,
        image_parts: list,
        history: list,
        memory_manager,
        override_config,
        memory_blocks,
        context_order=None,
        context_levels=None,
    ) -> tuple:
        """Google 検索付きリクエストの 3 段階リトライ。"""
        # === First Try ===
        print("[GeminiProvider] Search: First Try")
        try:
            chat_session = self._start_chat(
                history=history,
                memory_manager=memory_manager, override_config=override_config,
                use_google_search=True, memory_blocks=memory_blocks,
                context_order=context_order, context_levels=context_levels,
            )
            prompt_parts = [full_prompt] + image_parts
            print("[GeminiProvider] Sending message to Gemini API...")
            response = chat_session.send_message(prompt_parts)
            print("[GeminiProvider] Got response from Gemini API!")
            response_text, sources, finish_reason = self._extract_response(response)

            if finish_reason and "MALFORMED" in str(finish_reason):
                print(f"[GeminiProvider] Search: First Try failed - {finish_reason}")
            elif response_text:
                return response_text, sources
            else:
                print("[GeminiProvider] Search: First Try returned empty response")
        except Exception as e:
            print(f"[GeminiProvider] Search: First Try exception - {e}")

        # === Retry 1 ===
        print("[GeminiProvider] Search: Retry 1")
        try:
            corrected_prompt = full_prompt + "\n\n【システム注意】前回のGoogle検索ツール呼び出しが不正な形式でした。google_searchツールは自動的に実行されます。関数を明示的に呼び出す必要はありません。通常通り回答してください。"
            chat_session = self._start_chat(
                history=history,
                memory_manager=memory_manager, override_config=override_config,
                use_google_search=True, memory_blocks=memory_blocks,
                context_order=context_order, context_levels=context_levels,
            )
            prompt_parts = [corrected_prompt] + image_parts
            print("[GeminiProvider] Sending message to Gemini API...")
            response = chat_session.send_message(prompt_parts)
            print("[GeminiProvider] Got response from Gemini API!")
            response_text, sources, finish_reason = self._extract_response(response)

            if finish_reason and "MALFORMED" in str(finish_reason):
                print(f"[GeminiProvider] Search: Retry 1 also failed - {finish_reason}")
            elif response_text:
                return response_text, sources
            else:
                print("[GeminiProvider] Search: Retry 1 returned empty response")
        except Exception as e:
            print(f"[GeminiProvider] Search: Retry 1 exception - {e}")

        # === Fallback ===
        print("[GeminiProvider] Search: Fallback (without search)")
        chat_session = self._start_chat(
            history=history,
            memory_manager=memory_manager, override_config=override_config,
            use_google_search=False, memory_blocks=memory_blocks,
            context_order=context_order, context_levels=context_levels,
        )
        prompt_parts = [full_prompt] + image_parts
        print("[GeminiProvider] Sending message to Gemini API...")
        response = chat_session.send_message(prompt_parts)
        print("[GeminiProvider] Got response from Gemini API!")
        response_text, sources, _ = self._extract_response(response)

        if response_text:
            response_text = "【検索エラー】外部情報にアクセスできませんでした。以下は内部知識に基づく回答です。\n\n" + response_text
        else:
            response_text = "【検索エラー】外部情報にアクセスできませんでした。回答を生成できませんでした。"

        return response_text, sources
