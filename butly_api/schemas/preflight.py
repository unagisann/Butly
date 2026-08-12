"""Connection / embedding preflight の公開 DTO。"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


PreflightStatus = Literal[
    "ready",
    "degraded",
    "unavailable",
    "not_configured",
    "unreachable",
    "unsupported",
]


class ConnectionPreflight(BaseModel):
    """秘密値・接続 URL・provider の raw error を含まない疎通結果。"""

    connection_id: str
    label: str
    protocol: Literal["openai_compat", "gemini_native"]
    required_for: List[Literal["chat", "embedding"]] = Field(default_factory=list)
    configured: bool
    reachable: bool
    status: PreflightStatus
    reason: Optional[str] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)
    model_count: int = Field(default=0, ge=0)
    model_available: Optional[bool] = Field(
        default=None,
        description="active chat model の catalog 確認結果（非chat接続/空catalogはnull）",
    )
    models: List[str] = Field(
        default_factory=list,
        description="疎通で確認した model ID（最大20件）",
    )


class EmbeddingPreflight(BaseModel):
    """固定テスト文字列で実 embedding を生成した疎通結果。"""

    connection_id: Optional[str] = None
    model_name: Optional[str] = None
    configured: bool
    reachable: bool
    status: PreflightStatus
    reason: Optional[str] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)
    model_available: Optional[bool] = None
    dimension: Optional[int] = Field(default=None, ge=1)


class PreflightResponse(BaseModel):
    """Chat UI 起動前に表示する部分的 availability。"""

    status: Literal["ready", "degraded", "unavailable"]
    checked_at: datetime
    connections: List[ConnectionPreflight] = Field(default_factory=list)
    embedding: EmbeddingPreflight
