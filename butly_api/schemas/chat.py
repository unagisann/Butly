"""
chat.py
───────
`POST /api/v1/chat` / `POST /api/v1/chat/stream` /
`GET /api/v1/instances/{name}/messages` の contract schema（Phase 0 設計版）。

このモジュールは **schema 先行** で追加している。endpoint 実装（Phase 0 の
後続 PR）まで path operation には載らないが、OpenAPI components には
`create_app()` が常に埋め込み、TypeScript client / SSE parser fixture の
生成対象にする。

既存実装との接続方針
────────────────────
- ChatRequest:
    routers/chat.py の legacy `ChatRequest`（`message` field）と異なり
    `text` に統一する。router は本 DTO → butly_core.chat.types.ChatRequest
    へ変換して `deps.runtime.chat()` / `chat_stream()` に委譲する
    （legacy `/chat` は adapter として同じ変換 helper を共有する）。
- AttachmentInput:
    butly_core.chat.types.Attachment と同形だが、MIME / 枚数 / サイズ上限を
    OpenAPI に現すため Literal / constraint を明示する。上限値の正本は
    butly_core.chat.types の定数（MAX_ATTACHMENTS / MAX_ATTACHMENT_SIZE）。
- ChatResult:
    butly_core.chat.types.ChatResponse から public に出す field だけを写す。
    tier / session_state / gatekeeper_scores / debug_info は内部診断であり
    通常 response に含めない（include_debug は developer mode の後続課題）。
- Message / MessagePage:
    ButlyMemory.load_recent_sessions() の `{"role", "parts"}` 形式を
    正規化し、`last_interaction_at` は ButlyMemory.get_last_interaction_time()
    から返す。過去 turn は text しか保存していないため、当面
    attachments=[] / sources=[] で表現する（後方互換）。

SSE contract（frontend_migration_plan.ja.md §8.5 / §9.6）
────────────────────────────────────────────────────────
- OpenAPI では endpoint の media type を text/event-stream と記述するに留め、
  event framing の runtime parser は frontend 側で手書きする
  （`frontend/src/api/sse.ts` 予定）。ここで定義する event payload schema は
  その parser と contract test / fixture を共有するための正本。
- 不変条件:
    1. `request_id` は全 event で同一。
    2. 成功時は metadata 1回 → chunk 0回以上 → done 1回。
    3. 失敗時は error 1回で終端し、done は送らない。
    4. `sequence` は chunk ごとに単調増加。
    5. `done.full_text` は全 chunk 連結と一致する。
"""

from datetime import datetime
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from butly_api.schemas.common import ApiError
from butly_core.chat.types import MAX_ATTACHMENT_SIZE, MAX_ATTACHMENTS

# base64 は 3 byte → 4 文字（+ padding）なので、decoded 20MB の上限を
# schema 文字長として表すと 4 * ceil(MAX_ATTACHMENT_SIZE / 3)。
# 実バイト長の正確な検証は butly_core.chat.types.validate_attachments が正本で、
# この maxLength は OpenAPI に上限を現すための contract 表示 + 粗い前段防御。
MAX_DATA_BASE64_LENGTH = 4 * ((MAX_ATTACHMENT_SIZE + 2) // 3)

# ===================================================================
# 添付 / 引用
# ===================================================================


class AttachmentInput(BaseModel):
    """chat request で送る添付（現在は画像のみ）。

    上限: 最大3枚 / 1枚 20MB（butly_core.chat.types の定数が正本）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["image"] = "image"
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    data_base64: str = Field(
        max_length=MAX_DATA_BASE64_LENGTH,
        description=(
            "base64 エンコード済み画像データ（data URL ヘッダ不要）。"
            "decoded で最大 20MB（maxLength は base64 換算値）"
        ),
    )
    name: Optional[str] = None


class AttachmentSummary(BaseModel):
    """履歴表示用の添付メタ情報。データ本体は含めない。"""

    kind: Literal["image"] = "image"
    mime_type: str
    name: Optional[str] = None
    size_bytes: Optional[int] = None


class CitationSource(BaseModel):
    """Web 検索 / grounding の引用元。provider raw object は正規化して返す。"""

    title: str = ""
    url: str = ""


# ===================================================================
# Chat request / result
# ===================================================================


class ChatRequest(BaseModel):
    """`POST /api/v1/chat` / `/api/v1/chat/stream` 共通 request。

    - `use_google_search` と `use_web_search` は相互排他（backend で validation）。
    - `model_name` / `connection` は developer override。通常 UI は
      instance config に従い未指定にする。
    - unknown field は拒否し typo を早期検出する（legacy route のみ互換を緩める）。
    """

    model_config = ConfigDict(extra="forbid")

    instance_name: str = Field(min_length=1)
    text: str = ""
    attachments: List[AttachmentInput] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS
    )
    use_rag: bool = True
    use_google_search: bool = False
    use_web_search: bool = False
    model_name: Optional[str] = None
    connection: Optional[str] = None
    client_request_id: Optional[str] = Field(
        default=None,
        description="double submit 識別用に client が採番する ID",
    )

    @model_validator(mode="after")
    def _search_flags_are_mutually_exclusive(self) -> "ChatRequest":
        if self.use_google_search and self.use_web_search:
            raise ValueError(
                "use_google_search and use_web_search are mutually exclusive."
            )
        return self


class ChatResult(BaseModel):
    """`POST /api/v1/chat`（non-stream fallback / test 用）の応答。"""

    request_id: str
    text: str
    sources: List[CitationSource] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    status: Literal["completed"] = "completed"


# ===================================================================
# Message history
# ===================================================================


class Message(BaseModel):
    """会話履歴の1メッセージ。"""

    id: str
    role: Literal["user", "assistant"]
    text: str
    created_at: Optional[datetime] = Field(
        default=None,
        description="timezone 付き ISO 8601（UTC 保存）。旧データは None になり得る",
    )
    attachments: List[AttachmentSummary] = Field(default_factory=list)
    sources: List[CitationSource] = Field(default_factory=list)
    status: Literal["completed", "failed", "cancelled"] = "completed"


class MessagePage(BaseModel):
    """`GET /api/v1/instances/{name}/messages` の応答。"""

    items: List[Message]
    next_cursor: Optional[str] = Field(
        default=None, description="さらに古い履歴を取るための cursor。終端は None"
    )
    last_interaction_at: Optional[datetime] = None


# ===================================================================
# SSE event payloads（discriminated union）
# ===================================================================


class ChatMetadata(BaseModel):
    """metadata event の payload。Gatekeeper 判定の public な部分だけを写す。"""

    tier: Optional[str] = None
    need: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    search_targets: Optional[List[str]] = None


class ChatChunkData(BaseModel):
    text: str


class ChatDone(BaseModel):
    """done event の payload。full_text は全 chunk 連結と一致する。"""

    full_text: str
    sources: List[CitationSource] = Field(default_factory=list)


class ChatMetadataEvent(BaseModel):
    event: Literal["metadata"] = "metadata"
    request_id: str
    data: ChatMetadata


class ChatChunkEvent(BaseModel):
    event: Literal["chunk"] = "chunk"
    request_id: str
    sequence: int = Field(ge=0, description="chunk ごとに単調増加")
    data: ChatChunkData


class ChatDoneEvent(BaseModel):
    event: Literal["done"] = "done"
    request_id: str
    data: ChatDone


class ChatErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    request_id: str
    data: ApiError
    recoverable: bool = False


ChatStreamEvent = Annotated[
    Union[ChatMetadataEvent, ChatChunkEvent, ChatDoneEvent, ChatErrorEvent],
    Field(discriminator="event"),
]
