"""
system.py
─────────
health / readiness / app-info / capabilities の response schema。

- health は process 起動確認用の最小応答。外部 API / DB / user data を読まない。
- readiness は「UI を開いてよいか」。provider API key の有無は
  readiness failure にしない（capabilities 側で部分的 unavailable として表現）。
- capabilities は UI 条件分岐の正本。frontend が `os.environ` や
  model 名 prefix で分岐することを禁止するための endpoint。
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """`GET /api/v1/health` の応答。status と version 以外の情報を返さない。"""

    status: Literal["ok"] = "ok"
    backend_version: str
    api_version: str


class ReadinessCheck(BaseModel):
    """readiness を構成する個別チェックの結果。"""

    name: str = Field(description="チェック名（例: data_dir_writable）")
    ok: bool
    message: Optional[str] = Field(
        default=None, description="失敗時の補足。内部 path は含めない"
    )


class ReadinessResponse(BaseModel):
    """`GET /api/v1/ready` の応答。"""

    ready: bool
    checks: List[ReadinessCheck]


class AppInfoResponse(BaseModel):
    """`GET /api/v1/app-info` の応答。frontend/backend version handshake 用。"""

    name: str
    backend_version: str
    api_version: str
    platform: str = Field(description="sys.platform の値（win32 / linux / darwin 等）")
    feature_flags: Dict[str, bool] = Field(
        default_factory=dict,
        description="段階移行中の機能 flag。additive にのみ変更する",
    )


class Capability(BaseModel):
    """単一機能の利用可否。unavailable の理由は安定 code で返す。"""

    available: bool
    reason: Optional[str] = Field(
        default=None,
        description="unavailable 時の理由 code（例: gemini_api_key_not_configured）",
    )


class StreamingCapability(Capability):
    """chat streaming の可否と挙動。

    Gemini Google Search ON 時は provider が non-stream fallback するため、
    UI は chunk 数や到着間隔を成功条件にしてはならない。
    """

    mode: Literal["incremental", "buffered_fallback", "unsupported"] = "incremental"


class AttachmentLimits(BaseModel):
    """添付の制約。値は butly_core.chat.types の定数を正本とする。"""

    max_count: int
    max_size_bytes: int
    allowed_mime_types: List[str]


class CapabilitiesResponse(BaseModel):
    """`GET /api/v1/capabilities` の応答。"""

    active_connection: Optional[str] = None
    active_model: Optional[str] = None
    chat: Capability
    chat_debug: Capability
    streaming: StreamingCapability
    vision: Capability
    native_google_search: Capability
    generic_web_search: Capability
    attachments: AttachmentLimits
