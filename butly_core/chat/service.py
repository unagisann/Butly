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
    """モデル名が Gemini かどうかを判定する (旧 API - 後方互換)。

    新コードでは ``model_ref.connection_id == "google"`` を直接判定すること。
    """
    if not model_name:
        return True  # デフォルトは Gemini
    lower = model_name.lower()
    return lower.startswith("gemini") or lower.startswith("models/gemini")


def _uses_google_connection(connection_id: str) -> bool:
    """connection_id が Google (gemini_native protocol) かを判定する。

    Phase 2: model_name prefix 判定の代わりに connection で分岐する。
    Web 検索など Gemini 固有経路のスキップ条件に使う。
    """
    return connection_id == "google"


def _build_prompt_full(
    system_instruction: str,
    context_prefix: str,
    history_msgs: list,
    user_input: str,
) -> list:
    """debug_info.prompt_full を組み立てる。

    Provider 側が history を別管理 (e.g., Gemini Chat API) するため、
    そのままだと debug log に直近会話が映らない。明示的に history を
    展開して挿入する。

    順序: system → context_prefix → history (古→新) → current user_input
    """
    items: list = []
    if system_instruction:
        items.append({"role": "system", "content": system_instruction})
    if context_prefix:
        items.append({"role": "user", "content": context_prefix})
    for m in history_msgs or []:
        role = m.get("role", "user")
        if role in ("model", "assistant"):
            role = "assistant"
        content = m.get("parts", [""])
        text = content[0] if content else ""
        if isinstance(text, dict):
            text = text.get("text", "")
        items.append({"role": role, "content": text})
    if user_input:
        items.append({"role": "user", "content": user_input})
    return items


def _resolve_chat_model_ref(
    instance_config: dict,
    request,
    ai_config_chat: dict,
):
    """ChatService 用の chat ModelRef 解決ロジック。

    優先順位:
      1. request.connection / request.model_name (per-request override)
      2. instance_config.chat (.connection + .model_name)
      3. AI_CONFIG.chat (.connection + .model_name)

    request.model_name だけが指定され connection が省略された場合は、
    指定モデル名から再推定する (instance_config の古い connection を握りつぶす)。
    """
    from butly_core.llm.model_registry import resolve_role_model_ref

    # AI_CONFIG.chat と instance_config.chat をマージ (instance 優先)
    merged: dict = dict(ai_config_chat or {})
    if instance_config and isinstance(instance_config.get("chat"), dict):
        # 浅マージで十分 (provider/model_name は scalar)
        for k, v in instance_config["chat"].items():
            merged[k] = v

    # request 側の override を適用
    requested_model = getattr(request, "model_name", None)
    requested_connection = getattr(request, "connection", None)

    if requested_model:
        merged = dict(merged)
        merged["model_name"] = requested_model
        if requested_connection:
            merged["connection"] = requested_connection
        else:
            # model_name 変更時は古い connection を捨てて推定し直す
            merged.pop("connection", None)
    elif requested_connection:
        merged = dict(merged)
        merged["connection"] = requested_connection

    return resolve_role_model_ref(
        merged,
        fallback_model_name=(
            ai_config_chat.get("model_name") if ai_config_chat else None
        ),
        fallback_connection_id=(
            ai_config_chat.get("connection") if ai_config_chat else None
        ),
    )


