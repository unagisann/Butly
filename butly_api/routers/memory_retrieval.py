"""Versioned memory-retrieval settings resources."""

from pathlib import Path
from typing import Callable, Optional, TypeVar

from fastapi import APIRouter, Request

from butly_api.context import ApiContext
from butly_api.errors import ApiException
from butly_api.schemas.common import ApiError
from butly_api.schemas.memory_retrieval import (
    GlobalMemoryRetrievalSettingsResponse,
    InstanceMemoryRetrievalSettingsResponse,
    MemoryRetrievalGlobalPatch,
    MemoryRetrievalInstancePatch,
)
from butly_api.version import API_V1_PREFIX
from butly_core.settings.memory_retrieval import (
    MemoryRetrievalSettingsError,
    patch_global_memory_retrieval,
    patch_instance_memory_retrieval,
    resolve_global_memory_retrieval,
    resolve_instance_memory_retrieval,
)


router = APIRouter(prefix=API_V1_PREFIX, tags=["settings"])

_NOT_READY = {503: {"model": ApiError, "description": "Backend is not ready"}}
_INVALID = {422: {"model": ApiError, "description": "Invalid setting value"}}
_INSTANCE_ERRORS = {
    **_NOT_READY,
    **_INVALID,
    404: {"model": ApiError, "description": "Instance was not found"},
}
T = TypeVar("T")


def _require_context(request: Request) -> ApiContext:
    context: Optional[ApiContext] = getattr(
        request.app.state,
        "api_context",
        None,
    )
    if context is None or context.data_dir is None:
        raise ApiException(
            503,
            "backend_not_ready",
            "Backend runtime context is not initialized.",
        )
    return context


def _require_instance_dir(context: ApiContext, name: str) -> Path:
    if context.instances_dir is None:
        raise ApiException(
            503,
            "backend_not_ready",
            "Backend runtime context is not initialized.",
        )
    if name != Path(name).name or name.startswith("."):
        raise ApiException(
            404,
            "instance_not_found",
            f"Instance '{name}' was not found.",
        )
    instance_dir = context.instances_dir / name
    if not instance_dir.is_dir():
        raise ApiException(
            404,
            "instance_not_found",
            f"Instance '{name}' was not found.",
        )
    return instance_dir


def _settings_operation(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except MemoryRetrievalSettingsError as exc:
        if exc.issues:
            raise ApiException(
                422,
                "invalid_memory_retrieval_settings",
                "Memory retrieval settings are invalid.",
                {"errors": exc.issues},
            ) from exc
        raise ApiException(
            500,
            "settings_persistence_failed",
            "Memory retrieval settings could not be read or saved.",
        ) from exc
    except OSError as exc:
        raise ApiException(
            500,
            "settings_persistence_failed",
            "Memory retrieval settings could not be read or saved.",
        ) from exc


@router.get(
    "/settings/memory-retrieval",
    operation_id="get_global_memory_retrieval_settings",
    response_model=GlobalMemoryRetrievalSettingsResponse,
    response_model_exclude_none=True,
    summary="Get global memory retrieval settings",
    responses=_NOT_READY,
)
def get_global_memory_retrieval_settings(
    request: Request,
) -> GlobalMemoryRetrievalSettingsResponse:
    context = _require_context(request)
    state = _settings_operation(
        lambda: resolve_global_memory_retrieval(context.data_dir)
    )
    return GlobalMemoryRetrievalSettingsResponse.model_validate(state)


@router.patch(
    "/settings/memory-retrieval",
    operation_id="patch_global_memory_retrieval_settings",
    response_model=GlobalMemoryRetrievalSettingsResponse,
    response_model_exclude_none=True,
    summary="Update global memory retrieval settings",
    responses={**_NOT_READY, **_INVALID},
)
def update_global_memory_retrieval_settings(
    patch: MemoryRetrievalGlobalPatch,
    request: Request,
) -> GlobalMemoryRetrievalSettingsResponse:
    context = _require_context(request)
    updates = patch.model_dump(exclude_unset=True)
    state = _settings_operation(
        lambda: patch_global_memory_retrieval(context.data_dir, updates)
    )
    return GlobalMemoryRetrievalSettingsResponse.model_validate(state)


@router.get(
    "/instances/{name}/settings/memory-retrieval",
    operation_id="get_instance_memory_retrieval_settings",
    response_model=InstanceMemoryRetrievalSettingsResponse,
    response_model_exclude_none=True,
    summary="Get instance memory retrieval settings",
    responses=_INSTANCE_ERRORS,
)
def get_instance_memory_retrieval_settings(
    name: str,
    request: Request,
) -> InstanceMemoryRetrievalSettingsResponse:
    context = _require_context(request)
    instance_dir = _require_instance_dir(context, name)
    state = _settings_operation(
        lambda: resolve_instance_memory_retrieval(
            context.data_dir,
            instance_dir,
        )
    )
    return InstanceMemoryRetrievalSettingsResponse.model_validate(state)


@router.patch(
    "/instances/{name}/settings/memory-retrieval",
    operation_id="patch_instance_memory_retrieval_settings",
    response_model=InstanceMemoryRetrievalSettingsResponse,
    response_model_exclude_none=True,
    summary="Update instance memory retrieval settings",
    responses=_INSTANCE_ERRORS,
)
def update_instance_memory_retrieval_settings(
    name: str,
    patch: MemoryRetrievalInstancePatch,
    request: Request,
) -> InstanceMemoryRetrievalSettingsResponse:
    context = _require_context(request)
    instance_dir = _require_instance_dir(context, name)
    updates = patch.model_dump(exclude_unset=True)
    state = _settings_operation(
        lambda: patch_instance_memory_retrieval(
            context.data_dir,
            instance_dir,
            updates,
        )
    )
    return InstanceMemoryRetrievalSettingsResponse.model_validate(state)
