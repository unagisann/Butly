"""
generate_sse_fixture.py
───────────────────────
SSE parser contract fixture の生成（frontend_migration_plan.ja.md §9.6）。

`POST /api/v1/chat/stream` が実際に配信する SSE frame 列を、router と同じ
serializer（butly_api.routers.chat.format_sse_event）で deterministic に出力する。
frontend の手書き SSE parser（frontend/src/api/sse.ts 予定）は本 fixture を
入力とする unit test を持ち、backend / frontend で contract を共有する。

出力: openapi/sse_fixtures/
  - chat_stream_success.sse … metadata → chunk×3 → done（日本語 / 改行 / 絵文字込み）
  - chat_stream_error.sse   … metadata → chunk → error 終端（done なし）

再生成: venv/bin/python scripts/generate_sse_fixture.py
検証:   tests/test_sse_fixture.py（snapshot 一致 + 不変条件）
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from butly_api.routers.chat import format_sse_event  # noqa: E402
from butly_api.schemas.chat import (  # noqa: E402
    ChatChunkData,
    ChatChunkEvent,
    ChatDone,
    ChatDoneEvent,
    ChatErrorEvent,
    ChatMetadata,
    ChatMetadataEvent,
    CitationSource,
)
from butly_api.schemas.common import ApiError  # noqa: E402

FIXTURES_DIR = BASE_DIR / "openapi" / "sse_fixtures"

SUCCESS_REQUEST_ID = "req_fixture_success_001"
ERROR_REQUEST_ID = "req_fixture_error_001"

# parser が正しく扱うべき代表ケース: 日本語 / JSON escape される改行 / 絵文字
SUCCESS_CHUNKS = [
    "こんにちは、",
    "今日の予定は\n1. 散歩\n2. 買い物 です。",
    "🍵 どうぞ",
]


def build_success_frames() -> list:
    """成功 stream: metadata 1回 → chunk N回（sequence 単調増加）→ done 1回。"""
    frames = [
        ChatMetadataEvent(
            request_id=SUCCESS_REQUEST_ID,
            data=ChatMetadata(tier="mid", need="今日の予定", keywords=["予定"]),
        )
    ]
    for i, text in enumerate(SUCCESS_CHUNKS):
        frames.append(
            ChatChunkEvent(
                request_id=SUCCESS_REQUEST_ID,
                sequence=i,
                data=ChatChunkData(text=text),
            )
        )
    frames.append(
        ChatDoneEvent(
            request_id=SUCCESS_REQUEST_ID,
            data=ChatDone(
                full_text="".join(SUCCESS_CHUNKS),
                sources=[
                    CitationSource(title="天気予報", url="https://example.com/weather")
                ],
            ),
        )
    )
    return frames


def build_error_frames() -> list:
    """失敗 stream: error 1回で終端し、done は送らない。"""
    return [
        ChatMetadataEvent(
            request_id=ERROR_REQUEST_ID, data=ChatMetadata(tier="mid")
        ),
        ChatChunkEvent(
            request_id=ERROR_REQUEST_ID,
            sequence=0,
            data=ChatChunkData(text="途中まで"),
        ),
        ChatErrorEvent(
            request_id=ERROR_REQUEST_ID,
            data=ApiError(
                code="generation_failed",
                message="LLM generation failed.",
                request_id=ERROR_REQUEST_ID,
            ),
            recoverable=False,
        ),
    ]


def render(frames: list) -> str:
    return "".join(format_sse_event(f) for f in frames)


FIXTURES = {
    "chat_stream_success.sse": build_success_frames,
    "chat_stream_error.sse": build_error_frames,
}


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for filename, builder in FIXTURES.items():
        path = FIXTURES_DIR / filename
        path.write_text(render(builder()), encoding="utf-8", newline="\n")
        print(f"[sse-fixture] wrote {path}")


if __name__ == "__main__":
    main()
