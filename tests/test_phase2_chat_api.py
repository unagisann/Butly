"""Runnable Phase 2 vertical-slice API tests using async ASGI transport."""

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from butly_api import create_app
from butly_api.context import ApiContext
from butly_api.routers.instances import list_instance_messages
from butly_core.chat.types import ChatResponse
from butly_core.core.memory import ButlyMemory


class PersistingRuntime:
    def __init__(self, base_dir, chunks):
        self.base_dir = base_dir
        self.chunks = chunks
        self.calls = 0

    async def chat_stream(self, request):
        self.calls += 1
        yield {"type": "metadata", "data": {"tier": "mid"}}
        for chunk in self.chunks:
            yield {"type": "chunk", "text": chunk}
        yield {"type": "finalizing"}
        full_text = "".join(self.chunks)
        ButlyMemory(
            self.base_dir, instance_name=request.instance_name
        ).save_single_turn(request.text, full_text)
        yield {"type": "done", "data": {"full_text": full_text}}


class RetryingRuntime:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.calls = 0

    async def chat_stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "error",
                "message": "secret-token at /private/provider/path",
                "recoverable": True,
            }
            return
        yield {"type": "finalizing"}
        ButlyMemory(
            self.base_dir, instance_name=request.instance_name
        ).save_single_turn(request.text, "retry succeeded")
        yield {"type": "done", "data": {"full_text": "retry succeeded"}}


class RestFailureRuntime:
    async def chat(self, _request):
        raise RuntimeError("secret-token at /private/provider/path")


class DebugRuntime:
    async def chat(self, _request):
        return ChatResponse(
            text="debug response",
            debug_info={
                "prompt_full": "secret prompt",
                "raw_response": "secret provider response",
                "gatekeeper": {"tier": "mid", "need": "past_fact"},
                "rag": {"results": [{"content": "secret memory"}]},
            },
        )


def _parse_sse(body):
    events = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


def _history(app, name):
    request = Request({"type": "http", "app": app})
    return list_instance_messages(name, request, limit=50, before=None)


@pytest.mark.parametrize(
    "chunks",
    [["native ", "multi", " chunk"], ["gemini buffered fallback"]],
    ids=["native-multi-chunk", "gemini-buffered-fallback"],
)
def test_history_stream_done_reload_and_completed_replay(tmp_path, chunks):
    async def scenario():
        instances_dir = tmp_path / "butly_core" / "instances"
        (instances_dir / "phase2").mkdir(parents=True)
        runtime = PersistingRuntime(tmp_path, chunks)
        app = create_app(
            context=ApiContext(
                data_dir=tmp_path,
                instances_dir=instances_dir,
                runtime_supplier=lambda: runtime,
                settings_loaded=True,
            )
        )
        assert _history(app, "phase2").items == []

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            payload = {
                "instance_name": "phase2",
                "text": "question",
                "client_request_id": "logical-phase2",
            }
            first = await client.post(
                "/api/v1/chat/stream",
                headers={"X-Request-ID": "attempt-original"},
                json=payload,
            )
            assert first.status_code == 200
            first_events = _parse_sse(first.text)
            assert [name for name, _data in first_events] == [
                "metadata",
                *(["chunk"] * len(chunks)),
                "done",
            ]
            assert first_events[-1][1]["data"]["full_text"] == "".join(
                chunks
            )

            replay = await client.post(
                "/api/v1/chat/stream",
                headers={"X-Request-ID": "attempt-unused"},
                json=payload,
            )
            replay_events = _parse_sse(replay.text)
            assert replay.headers["X-Request-ID"] == "attempt-original"
            assert {
                event["request_id"] for _name, event in replay_events
            } == {"attempt-original"}

        history = _history(app, "phase2")
        assert [item.text for item in history.items] == [
            "question",
            "".join(chunks),
        ]
        assert runtime.calls == 1

    asyncio.run(scenario())


def test_recoverable_provider_failure_retries_with_same_logical_id(tmp_path):
    async def scenario():
        instances_dir = tmp_path / "butly_core" / "instances"
        (instances_dir / "phase2").mkdir(parents=True)
        runtime = RetryingRuntime(tmp_path)
        app = create_app(
            context=ApiContext(
                data_dir=tmp_path,
                instances_dir=instances_dir,
                runtime_supplier=lambda: runtime,
                settings_loaded=True,
            )
        )
        payload = {
            "instance_name": "phase2",
            "text": "retry me",
            "client_request_id": "logical-retry",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            failed = await client.post(
                "/api/v1/chat/stream",
                headers={"X-Request-ID": "retry-attempt-1"},
                json=payload,
            )
            failed_events = _parse_sse(failed.text)
            assert [name for name, _data in failed_events] == ["error"]
            error = failed_events[0][1]
            assert error["recoverable"] is True
            assert error["data"]["code"] == "generation_failed"
            assert "secret-token" not in failed.text
            assert _history(app, "phase2").items == []

            retried = await client.post(
                "/api/v1/chat/stream",
                headers={"X-Request-ID": "retry-attempt-2"},
                json=payload,
            )
            assert retried.headers["X-Request-ID"] == "retry-attempt-2"
            assert _parse_sse(retried.text)[-1][0] == "done"
            status = await client.get(
                "/api/v1/chat/requests/retry-attempt-2"
            )
            assert status.json()["attempt"] == 2
            assert status.json()["state"] == "completed"

        assert runtime.calls == 2
        history = _history(app, "phase2")
        assert [item.text for item in history.items] == [
            "retry me",
            "retry succeeded",
        ]

    asyncio.run(scenario())


def test_nonstream_provider_failure_is_generic_and_not_persisted(tmp_path):
    async def scenario():
        instances_dir = tmp_path / "butly_core" / "instances"
        (instances_dir / "phase2").mkdir(parents=True)
        app = create_app(
            context=ApiContext(
                data_dir=tmp_path,
                instances_dir=instances_dir,
                runtime_supplier=RestFailureRuntime,
                settings_loaded=True,
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, raise_app_exceptions=False
            ),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/chat",
                json={"instance_name": "phase2", "text": "fail safely"},
            )
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert response.json()["message"] == "An internal error occurred."
        assert "secret-token" not in response.text
        assert _history(app, "phase2").items == []

    asyncio.run(scenario())


def test_include_debug_is_tokenless_dev_only(tmp_path):
    async def request_for(developer_mode):
        instances_dir = tmp_path / "butly_core" / "instances"
        (instances_dir / "phase2").mkdir(parents=True, exist_ok=True)
        app = create_app(
            context=ApiContext(
                data_dir=tmp_path,
                instances_dir=instances_dir,
                runtime_supplier=DebugRuntime,
                developer_mode=developer_mode,
                auth_token=None,
                settings_loaded=True,
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/v1/chat",
                json={
                    "instance_name": "phase2",
                    "text": "debug",
                    "include_debug": True,
                },
            )

    denied = asyncio.run(request_for(False))
    assert denied.status_code == 403
    assert denied.json()["code"] == "debug_not_available"

    allowed = asyncio.run(request_for(True))
    assert allowed.status_code == 200
    assert allowed.json()["debug"]["gatekeeper"]["tier"] == "mid"
    assert "secret" not in allowed.text
