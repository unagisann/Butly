"""
routers/chat.py
───────────────
`/api/v1` の typed chat endpoint（Phase 0 PR2-B、frontend_migration_plan.ja.md §8.4-8.5）。

- `POST /api/v1/chat` — non-stream fallback / test 用。
- `POST /api/v1/chat/stream` — primary chat transport（POST SSE）。

transport adapter に徹し、生成・保存は `ButlyRuntime.chat()` / `chat_stream()` に
委譲する（legacy `/chat` `/chat/stream` と同じ経路。旧 route は互換のため維持）。

SSE contract（schemas/chat.py の discriminated union が正本）:
  - `request_id` は全 event で同一（RequestIDMiddleware の値 = `X-Request-ID`）。
  - 成功時は metadata 1回 → chunk 0回以上 → done 1回。
  - 失敗時は error 1回で終端し、done は送らない。
  - `sequence` は chunk ごとに単調増加。
  - `done.full_text` は全 chunk 連結と一致する（router が保証）。
"""

import asyncio
import hashlib
import json
import logging
import math
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from butly_api.context import ApiContext
from butly_api.chat_requests import (
    ChatFinalizingSignal,
    ChatRequestRecord,
    ChatRequestRegistry,
)
from butly_api.errors import ApiException
from butly_api.routers.instances import _require_instances_dir, _resolve_instance_dir
from butly_api.schemas.chat import (
    ChatChunkData,
    ChatChunkEvent,
    ChatDone,
    ChatDoneEvent,
    ChatDebugSummary,
    ChatErrorEvent,
    ChatRequestStatus,
    GatekeeperDebugSummary,
    ChatMetadata,
    ChatMetadataEvent,
    ChatRequest,
    ChatResult,
    CitationSource,
    RagDebugSummary,
)
from butly_api.schemas.common import ApiError
from butly_api.version import API_V1_PREFIX

from butly_core.chat.types import Attachment
from butly_core.chat.types import ChatRequest as InternalChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix=API_V1_PREFIX, tags=["chat"])

_CHAT_ERRORS = {
    404: {"model": ApiError, "description": "Instance not found"},
    403: {"model": ApiError, "description": "Developer debug is disabled"},
    422: {"model": ApiError, "description": "Validation error"},
    429: {"model": ApiError, "description": "Request registry capacity exceeded"},
    409: {"model": ApiError, "description": "Idempotency conflict"},
    503: {"model": ApiError, "description": "Backend is not ready"},
}

# SSE stream の error event 用の安定 code（frontend 分岐用）
GENERATION_FAILED_CODE = "generation_failed"


def _require_runtime(request: Request) -> Any:
    ctx: Optional[ApiContext] = getattr(request.app.state, "api_context", None)
    runtime = ctx.runtime_supplier() if ctx is not None else None
    if runtime is None:
        raise ApiException(
            503,
            "backend_not_ready",
            "Backend runtime context is not initialized.",
        )
    return runtime


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def _request_registry(request: Request) -> ChatRequestRegistry:
    return request.app.state.chat_request_registry


def _validate_debug_access(request: Request, body: ChatRequest) -> None:
    if not body.include_debug:
        return
    ctx: Optional[ApiContext] = getattr(request.app.state, "api_context", None)
    if ctx is None or not ctx.developer_mode:
        raise ApiException(
            403,
            "debug_not_available",
            "Chat debug summaries are available only in developer mode.",
        )


def _request_fingerprint(body: ChatRequest) -> str:
    payload = body.model_dump(mode="json", exclude={"client_request_id"})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _to_internal_request(body: ChatRequest) -> InternalChatRequest:
    """public ChatRequest → 内部 DTO。HTTP の都合をここで吸収する。

    schema 側で MIME / 枚数 / 相互排他は検証済み。decoded バイト長などの
    最終検証は従来どおり butly_core.chat.types.validate_attachments が行う。
    """
    return InternalChatRequest(
        text=body.text,
        attachments=[
            Attachment(
                kind=att.kind,
                mime_type=att.mime_type,
                data_base64=att.data_base64,
                name=att.name,
            )
            for att in body.attachments
        ],
        instance_name=body.instance_name,
        use_rag=body.use_rag,
        use_google_search=body.use_google_search,
        use_web_search=body.use_web_search,
        model_name=body.model_name,
        connection=body.connection,
        source="api",
    )


