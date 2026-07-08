"""
butly_api
─────────
正式フロントエンド向け versioned API（`/api/v1`）の transport 層。

- `create_app()` は side effect の少ない FastAPI app factory。
  user data / 外部 API / 実 instance に依存せず OpenAPI を生成できる。
- HTTP DTO（schemas/）は core domain model（butly_core）と分離し、
  provider SDK の型や内部構造を public contract に漏らさない。
- 旧 endpoint（routers/）は Streamlit 互換 adapter として main.py 側で
  同居させる。butly_api 自体は旧 routers を import しない。

設計の正本: docs/planning/active/frontend_migration_plan.ja.md §6.2 / §9
"""

from butly_api.app import create_app

__all__ = ["create_app"]
