"""
test_runtime.py
---------------
ButlyRuntime の単体テスト + chat route が Runtime 経由で動くことの回帰テスト。

検証項目:
  - ButlyRuntime が依存 (instance_manager / gatekeeper / mem_block_builder) を保持する
  - get_instance_components: 存在しない instance で 404 相当の例外
  - get_instance_components: 2 回目はキャッシュを返す (再生成しない)
  - runtime.chat() が ChatService.execute() に Runtime の依存を渡す
  - runtime.chat_stream() が ChatService.execute_stream() に委譲しイベントを転送する
  - /chat / /chat/stream が deps.runtime 経由で応答する (Web UI 非破壊)
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from butly_core.chat.service import ChatService
from butly_core.chat.types import ChatRequest, ChatResponse
from butly_core.runtime import ButlyRuntime


def _run_async(coro):
    return asyncio.run(coro)


async def _collect(gen):
    out = []
    async for ev in gen:
        out.append(ev)
    return out


@pytest.fixture
def runtime(tmp_path) -> ButlyRuntime:
    instances_dir = tmp_path / "butly_core" / "instances"
    instances_dir.mkdir(parents=True)
    return ButlyRuntime(
        data_dir=tmp_path,
        base_dir=tmp_path,
        instances_dir=instances_dir,
    )


# ===================================================================
# 構築 / パス
# ===================================================================

class TestRuntimeConstruction:
    def test_holds_dependencies(self, runtime):
        assert runtime.instance_manager is not None
        assert runtime.gatekeeper is not None
        assert runtime.mem_block_builder is not None
        assert isinstance(runtime.instance_store, dict)

    def test_default_instances_dir(self, tmp_path):
        rt = ButlyRuntime(data_dir=tmp_path)
        assert rt.base_dir == tmp_path
        assert rt.instances_dir == tmp_path / "butly_core" / "instances"


# ===================================================================
# get_instance_components
# ===================================================================

class TestGetInstanceComponents:
    def test_missing_instance_raises_404(self, runtime):
        with pytest.raises(HTTPException) as exc:
            runtime.get_instance_components("does_not_exist")
        assert exc.value.status_code == 404

    def test_caches_components(self, runtime):
        (runtime.instances_dir / "inst_a").mkdir()

        with patch("butly_core.runtime.ButlyMemory") as Mem, \
             patch("butly_core.runtime.ButlyBrain") as Brain, \
             patch("butly_core.runtime.ButlyChronos") as Chronos:
            first = runtime.get_instance_components("inst_a")
            second = runtime.get_instance_components("inst_a")

        # 同一オブジェクトを返す (キャッシュ)
        assert first is second
        assert set(first.keys()) == {"memory", "brain", "chronos"}
        # コンストラクタは 1 回だけ
        Mem.assert_called_once()
        Brain.assert_called_once()
        Chronos.assert_called_once()
        # instance_store に載っている
        assert "inst_a" in runtime.instance_store


# ===================================================================
# chat() / chat_stream() の委譲
# ===================================================================

class TestRuntimeChatDelegation:
    def test_chat_passes_runtime_deps_to_execute(self, runtime):
        req = ChatRequest(text="hi", instance_name="inst")
        sentinel_ws = object()
        fake_resp = ChatResponse(text="ok")

        with patch.object(
            ChatService, "execute", new=AsyncMock(return_value=fake_resp)
        ) as exec_mock:
            result = _run_async(runtime.chat(req, ws_manager=sentinel_ws))

        assert result is fake_resp
        exec_mock.assert_awaited_once()
        _, kwargs = exec_mock.call_args
        assert kwargs["request"] is req
        assert kwargs["get_instance_components"] == runtime.get_instance_components
        assert kwargs["instance_manager"] is runtime.instance_manager
        assert kwargs["instances_dir"] is runtime.instances_dir
        assert kwargs["gatekeeper"] is runtime.gatekeeper
        assert kwargs["mem_block_builder"] is runtime.mem_block_builder
        assert kwargs["ws_manager"] is sentinel_ws

    def test_chat_stream_forwards_events_and_deps(self, runtime):
        req = ChatRequest(text="hi", instance_name="inst")
        captured = {}

        async def fake_stream(**kwargs):
            captured.update(kwargs)
            yield {"type": "metadata", "data": {}}
            yield {"type": "done", "data": {}}

        with patch.object(ChatService, "execute_stream", new=fake_stream):
            events = _run_async(_collect(runtime.chat_stream(req)))

        assert [e["type"] for e in events] == ["metadata", "done"]
        assert captured["request"] is req
        assert captured["get_instance_components"] == runtime.get_instance_components
        assert captured["instance_manager"] is runtime.instance_manager
        assert captured["gatekeeper"] is runtime.gatekeeper
        assert captured["mem_block_builder"] is runtime.mem_block_builder


# ===================================================================
# chat route が Runtime 経由で動く (Web UI 非破壊)
# ===================================================================

class _FakeRuntime:
    """deps.runtime を差し替える軽量スタブ。"""

    def __init__(self):
        self.chat_requests = []
        self.stream_requests = []

    async def chat(self, request, ws_manager=None):
        self.chat_requests.append(request)
        return ChatResponse(text="hello-from-runtime", tier="mid")

    async def chat_stream(self, request):
        self.stream_requests.append(request)
        yield {"type": "metadata", "data": {"tier": "mid"}}
        yield {"type": "chunk", "text": "hi"}
        yield {"type": "done", "data": {"full_text": "hi"}}


@pytest.fixture
def client_with_fake_runtime():
    import dependencies as deps
    from routers import chat as chat_router

    app = FastAPI()
    app.include_router(chat_router.router)

    fake = _FakeRuntime()
    prev = getattr(deps, "runtime", None)
    deps.runtime = fake
    try:
        yield TestClient(app), fake
    finally:
        deps.runtime = prev


class TestChatRouteViaRuntime:
    def test_chat_post_uses_runtime(self, client_with_fake_runtime):
        client, fake = client_with_fake_runtime
        resp = client.post("/chat", json={"message": "yo", "instance_name": "inst"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["response"] == "hello-from-runtime"
        assert body["tier"] == "mid"
        # Runtime に内部 ChatRequest が渡っている
        assert len(fake.chat_requests) == 1
        assert fake.chat_requests[0].text == "yo"
        assert fake.chat_requests[0].source == "web"

    def test_chat_bad_attachment_returns_400_before_runtime(
        self, client_with_fake_runtime
    ):
        client, fake = client_with_fake_runtime
        resp = client.post(
            "/chat",
            json={"message": "yo", "attachments": [{"not": "valid"}]},
        )
        assert resp.status_code == 400
        # Runtime まで到達していない
        assert fake.chat_requests == []

    def test_chat_stream_uses_runtime(self, client_with_fake_runtime):
        client, fake = client_with_fake_runtime
        resp = client.post("/chat/stream", json={"message": "yo"})
        assert resp.status_code == 200
        text = resp.text
        assert "event: metadata" in text
        assert "event: chunk" in text
        assert "event: done" in text
        assert len(fake.stream_requests) == 1
