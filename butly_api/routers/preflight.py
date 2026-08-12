"""Connection / embedding preflight endpoint."""

import asyncio
import json
import math
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Query, Request

from butly_api.context import ApiContext
from butly_api.schemas.common import ApiError
from butly_api.schemas.preflight import (
    ConnectionPreflight,
    EmbeddingPreflight,
    PreflightResponse,
)
from butly_api.version import API_V1_PREFIX
from butly_core.llm.connections import Connection, list_connections, try_get_connection
from butly_core.llm.model_registry import resolve_role_model_ref
from butly_core.settings import get_settings

router = APIRouter(prefix=API_V1_PREFIX, tags=["system"])

_PROBE_TIMEOUT_SECONDS = 3.0
_PROBE_CONCURRENCY = 4
_MAX_MODELS_IN_RESPONSE = 20
_PREFLIGHT_CACHE_SECONDS = 30.0


@dataclass(frozen=True)
class _ProbeOutcome:
    configured: bool
    reachable: bool
    reason: Optional[str]
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class _EmbeddingOutcome:
    configured: bool
    reachable: bool
    reason: Optional[str]
    dimension: Optional[int] = None


def _safe_reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {401, 403}:
            return "authentication_failed"
        if exc.code == 404:
            return "models_endpoint_not_found"
        return "provider_http_error"
    sdk_status = getattr(exc, "status_code", None)
    if not isinstance(sdk_status, int):
        sdk_status = getattr(exc, "code", None)
    if isinstance(sdk_status, int):
        if sdk_status in {401, 403}:
            return "authentication_failed"
        if sdk_status == 404:
            return "models_endpoint_not_found"
        if sdk_status == 429 or sdk_status >= 500:
            return "provider_http_error"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "connection_timeout"
    if isinstance(exc, urllib.error.URLError):
        return "connection_unreachable"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError, ValueError)):
        return "invalid_provider_response"
    return "connection_unreachable"


def _openai_models(conn: Connection) -> tuple[str, ...]:
    base_url = conn.resolve_base_url()
    if not base_url:
        raise ValueError("base URL is not configured")

    if conn.id == "ollama":
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        url = f"{root}/api/tags"
    else:
        url = f"{base_url.rstrip('/')}/models"

    headers = {"Accept": "application/json"}
    api_key = conn.resolve_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(conn.extra_headers)

    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())

    raw_items = (
        payload.get("models", []) if conn.id == "ollama" else payload.get("data", [])
    )
    models: List[str] = []
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        model_id = (
            item.get("name") or item.get("model")
            if conn.id == "ollama"
            else item.get("id")
        )
        if isinstance(model_id, str) and model_id:
            models.append(model_id)
    return tuple(dict.fromkeys(models))


def _gemini_models(conn: Connection) -> tuple[str, ...]:
    from google import genai
    from google.genai import types

    api_key = conn.resolve_api_key()
    if not api_key:
        return ()
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=int(_PROBE_TIMEOUT_SECONDS * 1000)
        ),
    )
    models: List[str] = []
    for item in client.models.list():
        model_id = getattr(item, "name", None)
        if isinstance(model_id, str) and model_id.startswith("models/"):
            model_id = model_id[len("models/") :]
        if isinstance(model_id, str) and model_id:
            models.append(model_id)
        if len(models) >= 100:
            break
    return tuple(dict.fromkeys(models))


def _probe_connection_sync(conn: Connection) -> _ProbeOutcome:
    configured = not conn.api_key_env or bool(conn.resolve_api_key())
    if not configured:
        return _ProbeOutcome(
            configured=False,
            reachable=False,
            reason="api_key_not_configured",
        )
    if conn.protocol == "openai_compat" and not conn.resolve_base_url():
        return _ProbeOutcome(
            configured=False,
            reachable=False,
            reason="base_url_not_configured",
        )
    try:
        if conn.protocol == "gemini_native":
            models = _gemini_models(conn)
        elif conn.protocol == "openai_compat":
            models = _openai_models(conn)
        else:
            return _ProbeOutcome(
                configured=True,
                reachable=False,
                reason="unsupported_protocol",
            )
    except Exception as exc:
        return _ProbeOutcome(
            configured=True,
            reachable=False,
            reason=_safe_reason(exc),
        )
    return _ProbeOutcome(
        configured=True,
        reachable=True,
        reason=None,
        models=models,
    )


def _validate_embedding_vector(values: Any) -> Optional[int]:
    if not isinstance(values, (list, tuple)) or not values:
        return None
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in values
    ):
        return None
    return len(values)


