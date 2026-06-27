"""
builder.py
----------
ChatService の実行結果（Gatekeeper 出力・記憶ブロック・Provider 情報・timing 等）
から TraceGraph を組み立てる。

設計方針:
  - 純粋関数。ChatService が収集済みの「実行事実」だけを入力に取り、副作用を持たない。
    並列実行される生成フローへ span logger を差し込まず、確定済みの状態から再構成する
    ことで P0 のチャット経路を変更しない。
  - 入力が欠けていても落ちない（デバッグ用途のため best-effort）。
"""

from typing import Any, Dict, List, Optional

from butly_core.trace.types import (
    TraceEdge,
    TraceGraph,
    TraceNode,
    TraceStatus,
    summarize_text,
)


def _edge_status(node_status: TraceStatus) -> TraceStatus:
    """ノードの状態をエッジの線種用ステータスへ写す。

    warning は「通った上での注意」なのでエッジ上は active 扱いにする。
    """
    return "active" if node_status == "warning" else node_status


def _web_search_node_status(web_search_status: str) -> TraceStatus:
    """``_prepare_chat_context`` が記録した web 検索の結果文字列をノード状態へ。"""
    mapping: Dict[str, TraceStatus] = {
        "active": "active",
        "no_results": "warning",
        "unavailable": "warning",
        "disabled": "skipped",
        "native_google": "skipped",
        "": "skipped",
    }
    return mapping.get(web_search_status, "skipped")


def _web_search_reason(web_search_status: str, count: int) -> str:
    reasons = {
        "active": f"{count} 件取得",
        "no_results": "検索したが結果なし",
        "unavailable": "検索 API キー未設定",
        "disabled": "Web 検索 OFF",
        "native_google": "Gemini ネイティブ検索のため汎用検索は不使用",
        "": "対象外",
    }
    return reasons.get(web_search_status, "対象外")


