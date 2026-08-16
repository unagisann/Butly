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


# 補助 LLM ノード (collector 由来) の purpose → 表示ラベル
_AUX_LABELS: Dict[str, str] = {
    "context_classifier": "Context Classifier (LLM)",
    "state_updater": "State Updater (LLM)",
    "embedding": "Embedding",
    "keyword_extract": "Keyword Extract (LLM)",
    "reranker": "Memory Reranker",
}

# 補助 LLM ノードをぶら下げる親ノード (メインフロー上の位置)
_AUX_PARENTS: Dict[str, str] = {
    "context_classifier": "gatekeeper",
    "embedding": "memory_probe",
    "keyword_extract": "memory_probe",
    "reranker": "memory_probe",
    "state_updater": "state_update",
}


def _append_aux_llm_nodes(
    nodes: List[TraceNode],
    edges: List[TraceEdge],
    llm_calls: List[Dict[str, Any]],
) -> None:
    """collector が収集した補助 LLM 呼び出しをノード化して追加する。

    main 生成 (purpose="chat_generate") は既存の ``llm_call`` ノードが担うため
    ここでは追加しない (metadata 拡充は呼び出し元で行う)。同一 purpose が複数回
    ある場合 (embedding 等) は ``llm_embedding`` / ``llm_embedding_2`` と連番にする。
    """
    counts: Dict[str, int] = {}
    for call in llm_calls:
        purpose = call.get("purpose") or "llm"
        if purpose == "chat_generate":
            continue
        counts[purpose] = counts.get(purpose, 0) + 1
        idx = counts[purpose]
        node_id = f"llm_{purpose}" if idx == 1 else f"llm_{purpose}_{idx}"

        error = call.get("error")
        status: TraceStatus = "error" if error else "active"
        model = call.get("model") or ""
        duration = call.get("duration_ms")
        if error:
            summary = summarize_text(error)
        else:
            summary = model + (f" ({duration}ms)" if duration is not None else "")

        nodes.append(
            TraceNode(
                id=node_id,
                label=_AUX_LABELS.get(purpose, purpose),
                type="llm",
                status=status,
                summary=summary or None,
                metadata={
                    "aux": True,
                    "purpose": purpose,
                    "model": model,
                    "connection_id": call.get("connection_id", ""),
                    "duration_ms": duration,
                    "prompt_chars": call.get("prompt_chars"),
                    **({"error": error} if error else {}),
                },
            )
        )
        parent = _AUX_PARENTS.get(purpose, "llm_call")
        edges.append(TraceEdge(source=parent, target=node_id, status=status))


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
    active_node_trace: Optional[Dict[str, Any]] = None,
    generation_error: Optional[str] = None,
    llm_calls: Optional[List[Dict[str, Any]]] = None,
    turn_id: Optional[int] = None,
    source: str = "web",
    created_at: Optional[str] = None,
) -> TraceGraph:
    """1 ターン分の回答生成フローを TraceGraph として組み立てる。

    ``llm_calls`` は collector が収集した LLM 呼び出し記録。補助 LLM
    (context_classifier / embedding / keyword_extract / state_updater) は
    個別ノードとして追加され、main 生成 (chat_generate) は既存 ``llm_call``
    ノードの metadata を拡充する。
    """
    gk_result = gk_result or {}
    memory_blocks = memory_blocks or {}
    timing = timing or {}
    token_estimate = token_estimate or {}
    active_node_trace = active_node_trace or {}
    llm_calls = list(llm_calls or [])
    chat_gen_rec = next(
        (c for c in llm_calls if c.get("purpose") == "chat_generate"), None
    )

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
                "original_query": gk_result.get("original_query"),
                "retrieval_query": gk_result.get("retrieval_query"),
                "retrieval_query_status": gk_result.get(
                    "retrieval_query_status"
                ),
                "classifier_status": gk_result.get("classifier_status"),
                "fallback_reason": gk_result.get("fallback_reason"),
                "original_need_intent": gk_result.get("original_need_intent"),
                "intent_floor_applied": gk_result.get("intent_floor_applied"),
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
                # 検索は走ったが注入されなかったケースを読めるようにする
                "retrieval": probe.get("retrieval"),
                "retrieved_count": len(probe.get("retrieved_candidates") or []),
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
    if not active_node_trace:
        lookup = memory_blocks.get("active_node_lookup") or {}
        active_nodes = memory_blocks.get("active_nodes") or []
        active_node_trace = {
            "lookup": {
                "enabled": bool(lookup.get("enabled", False)),
                "attempted": bool(lookup.get("attempted", False)),
                "reason": lookup.get("reason"),
                "candidate_count": int(lookup.get("candidate_count") or 0),
                "matched_count": int(lookup.get("matched_count") or 0),
            },
            "rag_level": None,
            "prompt_observed": False,
            "eligible_count": len(active_nodes),
            "render_candidate_count": None,
            "prompt_included_count": None,
            "injection_status": "not_observed" if active_nodes else "no_matches",
            "nodes": [
                {
                    "id": node.get("id"),
                    "kind": node.get("kind"),
                    "subject": node.get("subject"),
                    "topic": node.get("topic"),
                    "statement": str(node.get("statement") or "")
                    .strip()
                    .replace("\n", " "),
                    "confidence": node.get("confidence"),
                    "source_instance": node.get("source_instance"),
                    "matched_card_ids": node.get("matched_card_ids") or [],
                    "render_candidate": None,
                    "prompt_included": None,
                }
                for node in active_nodes
            ],
        }
    if rag_context:
        rag_status: TraceStatus = "active"
        rag_summary = f"{len(rag_raw)} 件注入"
        rag_meta: Dict[str, Any] = {
            "query": need,
            "source_mode": memory_blocks.get("rag_source_mode"),
            "raw_reference": memory_blocks.get("rag_raw_reference"),
            "result_count": len(rag_raw),
            "results": [
                {
                    "id": r.get("id"),
                    "title": summarize_text(r.get("title", ""), 40),
                    "score": r.get("score"),
                    # hybrid では score は RRF スコア。cosine と BM25 の内訳は
                    # 別フィールドに残す（計画書 §3.2 / §3.7）
                    "vector_score": r.get("vector_score"),
                    "retrieval_source": r.get("retrieval_source"),
                    "vector_rank": r.get("vector_rank"),
                    "bm25_rank": r.get("bm25_rank"),
                    "matched_terms": r.get("matched_terms"),
                    "source_date": r.get("source_date"),
                    "source_instance": r.get("source_instance"),
                }
                for r in rag_raw[:5]
            ],
            "active_nodes": active_node_trace,
        }
    else:
        rag_status = "skipped"
        rag_summary = "need=null（長期記憶不要）" if not need else "候補なし/閾値未達"
        rag_meta = {
            "reason": rag_summary,
            "query": need,
            "active_nodes": active_node_trace,
        }
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
                "active_node_injection_status": active_node_trace.get(
                    "injection_status"
                ),
                "active_node_prompt_included_count": active_node_trace.get(
                    "prompt_included_count"
                ),
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
                **(
                    {
                        "prompt_chars": chat_gen_rec.get("prompt_chars"),
                        **(
                            {
                                "token_usage": (
                                    chat_gen_rec.get("metadata") or {}
                                ).get("token_usage")
                            }
                            if (chat_gen_rec.get("metadata") or {}).get(
                                "token_usage"
                            )
                            else {}
                        ),
                    }
                    if chat_gen_rec
                    else {}
                ),
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

    # --- 補助 LLM ノード (collector 由来) ---
    _append_aux_llm_nodes(nodes, edges, llm_calls)

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