def _openai_embedding(conn: Connection, model_name: str) -> Optional[int]:
    base_url = conn.resolve_base_url()
    if not base_url:
        raise ValueError("base URL is not configured")
    url = f"{base_url.rstrip('/')}/embeddings"
    payload = json.dumps(
        {
            "model": conn.strip_model_prefix(model_name),
            "input": "Butly embedding connectivity preflight.",
        }
    ).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    api_key = conn.resolve_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(conn.extra_headers)
    req = urllib.request.Request(
        url, data=payload, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECONDS) as response:
        body = json.loads(response.read())
    data = body.get("data") if isinstance(body, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    vector = first.get("embedding") if isinstance(first, dict) else None
    return _validate_embedding_vector(vector)


def _gemini_embedding(conn: Connection, model_name: str) -> Optional[int]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=conn.resolve_api_key(),
        http_options=types.HttpOptions(
            timeout=int(_PROBE_TIMEOUT_SECONDS * 1000)
        ),
    )
    response = client.models.embed_content(
        model=model_name,
        contents="Butly embedding connectivity preflight.",
    )
    embeddings = getattr(response, "embeddings", None)
    first = embeddings[0] if embeddings else getattr(response, "embedding", None)
    vector = getattr(first, "values", None)
    return _validate_embedding_vector(vector)


def _probe_embedding_sync(
    conn: Optional[Connection], model_name: Optional[str]
) -> _EmbeddingOutcome:
    if conn is None:
        return _EmbeddingOutcome(False, False, "embedding_connection_not_found")
    if not model_name:
        return _EmbeddingOutcome(False, False, "embedding_model_not_configured")
    if not conn.embeddings_supported:
        return _EmbeddingOutcome(True, False, "embedding_not_supported")
    configured = not conn.api_key_env or bool(conn.resolve_api_key())
    if not configured:
        return _EmbeddingOutcome(False, False, "api_key_not_configured")
    try:
        if conn.protocol == "gemini_native":
            dimension = _gemini_embedding(conn, model_name)
        elif conn.protocol == "openai_compat":
            dimension = _openai_embedding(conn, model_name)
        else:
            return _EmbeddingOutcome(True, False, "unsupported_protocol")
    except Exception as exc:
        return _EmbeddingOutcome(True, False, _safe_reason(exc))
    if dimension is None:
        return _EmbeddingOutcome(True, False, "invalid_embedding_response")
    return _EmbeddingOutcome(True, True, None, dimension)


