"""
test_chat_stream.py
-------------------
ChatService.execute_stream() のユニットテスト。
Provider / Gatekeeper / Memory はモック化し、stream の event 順序と
debug_info の生成を検証する。
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from butly_core.chat.service import ChatService
from butly_core.chat.types import ChatRequest, ChatResponse


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
        "gatekeeper": {"enabled": True},
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

        # save_single_turn が呼ばれている
        mock_components["memory"].save_single_turn.assert_called_once_with("hello", "こんにちは")

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
        # save_single_turn は呼ばれない (途中失敗のため)
        mock_components["memory"].save_single_turn.assert_not_called()

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