def _record_usage_count(
    *,
    memory_blocks: dict,
    brain,
    instance_name: str,
    instance_config: dict,
    context_levels_cfg: dict | None,
    log_prefix: str = "ChatService",
) -> None:
    """
    RAG 経由で Brain に渡されたカードの usage_count をインクリメントする。

    呼び出し条件:
      - Brain 呼び出しが成功した直後 (例外なく応答を得た後)
      - context_levels で rag セクションが "off" でない (off なら Brain にカードが渡っていない)

    multi-instance (readable_instances 横断) 時は candidate に付く `source_instance`
    に従って各インスタンスの DB に振り分けて記録する。
    """
    if not memory_blocks:
        return

    candidates = memory_blocks.get("rag_results_raw") or []
    if not candidates:
        return

    # rag セクションが off なら Brain にカードは渡っていない → カウントしない
    try:
        from butly_core.core.gatekeeper.memory_builder import _resolve_levels

        levels = _resolve_levels(
            context_levels_cfg, instance_config.get("context_order")
        )
        if levels.get("rag", "high") == "off":
            print(f"[{log_prefix}] usage_count: rag=off のため記録スキップ")
            return
    except Exception as _le:
        print(
            f"[{log_prefix}] usage_count: rag level 解決失敗、デフォルトで記録継続: {_le}"
        )

    # source_instance ごとにグルーピング (None → 現在の instance_name)
    by_inst: dict = {}
    for c in candidates:
        cid = c.get("id")
        if not cid:
            continue
        src = c.get("source_instance") or instance_name
        by_inst.setdefault(src, []).append(cid)

    if not by_inst:
        return

    try:
        from butly_core.core.database import ButlyDatabase
        from butly_core.config import SYSTEM_CONFIG as _sc

        dedup_hours = _sc.get("memory", {}).get("count_dedup_hours", 6)

        for src_inst, ids in by_inst.items():
            db_path = brain._get_db_path(src_inst)
            if not db_path.exists():
                print(
                    f"[{log_prefix}] usage_count: DB が存在しないためスキップ ({src_inst})"
                )
                continue
            ButlyDatabase(db_path=str(db_path)).record_card_usage(
                ids, dedup_hours=dedup_hours
            )
    except Exception as _uce:
        print(f"[{log_prefix}] usage_count 更新エラー（応答には影響なし）: {_uce}")


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
                    "tier": "mid",
                    "topic": "",
                    "need": None,
                    "need_intent": None,
                    "search_targets": None,
                    "state_delta": {},
                }
                tier = "mid"

            # 注: StateUpdater は post-response (Step 6 で generate と並列実行) で動かす
        else:
            # Gatekeeper 無効時: RAG ON なら need を設定、OFF なら mid
            use_rag = instance_config.get("brain", {}).get("use_rag", True)
            tier = "mid"
            gk_result = {
                "tier": tier,
                "topic": "",
                "need": "rag_search" if use_rag else None,
                "need_intent": "past_fact" if use_rag else None,
                "search_targets": None,
                "state_delta": {},
            }
            print(
                f"[ChatService] Gatekeeper disabled — defaulting to {tier} tier (rag={'on' if use_rag else 'off'})"
            )

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
        # ModelRef (connection + model_name) で Provider を決定
        model_ref = _resolve_chat_model_ref(
            instance_config,
            request,
            AI_CONFIG["chat"],
        )
        provider = ProviderFactory.create(model_ref)
        model_name = model_ref.model_name
        connection_id = model_ref.connection_id
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

        # --- 5.5 Web検索（非 Google + 検索ON時） ---
        # Google (Gemini) は SDK 側の grounding tool を使うのでここはスキップ。
        # それ以外の connection は汎用検索モジュールで Web 検索を行う。
        web_search_context = ""
        web_sources = []
        if request.use_web_search and not _uses_google_connection(connection_id):
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
                    web_sources = [
                        {"title": r.title, "url": r.url} for r in search_results
                    ]
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
            print(
                f"[ChatService] Provider: {type(provider).__name__}, attachments={len(request.attachments)}"
            )

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

        # RAG カードの usage_count インクリメント（Brain 成功時のみ到達）
        _record_usage_count(
            memory_blocks=memory_blocks,
            brain=brain,
            instance_name=instance_name,
            instance_config=instance_config,
            context_levels_cfg=context_levels_cfg,
            log_prefix="ChatService",
        )

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
            ja_chars = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", text))
            en_words = len(re.findall(r"[a-zA-Z]+", text))
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
            rag_debug_results = (
                [
                    {
                        "title": r.get("title", ""),
                        "score": r.get("score", 0),
                        "episode": r.get("episode", ""),
                    }
                    for r in rag_raw
                ]
                if rag_raw
                else []
            )

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
            "connection_id": connection_id,
            "model": model_name,
        }

        # Gemini 固有フィールドがあれば追加
        if provider_debug.get("system_instruction"):
            result.debug_info["gemini_system_instruction"] = provider_debug[
                "system_instruction"
            ]
            result.debug_info["gemini_context_prefix"] = provider_debug.get(
                "context_prefix", ""
            )
            result.debug_info["gemini_history_count"] = provider_debug.get(
                "history_count", 0
            )
            result.debug_info["prompt_full"] = _build_prompt_full(
                system_instruction=provider_debug.get("system_instruction_full", ""),
                context_prefix=provider_debug.get("context_prefix_full", ""),
                history_msgs=history_fmt,
                user_input=provider_debug.get("user_input", ""),
            )

        # --- 7. 会話保存 (debug log 保存より先に行い、floating_summary の最新状態を反映) ---
        memory.save_single_turn(request.text, result.text)
        memory.maintain_memory(brain)

        # --- 8. Debug log の自動保存 ---
        debug_log_payload = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "instance": instance_name,
            "user_input": request.text,
            "assistant_response": result.text,
            "debug_info": result.debug_info,
        }
        _save_debug_log(instance_dir, debug_log_payload)

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
                yield {
                    "type": "error",
                    "message": f"[エラー] {error}",
                    "recoverable": False,
                }
                return

        # 1. コンポーネント取得
        components = get_instance_components(instance_name)
        memory = components["memory"]
        brain = components["brain"]
        chronos = components["chronos"]

        # 2. 時刻コンテキスト
        last_ts = memory.get_last_interaction_time()
        sys_note = chronos.get_system_note(
            is_holiday=False, last_interaction_time=last_ts
        )
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
                    "tier": "mid",
                    "topic": "",
                    "need": None,
                    "need_intent": None,
                    "search_targets": None,
                    "state_delta": {},
                }
                tier = "mid"
        else:
            use_rag = instance_config.get("brain", {}).get("use_rag", True)
            tier = "mid"
            gk_result = {
                "tier": tier,
                "topic": "",
                "need": "rag_search" if use_rag else None,
                "need_intent": "past_fact" if use_rag else None,
                "search_targets": None,
                "state_delta": {},
            }

        _t_gk_end = time.time()
        session_state.increment_turn(tier, history_msgs=history_fmt)

        # 5. 記憶ブロック
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

        # Provider (ModelRef ルート: connection + model_name)
        model_ref = _resolve_chat_model_ref(
            instance_config,
            request,
            AI_CONFIG["chat"],
        )
        provider = ProviderFactory.create(model_ref)
        model_name = model_ref.model_name
        connection_id = model_ref.connection_id
        has_attachments = bool(request.attachments)

        if has_attachments and not provider.supports_vision(model_name):
            yield {
                "type": "error",
                "message": "[エラー] 選択中のモデルは画像入力に対応していません",
                "recoverable": False,
            }
            return

        # Web search (非 Google connection のとき汎用検索を実行)
        web_search_context = ""
        web_sources = []
        if request.use_web_search and not _uses_google_connection(connection_id):
            from butly_core.search import create_search_provider

            search_provider = create_search_provider(chat_model=model_name)
            if search_provider.is_available():
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
                    web_sources = [
                        {"title": r.title, "url": r.url} for r in search_results
                    ]
        if web_search_context:
            memory_blocks["web_search_context"] = web_search_context

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
            "rag_results": [],
            "use_rag": request.use_rag,
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
                    user_input=request.text,
                    history_msgs=history_fmt,
                    session_state=session_state.to_dict(),
                    override_config=instance_config,
                    instance_dir=instance_dir,
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

        # RAG カードの usage_count インクリメント（Stream 成功時のみ到達）
        _record_usage_count(
            memory_blocks=memory_blocks,
            brain=brain,
            instance_name=instance_name,
            instance_config=instance_config,
            context_levels_cfg=context_levels_cfg,
            log_prefix="ChatService.stream",
        )

        # トークン推定
        def _estimate_tokens(text: str) -> int:
            if not text:
                return 0
            ja_chars = len(re.findall(r"[　-鿿＀-￯]", text))
            en_words = len(re.findall(r"[a-zA-Z]+", text))
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
                "tier": tier,
                "enabled": gk_enabled,
                "scores": gk_result.get("llm_scoring"),
                "need": gk_result.get("need"),
                "need_intent": gk_result.get("need_intent"),
                "search_targets": gk_result.get("search_targets"),
                "memory_probe_status": gk_result.get("memory_probe", {}).get("status"),
                "session_state": session_state.to_dict(),
            },
            "rag": {"query": gk_result.get("need"), "results": []},
            "prompt": [],
            "prompt_full": [],
            "raw_response": full_text,
            "provider": type(provider).__name__,
            "connection_id": connection_id,
            "model": model_name,
        }
        if provider_debug.get("system_instruction"):
            debug_info["gemini_system_instruction"] = provider_debug[
                "system_instruction"
            ]
            debug_info["gemini_context_prefix"] = provider_debug.get(
                "context_prefix", ""
            )
            debug_info["gemini_history_count"] = provider_debug.get("history_count", 0)
            debug_info["prompt_full"] = _build_prompt_full(
                system_instruction=provider_debug.get("system_instruction_full", ""),
                context_prefix=provider_debug.get("context_prefix_full", ""),
                history_msgs=history_fmt,
                user_input=provider_debug.get("user_input", ""),
            )

        # 会話保存 (debug log より先に行い、floating_summary の最新状態を反映)
        memory.save_single_turn(request.text, full_text)
        memory.maintain_memory(brain)

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
