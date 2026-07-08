"""
auth.py
───────
desktop token 認証（frontend_migration_plan.ja.md §6.1 / §6.3）。

- Tauri shell が起動ごとに生成する per-launch token を、`/api/v1/*` への
  Bearer token として要求する。
- token は環境変数（既定: ``BUTLY_DESKTOP_TOKEN``）で backend に渡す。
  **未設定なら認証は無効**（従来の開発 / Streamlit 併用モードそのまま）。
- 除外:
  - `/api/v1/health` — 起動 polling 用。status / version 以外を返さない前提。
  - `/api/v1` 以外の path — legacy Streamlit route と `/line/webhook`
    （LINE signature 認証）は対象外。
  - `OPTIONS` — CORS preflight は認証より先に通す（§9.3）。
- SSE（StreamingResponse）と併用するため BaseHTTPMiddleware ではなく
  pure ASGI middleware で実装する。
"""

import hmac
import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from butly_api.version import is_api_v1_path

logger = logging.getLogger(__name__)

# token を受け取る環境変数の既定名（sidecar 起動時に --auth-token-env で指定予定）
DESKTOP_TOKEN_ENV = "BUTLY_DESKTOP_TOKEN"

# 認証を要求しない /api/v1 path（完全一致）
EXEMPT_V1_PATHS = frozenset({"/api/v1/health"})


def requires_desktop_token(path: str, method: str = "GET") -> bool:
    """この path / method が desktop token 認証の対象かを返す。

    OpenAPI の security requirement 付与（app factory）と runtime 判定の
    両方がこの関数を正本にする。
    """
    if method.upper() == "OPTIONS":
        return False
    if not is_api_v1_path(path):
        return False
    return path not in EXEMPT_V1_PATHS


def _extract_bearer(headers: list) -> str:
    for key, value in headers:
        if key == b"authorization":
            decoded = value.decode("latin-1")
            scheme, _, credentials = decoded.partition(" ")
            if scheme.lower() == "bearer":
                return credentials.strip()
            return ""
    return ""


class DesktopTokenAuthMiddleware:
    """`/api/v1/*` に Bearer token を要求する pure ASGI middleware。

    token が一致しない場合は 401 の ApiError envelope を返す。
    request_id は外側の RequestIDMiddleware が採番済みの値を使う。
    """

    def __init__(self, app: ASGIApp, *, token: str):
        if not token:
            raise ValueError("DesktopTokenAuthMiddleware requires a non-empty token")
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if not requires_desktop_token(path, method):
            await self.app(scope, receive, send)
            return

        supplied = _extract_bearer(scope.get("headers") or [])
        if supplied and hmac.compare_digest(
            supplied.encode("utf-8"), self._token.encode("utf-8")
        ):
            await self.app(scope, receive, send)
            return

        request_id = (scope.get("state") or {}).get("request_id")
        logger.warning(
            "[api] rejected unauthenticated request (path=%s, request_id=%s)",
            path,
            request_id,
        )
        # error envelope は schemas.common.ApiError と同形（middleware 層のため手組み）
        body = json.dumps(
            {
                "code": "unauthorized",
                "message": "A valid desktop token is required.",
                "details": {},
                "request_id": request_id,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
            (b"www-authenticate", b"Bearer"),
        ]
        await send(
            {"type": "http.response.start", "status": 401, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})
