"""
test_chat_stream.py
-------------------
ChatService.execute_stream() のユニットテスト。
Provider / Gatekeeper / Memory はモック化し、stream の event 順序と
debug_info の生成を検証する。
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from butly_core.chat.service import ChatService
from butly_core.chat.types import Attachment, ChatRequest, ChatResponse


def _run_async(coro):
    return asyncio.run(coro)


async def _collect(gen):
    out = []
    async for ev in gen:
        out.append(ev)
    return out


@pytest.fixture
def tmp_instance_dir(tmp_path):
    d = tmp_path / "TestInstance"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def direct_threadpool_for_unit_tests(monkeypatch):
    """Avoid this sandbox's Python 3.13 worker-thread shutdown deadlock."""
    async def direct(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("butly_core.chat.service.run_in_threadpool", direct)


@pytest.fixture
def request_obj():
    req = MagicMock(spec=ChatRequest)
    req.text = "hello"
    req.attachments = []
    req.instance_name = "TestInstance"
    req.model_name = "gemini-flash-test"
    req.use_rag = False
    req.use_google_search = False
    req.use_web_search = False
    return req


@pytest.fixture
def mock_components():
    memory = MagicMock()
    memory.get_last_interaction_time.return_value = None
    memory.load_recent_sessions.return_value = ([], None)
    memory.save_single_turn = MagicMock()
    memory.maintain_memory = MagicMock()

    brain = MagicMock()
    chronos = MagicMock()
    chronos.get_system_note.return_value = "TIME"

    return {"memory": memory, "brain": brain, "chronos": chronos}


@pytest.fixture
def mock_instance_manager():
    im = MagicMock()
    im.get_instance_config.return_value = {
        "chat": {"model_name": "gemini-flash-test"},
        "brain": {"use_rag": False},
        "gatekeeper": {"enabled": False},
    }
    return im


@pytest.fixture
def mock_gatekeeper():
    gk = MagicMock()
    gk.classify.return_value = {
        "tier": "mid",
        "topic": "test topic",
        "need": None,
        "need_intent": None,
        "search_targets": None,
        "state_delta": {},
        "llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.2, "continuity_need": 0.3},
        "memory_probe": {"status": "skipped", "candidates": [], "glossary_hits": []},
    }
    gk.update_state.return_value = {"topic": "新トピック", "mood": "calm"}
    return gk


@pytest.fixture
def mock_mem_builder():
    mb = MagicMock()
    mb.build.return_value = {"tier": "mid", "topic": "t", "short_term": ""}
    return mb


class TestExecuteStream:
    def test_attachment_and_sources_are_wired_to_safe_turn_metadata(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        request_obj.attachments = [
            Attachment(
                mime_type="image/png",
                data_base64="aGk=",
                name="tiny.png",
            )
        ]

        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": "answer"}
            yield {
                "type": "done",
                "full_text": "answer",
                "sources": [
                    {"title": "Reference", "uri": "https://example.com"}
                ],
                "debug": {},
            }

        provider = MagicMock()
        provider.async_generate_stream = fake_stream
        provider.supports_vision.return_value = True
        session_state = MagicMock()
        session_state.to_dict.return_value = {
            "topic": "",
            "mood": "neutral",
            "turn_count": 0,
        }
        with patch("butly_core.chat.service.ProviderFactory") as factory, patch(
            "butly_core.core.gatekeeper.SessionState",
            return_value=session_state,
        ):
            factory.create.return_value = provider
            events = _run_async(
                _collect(
                    ChatService.execute_stream(
                        request=request_obj,
                        get_instance_components=lambda _: mock_components,
                        instance_manager=mock_instance_manager,
                        instances_dir=tmp_instance_dir.parent,
                        gatekeeper=mock_gatekeeper,
                        mem_block_builder=mock_mem_builder,
                    )
                )
            )

        assert events[-1]["type"] == "done"
        _args, kwargs = mock_components["memory"].save_single_turn.call_args
        assert kwargs["meta"]["attachments"] == [
            {
                "kind": "image",
                "mime_type": "image/png",
                "name": "tiny.png",
                "size_bytes": 2,
            }
        ]
        assert kwargs["assistant_meta"]["sources"] == [
            {"title": "Reference", "url": "https://example.com"}
        ]
        assert "aGk=" not in json.dumps(kwargs)

    def test_finalizing_precedes_state_and_history_persistence(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        """API cancellation barrier is visible before any durable commit."""
        mock_instance_manager.get_instance_config.return_value["gatekeeper"] = {
            "enabled": True
        }
        state_started = asyncio.Event()
        release_state = asyncio.Event()

        async def delayed_state_update(_func, *args, **kwargs):
            state_started.set()
            await release_state.wait()
            return {"topic": "after barrier"}

        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": "answer"}
            yield {
                "type": "done",
                "full_text": "answer",
                "sources": [],
                "debug": {},
            }

        provider = MagicMock()
        provider.async_generate_stream = fake_stream
        provider.supports_vision.return_value = True
        session_state = MagicMock()
        session_state.to_dict.return_value = {
            "topic": "",
            "mood": "neutral",
            "turn_count": 0,
        }

        async def scenario():
            stream = ChatService.execute_stream(
                request=request_obj,
                get_instance_components=lambda _: mock_components,
                instance_manager=mock_instance_manager,
                instances_dir=tmp_instance_dir.parent,
                gatekeeper=mock_gatekeeper,
                mem_block_builder=mock_mem_builder,
            )
            assert (await anext(stream))["type"] == "metadata"
            assert (await anext(stream))["type"] == "chunk"
            pending = asyncio.create_task(anext(stream))
            await state_started.wait()
            await asyncio.sleep(0)
            assert pending.done() is False
            mock_components["memory"].save_single_turn.assert_not_called()
            session_state.increment_turn.assert_not_called()

            release_state.set()
            barrier = await asyncio.wait_for(pending, timeout=0.2)
            assert barrier == {"type": "finalizing"}
            mock_components["memory"].save_single_turn.assert_not_called()
            session_state.increment_turn.assert_not_called()
            remaining = [event async for event in stream]
            assert remaining[-1]["type"] == "done"

        with patch("butly_core.chat.service.ProviderFactory") as factory, patch(
            "butly_core.core.gatekeeper.SessionState",
            return_value=session_state,
        ), patch(
            "butly_core.chat.service.run_in_threadpool",
            new=delayed_state_update,
        ):
            factory.create.return_value = provider
            _run_async(scenario())

        session_state.increment_turn.assert_called_once()
        session_state.apply_delta.assert_called_once_with(
            {"topic": "after barrier"}
        )
        mock_components["memory"].save_single_turn.assert_called_once_with(
            "hello", "answer", meta=None
        )

    def test_pending_state_update_cancels_without_any_persistence(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        mock_instance_manager.get_instance_config.return_value["gatekeeper"] = {
            "enabled": True
        }
        state_started = asyncio.Event()

        async def blocked_state_update(_func, *args, **kwargs):
            state_started.set()
            await asyncio.Event().wait()

        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": "not committed"}
            yield {
                "type": "done",
                "full_text": "not committed",
                "sources": [],
                "debug": {},
            }

        provider = MagicMock()
        provider.async_generate_stream = fake_stream
        provider.supports_vision.return_value = True
        session_state = MagicMock()
        session_state.to_dict.return_value = {
            "topic": "",
            "mood": "neutral",
            "turn_count": 0,
        }

        async def scenario():
            stream = ChatService.execute_stream(
                request=request_obj,
                get_instance_components=lambda _: mock_components,
                instance_manager=mock_instance_manager,
                instances_dir=tmp_instance_dir.parent,
                gatekeeper=mock_gatekeeper,
                mem_block_builder=mock_mem_builder,
            )
            assert (await anext(stream))["type"] == "metadata"
            assert (await anext(stream))["type"] == "chunk"
            pending = asyncio.create_task(anext(stream))
            await state_started.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

        with patch("butly_core.chat.service.ProviderFactory") as factory, patch(
            "butly_core.core.gatekeeper.SessionState",
            return_value=session_state,
        ), patch(
            "butly_core.chat.service.run_in_threadpool",
            new=blocked_state_update,
        ):
            factory.create.return_value = provider
            _run_async(scenario())

        mock_components["memory"].save_single_turn.assert_not_called()
        mock_components["memory"].maintain_memory.assert_not_called()
        session_state.increment_turn.assert_not_called()
        session_state.apply_delta.assert_not_called()

    def test_post_commit_auxiliary_failures_still_complete(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": "committed"}
            yield {
                "type": "done",
                "full_text": "committed",
                "sources": [],
                "debug": {},
            }

        provider = MagicMock()
        provider.async_generate_stream = fake_stream
        provider.supports_vision.return_value = True
        mock_components["memory"].maintain_memory.side_effect = RuntimeError(
            "maintenance failed"
        )
        session_state = MagicMock()
        session_state.to_dict.return_value = {
            "topic": "",
            "mood": "neutral",
            "turn_count": 0,
        }

        with patch("butly_core.chat.service.ProviderFactory") as factory, patch(
            "butly_core.core.gatekeeper.SessionState",
            return_value=session_state,
        ), patch(
            "butly_core.chat.service._save_debug_log",
            side_effect=RuntimeError("debug failed"),
        ), patch(
            "butly_core.chat.service._build_and_save_trace",
            side_effect=RuntimeError("trace failed"),
        ):
            factory.create.return_value = provider
            events = _run_async(
                _collect(
                    ChatService.execute_stream(
                        request=request_obj,
                        get_instance_components=lambda _: mock_components,
                        instance_manager=mock_instance_manager,
                        instances_dir=tmp_instance_dir.parent,
                        gatekeeper=mock_gatekeeper,
                        mem_block_builder=mock_mem_builder,
                    )
                )
            )

        assert events[-1]["type"] == "done"
        assert events[-1]["data"]["full_text"] == "committed"
        mock_components["memory"].save_single_turn.assert_called_once_with(
            "hello", "committed", meta=None
        )

    def test_slow_post_commit_maintenance_keeps_event_loop_responsive(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        maintenance_started = asyncio.Event()
        release_maintenance = asyncio.Event()

        async def delayed_threadpool(func, *args, **kwargs):
            assert func is mock_components["memory"].maintain_memory
            maintenance_started.set()
            await release_maintenance.wait()
            return None

        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": "answer"}
            yield {
                "type": "done",
                "full_text": "answer",
                "sources": [],
                "debug": {},
            }

        provider = MagicMock()
        provider.async_generate_stream = fake_stream
        provider.supports_vision.return_value = True
        session_state = MagicMock()
        session_state.to_dict.return_value = {
            "topic": "",
            "mood": "neutral",
            "turn_count": 0,
        }

        async def scenario():
            stream = ChatService.execute_stream(
                request=request_obj,
                get_instance_components=lambda _: mock_components,
                instance_manager=mock_instance_manager,
                instances_dir=tmp_instance_dir.parent,
                gatekeeper=mock_gatekeeper,
                mem_block_builder=mock_mem_builder,
            )
            assert (await anext(stream))["type"] == "metadata"
            assert (await anext(stream))["type"] == "chunk"
            assert (await anext(stream))["type"] == "finalizing"
            pending_done = asyncio.create_task(anext(stream))
            await maintenance_started.wait()
            assert pending_done.done() is False

            # A separate event-loop task still runs while maintenance blocks.
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.1)

            release_maintenance.set()
            assert (await pending_done)["type"] == "done"

        with patch("butly_core.chat.service.ProviderFactory") as factory, patch(
            "butly_core.core.gatekeeper.SessionState",
            return_value=session_state,
        ), patch(
            "butly_core.chat.service.run_in_threadpool",
            new=delayed_threadpool,
        ):
            factory.create.return_value = provider
            _run_async(scenario())

        mock_components["memory"].save_single_turn.assert_called_once()

    def test_chunks_then_metadata_then_done(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        # Provider stream returns 3 chunks then done
        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": "こん"}
            yield {"type": "chunk", "text": "にち"}
            yield {"type": "chunk", "text": "は"}
            yield {
                "type": "done",
                "full_text": "こんにちは",
                "sources": [],
                "debug": {"system_instruction": "SYS", "system_instruction_full": "SYS"},
            }

        provider = MagicMock()
        provider.async_generate_stream = fake_stream
        provider.supports_vision = MagicMock(return_value=True)

        session_state_mock = MagicMock()
        session_state_mock.to_dict.return_value = {"topic": "", "mood": "neutral", "turn_count": 0}
        with patch("butly_core.chat.service.ProviderFactory") as PF, \
             patch("butly_core.core.gatekeeper.SessionState", return_value=session_state_mock):
            PF.create.return_value = provider

            events = _run_async(_collect(ChatService.execute_stream(
                request=request_obj,
                get_instance_components=lambda _: mock_components,
                instance_manager=mock_instance_manager,
                instances_dir=tmp_instance_dir.parent,
                gatekeeper=mock_gatekeeper,
                mem_block_builder=mock_mem_builder,
            )))

        # 順序: metadata → chunk×3 → done
        types = [e["type"] for e in events]
        assert types[0] == "metadata"
        assert types.count("chunk") == 3
        assert types[-1] == "done"

        # done に full_text と debug_info が入っている
        done = events[-1]
        assert done["data"]["full_text"] == "こんにちは"
        assert "timing" in done["data"]["debug_info"]
        assert done["data"]["debug_info"]["timing"]["ttfb_ms"] >= 0

        # save_single_turn が呼ばれている (Web 入口は外部帰属なし → meta=None)
        mock_components["memory"].save_single_turn.assert_called_once_with(
            "hello", "こんにちは", meta=None
        )
        session_state_mock.increment_turn.assert_called_once_with(
            "mid", history_msgs=[]
        )

        # issue #51: collector 経由の chat_generate 記録が trace.json に反映される
        trace = json.loads(
            (tmp_instance_dir / "traces" / "latest.json").read_text(encoding="utf-8")
        )
        llm_node = next(n for n in trace["nodes"] if n["id"] == "llm_call")
        assert llm_node["metadata"]["prompt_chars"] > 0

    def test_provider_error_yields_error_event(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        async def err_stream(*a, **kw):
            yield {"type": "chunk", "text": "abc"}
            yield {"type": "error", "message": "boom"}

        provider = MagicMock()
        provider.async_generate_stream = err_stream
        provider.supports_vision = MagicMock(return_value=True)

        session_state_mock = MagicMock()
        session_state_mock.to_dict.return_value = {"topic": "", "mood": "neutral", "turn_count": 0}
        with patch("butly_core.chat.service.ProviderFactory") as PF, \
             patch("butly_core.core.gatekeeper.SessionState", return_value=session_state_mock):
            PF.create.return_value = provider

            events = _run_async(_collect(ChatService.execute_stream(
                request=request_obj,
                get_instance_components=lambda _: mock_components,
                instance_manager=mock_instance_manager,
                instances_dir=tmp_instance_dir.parent,
                gatekeeper=mock_gatekeeper,
                mem_block_builder=mock_mem_builder,
            )))

        assert events[-1]["type"] == "error"
        assert "boom" in events[-1]["message"]
        assert events[-1]["recoverable"] is True
        # save_single_turn は呼ばれない (途中失敗のため)
        mock_components["memory"].save_single_turn.assert_not_called()
        session_state_mock.increment_turn.assert_not_called()

        # issue #51: 失敗時も error trace が残る (llm_call=error, 以降 skipped)
        trace_path = tmp_instance_dir / "traces" / "latest.json"
        assert trace_path.exists()
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        statuses = {n["id"]: n["status"] for n in trace["nodes"]}
        assert statuses["llm_call"] == "error"
        assert statuses["memory_write"] == "skipped"
        assert statuses["response"] == "skipped"

    def test_buffered_provider_exception_never_saves_success_turn(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        """Gemini Google Search fallback failures are terminal errors."""
        request_obj.use_google_search = True

        async def failed_buffered_stream(*args, **kwargs):
            if False:
                yield None
            raise RuntimeError("secret-token at /private/provider/path")

        provider = MagicMock()
        provider.async_generate_stream = failed_buffered_stream
        provider.supports_vision.return_value = True
        session_state = MagicMock()
        session_state.to_dict.return_value = {
            "topic": "",
            "mood": "neutral",
            "turn_count": 0,
        }

        with patch("butly_core.chat.service.ProviderFactory") as factory, patch(
            "butly_core.core.gatekeeper.SessionState",
            return_value=session_state,
        ):
            factory.create.return_value = provider
            events = _run_async(
                _collect(
                    ChatService.execute_stream(
                        request=request_obj,
                        get_instance_components=lambda _: mock_components,
                        instance_manager=mock_instance_manager,
                        instances_dir=tmp_instance_dir.parent,
                        gatekeeper=mock_gatekeeper,
                        mem_block_builder=mock_mem_builder,
                    )
                )
            )

        assert [event["type"] for event in events] == ["metadata", "error"]
        assert events[-1]["recoverable"] is True
        assert "done" not in [event["type"] for event in events]
        mock_components["memory"].save_single_turn.assert_not_called()
        session_state.increment_turn.assert_not_called()

    def test_attachment_vision_error(
        self, tmp_instance_dir, request_obj, mock_components,
        mock_instance_manager, mock_gatekeeper, mock_mem_builder,
    ):
        request_obj.attachments = [MagicMock()]
        request_obj.attachments[0].mime_type = "image/png"
        request_obj.attachments[0].size = 100
        request_obj.attachments[0].data = "x" * 100

        provider = MagicMock()
        provider.supports_vision = MagicMock(return_value=False)

        session_state_mock = MagicMock()
        session_state_mock.to_dict.return_value = {"topic": "", "mood": "neutral", "turn_count": 0}
        with patch("butly_core.chat.service.ProviderFactory") as PF, \
             patch("butly_core.chat.service.validate_attachments", return_value=None), \
             patch("butly_core.core.gatekeeper.SessionState", return_value=session_state_mock):
            PF.create.return_value = provider

            events = _run_async(_collect(ChatService.execute_stream(
                request=request_obj,
                get_instance_components=lambda _: mock_components,
                instance_manager=mock_instance_manager,
                instances_dir=tmp_instance_dir.parent,
                gatekeeper=mock_gatekeeper,
                mem_block_builder=mock_mem_builder,
            )))

        assert events[-1]["type"] == "error"
        assert "画像入力" in events[-1]["message"]
        session_state_mock.increment_turn.assert_not_called()
