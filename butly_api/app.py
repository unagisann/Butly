"""
app.py
──────
side effect の少ない FastAPI app factory。

- `create_app()` 単体では user data / `.env` / 実 instance / 外部 API に
  アクセスしない。OpenAPI 生成（scripts/generate_openapi.py）はこの状態で行う。
- 実行 entrypoint（main.py）は ApiContext / lifespan / legacy routers を注入し、
  Streamlit 互換 route を同居させる。CORS は entrypoint 側の責務
  （legacy 互換の wildcard は正式配布時に §6.3 のとおり撤去する）。
- Phase 0 で endpoint 未実装の chat / history / instances contract schema は
  OpenAPI components に常に埋め込み、TypeScript client / SSE parser fixture の
  生成対象にする。
"""

from typing import Any, Dict, Iterable, Optional

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRouter
from pydantic import TypeAdapter
from pydantic.json_schema import models_json_schema

from butly_api.context import ApiContext
from butly_api.errors import register_error_handlers
from butly_api.middleware import RequestIDMiddleware
from butly_api.routers import instances, system
from butly_api.schemas import chat as chat_schemas
from butly_api.schemas import instances as instance_schemas
from butly_api.version import BACKEND_VERSION, is_api_v1_path

_REF_TEMPLATE = "#/components/schemas/{model}"

# endpoint 実装前でも公開 contract に含める schema（Phase 0 設計対象）
_CONTRACT_MODELS = [
    chat_schemas.ChatRequest,
    chat_schemas.ChatResult,
    chat_schemas.Message,
    chat_schemas.MessagePage,
    instance_schemas.InstanceSummary,
    instance_schemas.InstanceListResponse,
]


def _contract_component_schemas() -> Dict[str, Any]:
    """chat / history / instances の contract schema を components 形式で返す。"""
    _, definitions = models_json_schema(
        [(model, "validation") for model in _CONTRACT_MODELS],
        ref_template=_REF_TEMPLATE,
    )
    schemas: Dict[str, Any] = dict(definitions.get("$defs", {}))

    # SSE union は oneOf + discriminator として明示的に載せる（§9.6）
    stream_schema = TypeAdapter(chat_schemas.ChatStreamEvent).json_schema(
        ref_template=_REF_TEMPLATE
    )
    for name, schema in stream_schema.pop("$defs", {}).items():
        schemas.setdefault(name, schema)
    schemas["ChatStreamEvent"] = stream_schema
    return schemas


def create_app(
    *,
    context: Optional[ApiContext] = None,
    lifespan: Optional[Any] = None,
    extra_routers: Iterable[APIRouter] = (),
) -> FastAPI:
    """Butly API の FastAPI application を構築する。

    Args:
        context: readiness / capabilities が参照する実行時状態。
            OpenAPI 生成時は None のままにする。
        lifespan: entrypoint 固有の起動・終了処理（bundle bootstrap 等）。
        extra_routers: legacy（Streamlit 互換）routers。butly_api からは
            import せず、entrypoint が注入する。
    """
    app = FastAPI(
        title="Butly API",
        version=BACKEND_VERSION,
        description="Butly backend API. Frontend-facing contract is under /api/v1.",
        lifespan=lifespan,
    )
    app.state.api_context = context

    app.add_middleware(RequestIDMiddleware)
    register_error_handlers(app)

    app.include_router(system.router)
    app.include_router(instances.router)
    for router in extra_routers:
        app.include_router(router)

    def custom_openapi() -> Dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        # legacy routers を同居させても、公開 contract（/openapi.json）は
        # /api/v1 だけにする。runtime の legacy route 挙動には影響しない。
        v1_routes = [
            route
            for route in app.routes
            if is_api_v1_path(getattr(route, "path", ""))
        ]
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=v1_routes,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, model_schema in _contract_component_schemas().items():
            components.setdefault(name, model_schema)
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
