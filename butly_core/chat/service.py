"""
service.py
----------
ChatService: チャット実行のオーケストレーション層。
main.py と brain.py / provider の橋渡しを行う。
ステートレス設計（リクエストごとに生成）。
"""

from pathlib import Path
from typing import Any, Dict, Optional

from butly_core.chat.types import (
    ChatRequest,
    ChatResponse,
    validate_attachments,
)
from butly_core.llm.factory import ProviderFactory
from starlette.concurrency import run_in_threadpool


def _is_gemini_model(model_name: str) -> bool:
    """モデル名が Gemini かどうかを判定する"""
    if not model_name:
        return True  # デフォルトは Gemini
    lower = model_name.lower()
    return lower.startswith("gemini") or lower.startswith("models/gemini")


class ChatService:
    """
    チャット実行のステートレスオーケストレーター。

    責務:
      1. インスタンスコンポーネントの取得
      2. 時刻コンテキスト構築
      3. Gatekeeper 分類 + SessionState 更新
      4. MemoryBlockBuilder で記憶ブロック構築
      5. Provider 選択と vision 対応チェック
      6. Provider / brain 経由で応答生成
      7. 会話保存
    """

    @staticmethod
    async def execute(
        request: ChatRequest,
        get_instance_components,
        instance_manager,
        instances_dir: Path,
        gatekeeper,
        mem_block_builder,
        ws_manager=None,
    ) -> ChatResponse:
        """
        チャットリクエストを処理する。

        Parameters
        ----------
        request : ChatRequest
            正規化済みのチャットリクエスト。
        get_instance_components : callable
            インスタンスコンポーネント取得関数。
        instance_manager : InstanceManager
        instances_dir : Path
        gatekeeper : Gatekeeper
        mem_block_builder : MemoryBlockBuilder
        ws_manager : ConnectionManager | None
            WebSocket マネージャー（通知用、REST呼び出し時は None）。

        Returns
        -------
        ChatResponse
        """
        from butly_core.core.gatekeeper import SessionState
        from butly_core.config import AI_CONFIG

        instance_name = request.instance_name

        # --- 添付バリデーション ---
        if request.attachments:
            error = validate_attachments(request.attachments)
            if error:
                return ChatResponse(text=f"[エラー] {error}")

        # --- 1. コンポーネント取得 ---
        components = get_instance_components(instance_name)
        memory = components["memory"]
        brain = components["brain"]
        chronos = components["chronos"]
        cached_content = components["cache"]

        # --- 2. 時刻コンテキスト ---
        last_ts = memory.get_last_interaction_time()
        sys_note = chronos.get_system_note(
            is_holiday=False, last_interaction_time=last_ts
        )
        full_prompt = f"{sys_note}\n\n{request.text}"

        # --- 3. インスタンス設定 ---
        instance_config = instance_manager.get_instance_config(instance_name)

        # --- 4. Gatekeeper 分類 ---
        history, _ = memory.load_recent_sessions(limit=6)
        history_fmt = []
        for m in history:
            content = m.get("parts", [""])[0]
            if isinstance(content, dict):
                content = content.get("text", "")
            history_fmt.append({"role": m.get("role"), "parts": [content]})

        instance_dir = instances_dir / instance_name
        session_state = SessionState(instance_dir)

        gk_enabled = instance_config.get("gatekeeper", {}).get("enabled", True)

        if gk_enabled:
            try:
                gk_result = gatekeeper.classify(
                    user_input=request.text,
                    history_msgs=history_fmt,
                    session_state=session_state.to_dict(),
                    override_config=instance_config,
                )
                tier = gk_result.get("tier", "mid")
            except Exception as e:
                print(f"[ChatService] Gatekeeper エラー、フォールバック: {e}")
                gk_result = {
                    "tier": "mid", "topic": "", "need": None,
                    "search_targets": None, "state_delta": {},
                }
                tier = "mid"

            # SessionState 更新
            state_delta = gk_result.get("state_delta", {})
            session_state.apply_delta(state_delta)
        else:
            # Gatekeeper 無効時: 常に mid 相当で動作（RAG なし）
            gk_result = {
                "tier": "mid", "topic": "", "need": None,
                "search_targets": None, "state_delta": {},
            }
            tier = "mid"
            print("[ChatService] Gatekeeper disabled — defaulting to mid tier")

        session_state.increment_turn(tier)

        # --- 5. 記憶ブロック構築 ---
        use_rag = instance_config.get("brain", {}).get("use_rag", True)

        memory_blocks = mem_block_builder.build(
            tier=tier,
            memory_manager=memory,
            brain=brain if (tier == "cortex" and use_rag) else None,
            user_input=request.text,
            instance_name=instance_name,
            override_config=instance_config,
            gatekeeper_output=gk_result,
        )

        # --- 6. Provider 選択と応答生成 ---
        model_name = request.model_name or AI_CONFIG["chat"]["model_name"]
        provider = ProviderFactory.create(model_name)
        has_attachments = bool(request.attachments)

        # vision 非対応チェック
        if has_attachments and not provider.supports_vision(model_name):
            return ChatResponse(
                text="[エラー] 選択中のモデルは画像入力に対応していません",
                tier=tier,
                need=gk_result.get("need"),
                search_targets=gk_result.get("search_targets"),
                session_state=session_state.to_dict(),
            )

        # RAG は Gatekeeper → MemoryBlockBuilder で一元管理
        # memory_blocks["rag_context"] に結果が格納済み（cortex + need有効時のみ）
        rag_results = []
        if memory_blocks and memory_blocks.get("rag_context"):
            print("[ChatService] RAG: Gatekeeper 経由の RAG コンテキストを使用")
        else:
            print("[ChatService] RAG: なし（Gatekeeper 判断によりスキップ）")

        # --- 5.5 Web検索（非Gemini + 検索ON時） ---
        web_search_context = ""
        web_sources = []
        if request.use_web_search and not _is_gemini_model(model_name):
            from butly_core.search import create_search_provider
            search_provider = create_search_provider()
            if search_provider.is_available():
                print("[ChatService] Web Search: 汎用検索モジュールで検索実行")
                search_results = await run_in_threadpool(
                    search_provider.search,
                    query=request.text,
                    max_results=3,
                )
                if search_results:
                    lines = []
                    for i, r in enumerate(search_results, 1):
                        lines.append(f"[{i}] {r.title}")
                        lines.append(f"    URL: {r.url}")
                        lines.append(f"    {r.content}")
                        lines.append("")
                    web_search_context = "\n".join(lines)
                    web_sources = [{"title": r.title, "url": r.url} for r in search_results]
                    print(f"[ChatService] Web Search: {len(search_results)} 件取得")
                else:
                    print("[ChatService] Web Search: 結果なし")
            else:
                print("[ChatService] Web Search: API キー未設定のためスキップ")

        if web_search_context:
            memory_blocks["web_search_context"] = web_search_context

        context = {
            "brain": brain,
            "memory_manager": memory,
            "history": history_fmt,
            "cached_content": cached_content,
            "override_config": instance_config,
            "memory_blocks": memory_blocks,
            "use_google_search": request.use_google_search,
            "rag_results": rag_results,
            "use_rag": request.use_rag,
            "context_order": instance_config.get("context_order"),
        }

        if has_attachments:
            print(f"[ChatService] Provider: {type(provider).__name__}, attachments={len(request.attachments)}")

        result = await run_in_threadpool(
            provider.generate,
            text=full_prompt,
            attachments=request.attachments if has_attachments else [],
            context=context,
        )

        # tier / gatekeeper 情報を付与
        result.tier = tier
        result.need = gk_result.get("need")
        result.search_targets = gk_result.get("search_targets")
        result.session_state = session_state.to_dict()
        result.gatekeeper_scores = gk_result.get("llm_scoring")

        # Web検索ソースをレスポンスに追加
        if web_sources:
            result.sources = web_sources + (result.sources or [])

        # --- 7. 会話保存 ---
        memory.save_single_turn(request.text, result.text)
        memory.maintain_memory(brain)

        return result
