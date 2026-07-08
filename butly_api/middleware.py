"""
middleware.py
─────────────
request ID の採番・伝播。

BaseHTTPMiddleware は SSE（StreamingResponse）でバッファリング問題を
起こし得るため、pure ASGI middleware で実装する。

- 受信 `X-Request-ID` があればそれを使い、なければ UUID4 を採番する。
- `request.state.request_id` に格納し、error envelope / log と突合可能にする。
- 応答 header `X-Request-ID` に必ず載せる。
"""

import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

_MAX_REQUEST_ID_LENGTH = 128


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming.strip()[:_MAX_REQUEST_ID_LENGTH] or str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if REQUEST_ID_HEADER not in headers:
                    headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)