def _normalize_sources(raw_sources: Any) -> List[CitationSource]:
    """provider 由来の source dict 群を public DTO に正規化する。"""
    sources: List[CitationSource] = []
    if not isinstance(raw_sources, list):
        return sources
    for s in raw_sources[:50]:
        if not isinstance(s, dict):
            continue
        title = s.get("title")
        url = s.get("url") or s.get("uri")
        sources.append(
            CitationSource(
                title=title[:500] if isinstance(title, str) else "",
                url=url[:4096] if isinstance(url, str) else "",
            )
        )
    return sources


def _validate_instance(request: Request, instance_name: str) -> None:
    """instance の存在チェック（不存在は instances router と同じ 404 code）。"""
    instances_dir = _require_instances_dir(request)
    _resolve_instance_dir(instances_dir, instance_name)


# ===================================================================
# POST /api/v1/chat（non-stream）
# ===================================================================


@router.post(
    "/chat",
    operation_id="send_chat",
    response_model=ChatResult,
    summary="Send a chat message (non-streaming)",
    description=(
        "チャットメッセージを送信し、完成した応答を返す。primary transport は "
        "`POST /api/v1/chat/stream`（SSE）で、この endpoint は non-stream "
        "fallback / test 用。legacy `POST /chat` の versioned 置換。"
    ),
    responses=_CHAT_ERRORS,
)
async def send_chat(body: ChatRequest, request: Request) -> ChatResult:
    _validate_instance(request, body.instance_name)
    _validate_debug_access(request, body)
    runtime = _require_runtime(request)

    result = await runtime.chat(_to_internal_request(body))
    return ChatResult(
        request_id=_request_id(request),
        text=result.text,
        sources=_normalize_sources(result.sources),
        keywords=list(result.keywords or []),
        debug=(
            _debug_from_done({"debug_info": result.debug_info})
            if body.include_debug
            else None
        ),
    )


# ===================================================================
# POST /api/v1/chat/stream（SSE）
# ===================================================================


def format_sse_event(event: BaseModel) -> str:
    """typed event を SSE frame（`event:` + `data:` 行）へ整形する。

    SSE fixture 生成（scripts/generate_sse_fixture.py）と同じ serializer を
    使うことで、frontend parser の contract test と実配信を一致させる。
    """
    name = getattr(event, "event")
    payload = event.model_dump_json()
    return f"event: {name}\ndata: {payload}\n\n"


