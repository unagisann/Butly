"""
test_chat_stream_route.py
-------------------------
/chat/stream SSE エンドポイントの sse format helper + 簡易テスト。
"""

import json
from unittest.mock import patch, MagicMock

import pytest


def test_sse_event_format():
    from routers.chat import _sse_event
    out = _sse_event("chunk", {"text": "hello"})
    assert out.startswith("event: chunk\n")
    assert "data: " in out
    assert out.endswith("\n\n")

    parsed = json.loads(out.split("data: ", 1)[1].strip())
    assert parsed == {"text": "hello"}


def test_sse_event_handles_japanese():
    from routers.chat import _sse_event
    out = _sse_event("done", {"text": "こんにちは"})
    assert "こんにちは" in out  # ensure_ascii=False


def test_sse_event_handles_non_serializable():
    from routers.chat import _sse_event

    class Custom:
        def __repr__(self):
            return "CustomObj"

    out = _sse_event("metadata", {"x": Custom()})
    parsed = json.loads(out.split("data: ", 1)[1].strip())
    assert parsed["x"] == "CustomObj"
