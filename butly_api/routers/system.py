"""
routers/system.py
─────────────────
`/api/v1` の runtime / application 系 endpoint。

health / ready / app-info / capabilities（frontend_migration_plan.ja.md §8.1）。
user data・外部 API へアクセスせず、ApiContext と環境変数だけで応答する。
"""

import os
import logging
import sys
from typing import Optional

from fastapi import APIRouter, Request

from butly_api.context import ApiContext
from butly_api.schemas.system import (
    AppInfoResponse,
    AttachmentLimits,
    CapabilitiesResponse,
    Capability,
    HealthResponse,
    ReadinessCheck,
    ReadinessResponse,
    StreamingCapability,
)
from butly_api.version import API_V1_PREFIX, API_VERSION, BACKEND_VERSION

router = APIRouter(prefix=API_V1_PREFIX, tags=["system"])
logger = logging.getLogger(__name__)


def _context(request: Request) -> Optional[ApiContext]:
    return getattr(request.app.state, "api_context", None)


@router.get(
    "/health",
    operation_id="get_health",
    response_model=HealthResponse,
    summary="Liveness check",
    description="process 起動確認用の最小応答。外部 API / DB / user data を読まない。",
)
def get_health() -> HealthResponse:
    return HealthResponse(backend_version=BACKEND_VERSION, api_version=API_VERSION)


@router.get(
    "/ready",
    operation_id="get_readiness",
    response_model=ReadinessResponse,
    summary="Readiness check",
    description=(
        "UI を開いてよいかを返す。provider API key の有無は readiness failure に"
        "しない（capabilities 側で表現する）。"
    ),
)
def get_readiness(request: Request) -> ReadinessResponse:
    ctx = _context(request)
    checks = []

    if ctx is None:
        checks.append(
            ReadinessCheck(
                name="runtime_initialized",
                ok=False,
                message="API context is not attached (schema-generation app).",
            )
        )
        return ReadinessResponse(ready=False, checks=checks)

    runtime = ctx.runtime_supplier()
    checks.append(ReadinessCheck(name="runtime_initialized", ok=runtime is not None))

    data_dir_ok = bool(
        ctx.data_dir and ctx.data_dir.is_dir() and os.access(ctx.data_dir, os.W_OK)
    )
    checks.append(ReadinessCheck(name="data_dir_writable", ok=data_dir_ok))

    instances_ok = bool(ctx.instances_dir and ctx.instances_dir.is_dir())
    checks.append(ReadinessCheck(name="instances_dir_exists", ok=instances_ok))

    checks.append(ReadinessCheck(name="settings_loaded", ok=ctx.settings_loaded))

    return ReadinessResponse(ready=all(c.ok for c in checks), checks=checks)


@router.get(
    "/app-info",
    operation_id="get_app_info",
    response_model=AppInfoResponse,
    summary="Application info",
    description="frontend / backend の version handshake 用情報。",
)
def get_app_info() -> AppInfoResponse:
    return AppInfoResponse(
        name="Butly",
        backend_version=BACKEND_VERSION,
        api_version=API_VERSION,
        platform=sys.platform,
        feature_flags={},
    )


def _has_web_search_key() -> bool:
    return bool(
        os.getenv("OLLAMA_WEB_SEARCH_API_KEY") or os.getenv("TAVILY_API_KEY")
    )


def _active_chat(request: Request):
    from butly_core.llm.connections import try_get_connection
    from butly_core.llm.model_registry import resolve_role_model_ref
    from butly_core.settings import get_settings

    ctx = _context(request)
    config_path = (
        ctx.data_dir / "user_config.json"
        if ctx is not None and ctx.data_dir is not None
        else None
    )
    settings = get_settings(config_path)
    role_config = settings.ai.chat
    if hasattr(role_config, "model_dump"):
        role_config = role_config.model_dump(mode="python")
    ref = resolve_role_model_ref(role_config)
    return ref, try_get_connection(ref.connection_id)


@router.get(
    "/capabilities",
    operation_id="get_capabilities",
    response_model=CapabilitiesResponse,
    summary="Feature availability",
    description=(
        "UI 条件分岐の正本。frontend は環境変数や model 名 prefix で分岐せず、"
        "この応答に従う。"
    ),
)
def get_capabilities(request: Request) -> CapabilitiesResponse:
    from butly_core.chat.types import (
        ALLOWED_IMAGE_MIMES,
        MAX_ATTACHMENT_SIZE,
        MAX_ATTACHMENTS,
    )

    ctx = _context(request)
    runtime_ready = ctx is not None and ctx.runtime_supplier() is not None
    try:
        active_ref, active_connection = _active_chat(request)
    except Exception as exc:
        logger.warning(
            "[api] active chat configuration resolution failed (%s)",
            type(exc).__name__,
        )
        active_ref, active_connection = None, None
    connection_configured = bool(
        active_connection
        and (
            not active_connection.api_key_env
            or active_connection.resolve_api_key()
        )
    )
    chat = Capability(
        available=runtime_ready and connection_configured,
        reason=(
            None
            if runtime_ready and connection_configured
            else (
                "runtime_not_initialized"
                if not runtime_ready
                else "active_connection_not_configured"
            )
        ),
    )

    web_search_key = _has_web_search_key()
    native_google_available = bool(
        chat.available
        and active_connection
        and active_connection.protocol == "gemini_native"
        and connection_configured
    )
    vision_supported = False
    if (
        connection_configured
        and active_ref is not None
        and active_connection is not None
    ):
        try:
            from butly_core.llm.factory import ProviderFactory

            provider = ProviderFactory.create(active_ref)
            vision_supported = provider.supports_vision(active_ref.model_name)
        except Exception as exc:
            logger.warning(
                "[api] active model capability resolution failed (%s)",
                type(exc).__name__,
            )
            vision_supported = False

    return CapabilitiesResponse(
        active_connection=(
            active_ref.connection_id if active_ref is not None else None
        ),
        active_model=active_ref.model_name if active_ref is not None else None,
        chat=chat,
        chat_debug=Capability(
            available=bool(ctx and ctx.developer_mode),
            reason=(
                None
                if ctx and ctx.developer_mode
                else "developer_mode_disabled"
            ),
        ),
        streaming=StreamingCapability(
            available=chat.available,
            reason=chat.reason,
            mode="incremental",
        ),
        vision=Capability(
            available=chat.available and vision_supported,
            reason=(
                None
                if chat.available and vision_supported
                else (
                    chat.reason
                    if not chat.available
                    else "active_model_does_not_support_vision"
                )
            ),
        ),
        native_google_search=Capability(
            available=native_google_available,
            reason=(
                None
                if native_google_available
                else (
                    chat.reason
                    if not chat.available
                    else "active_connection_does_not_support_google_search"
                )
            ),
        ),
        generic_web_search=Capability(
            available=chat.available and web_search_key,
            reason=(
                None
                if chat.available and web_search_key
                else (
                    chat.reason
                    if not chat.available
                    else "web_search_api_key_not_configured"
                )
            ),
        ),
        attachments=AttachmentLimits(
            max_count=MAX_ATTACHMENTS,
            max_size_bytes=MAX_ATTACHMENT_SIZE,
            allowed_mime_types=sorted(ALLOWED_IMAGE_MIMES),
        ),
    )
