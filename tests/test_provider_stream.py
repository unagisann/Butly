"""
test_provider_stream.py
-----------------------
BaseProvider.async_generate_stream() のデフォルト fallback と
GeminiProvider のネイティブ stream のテスト。
"""

import asyncio
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from butly_core.chat.types import Attachment, ChatResponse
from butly_core.llm.base import BaseProvider


def _run_async(coro):
    """asyncio.run のラッパー (pytest-asyncio 不使用時の代替)"""
    return asyncio.run(coro)


async def _collect_stream(provider, text, attachments, context):
    events = []
    async for event in provider.async_generate_stream(text, attachments, context):
        events.append(event)
    return events


class _DummyProvider(BaseProvider):
    """テスト用: generate() が固定レスポンスを返す。stream は base のデフォルト fallback を使う。"""

    def __init__(self, text: str = "hello world", sources=None):
        self._text = text
        self._sources = sources or []

    def generate(self, text, attachments, context):
        return ChatResponse(text=self._text, sources=self._sources, debug_info={"k": "v"})

    @staticmethod
    def supports_vision(model_name):
        return False

    def summarize(self, conversation_text, config):
        return ""

    def embed(self, text):
        return None

    def classify(self, prompt, config):
        return ""


class TestBaseDefaultStream:
    """BaseProvider のデフォルト fallback 実装のテスト"""

    def test_default_yields_single_chunk_then_done(self):
        provider = _DummyProvider("foobar")
        events = _run_async(_collect_stream(provider, "test", [], {}))
        assert len(events) == 2
        assert events[0] == {"type": "chunk", "text": "foobar"}
        assert events[1]["type"] == "done"
        assert events[1]["full_text"] == "foobar"
        assert events[1]["sources"] == []
        assert events[1]["debug"] == {"k": "v"}

    def test_default_empty_text_skips_chunk_yields_done(self):
        provider = _DummyProvider("")
        events = _run_async(_collect_stream(provider, "test", [], {}))
        assert len(events) == 1
        assert events[0]["type"] == "done"
        assert events[0]["full_text"] == ""


class TestGeminiStream:
    """GeminiProvider.async_generate_stream のテスト"""

    @pytest.fixture
    def gemini_provider(self):
        from butly_core.llm.providers.gemini import GeminiProvider
        return GeminiProvider()

    def _mk_chunk(self, text):
        chunk = MagicMock()
        chunk.text = text
        return chunk

    def test_stream_yields_chunks_then_done(self, gemini_provider, monkeypatch):
        mock_chat = MagicMock()
        mock_chat.send_message_stream.return_value = iter([
            self._mk_chunk("こん"),
            self._mk_chunk("にち"),
            self._mk_chunk("は"),
        ])

        monkeypatch.setattr(gemini_provider, "_start_chat", lambda **kw: mock_chat)
        monkeypatch.setattr(
            "butly_core.core.gatekeeper.build_system_instruction_from_blocks",
            lambda **kw: "SYS",
        )
        monkeypatch.setattr(
            "butly_core.core.gatekeeper.build_context_prefix",
            lambda **kw: "CTX",
        )

        events = _run_async(_collect_stream(
            gemini_provider, "hello", [],
            {"memory_manager": MagicMock(), "memory_blocks": {}},
        ))

        chunks = [e for e in events if e["type"] == "chunk"]
        assert [c["text"] for c in chunks] == ["こん", "にち", "は"]
        assert events[-1]["type"] == "done"
        assert events[-1]["full_text"] == "こんにちは"

    def test_stream_error_yields_error_event(self, gemini_provider, monkeypatch):
        def _raise(**kw):
            raise RuntimeError("API down")

        monkeypatch.setattr(gemini_provider, "_start_chat", _raise)
        monkeypatch.setattr(
            "butly_core.core.gatekeeper.build_system_instruction_from_blocks",
            lambda **kw: "SYS",
        )
        monkeypatch.setattr(
            "butly_core.core.gatekeeper.build_context_prefix",
            lambda **kw: "CTX",
        )

        events = _run_async(_collect_stream(
            gemini_provider, "x", [],
            {"memory_manager": MagicMock(), "memory_blocks": {}},
        ))

        assert events[-1]["type"] == "error"
        assert "API down" in events[-1]["message"]

    def test_google_search_falls_back_to_non_stream(self, gemini_provider, monkeypatch):
        """use_google_search=True なら非 stream に fallback して 1 チャンクで返す"""
        async def fake_async_generate(text, attachments, context):
            return ChatResponse(text="fallback resp", sources=[{"title": "src"}])

        monkeypatch.setattr(gemini_provider, "async_generate", fake_async_generate)

        events = _run_async(_collect_stream(
            gemini_provider, "q", [], {"use_google_search": True},
        ))

        assert len(events) == 2
        assert events[0] == {"type": "chunk", "text": "fallback resp"}
        assert events[1]["sources"] == [{"title": "src"}]

    def test_google_search_failure_has_no_success_events(
        self, gemini_provider, monkeypatch
    ):
        """Buffered Gemini failure propagates instead of becoming saved text."""
        async def failed_generate(text, attachments, context):
            raise RuntimeError("Gemini generation failed")

        monkeypatch.setattr(gemini_provider, "async_generate", failed_generate)

        with pytest.raises(RuntimeError, match="Gemini generation failed"):
            _run_async(
                _collect_stream(
                    gemini_provider,
                    "q",
                    [],
                    {"use_google_search": True},
                )
            )

    def test_generate_sanitizes_google_search_provider_exception(
        self, gemini_provider, monkeypatch
    ):
        raw = "secret-token at /private/provider/path"

        def failed_search(*args, **kwargs):
            raise RuntimeError(raw)

        monkeypatch.setattr(
            gemini_provider, "_try_search_with_retry", failed_search
        )
        with pytest.raises(RuntimeError) as caught:
            gemini_provider.generate(
                "q",
                [],
                {
                    "brain": MagicMock(),
                    "memory_manager": MagicMock(),
                    "use_google_search": True,
                },
            )
        assert str(caught.value) == "Gemini generation failed"
        assert raw not in str(caught.value)