def _safe_str_list(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    return [item[:500] for item in value[:100] if isinstance(item, str)]


def _safe_scores(value: Any) -> Optional[Dict[str, float]]:
    if not isinstance(value, dict):
        return None
    scores = {
        str(key)[:100]: float(score)
        for key, score in list(value.items())[:50]
        if isinstance(score, (int, float)) and not isinstance(score, bool)
        and math.isfinite(float(score))
    }
    return scores or None


def _debug_from_gatekeeper(data: Dict[str, Any]) -> ChatDebugSummary:
    return ChatDebugSummary(
        gatekeeper=GatekeeperDebugSummary(
            tier=(
                data.get("tier")[:100]
                if isinstance(data.get("tier"), str)
                else None
            ),
            need=(
                data.get("need")[:500]
                if isinstance(data.get("need"), str)
                else None
            ),
            scores=_safe_scores(data.get("scores") or data.get("llm_scoring")),
            search_targets=_safe_str_list(data.get("search_targets")),
            fallback_reason=(
                data.get("fallback_reason")[:500]
                if isinstance(data.get("fallback_reason"), str)
                else None
            ),
            memory_probe_status=(
                data.get("memory_probe_status")[:500]
                if isinstance(data.get("memory_probe_status"), str)
                else None
            ),
        )
    )


def _debug_from_done(data: Dict[str, Any]) -> Optional[ChatDebugSummary]:
    debug_info = data.get("debug_info")
    if not isinstance(debug_info, dict):
        return None
    gatekeeper = debug_info.get("gatekeeper")
    gatekeeper = gatekeeper if isinstance(gatekeeper, dict) else {}
    rag = debug_info.get("rag")
    rag = rag if isinstance(rag, dict) else {}
    results = rag.get("results")
    candidate_count = len(results) if isinstance(results, list) else 0
    active_trace = rag.get("active_nodes")
    active_trace = active_trace if isinstance(active_trace, dict) else {}
    injected_raw = active_trace.get("prompt_included_count")
    injected_count = (
        injected_raw
        if isinstance(injected_raw, int) and not isinstance(injected_raw, bool)
        else 0
    )
    nodes = active_trace.get("nodes")
    active_node_ids = []
    if isinstance(nodes, list):
        active_node_ids = [
            node["id"][:500]
            for node in nodes[:100]
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        ]
    retrieval = rag.get("retrieval")
    enabled = candidate_count > 0 or bool(retrieval)
    gatekeeper_data = dict(gatekeeper)
    if "scores" not in gatekeeper_data:
        gatekeeper_data["scores"] = gatekeeper.get("scores")
    summary = _debug_from_gatekeeper(gatekeeper_data)
    summary.rag = RagDebugSummary(
        enabled=enabled,
        candidate_count=candidate_count,
        injected_count=injected_count,
        injection_status=(
                active_trace.get("injection_status")[:500]
            if isinstance(active_trace.get("injection_status"), str)
            else None
        ),
        active_nodes=active_node_ids,
    )
    return summary


def _metadata_from_event(
    raw: Dict[str, Any], *, include_debug: bool = False
) -> ChatMetadata:
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    keywords = _safe_str_list(data.get("keywords")) or []
    search_targets = _safe_str_list(data.get("search_targets"))
    return ChatMetadata(
        tier=(data.get("tier")[:100] if isinstance(data.get("tier"), str) else None),
        need=(data.get("need")[:500] if isinstance(data.get("need"), str) else None),
        keywords=keywords,
        search_targets=search_targets,
        debug=_debug_from_gatekeeper(data) if include_debug else None,
    )


async def _typed_events(
    runtime: Any,
    internal_request: InternalChatRequest,
    request_id: str,
    *,
    include_debug: bool = False,
) -> AsyncGenerator[BaseModel, None]:
    """runtime.chat_stream() の raw event を typed SSE frame へ変換する。

    不変条件（error で終端 / done は成功時のみ / sequence 単調増加）を
    ここで保証する。
    """
    sequence = 0
    full_text_parts: List[str] = []
    try:
        async for event in runtime.chat_stream(internal_request):
            ev_type = event.get("type")
            if ev_type == "metadata":
                yield ChatMetadataEvent(
                    request_id=request_id,
                    data=_metadata_from_event(event, include_debug=include_debug),
                )
            elif ev_type == "chunk":
                text = event.get("text", "")
                if not isinstance(text, str):
                    text = str(text)
                full_text_parts.append(text)
                yield ChatChunkEvent(
                    request_id=request_id,
                    sequence=sequence,
                    data=ChatChunkData(text=text),
                )
                sequence += 1
            elif ev_type == "done":
                data = event.get("data") or {}
                raw_full_text = data.get("full_text")
                if not full_text_parts and isinstance(raw_full_text, str):
                    full_text_parts.append(raw_full_text)
                    if raw_full_text:
                        yield ChatChunkEvent(
                            request_id=request_id,
                            sequence=sequence,
                            data=ChatChunkData(text=raw_full_text),
                        )
                        sequence += 1
                yield ChatDoneEvent(
                    request_id=request_id,
                    data=ChatDone(
                        full_text="".join(full_text_parts),
                        sources=_normalize_sources(data.get("sources")),
                        debug=_debug_from_done(data) if include_debug else None,
                    ),
                )
                return
            elif ev_type == "error":
                yield ChatErrorEvent(
                    request_id=request_id,
                    data=ApiError(
                        code=GENERATION_FAILED_CODE,
                        message="Generation failed.",
                        request_id=request_id,
                    ),
                    recoverable=bool(event.get("recoverable", False)),
                )
                return
            elif ev_type == "finalizing":
                yield ChatFinalizingSignal()
            # 未知 event type は送らない（contract 外の frame を流さない）
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(
            "[api] chat stream failed (request_id=%s): %s", request_id, e
        )
        # exception 文字列は log のみ。client には安定 code だけを返す
        yield ChatErrorEvent(
            request_id=request_id,
            data=ApiError(
                code="internal_error",
                message="An internal error occurred.",
                request_id=request_id,
            ),
            recoverable=False,
        )


async def _format_registered_stream(
    registry: ChatRequestRegistry, record: ChatRequestRecord
) -> AsyncGenerator[str, None]:
    async for event in registry.stream(record):
        yield format_sse_event(event)


@router.post(
    "/chat/stream",
    operation_id="stream_chat",
    summary="Send a chat message (SSE streaming)",
    description=(
        "チャットメッセージを送信し、`text/event-stream` で "
        "metadata → chunk* → done（失敗時は error 終端）を返す。"
        "event payload は components の `ChatStreamEvent`（oneOf + discriminator）。"
        "legacy `POST /chat/stream` の versioned 置換。"
    ),
    responses={
        200: {
            "description": "SSE stream of ChatStreamEvent frames",
            "content": {
                "text/event-stream": {
                    "schema": {"$ref": "#/components/schemas/ChatStreamEvent"}
                }
            },
        },
        **_CHAT_ERRORS,
    },
)
async def stream_chat(body: ChatRequest, request: Request) -> StreamingResponse:
    _validate_instance(request, body.instance_name)
    _validate_debug_access(request, body)
    runtime = _require_runtime(request)
    registry = _request_registry(request)
    internal_request = _to_internal_request(body)

    def event_source(record: ChatRequestRecord) -> AsyncGenerator[BaseModel, None]:
        return _typed_events(
            runtime,
            internal_request,
            record.request_id,
            include_debug=body.include_debug,
        )

    record = await registry.start_or_attach(
        request_id=_request_id(request),
        client_request_id=body.client_request_id,
        fingerprint=_request_fingerprint(body),
        event_source_factory=event_source,
    )

    return StreamingResponse(
        _format_registered_stream(registry, record),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # reverse proxy のバッファ無効化
            # replay / attach 時も event request_id と response header を一致させる。
            "X-Request-ID": record.request_id,
        },
    )


@router.get(
    "/chat/requests/{request_id}",
    operation_id="get_chat_request_status",
    response_model=ChatRequestStatus,
    summary="Get generation request status",
    responses={404: {"model": ApiError, "description": "Request not found"}},
)
async def get_chat_request_status(
    request_id: str, request: Request
) -> ChatRequestStatus:
    record = await _request_registry(request).get(request_id)
    return record.public_status()


@router.post(
    "/chat/requests/{request_id}/cancel",
    operation_id="cancel_chat_request",
    response_model=ChatRequestStatus,
    summary="Cancel generation",
    description=(
        "実行中の server task をキャンセルし、対応する SDK では provider stream の"
        "close も試みる。同期 SDK が close を提供しない場合は provider 側の処理が"
        "継続し得るが、部分応答は保存しない。保存開始後（finalizing）の cancel は"
        "拒否され、1回だけ保存して completed になる。terminal request への再実行は"
        "idempotent。"
    ),
    responses={404: {"model": ApiError, "description": "Request not found"}},
)
async def cancel_chat_request(
    request_id: str, request: Request
) -> ChatRequestStatus:
    registry = _request_registry(request)
    record = await registry.get(request_id)
    await registry.cancel(record)
    return record.public_status()
