"""
service.py
----------
ChatService: チャット実行のオーケストレーション層。
main.py と brain.py / provider の橋渡しを行う。
ステートレス設計（リクエストごとに生成）。
"""

import asyncio
import datetime
import json
import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

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


def _save_debug_log(
    instance_dir: Path,
    payload: dict,
    max_history: int = 20,
) -> None:
    """
    instance_dir/debug_logs/ 以下にデバッグ情報を保存する。

    - latest.json: 毎ターン上書き (常に最新)
    - history/{YYYYMMDD_HHMMSS_uuid}.json: ローテーション (max_history 件保持)

    保存失敗は応答に影響させない (warning ログのみ)。
    """
    try:
        debug_dir = instance_dir / "debug_logs"
        debug_dir.mkdir(exist_ok=True)
        history_dir = debug_dir / "history"
        history_dir.mkdir(exist_ok=True)

        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

        (debug_dir / "latest.json").write_text(text, encoding="utf-8")

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        (history_dir / f"{ts}.json").write_text(text, encoding="utf-8")

        # 古い履歴を削除
        files = sorted(history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-max_history]:
            old.unlink(missing_ok=True)
    except Exception as e:
        print(f"[ChatService] debug_log 保存エラー (応答には影響なし): {e}")


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

        _t_start = time.time()

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

        _t_gk_start = time.time()

        if gk_enabled:
            try:
                gk_result = gatekeeper.classify(
                    user_input=request.text,
                    history_msgs=history_fmt,
                    session_state=session_state.to_dict(),
                    override_config=instance_config,
                    instance_dir=instance_dir,
                    brain=brain,
                    memory_manager=memory,
                )
                tier = gk_result.get("tier", "mid")
            except Exception as e:
                print(f"[ChatService] Gatekeeper エラー、フォールバック: {e}")
                gk_result = {
                    "tier": "mid", "topic": "", "need": None,
                    "need_intent": None,
                    "search_targets": None, "state_delta": {},
                }
                tier = "mid"

            # 注: StateUpdater は post-response (Step 6 で generate と並列実行) で動かす
        else:
            # Gatekeeper 無効時: RAG ON なら need を設定、OFF なら mid
            use_rag = instance_config.get("brain", {}).get("use_rag", True)
            tier = "mid"
            gk_result = {
                "tier": tier, "topic": "", "need": "rag_search" if use_rag else None,
                "need_intent": "past_fact" if use_rag else None,
                "search_targets": None, "state_delta": {},
            }
            print(f"[ChatService] Gatekeeper disabled — defaulting to {tier} tier (rag={'on' if use_rag else 'off'})")

        _t_gk_end = time.time()

        session_state.increment_turn(tier, history_msgs=history_fmt)

        # --- 5. 記憶ブロック構築 ---
        use_rag = instance_config.get("brain", {}).get("use_rag", True)

        _t_mem_start = time.time()

        memory_blocks = mem_block_builder.build(
            tier=tier,
            memory_manager=memory,
            brain=brain if (gk_result.get("need") and use_rag) else None,
            user_input=request.text,
            instance_name=instance_name,
            override_config=instance_config,
            gatekeeper_output=gk_result,
        )

        _t_mem_end = time.time()

        # --- 6. Provider 選択と応答生成 ---
        # インスタンス設定 > リクエスト指定 > グローバル設定 の優先順で model_name を決定
        model_name = (
            instance_config.get("chat", {}).get("model_name")
            or request.model_name
            or AI_CONFIG["chat"]["model_name"]
        )
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
        # memory_blocks["rag_context"] に結果が格納済み（need有効時のみ）
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
            search_provider = create_search_provider(chat_model=model_name)
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

        # --- context_levels 取得（後方互換: 旧 context_order のみの場合は変換） ---
        context_levels_cfg = instance_config.get("context_levels")
        if context_levels_cfg is None and "context_order" in instance_config:
            from butly_core.core.gatekeeper import migrate_context_order_to_levels
            instance_config = migrate_context_order_to_levels(instance_config)
            context_levels_cfg = instance_config.get("context_levels")

        context = {
            "brain": brain,
            "memory_manager": memory,
            "history": history_fmt,
            "override_config": instance_config,
            "memory_blocks": memory_blocks,
            "use_google_search": request.use_google_search,
            "rag_results": rag_results,
            "use_rag": request.use_rag,
            "context_order": instance_config.get("context_order"),
            "context_levels": context_levels_cfg,
        }

        if has_attachments:
            print(f"[ChatService] Provider: {type(provider).__name__}, attachments={len(request.attachments)}")

        _t_gen_start = time.time()

        # Generate と StateUpdater を並列実行 (post-response の StateUpdater を
        # クリティカルパスから外す)
        gen_elapsed_ms = {"value": 0}
        state_elapsed_ms = {"value": 0}

        async def _run_generate():
            t0 = time.time()
            res = await run_in_threadpool(
                provider.generate,
                text=full_prompt,
                attachments=request.attachments if has_attachments else [],
                context=context,
            )
            gen_elapsed_ms["value"] = int((time.time() - t0) * 1000)
            return res

        async def _run_state_update():
            if not gk_enabled:
                return {}
            t0 = time.time()
            try:
                res = await run_in_threadpool(
                    gatekeeper.update_state,
                    user_input=request.text,
                    history_msgs=history_fmt,
                    session_state=session_state.to_dict(),
                    override_config=instance_config,
                    instance_dir=instance_dir,
                )
            except Exception as e:
                print(f"[ChatService] StateUpdater post-response エラー: {e}")
                res = {}
            state_elapsed_ms["value"] = int((time.time() - t0) * 1000)
            return res

        result, state_delta = await asyncio.gather(
            _run_generate(),
            _run_state_update(),
        )

        # 取得した state_delta を反映 (次ターン以降の context に活かす)
        if state_delta:
            session_state.apply_delta(state_delta)
            gk_result["state_delta"] = state_delta

        _t_gen_end = time.time()
        _t_total = time.time() - _t_start

        # tier / gatekeeper 情報を付与
        result.tier = tier
        result.need = gk_result.get("need")
        result.search_targets = gk_result.get("search_targets")
        result.session_state = session_state.to_dict()
        result.gatekeeper_scores = gk_result.get("llm_scoring")

        # Web検索ソースをレスポンスに追加
        if web_sources:
            result.sources = web_sources + (result.sources or [])

        # --- debug_info 統合 ---
        def _estimate_tokens(text: str) -> int:
            if not text:
                return 0
            ja_chars = len(re.findall(r'[\u3000-\u9fff\uff00-\uffef]', text))
            en_words = len(re.findall(r'[a-zA-Z]+', text))
            return int(ja_chars * 1.5 + en_words + len(text) * 0.1)

        provider_debug = result.debug_info or {}

        # トークン概算用のプロンプトテキスト収集
        total_prompt_text = ""
        if provider_debug.get("messages_full"):
            for m in provider_debug["messages_full"]:
                total_prompt_text += m.get("content", "")
        elif provider_debug.get("system_instruction_full"):
            total_prompt_text = (
                provider_debug.get("system_instruction_full", "")
                + provider_debug.get("context_prefix_full", "")
                + provider_debug.get("user_input", "")
            )

        # RAG 結果がメモリブロックに含まれている場合はそこから取得
        rag_debug_results = []
        if memory_blocks and memory_blocks.get("rag_context"):
            rag_raw = memory_blocks.get("rag_results_raw", [])
            rag_debug_results = [
                {"title": r.get("title", ""), "score": r.get("score", 0), "episode": r.get("episode", "")}
                for r in rag_raw
            ] if rag_raw else []

        result.debug_info = {
            "timing": {
                "gatekeeper_ms": int((_t_gk_end - _t_gk_start) * 1000),
                "memory_build_ms": int((_t_mem_end - _t_mem_start) * 1000),
                "rag_search_ms": 0,
                "generation_ms": gen_elapsed_ms["value"],
                "state_update_ms": state_elapsed_ms["value"],
                "total_ms": int(_t_total * 1000),
            },
            "token_estimate": {
                "prompt": _estimate_tokens(total_prompt_text),
                "response": _estimate_tokens(result.text),
            },
            "gatekeeper": {
                "tier": tier,
                "enabled": gk_enabled,
                "scores": gk_result.get("llm_scoring"),
                "need": gk_result.get("need"),
                "need_intent": gk_result.get("need_intent"),
                "search_targets": gk_result.get("search_targets"),
                "memory_probe_status": gk_result.get("memory_probe", {}).get("status"),
                "memory_probe_layers": gk_result.get("memory_probe", {}).get("layers"),
                "session_state": session_state.to_dict(),
            },
            "rag": {
                "query": gk_result.get("need"),
                "results": rag_debug_results,
            },
            "prompt": provider_debug.get("messages", []),
            "prompt_full": provider_debug.get("messages_full", []),
            "raw_response": provider_debug.get("raw_response", result.text),
            "provider": type(provider).__name__,
            "model": instance_config.get("chat", {}).get("model_name", ""),
        }

        # Gemini 固有フィールドがあれば追加
        if provider_debug.get("system_instruction"):
            result.debug_info["gemini_system_instruction"] = provider_debug["system_instruction"]
            result.debug_info["gemini_context_prefix"] = provider_debug.get("context_prefix", "")
            result.debug_info["gemini_history_count"] = provider_debug.get("history_count", 0)
            result.debug_info["prompt_full"] = [
                {"role": "system", "content": provider_debug.get("system_instruction_full", "")},
                {"role": "user", "content": provider_debug.get("context_prefix_full", "")},
                {"role": "user", "content": provider_debug.get("user_input", "")},
            ]

        # --- 6.5. Debug log の自動保存 (instance_dir/debug_logs/) ---
        debug_log_payload = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "instance": instance_name,
            "user_input": request.text,
            "assistant_response": result.text,
            "debug_info": result.debug_info,
        }
        _save_debug_log(instance_dir, debug_log_payload)

        # --- 7. 会話保存 ---
        memory.save_single_turn(request.text, result.text)
        memory.maintain_memory(brain)

        return result

    # ==================================================================
    # ストリーミング版
    # ==================================================================

    @staticmethod
    async def execute_stream(
        request: ChatRequest,
        get_instance_components,
        instance_manager,
        instances_dir: Path,
        gatekeeper,
        mem_block_builder,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        ストリーミング版チャット処理。Provider が逐次 yield する chunk を
        そのまま転送し、最後に metadata + done イベントを yield する。

        yield 形式:
          - {"type": "chunk", "text": str}            : 部分テキスト
          - {"type": "metadata", "data": {...}}        : tier/need 等の Gatekeeper メタ (stream 開始直後)
          - {"type": "done", "data": {timing, debug, session_state, sources, ...}}
          - {"type": "error", "message": str, "recoverable": bool}
        """
        from butly_core.core.gatekeeper import SessionState
        from butly_core.config import AI_CONFIG

        _t_start = time.time()
        instance_name = request.instance_name

        # 添付バリデーション
        if request.attachments:
            error = validate_attachments(request.attachments)
            if error:
                yield {"type": "error", "message": f"[エラー] {error}", "recoverable": False}
                return

        # 1. コンポーネント取得
        components = get_instance_components(instance_name)
        memory = components["memory"]
        brain = components["brain"]
        chronos = components["chronos"]

        # 2. 時刻コンテキスト
        last_ts = memory.get_last_interaction_time()
        sys_note = chronos.get_system_note(is_holiday=False, last_interaction_time=last_ts)
        full_prompt = f"{sys_note}\n\n{request.text}"

        # 3. インスタンス設定
        instance_config = instance_manager.get_instance_config(instance_name)

        # 4. Gatekeeper
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
        _t_gk_start = time.time()

        if gk_enabled:
            try:
                gk_result = gatekeeper.classify(
                    user_input=request.text,
                    history_msgs=history_fmt,
                    session_state=session_state.to_dict(),
                    override_config=instance_config,
                    instance_dir=instance_dir,
                    brain=brain,
                    memory_manager=memory,
                )
                tier = gk_result.get("tier", "mid")
            except Exception as e:
                print(f"[ChatService.stream] Gatekeeper エラー、フォールバック: {e}")
                gk_result = {
                    "tier": "mid", "topic": "", "need": None,
                    "need_intent": None, "search_targets": None, "state_delta": {},
                }
                tier = "mid"
        else:
            use_rag = instance_config.get("brain", {}).get("use_rag", True)
            tier = "mid"
            gk_result = {
                "tier": tier, "topic": "", "need": "rag_search" if use_rag else None,
                "need_intent": "past_fact" if use_rag else None,
                "search_targets": None, "state_delta": {},
            }

        _t_gk_end = time.time()
        session_state.increment_turn(tier, history_msgs=history_fmt)

        # 5. 記憶ブロック
        use_rag = instance_config.get("brain", {}).get("use_rag", True)
        _t_mem_start = time.time()
        memory_blocks = mem_block_builder.build(
            tier=tier, memory_manager=memory,
            brain=brain if (gk_result.get("need") and use_rag) else None,
            user_input=request.text, instance_name=instance_name,
            override_config=instance_config, gatekeeper_output=gk_result,
        )
        _t_mem_end = time.time()

        # Provider
        model_name = (
            instance_config.get("chat", {}).get("model_name")
            or request.model_name
            or AI_CONFIG["chat"]["model_name"]
        )
        provider = ProviderFactory.create(model_name)
        has_attachments = bool(request.attachments)

        if has_attachments and not provider.supports_vision(model_name):
            yield {"type": "error",
                   "message": "[エラー] 選択中のモデルは画像入力に対応していません",
                   "recoverable": False}
            return

        # Web search (非 Gemini)
        web_search_context = ""
        web_sources = []
        if request.use_web_search and not _is_gemini_model(model_name):
            from butly_core.search import create_search_provider
            search_provider = create_search_provider(chat_model=model_name)
            if search_provider.is_available():
                search_results = await run_in_threadpool(
                    search_provider.search, query=request.text, max_results=3,
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
        if web_search_context:
            memory_blocks["web_search_context"] = web_search_context

        context_levels_cfg = instance_config.get("context_levels")
        if context_levels_cfg is None and "context_order" in instance_config:
            from butly_core.core.gatekeeper import migrate_context_order_to_levels
            instance_config = migrate_context_order_to_levels(instance_config)
            context_levels_cfg = instance_config.get("context_levels")

        context = {
            "brain": brain, "memory_manager": memory, "history": history_fmt,
            "override_config": instance_config, "memory_blocks": memory_blocks,
            "use_google_search": request.use_google_search,
            "rag_results": [], "use_rag": request.use_rag,
            "context_order": instance_config.get("context_order"),
            "context_levels": context_levels_cfg,
        }

        # metadata イベント (stream 開始前に Gatekeeper 情報を即送信)
        yield {
            "type": "metadata",
            "data": {
                "tier": tier,
                "need": gk_result.get("need"),
                "need_intent": gk_result.get("need_intent"),
                "search_targets": gk_result.get("search_targets"),
                "scores": gk_result.get("llm_scoring"),
                "memory_probe_status": gk_result.get("memory_probe", {}).get("status"),
            },
        }

        # 6. Stream generate + StateUpdater 並列
        _t_gen_start = time.time()
        ttfb_ms = {"value": None}
        state_elapsed_ms = {"value": 0}

        async def _run_state_update():
            if not gk_enabled:
                return {}
            t0 = time.time()
            try:
                res = await run_in_threadpool(
                    gatekeeper.update_state,
                    user_input=request.text, history_msgs=history_fmt,
                    session_state=session_state.to_dict(),
                    override_config=instance_config, instance_dir=instance_dir,
                )
            except Exception as e:
                print(f"[ChatService.stream] StateUpdater エラー: {e}")
                res = {}
            state_elapsed_ms["value"] = int((time.time() - t0) * 1000)
            return res

        state_task = asyncio.create_task(_run_state_update())

        full_text = ""
        provider_debug = {}
        sources = list(web_sources)
        stream_err = None

        try:
            async for event in provider.async_generate_stream(
                text=full_prompt,
                attachments=request.attachments if has_attachments else [],
                context=context,
            ):
                if event["type"] == "chunk":
                    if ttfb_ms["value"] is None:
                        ttfb_ms["value"] = int((time.time() - _t_gen_start) * 1000)
                    full_text += event["text"]
                    yield {"type": "chunk", "text": event["text"]}
                elif event["type"] == "done":
                    full_text = event.get("full_text", full_text)
                    provider_debug = event.get("debug", {})
                    sources.extend(event.get("sources", []) or [])
                    break
                elif event["type"] == "error":
                    stream_err = event.get("message", "stream failed")
                    break
        except Exception as e:
            stream_err = str(e)

        _t_gen_end = time.time()
        gen_elapsed_ms = int((_t_gen_end - _t_gen_start) * 1000)

        if stream_err:
            # cancel state_task to free resources
            state_task.cancel()
            try:
                await state_task
            except (asyncio.CancelledError, Exception):
                pass
            yield {"type": "error", "message": stream_err, "recoverable": False}
            return

        # state_update 完了待ち + 反映
        try:
            state_delta = await state_task
        except Exception:
            state_delta = {}
        if state_delta:
            session_state.apply_delta(state_delta)
            gk_result["state_delta"] = state_delta

        # トークン推定
        def _estimate_tokens(text: str) -> int:
            if not text:
                return 0
            ja_chars = len(re.findall(r'[　-鿿＀-￯]', text))
            en_words = len(re.findall(r'[a-zA-Z]+', text))
            return int(ja_chars * 1.5 + en_words + len(text) * 0.1)

        total_prompt_text = ""
        if provider_debug.get("system_instruction_full"):
            total_prompt_text = (
                provider_debug.get("system_instruction_full", "")
                + provider_debug.get("context_prefix_full", "")
                + provider_debug.get("user_input", "")
            )

        _t_total = time.time() - _t_start

        # debug_info 構築
        debug_info = {
            "timing": {
                "gatekeeper_ms": int((_t_gk_end - _t_gk_start) * 1000),
                "memory_build_ms": int((_t_mem_end - _t_mem_start) * 1000),
                "rag_search_ms": 0,
                "generation_ms": gen_elapsed_ms,
                "ttfb_ms": ttfb_ms["value"] or 0,
                "state_update_ms": state_elapsed_ms["value"],
                "total_ms": int(_t_total * 1000),
            },
            "token_estimate": {
                "prompt": _estimate_tokens(total_prompt_text),
                "response": _estimate_tokens(full_text),
            },
            "gatekeeper": {
                "tier": tier, "enabled": gk_enabled,
                "scores": gk_result.get("llm_scoring"),
                "need": gk_result.get("need"),
                "need_intent": gk_result.get("need_intent"),
                "search_targets": gk_result.get("search_targets"),
                "memory_probe_status": gk_result.get("memory_probe", {}).get("status"),
                "session_state": session_state.to_dict(),
            },
            "rag": {"query": gk_result.get("need"), "results": []},
            "prompt": [], "prompt_full": [],
            "raw_response": full_text,
            "provider": type(provider).__name__,
            "model": model_name,
        }
        if provider_debug.get("system_instruction"):
            debug_info["gemini_system_instruction"] = provider_debug["system_instruction"]
            debug_info["gemini_context_prefix"] = provider_debug.get("context_prefix", "")
            debug_info["gemini_history_count"] = provider_debug.get("history_count", 0)
            debug_info["prompt_full"] = [
                {"role": "system", "content": provider_debug.get("system_instruction_full", "")},
                {"role": "user", "content": provider_debug.get("context_prefix_full", "")},
                {"role": "user", "content": provider_debug.get("user_input", "")},
            ]

        # debug log 保存
        debug_log_payload = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "instance": instance_name,
            "user_input": request.text,
            "assistant_response": full_text,
            "debug_info": debug_info,
            "streaming": True,
        }
        _save_debug_log(instance_dir, debug_log_payload)

        # 会話保存
        memory.save_single_turn(request.text, full_text)
        memory.maintain_memory(brain)

        # done イベント
        yield {
            "type": "done",
            "data": {
                "full_text": full_text,
                "sources": sources,
                "session_state": session_state.to_dict(),
                "debug_info": debug_info,
            },
        }