def build_chat_trace(
    *,
    instance_name: str,
    user_input: str,
    assistant_response: str,
    gk_result: Dict[str, Any],
    tier: str,
    memory_blocks: Optional[Dict[str, Any]] = None,
    gk_enabled: bool = True,
    gk_error: Optional[str] = None,
    web_search_status: str = "",
    web_search_count: int = 0,
    connection_id: str = "",
    model_name: str = "",
    provider_name: str = "",
    has_attachments: bool = False,
    timing: Optional[Dict[str, Any]] = None,
    token_estimate: Optional[Dict[str, Any]] = None,
    generation_error: Optional[str] = None,
    turn_id: Optional[int] = None,
    source: str = "web",
    created_at: Optional[str] = None,
) -> TraceGraph:
    """1 ターン分の回答生成フローを TraceGraph として組み立てる。"""
    gk_result = gk_result or {}
    memory_blocks = memory_blocks or {}
    timing = timing or {}
    token_estimate = token_estimate or {}

    nodes: List[TraceNode] = []
    edges: List[TraceEdge] = []

    # --- user_message ---
    nodes.append(
        TraceNode(
            id="user_message",
            label="User Message",
            type="input",
            status="active",
            summary=summarize_text(user_input),
            metadata={
                "has_attachments": has_attachments,
                "source": source,
            },
        )
    )

    # --- gatekeeper ---
    if not gk_enabled:
        gk_status: TraceStatus = "skipped"
    elif gk_error:
        gk_status = "fallback"
    else:
        gk_status = "active"
    need = gk_result.get("need")
    nodes.append(
        TraceNode(
            id="gatekeeper",
            label="Gatekeeper",
            type="decision",
            status=gk_status,
            summary=f"tier={tier}" + (f", need={need}" if need else ", need=null"),
            metadata={
                "enabled": gk_enabled,
                "tier": tier,
                "need": need,
                "need_intent": gk_result.get("need_intent"),
                "scores": gk_result.get("llm_scoring"),
                "search_targets": gk_result.get("search_targets"),
                **({"error": gk_error} if gk_error else {}),
            },
        )
    )
    edges.append(TraceEdge(source="user_message", target="gatekeeper"))

    # --- memory_probe (Gatekeeper 内の事実ベース記憶検索) ---
    probe = gk_result.get("memory_probe") or {}
    probe_status_raw = probe.get("status")
    if not gk_enabled:
        probe_status: TraceStatus = "skipped"
    elif probe_status_raw in ("hit", "deep_search", "no_hit"):
        probe_status = "active"
    elif probe_status_raw == "skipped":
        probe_status = "skipped"
    else:
        probe_status = "active" if probe else "skipped"
    nodes.append(
        TraceNode(
            id="memory_probe",
            label="Memory Probe",
            type="retrieval",
            status=probe_status,
            summary=(f"probe={probe_status_raw}" if probe_status_raw else "未実行"),
            metadata={
                "status": probe_status_raw,
                "layers": probe.get("layers"),
                "candidate_count": len(probe.get("candidates") or []),
                "glossary_hit_count": len(probe.get("glossary_hits") or []),
            },
        )
    )
    edges.append(
        TraceEdge(
            source="gatekeeper",
            target="memory_probe",
            status=_edge_status(probe_status),
        )
    )

    # --- rag (RAG 検索結果の注入有無) ---
    rag_context = memory_blocks.get("rag_context")
    rag_raw = memory_blocks.get("rag_results_raw") or []
    if rag_context:
        rag_status: TraceStatus = "active"
        rag_summary = f"{len(rag_raw)} 件注入"
        rag_meta: Dict[str, Any] = {
            "query": need,
            "result_count": len(rag_raw),
            "results": [
                {
                    "title": summarize_text(r.get("title", ""), 40),
                    "score": r.get("score"),
                }
                for r in rag_raw[:5]
            ],
        }
    else:
        rag_status = "skipped"
        rag_summary = "need=null（長期記憶不要）" if not need else "候補なし/閾値未達"
        rag_meta = {"reason": rag_summary, "query": need}
    nodes.append(
        TraceNode(
            id="rag",
            label="RAG Search",
            type="retrieval",
            status=rag_status,
            summary=rag_summary,
            metadata=rag_meta,
        )
    )
    edges.append(
        TraceEdge(source="memory_probe", target="rag", status=_edge_status(rag_status))
    )

    # --- web_search ---
    web_status = _web_search_node_status(web_search_status)
    nodes.append(
        TraceNode(
            id="web_search",
            label="Web Search",
            type="tool",
            status=web_status,
            summary=_web_search_reason(web_search_status, web_search_count),
            metadata={
                "status": web_search_status,
                "result_count": web_search_count,
            },
        )
    )

    # --- context_assembly ---
    section_present = {
        "short_term": bool(memory_blocks.get("short_term")),
        "session_digest": bool(memory_blocks.get("session_digest")),
        "mid_term": bool(
            memory_blocks.get("mid_term") or memory_blocks.get("mid_term_digest")
        ),
        "glossary": bool(memory_blocks.get("glossary_hits")),
        "rag": bool(rag_context),
        "web_search": bool(memory_blocks.get("web_search_context")),
    }
    included = [k for k, v in section_present.items() if v]
    excluded = [k for k, v in section_present.items() if not v]
    nodes.append(
        TraceNode(
            id="context_assembly",
            label="Context Assembly",
            type="context",
            status="active",
            summary="included: " + (", ".join(included) if included else "(none)"),
            metadata={
                "tier": tier,
                "included": included,
                "excluded": excluded,
                "mid_term_mode": memory_blocks.get("mid_term_mode"),
            },
        )
    )
    edges.append(TraceEdge(source="gatekeeper", target="context_assembly"))
    edges.append(
        TraceEdge(
            source="rag", target="context_assembly", status=_edge_status(rag_status)
        )
    )
    edges.append(
        TraceEdge(
            source="web_search",
            target="context_assembly",
            status=_edge_status(web_status),
        )
    )

    # --- provider ---
    nodes.append(
        TraceNode(
            id="provider",
            label="Provider Select",
            type="provider",
            status="active",
            summary=f"{connection_id}/{model_name}" if model_name else provider_name,
            metadata={
                "connection_id": connection_id,
                "model": model_name,
                "provider": provider_name,
                "vision_used": has_attachments,
            },
        )
    )
    edges.append(TraceEdge(source="context_assembly", target="provider"))

    # --- llm_call ---
    # provider 生成が失敗した場合は llm_call を error にし、以降の
    # memory_write / state_update / response は skipped にする
    # （issue #51: 失敗時こそ「どこで止まったか」を残すのが目的）。
    llm_failed = bool(generation_error)
    llm_status: TraceStatus = "error" if llm_failed else "active"
    downstream_status: TraceStatus = "skipped" if llm_failed else "active"
    nodes.append(
        TraceNode(
            id="llm_call",
            label="LLM Provider Call",
            type="llm",
            status=llm_status,
            summary=(
                summarize_text(generation_error)
                if llm_failed
                else (
                    f"{timing.get('generation_ms')}ms"
                    if timing.get("generation_ms") is not None
                    else None
                )
            ),
            metadata={
                "generation_ms": timing.get("generation_ms"),
                "ttfb_ms": timing.get("ttfb_ms"),
                "token_estimate": token_estimate,
                **({"error": generation_error} if llm_failed else {}),
            },
        )
    )
    edges.append(TraceEdge(source="provider", target="llm_call"))

    # --- memory_write ---
    nodes.append(
        TraceNode(
            id="memory_write",
            label="Memory Write",
            type="memory",
            status=downstream_status,
            summary=(
                "生成失敗のためスキップ"
                if llm_failed
                else "save_single_turn + maintain_memory"
            ),
            metadata={},
        )
    )
    edges.append(
        TraceEdge(
            source="llm_call",
            target="memory_write",
            status=_edge_status(downstream_status),
        )
    )

    # --- state_update (post-response の Housekeeper) ---
    if llm_failed:
        state_status: TraceStatus = "skipped"
    elif gk_enabled:
        state_status = "active"
    else:
        state_status = "skipped"
    state_delta = gk_result.get("state_delta") or {}
    if llm_failed:
        state_summary = "生成失敗のためスキップ"
    elif not gk_enabled:
        state_summary = "StateUpdater (gatekeeper 無効のため未実行)"
    else:
        state_summary = f"delta keys: {', '.join(state_delta.keys()) or '(none)'}"
    nodes.append(
        TraceNode(
            id="state_update",
            label="State Update",
            type="housekeeper",
            status=state_status,
            summary=state_summary,
            metadata={
                "state_update_ms": timing.get("state_update_ms"),
                "delta": state_delta,
            },
        )
    )
    edges.append(
        TraceEdge(
            source="llm_call", target="state_update", status=_edge_status(state_status)
        )
    )

    # --- response ---
    nodes.append(
        TraceNode(
            id="response",
            label="Response",
            type="end",
            status=downstream_status,
            summary=(
                "(生成失敗のため応答なし)"
                if llm_failed
                else summarize_text(assistant_response)
            ),
            metadata={"response_tokens": token_estimate.get("response")},
        )
    )
    edges.append(
        TraceEdge(
            source="memory_write",
            target="response",
            status=_edge_status(downstream_status),
        )
    )

    trace_id = f"turn_{turn_id}" if turn_id is not None else "turn_unknown"
    return TraceGraph(
        trace_id=trace_id,
        instance=instance_name,
        turn_id=turn_id,
        source=source,
        created_at=created_at,
        nodes=nodes,
        edges=edges,
    )
