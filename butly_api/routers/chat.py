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
  - `done.full_text` は全 chunk 連結と一致する（ChatService が保証）。
"""

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from butly_api.context import ApiContext
from butly_api.errors import ApiException
from butly_api.routers.instances import _require_instances_dir, _resolve_instance_dir
from butly_api.schemas.chat import (
    ChatChunkData,
    ChatChunkEvent,
    ChatDone,
    ChatDoneEvent,
    ChatErrorEvent,
    ChatMetadata,
    ChatMetadataEvent,
    ChatRequest,
    ChatResult,
    CitationSource,
)
from butly_api.schemas.common import ApiError
from butly_api.version import API_V1_PREFIX

from butly_core.chat.types import Attachment
from butly_core.chat.types import ChatRequest as InternalChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix=API_V1_PREFIX, tags=["chat"])

_CHAT_ERRORS = {
    404: {"model": ApiError, "description": "Instance not found"},
    422: {"model": ApiError, "description": "Validation error"},
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
    for s in raw_sources:
        if not isinstance(s, dict):
            continue
        title = s.get("title")
        url = s.get("url") or s.get("uri")
        sources.append(
            CitationSource(
                title=title if isinstance(title, str) else "",
                url=url if isinstance(url, str) else "",
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
    runtime = _require_runtime(request)

    result = await runtime.chat(_to_internal_request(body))
    return ChatResult(
        request_id=_request_id(request),
        text=result.text,
        sources=_normalize_sources(result.sources),
        keywords=list(result.keywords or []),
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


def _metadata_from_event(raw: Dict[str, Any]) -> ChatMetadata:
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    keywords = data.get("keywords")
    search_targets = data.get("search_targets")
    return ChatMetadata(
        tier=data.get("tier"),
        need=data.get("need"),
        keywords=keywords if isinstance(keywords, list) else [],
        search_targets=search_targets if isinstance(search_targets, list) else None,
    )


async def _typed_event_stream(
    runtime: Any, internal_request: InternalChatRequest, request_id: str
) -> AsyncGenerator[str, None]:
    """runtime.chat_stream() の raw event を typed SSE frame へ変換する。

    不変条件（error で終端 / done は成功時のみ / sequence 単調増加）を
    ここで保証する。
    """
    sequence = 0
    try:
        async for event in runtime.chat_stream(internal_request):
            ev_type = event.get("type")
            if ev_type == "metadata":
                yield format_sse_event(
                    ChatMetadataEvent(
                        request_id=request_id, data=_metadata_from_event(event)
                    )
                )
            elif ev_type == "chunk":
                yield format_sse_event(
                    ChatChunkEvent(
                        request_id=request_id,
                        sequence=sequence,
                        data=ChatChunkData(text=event.get("text", "")),
                    )
                )
                sequence += 1
            elif ev_type == "done":
                data = event.get("data") or {}
                yield format_sse_event(
                    ChatDoneEvent(
                        request_id=request_id,
                        data=ChatDone(
                            full_text=data.get("full_text", ""),
                            sources=_normalize_sources(data.get("sources")),
                        ),
                    )
                )
                return
            elif ev_type == "error":
                yield format_sse_event(
                    ChatErrorEvent(
                        request_id=request_id,
                        data=ApiError(
                            code=GENERATION_FAILED_CODE,
                            message=str(event.get("message", "generation failed")),
                            request_id=request_id,
                        ),
                        recoverable=bool(event.get("recoverable", False)),
                    )
                )
                return
            # 未知 event type は送らない（contract 外の frame を流さない）
    except Exception as e:
        logger.exception(
            "[api] chat stream failed (request_id=%s): %s", request_id, e
        )
        # exception 文字列は log のみ。client には安定 code だけを返す
        yield format_sse_event(
            ChatErrorEvent(
                request_id=request_id,
                data=ApiError(
                    code="internal_error",
                    message="An internal error occurred.",
                    request_id=request_id,
                ),
                recoverable=False,
            )
        )


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
    runtime = _require_runtime(request)

    return StreamingResponse(
        _typed_event_stream(
            runtime, _to_internal_request(body), _request_id(request)
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # reverse proxy のバッファ無効化
        },
    )
