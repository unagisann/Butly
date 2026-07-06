"""
butly_api.schemas
─────────────────
frontend-facing HTTP DTO（transport 層の Pydantic model）。

butly_core の domain model（butly_core.chat.types 等）とは分離し、
wire format の互換性を internal refactor から独立させる。
"""
