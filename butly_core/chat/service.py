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
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from butly_core.chat.types import (
    ChatRequest,
    ChatResponse,
    validate_attachments,
)
from butly_core.llm.factory import ProviderFactory
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


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


def _aggregate_turn_token_usage():
    """このターンで trace collector に記録された全 LLM 呼び出しの usage 合算。

    classifier / state_updater / embedding / keyword_extract / chat_generate を
    横断したコスト指標。trace 収集が無効（record_llm_call が no-op）のターンや
    usage 非対応 provider のみの場合は None。
    """
    from butly_core.trace.collector import aggregate_token_usage, get_collected

    return aggregate_token_usage(get_collected())


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


def _prompt_content_to_text(content: Any) -> str:
    """Provider debug の message content からテキスト部分だけを取り出す。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "\n".join(
            text
            for item in content
            if (text := _prompt_content_to_text(item))
        )
    return ""


def _collect_prompt_text(provider_debug: dict) -> str:
    """Provider が実際に組み立てた最終プロンプトのテキストを回収する。"""
    messages = provider_debug.get("messages_full")
    if isinstance(messages, list) and messages:
        return "\n".join(
            text
            for message in messages
            if isinstance(message, dict)
            and (text := _prompt_content_to_text(message.get("content")))
        )

    return "\n".join(
        text
        for key in (
            "system_instruction_full",
            "context_prefix_full",
            "user_input",
        )
        if (text := provider_debug.get(key)) and isinstance(text, str)
    )


def _resolve_rag_level(
    context_levels_cfg: Optional[dict],
    instance_config: Optional[dict],
) -> str:
    """実際の context 設定から RAG セクションの描画レベルを解決する。"""
    try:
        from butly_core.core.gatekeeper.memory_builder import _resolve_levels

        levels = _resolve_levels(
            context_levels_cfg,
            (instance_config or {}).get("context_order"),
        )
        return str(levels.get("rag", "high"))
    except Exception as e:
        print(f"[ChatService] RAG context level の観測に失敗、high 扱い: {e}")
        return "high"


def _build_active_node_trace(
    memory_blocks: Optional[dict],
    prompt_text: str,
    *,
    rag_level: str,
) -> dict:
    """active node の検索結果と最終プロンプトへの注入事実を記録する。"""
    blocks = memory_blocks or {}
    lookup = blocks.get("active_node_lookup") or {}
    active_nodes = blocks.get("active_nodes") or []
    prompt_observed = bool(prompt_text)
    can_render = bool(blocks.get("rag_context")) and rag_level not in ("off", "low")

    observed_nodes = []
    for index, node in enumerate(active_nodes):
        statement = str(node.get("statement") or "").strip().replace("\n", " ")
        confidence = node.get("confidence")
        confidence_suffix = (
            f" (conf={confidence:.2f})"
            if isinstance(confidence, (int, float))
            else ""
        )
        render_candidate = can_render and index < 5 and bool(statement)
        prompt_included = (
            (statement + confidence_suffix) in prompt_text
            if prompt_observed and render_candidate
            else None
        )
        observed_nodes.append(
            {
                "id": node.get("id"),
                "kind": node.get("kind"),
                "topic": node.get("topic"),
                "statement": statement,
                "confidence": confidence,
                "source_instance": node.get("source_instance"),
                "matched_card_ids": node.get("matched_card_ids") or [],
                "render_candidate": render_candidate,
                "prompt_included": prompt_included,
            }
        )

    render_candidates = [
        node for node in observed_nodes if node["render_candidate"]
    ]
    included_count = sum(
        node["prompt_included"] is True for node in render_candidates
    )
    if not active_nodes:
        injection_status = "no_matches"
    elif not render_candidates:
        injection_status = "context_level_excluded"
    elif not prompt_observed:
        injection_status = "not_observed"
    elif included_count == len(render_candidates):
        injection_status = "confirmed"
    elif included_count:
        injection_status = "partial"
    else:
        injection_status = "missing"

    return {
        "lookup": {
            "enabled": bool(lookup.get("enabled", False)),
            "attempted": bool(lookup.get("attempted", False)),
            "reason": lookup.get("reason"),
            "candidate_count": int(lookup.get("candidate_count") or 0),
            "matched_count": int(lookup.get("matched_count") or 0),
        },
        "rag_level": rag_level,
        "prompt_observed": prompt_observed,
        "eligible_count": len(active_nodes),
        "render_candidate_count": len(render_candidates),
        "prompt_included_count": included_count if prompt_observed else None,
        "injection_status": injection_status,
        "nodes": observed_nodes,
    }


def _build_rag_debug(
    *,
    memory_blocks: Optional[dict],
    gk_result: dict,
    prompt_text: str,
    rag_level: str,
) -> dict:
    """通常応答と stream で共通の RAG 観測情報を組み立てる。"""
    blocks = memory_blocks or {}
    rag_raw = blocks.get("rag_results_raw") or []
    results = [
        {
            "id": result.get("id"),
            "title": result.get("title", ""),
            "score": result.get("score", 0),
            "episode": result.get("episode", ""),
            "source_date": result.get("source_date"),
            "source_instance": result.get("source_instance"),
        }
        for result in rag_raw
    ]
    return {
        "query": gk_result.get("original_query"),
        "original_query": gk_result.get("original_query"),
        "retrieval_query": gk_result.get("retrieval_query"),
        "results": results,
        # 検索の実行/候補/注入判定（計画書 §3.7）。注入されなかった検索も
        # ここに残るので、search_execution と memory_injection を分けて測れる。
        "retrieval": (gk_result.get("memory_probe") or {}).get("retrieval"),
        "source_mode": blocks.get("rag_source_mode"),
        "raw_reference": blocks.get("rag_raw_reference"),
        "active_nodes": _build_active_node_trace(
            blocks,
            prompt_text,
            rag_level=rag_level,
        ),
    }


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
        # 浅マージで十分 (provider/model_name は scalar)。
        # ただし instance 側が model_name だけを持つ旧形式の場合、AI_CONFIG の
        # connection を引き継ぐと Grok を Gemini に投げるような不整合が起きる。
        # request.model_name と同じく connection を捨てて model_name から再推定する。
        inst_chat = instance_config["chat"]
        if inst_chat.get("model_name") and not inst_chat.get("connection"):
            merged.pop("connection", None)
        for k, v in inst_chat.items():
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


def _write_rotating_json(
    target_dir: Path,
    payload: dict,
    max_history: int,
    *,
    log_label: str,
) -> None:
    """
    target_dir 以下に latest.json + history/ ローテーションで JSON を書き出す。

    - latest.json: 毎ターン上書き (常に最新)
    - history/{YYYYMMDD_HHMMSS_uuid}.json: ローテーション (max_history 件保持)

    保存失敗は応答に影響させない (warning ログのみ)。デバッグ/トレース telemetry は
    ローテーション付きで再構築可能なため atomic write 対象外 (coding_conventions)。
    """
    try:
        target_dir.mkdir(exist_ok=True)
        history_dir = target_dir / "history"
        history_dir.mkdir(exist_ok=True)

        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

        (target_dir / "latest.json").write_text(text, encoding="utf-8")

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        (history_dir / f"{ts}.json").write_text(text, encoding="utf-8")

        # 古い履歴を削除
        files = sorted(history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-max_history]:
            old.unlink(missing_ok=True)
    except Exception as e:
        print(f"[ChatService] {log_label} 保存エラー (応答には影響なし): {e}")


def _save_debug_log(
    instance_dir: Path,
    payload: dict,
    max_history: int = 20,
) -> None:
    """instance_dir/debug_logs/ 以下にデバッグ情報を保存する (latest + history ローテーション)。"""
    _write_rotating_json(
        instance_dir / "debug_logs", payload, max_history, log_label="debug_log"
    )


def _save_trace(
    instance_dir: Path,
    trace_payload: dict,
    max_history: int = 20,
) -> None:
    """instance_dir/traces/ 以下に trace.json を保存する (latest + history ローテーション)。"""
    _write_rotating_json(
        instance_dir / "traces", trace_payload, max_history, log_label="trace"
    )


def _build_and_save_trace(
    *,
    instance_dir: Path,
    instance_name: str,
    request,
    prepared: "_PreparedChat",
    tier: str,
    gk_result: dict,
    assistant_response: str,
    debug_info: dict,
    session_state,
    provider,
    connection_id: str,
    model_name: str,
    generation_error: Optional[str] = None,
) -> None:
    """1 ターン分の Trace Graph を組み立て trace.json として保存する (issue #51)。

    実行事実 (gatekeeper 出力・記憶ブロック・timing 等) から再構成する best-effort
    処理。``generation_error`` が指定された場合は LLM 生成失敗として保存する
    （失敗時こそ「どこで止まったか」を残すのが目的）。trace の構築/保存失敗は
    応答に影響させない。
    """
    try:
        from butly_core.settings import get_settings

        trace_cfg = get_settings().system.trace or {}
        if not trace_cfg.get("enabled", True):
            return
    except Exception as e:
        print(f"[ChatService] trace 設定読み込みエラー、既定 (enabled) で継続: {e}")

    try:
        from butly_core.trace import build_chat_trace
        from butly_core.trace.collector import get_collected

        session_dict = session_state.to_dict() if session_state else {}
        trace = build_chat_trace(
            instance_name=instance_name,
            user_input=request.text,
            assistant_response=assistant_response,
            gk_result=gk_result,
            tier=tier,
            memory_blocks=prepared.memory_blocks,
            gk_enabled=prepared.gk_enabled,
            gk_error=prepared.gk_error,
            web_search_status=prepared.web_search_status,
            web_search_count=prepared.web_search_count,
            connection_id=connection_id,
            model_name=model_name,
            provider_name=type(provider).__name__,
            has_attachments=prepared.has_attachments,
            timing=debug_info.get("timing"),
            token_estimate=debug_info.get("token_estimate"),
            active_node_trace=(
                (debug_info.get("rag") or {}).get("active_nodes")
                if isinstance(debug_info.get("rag"), dict)
                else None
            ),
            generation_error=generation_error,
            llm_calls=get_collected(),
            turn_id=session_dict.get("turn_count"),
            source=getattr(request, "source", "web"),
            created_at=datetime.datetime.now().isoformat(timespec="seconds"),
        )
        _save_trace(instance_dir, trace.to_json_dict())
    except Exception as e:
        print(f"[ChatService] trace 構築/保存エラー (応答には影響なし): {e}")


def _attachment_summaries(request: ChatRequest) -> List[Dict[str, Any]]:
    """Persist display metadata only; never retain attachment base64 data."""
    summaries: List[Dict[str, Any]] = []
    for attachment in request.attachments:
        encoded = attachment.data_base64 or ""
        padding = len(encoded) - len(encoded.rstrip("="))
        size_bytes = max(0, (len(encoded) * 3) // 4 - padding)
        summaries.append(
            {
                "kind": attachment.kind,
                "mime_type": attachment.mime_type,
                "name": attachment.name[:255] if attachment.name else None,
                "size_bytes": size_bytes,
            }
        )
    return summaries


def _assistant_turn_meta(sources: Any) -> Optional[Dict[str, Any]]:
    """Normalize public citation fields for history reload."""
    normalized = []
    if isinstance(sources, list):
        for source in sources[:50]:
            if not isinstance(source, dict):
                continue
            title = source.get("title")
            url = source.get("url") or source.get("uri")
            normalized.append(
                {
                    "title": title[:500] if isinstance(title, str) else "",
                    "url": url[:4096] if isinstance(url, str) else "",
                }
            )
    return {"sources": normalized} if normalized else None


def _build_turn_meta(request: ChatRequest) -> Optional[Dict[str, Any]]:
    """書き込み時の話者帰属メタを組み立てる (group_context_lanes_plan §2.5)。

    person_id / lane / source / channel_key と、base64 を含まない添付要約を
    ``save_single_turn`` まで貫通させる。外部帰属も添付もないリクエスト
    (従来 Web UI 等) は None を返し、従来形式を保つ。
    """
    person_id = getattr(request, "person_id", None)
    if not person_id and getattr(request, "external_user_id", None):
        if getattr(request, "source", None) == "line":
            # LINE は現行 1:1 スコープ。Runtime を経由しないテスト/呼び出しでも
            # 未登録 LINE ユーザーを別人扱いせず owner として保存する。
            from butly_core.external.person_registry import OWNER_FALLBACK_PERSON_ID

            person_id = OWNER_FALLBACK_PERSON_ID
        else:
            # runtime での解決が無くても、決定的な仮 ID で帰属だけは確保する
            from butly_core.external.person_registry import provisional_person_id

            person_id = provisional_person_id(request.source, request.external_user_id)
    meta: Dict[str, Any] = {}
    if person_id:
        meta.update(
            {
                "person_id": person_id,
                "lane": getattr(request, "lane", None) or "direct",
                "source": request.source,
            }
        )
        display_name = getattr(request, "external_display_name", None)
        if display_name:
            meta["display_name"] = display_name
        channel_id = getattr(request, "external_channel_id", None)
        if channel_id:
            guild_id = getattr(request, "external_guild_id", None)
            meta["channel_key"] = (
                f"{guild_id}:{channel_id}" if guild_id else channel_id
            )
    attachments = _attachment_summaries(request)
    if attachments:
        meta["attachments"] = attachments
    return meta or None


def _build_history_fmt(history: list) -> list:
    """memory.load_recent_sessions() の生履歴を Gatekeeper / Provider 用に整形する。

    parts[0] が dict ({"text": ...}) の場合は text を取り出して平坦化する。
    """
    history_fmt = []
    for m in history:
        content = m.get("parts", [""])[0]
        if isinstance(content, dict):
            content = content.get("text", "")
        history_fmt.append({"role": m.get("role"), "parts": [content]})
    return history_fmt


@dataclass
class _PreparedChat:
    """execute() / execute_stream() が共有するチャット前処理の結果。

    ``error`` が None でない場合は前処理段階でエラーが発生している:
      - ``session_state is None`` → 添付バリデーションエラー (Gatekeeper 未実行)
      - ``session_state`` 設定済み → vision 非対応エラー (tier / gk_result も有効)
    呼び出し側はこの区別に従い、各々の形式 (ChatResponse or error event) で返す。
    """

    error: Optional[str] = None

    memory: Any = None
    brain: Any = None
    full_prompt: str = ""
    instance_config: Optional[dict] = None
    history_fmt: Optional[list] = None
    instance_dir: Optional[Path] = None
    session_state: Any = None
    gk_enabled: bool = True
    gk_result: Optional[dict] = None
    tier: str = "mid"
    memory_blocks: Optional[dict] = None
    provider: Any = None
    model_name: str = ""
    connection_id: str = ""
    has_attachments: bool = False
    web_sources: List[Dict[str, Any]] = field(default_factory=list)
    context: Optional[dict] = None
    context_levels_cfg: Any = None

    # Trace Graph (issue #51) 用の状態マーカー。
    # gk_error: Gatekeeper が例外でフォールバックした場合の理由。
    # web_search_status: disabled / native_google / active / no_results / unavailable。
    gk_error: Optional[str] = None
    web_search_status: str = ""
    web_search_count: int = 0

    # タイミング計測マーカー (debug_info.timing 用)
    t_gk_start: float = 0.0
    t_gk_end: float = 0.0
    t_mem_start: float = 0.0
    t_mem_end: float = 0.0


async def _prepare_chat_context(
    *,
    request: ChatRequest,
    get_instance_components,
    instance_manager,
    instances_dir: Path,
    gatekeeper,
    mem_block_builder,
    ai_config_chat: dict,
    log_prefix: str = "ChatService",
) -> _PreparedChat:
    """execute() / execute_stream() 共通の前処理。

    添付バリデーション → コンポーネント取得 → 時刻コンテキスト → Gatekeeper 分類
    → 記憶ブロック構築 → Provider 選択 / vision チェック → Web 検索 → context 構築
    までを行い、生成直前の状態をまとめた ``_PreparedChat`` を返す。

    振る舞いは execute() / execute_stream() の従来処理と等価。生成・状態更新・
    保存・debug_info 構築は各メソッド側に残す。
    """
    from butly_core.core.gatekeeper import SessionState

    instance_name = request.instance_name

    # --- 添付バリデーション ---
    if request.attachments:
        error = validate_attachments(request.attachments)
        if error:
            return _PreparedChat(error=f"[エラー] {error}")

    # --- 1. コンポーネント取得 ---
    components = get_instance_components(instance_name)
    memory = components["memory"]
    brain = components["brain"]
    # --- 2. インスタンス設定 ---
    instance_config = instance_manager.get_instance_config(instance_name)

    # --- 3. ユーザー入力 ---
    # 現在時刻は context_prefix 側に集約し、質問直前には重複注入しない。
    full_prompt = request.text

    # 外部入口（Discord 等）の reply profile による生成時 style hint。
    # request.metadata に "style_hint" がある場合のみ full_prompt 先頭に注入する。
    # - 記憶本文（request.text）には混ぜないため、save_single_turn の保存内容は不変。
    # - Web 経路は metadata=None なので従来どおり（no-op）。
    request_metadata = getattr(request, "metadata", None)
    style_hint = (
        request_metadata.get("style_hint")
        if isinstance(request_metadata, dict)
        else None
    )
    if style_hint:
        full_prompt = f"[応答スタイル指示: {style_hint}]\n\n{request.text}"

    # --- 4. Gatekeeper 分類 ---
    history, _ = memory.load_recent_sessions(limit=6)
    history_fmt = _build_history_fmt(history)

    instance_dir = instances_dir / instance_name
    session_state = SessionState(instance_dir)

    gk_enabled = instance_config.get("gatekeeper", {}).get("enabled", True)

    t_gk_start = time.time()
    gk_error: Optional[str] = None

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
            print(f"[{log_prefix}] Gatekeeper エラー、フォールバック: {e}")
            gk_error = str(e)
            gk_result = {
                "tier": "mid",
                "topic": "",
                "need": None,
                "need_intent": None,
                "search_targets": None,
                "state_delta": {},
            }
            tier = "mid"
        # 注: StateUpdater は post-response で動かす (各メソッド側)
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
            f"[{log_prefix}] Gatekeeper disabled — defaulting to {tier} tier "
            f"(rag={'on' if use_rag else 'off'})"
        )

    t_gk_end = time.time()

    # --- 5. 記憶ブロック構築 ---
    use_rag = instance_config.get("brain", {}).get("use_rag", True)

    t_mem_start = time.time()
    memory_blocks = mem_block_builder.build(
        tier=tier,
        memory_manager=memory,
        brain=brain if (gk_result.get("need") and use_rag) else None,
        user_input=request.text,
        instance_name=instance_name,
        override_config=instance_config,
        gatekeeper_output=gk_result,
    )
    t_mem_end = time.time()

    # --- 6. Provider 選択 (ModelRef: connection + model_name) ---
    model_ref = _resolve_chat_model_ref(instance_config, request, ai_config_chat)
    provider = ProviderFactory.create(model_ref)
    model_name = model_ref.model_name
    connection_id = model_ref.connection_id
    has_attachments = bool(request.attachments)

    # vision 非対応チェック
    if has_attachments and not provider.supports_vision(model_name):
        return _PreparedChat(
            error="[エラー] 選択中のモデルは画像入力に対応していません",
            tier=tier,
            gk_result=gk_result,
            session_state=session_state,
        )

    # RAG は Gatekeeper → MemoryBlockBuilder で一元管理
    if memory_blocks and memory_blocks.get("rag_context"):
        print(f"[{log_prefix}] RAG: Gatekeeper 経由の RAG コンテキストを使用")
    else:
        print(f"[{log_prefix}] RAG: なし（Gatekeeper 判断によりスキップ）")

    # --- Web 検索 (非 Google connection + 検索 ON 時) ---
    web_search_context = ""
    web_sources: List[Dict[str, Any]] = []
    web_search_count = 0
    web_search_status = ""
    if not request.use_web_search:
        web_search_status = "disabled"
    elif _uses_google_connection(connection_id):
        web_search_status = "native_google"
    else:
        from butly_core.search import create_search_provider

        search_provider = create_search_provider(chat_model=model_name)
        if search_provider.is_available():
            print(f"[{log_prefix}] Web Search: 汎用検索モジュールで検索実行")
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
                web_search_count = len(search_results)
                web_search_status = "active"
                print(f"[{log_prefix}] Web Search: {len(search_results)} 件取得")
            else:
                web_search_status = "no_results"
                print(f"[{log_prefix}] Web Search: 結果なし")
        else:
            web_search_status = "unavailable"
            print(f"[{log_prefix}] Web Search: API キー未設定のためスキップ")

    if web_search_context:
        memory_blocks["web_search_context"] = web_search_context

    # --- context_levels 取得 (後方互換: 旧 context_order のみの場合は変換) ---
    context_levels_cfg = instance_config.get("context_levels")
    if context_levels_cfg is None and "context_order" in instance_config:
        from butly_core.core.gatekeeper import migrate_context_order_to_levels

        instance_config = migrate_context_order_to_levels(instance_config)
        context_levels_cfg = instance_config.get("context_levels")

    if has_attachments:
        print(
            f"[{log_prefix}] Provider: {type(provider).__name__}, "
            f"attachments={len(request.attachments)}"
        )

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

    return _PreparedChat(
        memory=memory,
        brain=brain,
        full_prompt=full_prompt,
        instance_config=instance_config,
        history_fmt=history_fmt,
        instance_dir=instance_dir,
        session_state=session_state,
        gk_enabled=gk_enabled,
        gk_result=gk_result,
        tier=tier,
        memory_blocks=memory_blocks,
        provider=provider,
        model_name=model_name,
        connection_id=connection_id,
        has_attachments=has_attachments,
        web_sources=web_sources,
        context=context,
        context_levels_cfg=context_levels_cfg,
        gk_error=gk_error,
        web_search_status=web_search_status,
        web_search_count=web_search_count,
        t_gk_start=t_gk_start,
        t_gk_end=t_gk_end,
        t_mem_start=t_mem_start,
        t_mem_end=t_mem_end,
    )


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

    前処理 (1〜5 + Provider 選択 + Web 検索 + context 構築) は
    ``_prepare_chat_context`` に共通化されており、execute() / execute_stream()
    はそこから生成・保存・debug_info 構築のみを担う。
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
        from butly_core.trace.collector import reset_collection, start_collection

        # Trace Graph (issue #51): ターン全体の LLM 呼び出しを収集する。
        # token reset を finally に置くことで、例外経路でも収集が残留しない。
        _trace_token = start_collection()
        try:
            return await ChatService._execute_impl(
                request=request,
                get_instance_components=get_instance_components,
                instance_manager=instance_manager,
                instances_dir=instances_dir,
                gatekeeper=gatekeeper,
                mem_block_builder=mem_block_builder,
                ws_manager=ws_manager,
            )
        finally:
            reset_collection(_trace_token)

    @staticmethod
    async def _execute_impl(
        request: ChatRequest,
        get_instance_components,
        instance_manager,
        instances_dir: Path,
        gatekeeper,
        mem_block_builder,
        ws_manager=None,
    ) -> ChatResponse:
        """execute() の本体 (LLM 呼び出し収集の内側で実行される)。"""
        from butly_core.config import AI_CONFIG

        _t_start = time.time()

        instance_name = request.instance_name

        # --- 前処理 (共通) ---
        prepared = await _prepare_chat_context(
            request=request,
            get_instance_components=get_instance_components,
            instance_manager=instance_manager,
            instances_dir=instances_dir,
            gatekeeper=gatekeeper,
            mem_block_builder=mem_block_builder,
            ai_config_chat=AI_CONFIG["chat"],
            log_prefix="ChatService",
        )

        # 前処理エラー (添付バリデーション / vision 非対応)
        if prepared.error is not None:
            if prepared.session_state is None:
                # 添付バリデーションエラー (Gatekeeper 未実行)
                return ChatResponse(text=prepared.error)
            # vision 非対応エラー (gatekeeper 情報あり)
            return ChatResponse(
                text=prepared.error,
                tier=prepared.tier,
                need=prepared.gk_result.get("need"),
                search_targets=prepared.gk_result.get("search_targets"),
                session_state=prepared.session_state.to_dict(),
            )

        memory = prepared.memory
        brain = prepared.brain
        full_prompt = prepared.full_prompt
        instance_config = prepared.instance_config
        history_fmt = prepared.history_fmt
        instance_dir = prepared.instance_dir
        session_state = prepared.session_state
        gk_enabled = prepared.gk_enabled
        gk_result = prepared.gk_result
        tier = prepared.tier
        memory_blocks = prepared.memory_blocks
        provider = prepared.provider
        model_name = prepared.model_name
        connection_id = prepared.connection_id
        has_attachments = prepared.has_attachments
        web_sources = prepared.web_sources
        context = prepared.context
        context_levels_cfg = prepared.context_levels_cfg

        # Generate と StateUpdater を並列実行 (post-response の StateUpdater を
        # クリティカルパスから外す)
        gen_elapsed_ms = {"value": 0}
        state_elapsed_ms = {"value": 0}

        async def _run_generate():
            from butly_core.trace.collector import record_llm_call

            t0 = time.time()
            gen_error = None
            res = None
            try:
                res = await run_in_threadpool(
                    provider.generate,
                    text=full_prompt,
                    attachments=request.attachments if has_attachments else [],
                    context=context,
                )
            except Exception as e:
                gen_error = str(e)
                raise
            finally:
                gen_elapsed_ms["value"] = int((time.time() - t0) * 1000)
                _token_usage = (
                    (getattr(res, "debug_info", None) or {}).get("token_usage")
                    if res is not None
                    else None
                )
                record_llm_call(
                    purpose="chat_generate",
                    model=model_name,
                    connection_id=connection_id,
                    duration_ms=gen_elapsed_ms["value"],
                    prompt_chars=len(full_prompt),
                    error=gen_error,
                    metadata=(
                        {"token_usage": _token_usage} if _token_usage else None
                    ),
                )
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

        try:
            result, state_delta = await asyncio.gather(
                _run_generate(),
                _run_state_update(),
            )
        except Exception as gen_err:
            # LLM 生成失敗: error trace を残してから従来どおり例外を伝播する
            # (issue #51: 失敗時こそ「どこで止まったか」を残す)
            _build_and_save_trace(
                instance_dir=instance_dir,
                instance_name=instance_name,
                request=request,
                prepared=prepared,
                tier=tier,
                gk_result=gk_result,
                assistant_response="",
                debug_info={
                    "timing": {
                        "gatekeeper_ms": int(
                            (prepared.t_gk_end - prepared.t_gk_start) * 1000
                        ),
                        "memory_build_ms": int(
                            (prepared.t_mem_end - prepared.t_mem_start) * 1000
                        ),
                        "generation_ms": gen_elapsed_ms["value"],
                        "total_ms": int((time.time() - _t_start) * 1000),
                    },
                    "token_estimate": {},
                },
                session_state=session_state,
                provider=provider,
                connection_id=connection_id,
                model_name=model_name,
                generation_error=str(gen_err),
            )
            raise

        session_state.increment_turn(tier, history_msgs=history_fmt)

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
        total_prompt_text = _collect_prompt_text(provider_debug)
        rag_level = _resolve_rag_level(context_levels_cfg, instance_config)
        rag_debug = _build_rag_debug(
            memory_blocks=memory_blocks,
            gk_result=gk_result,
            prompt_text=total_prompt_text,
            rag_level=rag_level,
        )

        result.debug_info = {
            "timing": {
                "gatekeeper_ms": int((prepared.t_gk_end - prepared.t_gk_start) * 1000),
                "memory_build_ms": int(
                    (prepared.t_mem_end - prepared.t_mem_start) * 1000
                ),
                "rag_search_ms": 0,
                "generation_ms": gen_elapsed_ms["value"],
                "state_update_ms": state_elapsed_ms["value"],
                "total_ms": int(_t_total * 1000),
            },
            "token_estimate": {
                "prompt": _estimate_tokens(total_prompt_text),
                "response": _estimate_tokens(result.text),
            },
            # API 実測トークン数（provider が返した場合のみ。無い場合 None）
            "token_usage": provider_debug.get("token_usage"),
            # このターンの全 LLM 呼び出し合算（classifier / state_updater /
            # embedding / keyword_extract / chat）。trace 収集が有効なときのみ
            "token_usage_total": _aggregate_turn_token_usage(),
            "gatekeeper": {
                "tier": tier,
                "enabled": gk_enabled,
                "scores": gk_result.get("llm_scoring"),
                "need": gk_result.get("need"),
                "need_intent": gk_result.get("need_intent"),
                "retrieval_query": gk_result.get("retrieval_query"),
                "retrieval_query_status": gk_result.get(
                    "retrieval_query_status"
                ),
                "classifier_status": gk_result.get("classifier_status"),
                "fallback_reason": gk_result.get("fallback_reason"),
                "original_need_intent": gk_result.get("original_need_intent"),
                "intent_floor_applied": gk_result.get("intent_floor_applied"),
                "search_targets": gk_result.get("search_targets"),
                "memory_probe_status": gk_result.get("memory_probe", {}).get("status"),
                "memory_probe_layers": gk_result.get("memory_probe", {}).get("layers"),
                "session_state": session_state.to_dict(),
            },
            "rag": rag_debug,
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

        # --- 7. 会話保存 (debug log 保存より先に行い、session_digest の最新状態を反映) ---
        save_kwargs: Dict[str, Any] = {"meta": _build_turn_meta(request)}
        assistant_meta = _assistant_turn_meta(result.sources)
        if assistant_meta:
            save_kwargs["assistant_meta"] = assistant_meta
        memory.save_single_turn(request.text, result.text, **save_kwargs)
        try:
            await run_in_threadpool(memory.maintain_memory, brain)
        except Exception:
            logger.exception(
                "post-commit memory maintenance failed for instance=%s",
                instance_name,
            )

        # --- 8. Debug log の自動保存 ---
        debug_log_payload = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "instance": instance_name,
            "source": getattr(request, "source", "web"),
            "user_input": request.text,
            "assistant_response": result.text,
            "debug_info": result.debug_info,
        }
        try:
            _save_debug_log(instance_dir, debug_log_payload)
        except Exception:
            logger.exception(
                "post-commit debug log failed for instance=%s", instance_name
            )

        # --- 9. Trace Graph 保存 (issue #51) ---
        try:
            _build_and_save_trace(
                instance_dir=instance_dir,
                instance_name=instance_name,
                request=request,
                prepared=prepared,
                tier=tier,
                gk_result=gk_result,
                assistant_response=result.text,
                debug_info=result.debug_info,
                session_state=session_state,
                provider=provider,
                connection_id=connection_id,
                model_name=model_name,
            )
        except Exception:
            logger.exception(
                "post-commit trace save failed for instance=%s", instance_name
            )

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
        from butly_core.trace.collector import reset_collection, start_collection

        # Trace Graph (issue #51): generator 全体を包む位置で収集を開始し、
        # yield をまたいでも finally で必ず reset する。
        _trace_token = start_collection()
        try:
            async for event in ChatService._execute_stream_impl(
                request=request,
                get_instance_components=get_instance_components,
                instance_manager=instance_manager,
                instances_dir=instances_dir,
                gatekeeper=gatekeeper,
                mem_block_builder=mem_block_builder,
            ):
                yield event
        finally:
            reset_collection(_trace_token)

    @staticmethod
    async def _execute_stream_impl(
        request: ChatRequest,
        get_instance_components,
        instance_manager,
        instances_dir: Path,
        gatekeeper,
        mem_block_builder,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """execute_stream() の本体 (LLM 呼び出し収集の内側で実行される)。"""
        from butly_core.config import AI_CONFIG

        _t_start = time.time()
        instance_name = request.instance_name

        # --- 前処理 (共通) ---
        prepared = await _prepare_chat_context(
            request=request,
            get_instance_components=get_instance_components,
            instance_manager=instance_manager,
            instances_dir=instances_dir,
            gatekeeper=gatekeeper,
            mem_block_builder=mem_block_builder,
            ai_config_chat=AI_CONFIG["chat"],
            log_prefix="ChatService.stream",
        )

        # 前処理エラー (添付バリデーション / vision 非対応) は error イベントで返す
        if prepared.error is not None:
            yield {
                "type": "error",
                "message": prepared.error,
                "recoverable": False,
            }
            return

        memory = prepared.memory
        brain = prepared.brain
        full_prompt = prepared.full_prompt
        instance_config = prepared.instance_config
        history_fmt = prepared.history_fmt
        instance_dir = prepared.instance_dir
        session_state = prepared.session_state
        gk_enabled = prepared.gk_enabled
        gk_result = prepared.gk_result
        tier = prepared.tier
        memory_blocks = prepared.memory_blocks
        provider = prepared.provider
        model_name = prepared.model_name
        connection_id = prepared.connection_id
        has_attachments = prepared.has_attachments
        web_sources = prepared.web_sources
        context = prepared.context
        context_levels_cfg = prepared.context_levels_cfg

        # metadata イベント (stream 開始前に Gatekeeper 情報を即送信)
        yield {
            "type": "metadata",
            "data": {
                "tier": tier,
                "need": gk_result.get("need"),
                "need_intent": gk_result.get("need_intent"),
                "retrieval_query": gk_result.get("retrieval_query"),
                "retrieval_query_status": gk_result.get(
                    "retrieval_query_status"
                ),
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
        stream_recoverable = True

        provider_stream = provider.async_generate_stream(
                text=full_prompt,
                attachments=request.attachments if has_attachments else [],
                context=context,
        )
        try:
            try:
                async for event in provider_stream:
                    if event["type"] == "chunk":
                        if ttfb_ms["value"] is None:
                            ttfb_ms["value"] = int(
                                (time.time() - _t_gen_start) * 1000
                            )
                        full_text += event["text"]
                        yield {"type": "chunk", "text": event["text"]}
                    elif event["type"] == "done":
                        full_text = event.get("full_text", full_text)
                        provider_debug = event.get("debug", {})
                        sources.extend(event.get("sources", []) or [])
                        break
                    elif event["type"] == "error":
                        stream_err = event.get("message", "stream failed")
                        stream_recoverable = bool(
                            event.get("recoverable", True)
                        )
                        break
            except Exception as e:
                stream_err = str(e)
                stream_recoverable = True
            finally:
                try:
                    await provider_stream.aclose()
                except Exception:
                    logger.debug("provider stream cleanup failed", exc_info=True)
        except asyncio.CancelledError:
            state_task.cancel()
            try:
                await state_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("state task cancellation failed", exc_info=True)
            raise

        _t_gen_end = time.time()
        gen_elapsed_ms = int((_t_gen_end - _t_gen_start) * 1000)

        # Trace Graph 用: main 生成の呼び出し記録 (成功/失敗どちらも)
        from butly_core.trace.collector import record_llm_call

        record_llm_call(
            purpose="chat_generate",
            model=model_name,
            connection_id=connection_id,
            duration_ms=gen_elapsed_ms,
            prompt_chars=len(full_prompt),
            error=stream_err,
        )

        if stream_err:
            # cancel state_task to free resources
            state_task.cancel()
            try:
                await state_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("state task cancellation failed", exc_info=True)
            # error trace を残す (issue #51: どこで失敗したか追えるように)
            _build_and_save_trace(
                instance_dir=instance_dir,
                instance_name=instance_name,
                request=request,
                prepared=prepared,
                tier=tier,
                gk_result=gk_result,
                assistant_response="",
                debug_info={
                    "timing": {
                        "gatekeeper_ms": int(
                            (prepared.t_gk_end - prepared.t_gk_start) * 1000
                        ),
                        "memory_build_ms": int(
                            (prepared.t_mem_end - prepared.t_mem_start) * 1000
                        ),
                        "generation_ms": gen_elapsed_ms,
                        "ttfb_ms": ttfb_ms["value"] or 0,
                        "total_ms": int((time.time() - _t_start) * 1000),
                    },
                    "token_estimate": {},
                },
                session_state=session_state,
                provider=provider,
                connection_id=connection_id,
                model_name=model_name,
                generation_error=stream_err,
            )
            yield {
                "type": "error",
                "message": stream_err,
                "recoverable": stream_recoverable,
            }
            return

        # state_update 完了待ち + 反映
        try:
            state_delta = await state_task
        except asyncio.CancelledError:
            state_task.cancel()
            raise
        except Exception:
            logger.debug("state task failed", exc_info=True)
            state_delta = {}

        # Provider/state generation is complete. From this point onward
        # session/history/usage persistence is one non-cancellable finalization
        # phase. The API registry observes this barrier before resuming the
        # generator, so no durable side effect can be exposed as retryable.
        yield {"type": "finalizing"}

        session_state.increment_turn(tier, history_msgs=history_fmt)
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

        total_prompt_text = _collect_prompt_text(provider_debug)
        rag_level = _resolve_rag_level(context_levels_cfg, instance_config)
        rag_debug = _build_rag_debug(
            memory_blocks=memory_blocks,
            gk_result=gk_result,
            prompt_text=total_prompt_text,
            rag_level=rag_level,
        )

        _t_total = time.time() - _t_start

        # debug_info 構築
        debug_info = {
            "timing": {
                "gatekeeper_ms": int((prepared.t_gk_end - prepared.t_gk_start) * 1000),
                "memory_build_ms": int(
                    (prepared.t_mem_end - prepared.t_mem_start) * 1000
                ),
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
                "retrieval_query": gk_result.get("retrieval_query"),
                "retrieval_query_status": gk_result.get(
                    "retrieval_query_status"
                ),
                "classifier_status": gk_result.get("classifier_status"),
                "fallback_reason": gk_result.get("fallback_reason"),
                "original_need_intent": gk_result.get("original_need_intent"),
                "intent_floor_applied": gk_result.get("intent_floor_applied"),
                "search_targets": gk_result.get("search_targets"),
                "memory_probe_status": gk_result.get("memory_probe", {}).get("status"),
                "session_state": session_state.to_dict(),
            },
            "rag": rag_debug,
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

        # 会話保存 (debug log より先に行い、session_digest の最新状態を反映)
        save_kwargs: Dict[str, Any] = {"meta": _build_turn_meta(request)}
        assistant_meta = _assistant_turn_meta(sources)
        if assistant_meta:
            save_kwargs["assistant_meta"] = assistant_meta
        memory.save_single_turn(request.text, full_text, **save_kwargs)
        try:
            # Consolidation can perform slow synchronous I/O/LLM work. Keep the
            # API event loop responsive while this non-cancellable post-commit
            # finalization finishes.
            await run_in_threadpool(memory.maintain_memory, brain)
        except Exception:
            logger.exception(
                "post-commit memory maintenance failed for instance=%s",
                instance_name,
            )

        # debug log 保存
        debug_log_payload = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "instance": instance_name,
            "source": getattr(request, "source", "web"),
            "user_input": request.text,
            "assistant_response": full_text,
            "debug_info": debug_info,
            "streaming": True,
        }
        try:
            _save_debug_log(instance_dir, debug_log_payload)
        except Exception:
            logger.exception(
                "post-commit debug log failed for instance=%s", instance_name
            )

        # Trace Graph 保存 (issue #51)
        try:
            _build_and_save_trace(
                instance_dir=instance_dir,
                instance_name=instance_name,
                request=request,
                prepared=prepared,
                tier=tier,
                gk_result=gk_result,
                assistant_response=full_text,
                debug_info=debug_info,
                session_state=session_state,
                provider=provider,
                connection_id=connection_id,
                model_name=model_name,
            )
        except Exception:
            logger.exception(
                "post-commit trace save failed for instance=%s", instance_name
            )

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
