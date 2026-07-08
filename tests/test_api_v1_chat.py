"""
test_api_v1_chat.py
-------------------
`POST /api/v1/chat` / `POST /api/v1/chat/stream` の contract テスト。

FakeRuntime を注入し、実 LLM / 実 instance データには触れない。
SSE は不変条件（request_id 同一 / sequence 単調増加 / done.full_text 一致 /
error 終端で done なし）を検証する。
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from butly_api import create_app
from butly_api.context import ApiContext
from butly_core.chat.types import ChatResponse


# ===================================================================
# helpers / fixtures
# ===================================================================


class FakeRuntime:
    def __init__(
        self,
        *,
        result: ChatResponse = None,
        stream_events: List[Dict[str, Any]] = None,
        stream_exception: Exception = None,
    ):
        self.result = result or ChatResponse(text="こんにちは")
        self.stream_events = stream_events or []
        self.stream_exception = stream_exception
        self.received_requests: List[Any] = []

    async def chat(self, request):
        self.received_requests.append(request)
        return self.result

    async def chat_stream(self, request):
        self.received_requests.append(request)
        if self.stream_exception is not None:
            raise self.stream_exception
        for event in self.stream_events:
            yield event


def make_client(tmp_path: Path, runtime: FakeRuntime) -> TestClient:
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "t1").mkdir(parents=True)
    context = ApiContext(
        data_dir=tmp_path,
        instances_dir=instances_dir,
        runtime_supplier=lambda: runtime,
        settings_loaded=True,
    )
    return TestClient(create_app(context=context))


def parse_sse(body: str) -> List[Dict[str, Any]]:
    """SSE フレーム列を [{event, payload}] にパースする。"""
    events = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        event_name = None
        data_lines = []
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        events.append(
            {"event": event_name, "payload": json.loads("\n".join(data_lines))}
        )
    return events


SUCCESS_STREAM = [
    {"type": "metadata", "data": {"tier": "mid", "need": "予定", "scores": {"rc": 1}}},
    {"type": "chunk", "text": "こんに"},
    {"type": "chunk", "text": "ちは"},
    {
        "type": "done",
        "data": {
            "full_text": "こんにちは",
            "sources": [{"title": "T", "url": "https://example.com"}],
            "session_state": {"turn_count": 3},
            "debug_info": {"secret": "internal"},
        },
    },
]


# ===================================================================
# POST /api/v1/chat（non-stream）
# ===================================================================


class TestSendChat:
    def test_success(self, tmp_path):
        runtime = FakeRuntime(
            result=ChatResponse(
                text="はい、こんにちは",
                keywords=["挨拶"],
                sources=[{"title": "T", "uri": "https://example.com"}],
                tier="mid",
                debug_info={"secret": "internal"},
            )
        )
        client = make_client(tmp_path, runtime)
        resp = client.post(
            "/api/v1/chat", json={"instance_name": "t1", "text": "こんにちは"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "はい、こんにちは"
        assert body["keywords"] == ["挨拶"]
        assert body["sources"] == [{"title": "T", "url": "https://example.com"}]
        assert body["status"] == "completed"
        assert body["request_id"] == resp.headers["X-Request-ID"]
        # 内部診断（tier / debug_info / session_state）は public contract に出さない
        assert "debug_info" not in body
        assert "tier" not in body

    def test_internal_request_conversion(self, tmp_path):
        runtime = FakeRuntime()
        client = make_client(tmp_path, runtime)
        client.post(
            "/api/v1/chat",
            json={
                "instance_name": "t1",
                "text": "hi",
                "use_google_search": True,
                "model_name": "gemini-x",
            },
        )
        internal = runtime.received_requests[0]
        assert internal.instance_name == "t1"
        assert internal.text == "hi"
        assert internal.use_google_search is True
        assert internal.model_name == "gemini-x"
        assert internal.source == "api"

    def test_unknown_instance_404(self, tmp_path):
        client = make_client(tmp_path, FakeRuntime())
        resp = client.post(
            "/api/v1/chat", json={"instance_name": "nope", "text": "hi"}
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "instance_not_found"

    def test_search_flags_mutually_exclusive_422(self, tmp_path):
        client = make_client(tmp_path, FakeRuntime())
        resp = client.post(
            "/api/v1/chat",
            json={
                "instance_name": "t1",
                "text": "hi",
                "use_google_search": True,
                "use_web_search": True,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_unknown_field_rejected_422(self, tmp_path):
        """unknown request field は拒否して typo を早期検出する（移行計画 §9.7）"""
        client = make_client(tmp_path, FakeRuntime())
        resp = client.post(
            "/api/v1/chat",
            json={"instance_name": "t1", "text": "hi", "mesage": "typo"},
        )
        assert resp.status_code == 422

    def test_no_runtime_503(self, tmp_path):
        instances_dir = tmp_path / "butly_core" / "instances"
        (instances_dir / "t1").mkdir(parents=True)
        context = ApiContext(instances_dir=instances_dir)
        client = TestClient(create_app(context=context))
        resp = client.post(
            "/api/v1/chat", json={"instance_name": "t1", "text": "hi"}
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "backend_not_ready"


# ===================================================================
# POST /api/v1/chat/stream（SSE）
# ===================================================================


class TestStreamChat:
    def test_success_event_sequence(self, tmp_path):
        runtime = FakeRuntime(stream_events=SUCCESS_STREAM)
        client = make_client(tmp_path, runtime)
        resp = client.post(
            "/api/v1/chat/stream", json={"instance_name": "t1", "text": "hi"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = parse_sse(resp.text)
        assert [e["event"] for e in events] == ["metadata", "chunk", "chunk", "done"]

        request_id = resp.headers["X-Request-ID"]
        assert all(e["payload"]["request_id"] == request_id for e in events)

        # payload の discriminator は event 名と一致する
        assert all(e["payload"]["event"] == e["event"] for e in events)

        chunks = [e for e in events if e["event"] == "chunk"]
        assert [c["payload"]["sequence"] for c in chunks] == [0, 1]

        done = events[-1]["payload"]
        joined = "".join(c["payload"]["data"]["text"] for c in chunks)
        assert done["data"]["full_text"] == joined == "こんにちは"
        assert done["data"]["sources"] == [
            {"title": "T", "url": "https://example.com"}
        ]
        # session_state / debug_info は SSE contract に出さない
        assert "session_state" not in done["data"]
        assert "debug_info" not in done["data"]

    def test_metadata_public_fields_only(self, tmp_path):
        runtime = FakeRuntime(stream_events=SUCCESS_STREAM)
        client = make_client(tmp_path, runtime)
        resp = client.post(
            "/api/v1/chat/stream", json={"instance_name": "t1", "text": "hi"}
        )
        metadata = parse_sse(resp.text)[0]["payload"]["data"]
        assert metadata["tier"] == "mid"
        assert metadata["need"] == "予定"
        # gatekeeper 内部 score は公開しない
        assert "scores" not in metadata

    def test_done_full_text_is_router_chunk_concat(self, tmp_path):
        runtime = FakeRuntime(
            stream_events=[
                {"type": "chunk", "text": "正"},
                {"type": "chunk", "text": "解"},
                {"type": "done", "data": {"full_text": "runtime mismatch"}},
            ]
        )
        client = make_client(tmp_path, runtime)
        resp = client.post(
            "/api/v1/chat/stream", json={"instance_name": "t1", "text": "hi"}
        )

        events = parse_sse(resp.text)
        chunks = [e for e in events if e["event"] == "chunk"]
        joined = "".join(c["payload"]["data"]["text"] for c in chunks)
        done = events[-1]["payload"]

        assert joined == "正解"
        assert done["data"]["full_text"] == joined

    def test_error_event_terminates_stream(self, tmp_path):
        runtime = FakeRuntime(
            stream_events=[
                {"type": "metadata", "data": {"tier": "mid"}},
                {"type": "error", "message": "provider down", "recoverable": False},
                # error 後の event は流れてはいけない（終端保証）
                {"type": "chunk", "text": "leak"},
                {"type": "done", "data": {"full_text": "leak"}},
            ]
        )
        client = make_client(tmp_path, runtime)
        resp = client.post(
            "/api/v1/chat/stream", json={"instance_name": "t1", "text": "hi"}
        )
        events = parse_sse(resp.text)
        assert [e["event"] for e in events] == ["metadata", "error"]
        error = events[-1]["payload"]
        assert error["data"]["code"] == "generation_failed"
        assert error["recoverable"] is False

    def test_runtime_exception_yields_internal_error(self, tmp_path):
        runtime = FakeRuntime(stream_exception=RuntimeError("boom with secret path"))
        client = make_client(tmp_path, runtime)
        resp = client.post(
            "/api/v1/chat/stream", json={"instance_name": "t1", "text": "hi"}
        )
        events = parse_sse(resp.text)
        assert events[-1]["event"] == "error"
        payload = events[-1]["payload"]
        assert payload["data"]["code"] == "internal_error"
        # 例外文字列は client に出さない（log のみ）
        assert "boom" not in payload["data"]["message"]

    def test_unknown_instance_404(self, tmp_path):
        client = make_client(tmp_path, FakeRuntime())
        resp = client.post(
            "/api/v1/chat/stream", json={"instance_name": "nope", "text": "hi"}
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "instance_not_found"
