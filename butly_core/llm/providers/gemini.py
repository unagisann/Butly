"""
gemini.py
---------
Gemini LLM プロバイダー。
attachments → Gemini parts への変換をここで完結し、
brain.py には画像を渡さない設計。
"""

import base64
import os
import tempfile
from typing import Any, Dict, List

from butly_core.chat.types import (
    Attachment,
    ChatResponse,
    INLINE_SIZE_PER_FILE,
    INLINE_SIZE_TOTAL,
)
from butly_core.llm.base import BaseProvider


# 明示的な非対応リスト（vision 非対応が判明したモデルのみ追加）
VISION_UNSUPPORTED_MODELS = {
    "gemini-3.1-flash-lite-preview",  # テキスト特化軽量モデル
}


class GeminiProvider(BaseProvider):
    """
    Gemini API を使った LLM プロバイダー。

    画像入力は 2 経路:
      - 小さい画像（1枚2MB以下 & 合計8MB以下）: inline data
      - 大きい画像: Files API 経由
    """

    @staticmethod
    def supports_vision(model_name: str) -> bool:
        """
        非対応モデルリストに含まれていなければ vision 対応とみなす。
        新モデルはデフォルトで対応扱い。非対応が判明したら追加する。
        """
        return model_name not in VISION_UNSUPPORTED_MODELS

    async def generate(
        self,
        text: str,
        attachments: List[Attachment],
        context: Dict[str, Any],
    ) -> ChatResponse:
        """
        テキスト + 添付画像を Gemini API で処理する。

        フロー:
          1. attachments → Gemini parts に変換（ここで完結）
          2. brain.start_chat() でセッション取得
          3. テキスト + parts を send_message() で送信
          4. brain._extract_response() で結果を抽出
        """
        from google.genai import types

        brain = context["brain"]
        memory_manager = context["memory_manager"]
        history = context.get("history", [])
        cached_content = context.get("cached_content")
        override_config = context.get("override_config")
        memory_blocks = context.get("memory_blocks")
        use_google_search = context.get("use_google_search", False)

        # --- 1. 画像 parts の構築 ---
        image_parts = []
        if attachments:
            image_parts = self._convert_attachments_to_parts(
                attachments, brain, types
            )
            print(f"[GeminiProvider] 画像 parts 構築完了: {len(image_parts)}枚")

        # --- 2. RAG コンテキスト構築（brain の既存ロジックを活用）---
        tier = memory_blocks.get("tier", "mid") if memory_blocks else "mid"

        keyword_data = brain.extract_keywords(text, override_config)
        keywords = keyword_data.get("keywords", [])
        current_instance = memory_manager.instance_dir.name

        if memory_blocks and tier == "cortex" and memory_blocks.get("rag_context"):
            knowledge_list = []
            rag_context = memory_blocks["rag_context"]
            print("[GeminiProvider] RAG: cortex モード、Gatekeeper 済み RAG を流用")
        else:
            from butly_core.config import SYSTEM_CONFIG
            limit = SYSTEM_CONFIG["brain"]["search_limit"]
            knowledge_list = brain.search_knowledge(
                keywords, text,
                instance_name=current_instance,
                limit=limit,
                override_config=override_config,
            )
            rag_context = ""
            print(f"[GeminiProvider] RAG Search: keywords={keywords}, hits={len(knowledge_list)}")
            if knowledge_list:
                rag_context = "\n\n[過去の記憶]\n"
                for k in knowledge_list:
                    rag_context += f"・{k['title']}: {k['summary']}\n"
                    if k.get("episode"):
                        rag_context += f"  (補足: {k['episode']})\n"
                rag_context += "\n"

        full_prompt = f"{rag_context}\nユーザー: {text}"
        if use_google_search and rag_context:
            full_prompt += "\n\n(Note: Prioritize the local database information above. Use Google Search only if the database lacks sufficient information or for checking the latest facts.)"

        # --- 3. チャット実行 ---
        try:
            if use_google_search:
                # Google検索付きリトライ: 画像 parts 付きで実行
                response_text, sources = await self._try_search_with_retry_visual(
                    brain, full_prompt, image_parts,
                    cached_content, history, memory_manager,
                    override_config, memory_blocks, types,
                )
            else:
                chat_session = await brain.start_chat(
                    cached_content=cached_content,
                    history=history,
                    memory_manager=memory_manager,
                    override_config=override_config,
                    use_google_search=False,
                    memory_blocks=memory_blocks,
                )

                # テキスト + 画像 parts を組み立てて送信
                prompt_parts = [full_prompt] + image_parts

                print("[GeminiProvider] Sending message to Gemini API...")
                response = await chat_session.send_message(prompt_parts)
                print("[GeminiProvider] Got response from Gemini API!")
                response_text, sources, _ = brain._extract_response(response)

            return ChatResponse(
                text=response_text,
                keywords=keywords,
                refs=[dict(k) for k in knowledge_list] if knowledge_list else [],
                sources=sources if sources else [],
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            return ChatResponse(
                text=f"Error: {e}",
                keywords=keywords,
                refs=[dict(k) for k in knowledge_list] if knowledge_list else [],
                sources=[],
            )

    # ------------------------------------------------------------------
    # 内部メソッド: 画像変換
    # ------------------------------------------------------------------

    def _convert_attachments_to_parts(
        self,
        attachments: List[Attachment],
        brain,
        types_module,
    ) -> list:
        """
        Attachment リストを Gemini の Part リストに変換する。

        - 小さい画像: inline data（Part.from_bytes）
        - 大きい画像: Files API 経由でアップロード
        """
        parts = []
        total_size = 0
        temp_files = []  # cleanup 用

        try:
            for i, att in enumerate(attachments):
                img_bytes = base64.b64decode(att.data_base64)
                file_size = len(img_bytes)
                total_size += file_size

                # inline 判定: 1枚2MB以下 & 累計8MB以下
                if file_size <= INLINE_SIZE_PER_FILE and total_size <= INLINE_SIZE_TOTAL:
                    part = types_module.Part.from_bytes(
                        data=img_bytes,
                        mime_type=att.mime_type,
                    )
                    parts.append(part)
                    print(f"[GeminiProvider] 画像{i+1}: inline ({file_size / 1024:.0f}KB)")
                else:
                    # Files API 経由
                    part, tmp_path = self._upload_via_files_api(
                        brain, img_bytes, att, i, types_module
                    )
                    parts.append(part)
                    if tmp_path:
                        temp_files.append(tmp_path)
        finally:
            # ローカル一時ファイルを確実に削除
            for tmp in temp_files:
                try:
                    os.unlink(tmp)
                    print(f"[GeminiProvider] 一時ファイル削除: {tmp}")
                except Exception as e:
                    print(f"[GeminiProvider] 一時ファイル削除失敗: {tmp} - {e}")

        return parts

    def _upload_via_files_api(
        self,
        brain,
        img_bytes: bytes,
        att: Attachment,
        index: int,
        types_module,
    ) -> tuple:
        """
        Files API を使って画像をアップロードし、Part を返す。

        Returns:
            (Part, tmp_path): Part と一時ファイルのパス（cleanup用）
        """
        # 拡張子の決定
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        ext = ext_map.get(att.mime_type, ".bin")

        # 一時ファイルに書き出し
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(img_bytes)

            # Files API にアップロード
            uploaded = brain.client.files.upload(
                file=tmp_path,
                config={"mime_type": att.mime_type},
            )
            file_size = len(img_bytes)
            print(f"[GeminiProvider] 画像{index+1}: Files API ({file_size / 1024:.0f}KB) → {uploaded.name}")

            return uploaded, tmp_path

        except Exception as e:
            # アップロード失敗時もローカルファイルは cleanup
            print(f"[GeminiProvider] Files API アップロードエラー: {e}")
            raise

    # ------------------------------------------------------------------
    # 内部メソッド: Google検索リトライ（画像付き）
    # ------------------------------------------------------------------

    async def _try_search_with_retry_visual(
        self,
        brain,
        full_prompt: str,
        image_parts: list,
        cached_content,
        history: list,
        memory_manager,
        override_config,
        memory_blocks,
        types_module,
    ) -> tuple:
        """
        Google検索付きリクエストの3段階リトライ（画像 parts 付き）。
        brain._try_search_with_retry と同等のロジックだが、
        画像 parts を GeminiProvider 側で管理する。
        """
        # === First Try ===
        print("[GeminiProvider] Search: First Try")
        try:
            chat_session = await brain.start_chat(
                cached_content=cached_content,
                history=history,
                memory_manager=memory_manager,
                override_config=override_config,
                use_google_search=True,
                memory_blocks=memory_blocks,
            )
            prompt_parts = [full_prompt] + image_parts
            print("[GeminiProvider] Sending message to Gemini API...")
            response = await chat_session.send_message(prompt_parts)
            print("[GeminiProvider] Got response from Gemini API!")
            response_text, sources, finish_reason = brain._extract_response(response)

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
            chat_session = await brain.start_chat(
                cached_content=cached_content,
                history=history,
                memory_manager=memory_manager,
                override_config=override_config,
                use_google_search=True,
                memory_blocks=memory_blocks,
            )
            prompt_parts = [corrected_prompt] + image_parts
            print("[GeminiProvider] Sending message to Gemini API...")
            response = await chat_session.send_message(prompt_parts)
            print("[GeminiProvider] Got response from Gemini API!")
            response_text, sources, finish_reason = brain._extract_response(response)

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
        chat_session = await brain.start_chat(
            cached_content=cached_content,
            history=history,
            memory_manager=memory_manager,
            override_config=override_config,
            use_google_search=False,
            memory_blocks=memory_blocks,
        )
        prompt_parts = [full_prompt] + image_parts
        print("[GeminiProvider] Sending message to Gemini API...")
        response = await chat_session.send_message(prompt_parts)
        print("[GeminiProvider] Got response from Gemini API!")
        response_text, sources, _ = brain._extract_response(response)

        if response_text:
            response_text = "【検索エラー】外部情報にアクセスできませんでした。以下は内部知識に基づく回答です。\n\n" + response_text
        else:
            response_text = "【検索エラー】外部情報にアクセスできませんでした。回答を生成できませんでした。"

        return response_text, sources
