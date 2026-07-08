"""
errors.py
─────────
`/api/v1` の共通 error envelope（schemas.common.ApiError）への正規化。

- `/api/v1` 配下: HTTPException / validation error / 未捕捉例外を
  すべて ApiError envelope で返す。
- それ以外（legacy route）: FastAPI default（`{"detail": ...}` /
  plain "Internal Server Error"）を維持し、Streamlit 互換を壊さない。
- 500 は exception string を body に出さず、request_id で server log と
  突合する（トレースは logging に残す）。
"""

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
    request_validation_exception_handler as fastapi_validation_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from butly_api.schemas.common import ApiError
from butly_api.version import is_api_v1_path

logger = logging.getLogger(__name__)

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "too_many_requests",
    500: "internal_error",
    503: "service_unavailable",
}


class ApiException(Exception):
    """`/api/v1` 用の typed error。frontend 分岐用の安定 code を必ず持つ。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def _is_api_v1(request: Request) -> bool:
    return is_api_v1_path(request.url.path)


def _request_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)


def _envelope_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    rid = _request_id(request)
    error = ApiError(code=code, message=message, details=details or {}, request_id=rid)
    headers = {"X-Request-ID": rid} if rid else None
    return JSONResponse(
        status_code=status_code,
        content=error.model_dump(),
        headers=headers,
    )


async def _handle_api_exception(request: Request, exc: ApiException) -> Response:
    return _envelope_response(
        request, exc.status_code, exc.code, exc.message, exc.details
    )


async def _handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> Response:
    if not _is_api_v1(request):
        return await fastapi_http_exception_handler(request, exc)
    code = _CODE_BY_STATUS.get(exc.status_code, f"http_{exc.status_code}")
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    return _envelope_response(request, exc.status_code, code, message)


async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> Response:
    if not _is_api_v1(request):
        return await fastapi_validation_handler(request, exc)
    # input 値は secret を含み得るため loc / msg / type だけを返す
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return _envelope_response(
        request,
        422,
        "validation_error",
        "Request validation failed.",
        {"errors": errors},
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    if not _is_api_v1(request):
        # legacy route は Starlette default と同じ plain text 500 を維持
        logger.exception(
            "[legacy] unhandled error (request_id=%s): %s", _request_id(request), exc
        )
        return PlainTextResponse("Internal Server Error", status_code=500)
    logger.exception(
        "[api] unhandled error (request_id=%s): %s", _request_id(request), exc
    )
    return _envelope_response(
        request, 500, "internal_error", "An internal error occurred."
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiException, _handle_api_exception)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