async def _probe_connection(
    conn: Connection, semaphore: asyncio.Semaphore
) -> tuple[_ProbeOutcome, int]:
    started = time.monotonic()

    async def _run() -> _ProbeOutcome:
        async with semaphore:
            return await asyncio.to_thread(_probe_connection_sync, conn)

    try:
        outcome = await asyncio.wait_for(_run(), timeout=_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        outcome = _ProbeOutcome(
            configured=(not conn.api_key_env or bool(conn.resolve_api_key())),
            reachable=False,
            reason="connection_timeout",
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    return outcome, latency_ms


async def _probe_embedding(
    conn: Optional[Connection], model_name: Optional[str]
) -> tuple[_EmbeddingOutcome, int]:
    started = time.monotonic()
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(_probe_embedding_sync, conn, model_name),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        configured = bool(
            conn
            and model_name
            and (not conn.api_key_env or conn.resolve_api_key())
        )
        outcome = _EmbeddingOutcome(
            configured=configured,
            reachable=False,
            reason="connection_timeout",
        )
    return outcome, int((time.monotonic() - started) * 1000)


def _role_refs(context: Optional[ApiContext]) -> tuple[Any, Any]:
    config_path = (
        context.data_dir / "user_config.json"
        if context is not None and context.data_dir is not None
        else None
    )
    settings = get_settings(config_path)
    chat_config = settings.ai.chat
    embedding_config = settings.ai.embedding
    if hasattr(chat_config, "model_dump"):
        chat_config = chat_config.model_dump(mode="python")
    if hasattr(embedding_config, "model_dump"):
        embedding_config = embedding_config.model_dump(mode="python")
    return (
        resolve_role_model_ref(chat_config),
        resolve_role_model_ref(embedding_config),
    )


def _matches_model(configured_model: str, available_models: tuple[str, ...]) -> bool:
    def normalize(value: str) -> str:
        return value.removeprefix("models/").removeprefix("ollama/")

    wanted = normalize(configured_model)
    available = {normalize(model) for model in available_models}
    if wanted in available:
        return True
    # Ollama treats an omitted tag as :latest, but distinct explicit tags
    # (e.g. 8b vs 70b) must never be considered interchangeable.
    return ":" not in wanted and f"{wanted}:latest" in available


@router.get(
    "/preflight",
    operation_id="get_preflight",
    response_model=PreflightResponse,
    summary="Check active LLM and embedding connectivity",
    description=(
        "Connection ごとの疎通と active embedding model の利用可否を並列・期限付きで"
        "確認する。embedding は固定の非ユーザー文字列から有限のvectorを実生成し、"
        "dimensionだけを返す。結果は30秒cacheする。秘密値、接続 URL、provider の"
        "raw error は返さない。"
    ),
    responses={503: {"model": ApiError, "description": "Backend is not ready"}},
)
async def get_preflight(
    request: Request,
    refresh: bool = Query(
        False,
        description="true なら30秒のpreflight cacheを使わず再確認する",
    ),
) -> PreflightResponse:
    cached = getattr(request.app.state, "preflight_cache", None)
    if (
        not refresh
        and isinstance(cached, tuple)
        and len(cached) == 2
        and cached[0] > time.monotonic()
        and isinstance(cached[1], PreflightResponse)
    ):
        return cached[1].model_copy(deep=True)

    context: Optional[ApiContext] = getattr(request.app.state, "api_context", None)
    chat_ref, embedding_ref = _role_refs(context)
    connections = list(list_connections())
    embedding_conn = try_get_connection(embedding_ref.connection_id)
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
    connection_task = asyncio.gather(
        *(_probe_connection(conn, semaphore) for conn in connections)
    )
    embedding_task = _probe_embedding(embedding_conn, embedding_ref.model_name)
    probes, (embedding_outcome, embedding_latency_ms) = await asyncio.gather(
        connection_task,
        embedding_task,
    )

    items: List[ConnectionPreflight] = []
    outcomes: dict[str, tuple[_ProbeOutcome, int]] = {}
    for conn, (outcome, latency_ms) in zip(connections, probes):
        outcomes[conn.id] = (outcome, latency_ms)
        required_for = []
        if conn.id == chat_ref.connection_id:
            required_for.append("chat")
        if conn.id == embedding_ref.connection_id:
            required_for.append("embedding")
        chat_model_available = None
        if conn.id == chat_ref.connection_id and outcome.reachable:
            chat_model_available = (
                _matches_model(chat_ref.model_name, outcome.models)
                if outcome.models
                else None
            )
        if outcome.reachable and chat_model_available is False:
            status = "unavailable"
            reason = "chat_model_not_available"
        elif outcome.reachable and conn.id == chat_ref.connection_id and not outcome.models:
            status = "degraded"
            reason = "chat_model_not_confirmed"
        elif outcome.reachable:
            status = "ready"
            reason = None
        elif not outcome.configured:
            status = "not_configured"
            reason = outcome.reason
        else:
            status = "unreachable"
            reason = outcome.reason
        items.append(
            ConnectionPreflight(
                connection_id=conn.id,
                label=conn.display_label,
                protocol=conn.protocol,
                required_for=required_for,
                configured=outcome.configured,
                reachable=outcome.reachable,
                status=status,
                reason=reason,
                latency_ms=latency_ms,
                model_count=len(outcome.models),
                model_available=chat_model_available,
                models=list(outcome.models[:_MAX_MODELS_IN_RESPONSE]),
            )
        )

    embedding_model = embedding_ref.model_name
    if embedding_conn is None:
        embedding = EmbeddingPreflight(
            connection_id=embedding_ref.connection_id,
            model_name=embedding_model,
            configured=False,
            reachable=False,
            status="unavailable",
            reason="embedding_connection_not_found",
        )
    elif not embedding_conn.embeddings_supported:
        embedding = EmbeddingPreflight(
            connection_id=embedding_conn.id,
            model_name=embedding_model,
            configured=True,
            reachable=False,
            status="unsupported",
            reason="embedding_not_supported",
        )
    else:
        outcome, latency_ms = outcomes.get(
            embedding_conn.id,
            (
                _ProbeOutcome(False, False, "embedding_connection_not_found"),
                0,
            ),
        )
        model_available = (
            _matches_model(embedding_model, outcome.models)
            if embedding_model and outcome.models
            else None
        )
        if embedding_outcome.reachable:
            model_available = True
        if not embedding_outcome.configured:
            embedding_status = "not_configured"
            reason = embedding_outcome.reason
        elif not embedding_outcome.reachable:
            embedding_status = "unreachable"
            reason = embedding_outcome.reason
        elif embedding_outcome.reachable:
            embedding_status = "ready"
            reason = None
        elif model_available is False:
            embedding_status = "unavailable"
            reason = "embedding_model_not_available"
        else:
            embedding_status = "degraded"
            reason = "embedding_model_not_confirmed"
        embedding = EmbeddingPreflight(
            connection_id=embedding_conn.id,
            model_name=embedding_model,
            configured=embedding_outcome.configured,
            reachable=embedding_outcome.reachable,
            status=embedding_status,
            reason=reason,
            latency_ms=embedding_latency_ms,
            model_available=model_available,
            dimension=embedding_outcome.dimension,
        )

    chat_item = next(
        (item for item in items if "chat" in item.required_for), None
    )
    if chat_item is None or chat_item.status in {
        "unavailable",
        "not_configured",
        "unreachable",
        "unsupported",
    }:
        overall_status = "unavailable"
    elif chat_item.status != "ready" or embedding.status != "ready":
        overall_status = "degraded"
    else:
        overall_status = "ready"

    response = PreflightResponse(
        status=overall_status,
        checked_at=datetime.now(timezone.utc),
        connections=items,
        embedding=embedding,
    )
    request.app.state.preflight_cache = (
        time.monotonic() + _PREFLIGHT_CACHE_SECONDS,
        response,
    )
    return response
