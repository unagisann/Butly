"""
common.py
─────────
`/api/v1` 全 endpoint で共有する共通 schema。

error contract（frontend_migration_plan.ja.md §9.4）:
- FastAPI default の `{"detail": ...}` と成功 body 内 error を混在させず、
  `/api/v1` はすべて `ApiError` envelope に統一する。
- `code` は frontend 分岐用の安定値。`message` は表示可能な文字列だが、
  既知 code は frontend 側で localize してよい。
- exception string / file path / API key / raw provider response は含めない。
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


def _require_all_envelope_keys(schema: Dict[str, Any]) -> None:
    # runtime の envelope は常に全 key を含める（request_id は null になり得る）ため、
    # OpenAPI 上も全 field を required にして generated client の null check を減らす
    schema["required"] = sorted(schema.get("properties", {}))


class ApiError(BaseModel):
    """`/api/v1` の共通 error envelope。

    HTTP response では常に 4 field すべてを含める（default 省略はしない）。
    """

    model_config = ConfigDict(json_schema_extra=_require_all_envelope_keys)

    code: str = Field(description="frontend 分岐用の安定した error code（snake_case）")
    message: str = Field(description="人間可読なエラー説明。secret や内部 path を含めない")
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="error code ごとの補足情報（validation error の一覧など）",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="server log と突合するための request ID（X-Request-ID と同値）",
    )
