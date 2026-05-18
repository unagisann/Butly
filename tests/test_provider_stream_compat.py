"""
test_provider_stream_compat.py
------------------------------
OpenAI / xAI / Ollama (OpenAI 互換 SDK 経由) の async_generate_stream
のテスト。SDK レイヤーをモックして event 順序とエラーパスを検証する。
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _run_async(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def stub_compat_resolvers(monkeypatch):
    """memory_blocks の中身に依存させない: prompt 組み立てヘルパーをスタブ化"""
    monkeypatch.setattr(
        "butly_core.llm._openai_compat.resolve_system_instruction",
        lambda ctx: "SYS",
    )
    monkeypatch.setattr(
        "butly_core.llm._openai_compat.resolve_context_prefix",
        lambda ctx: "CTX",
    )


async def _collect(provider, text="hi", attachments=None, context=None):
    out = []
    async for event in provider.async_generate_stream(
        text=text,
        attachments=attachments or [],
        context=context or {"override_config": {}, "history": [],
                            "memory_manager": MagicMock(), "memory_blocks": {}},
    ):
        out.append(event)
    return out


def _mock_stream_chunks(texts: list):
    """OpenAI ChatCompletionChunk 風のモック iterator を返す。"""
    chunks = []
    for t in texts:
        c = MagicMock()
        choice = MagicMock()
        choice.delta.content = t
        c.choices = [choice]
        chunks.append(c)
    return iter(chunks)


class TestOpenAIStream:
    @pytest.fixture
    def provider(self):
        from butly_core.llm.providers.openai import OpenAIProvider
        p = OpenAIProvider()
        # avoid hitting real API key
        p._client = MagicMock()
        return p

    def test_yields_chunks_then_done(self, provider):
        provider._client.chat.completions.create.return_value = _mock_stream_chunks(
            ["Hello ", "world", "!"]
        )
        events = _run_async(_collect(provider))
        chunks = [e for e in events if e["type"] == "chunk"]
        assert [c["text"] for c in chunks] == ["Hello ", "world", "!"]
        assert events[-1]["type"] == "done"
        assert events[-1]["full_text"] == "Hello world!"

    def test_error_event_on_exception(self, provider):
        provider._client.chat.completions.create.side_effect = RuntimeError("boom")
        events = _run_async(_collect(provider))
        assert events[-1]["type"] == "error"
        assert "boom" in events[-1]["message"]

    def test_passes_stream_true(self, provider):
        provider._client.chat.completions.create.return_value = _mock_stream_chunks(["x"])
        _run_async(_collect(provider))
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("stream") is True


class TestXaiStream:
    @pytest.fixture
    def provider(self):
        from butly_core.llm.providers.xai import XaiProvider
        p = XaiProvider()
        p._client = MagicMock()
        return p

    def test_yields_chunks(self, provider):
        provider._client.chat.completions.create.return_value = _mock_stream_chunks(["こん", "にちは"])
        events = _run_async(_collect(provider))
        chunks = [e for e in events if e["type"] == "chunk"]
        assert [c["text"] for c in chunks] == ["こん", "にちは"]
        assert events[-1]["full_text"] == "こんにちは"

    def test_strips_xai_prefix(self, provider):
        provider._client.chat.completions.create.return_value = _mock_stream_chunks(["x"])
        ctx = {"override_config": {"chat": {"model_name": "xai/grok-4-1-fast"}},
               "history": [], "memory_manager": MagicMock(), "memory_blocks": {}}
        _run_async(_collect(provider, context=ctx))
        # xai/ prefix が除去されたモデル名が API に渡る
        passed_model = provider._client.chat.completions.create.call_args.kwargs.get("model", "")
        assert not passed_model.startswith("xai/")


class TestOllamaStream:
    @pytest.fixture
    def provider(self):
        from butly_core.llm.providers.ollama import OllamaProvider
        p = OllamaProvider()
        p._client = MagicMock()
        return p

    def test_yields_chunks(self, provider):
        provider._client.chat.completions.create.return_value = _mock_stream_chunks(["abc", "def"])
        events = _run_async(_collect(provider))
        assert [e["text"] for e in events if e["type"] == "chunk"] == ["abc", "def"]
        assert events[-1]["full_text"] == "abcdef"

    def test_strips_ollama_prefix(self, provider):
        provider._client.chat.completions.create.return_value = _mock_stream_chunks(["y"])
        ctx = {"override_config": {"chat": {"model_name": "ollama/llama3.2:8b"}},
               "history": [], "memory_manager": MagicMock(), "memory_blocks": {}}
        _run_async(_collect(provider, context=ctx))
        passed_model = provider._client.chat.completions.create.call_args.kwargs.get("model", "")
        assert not passed_model.startswith("ollama/")


class TestCompatHelperDirect:
    """async_chat_completion_stream を直接呼ぶテスト"""

    def test_chunks_with_none_choices_skipped(self):
        """choices が None や空のチャンクはスキップされる"""
        from butly_core.llm import _openai_compat as compat

        # None choices チャンク + 通常チャンク
        none_chunk = MagicMock()
        none_chunk.choices = None
        normal_chunk = MagicMock()
        normal_chunk.choices = [MagicMock()]
        normal_chunk.choices[0].delta.content = "valid"

        client = MagicMock()
        client.chat.completions.create.return_value = iter([none_chunk, normal_chunk])

        async def run():
            out = []
            async for event in compat.async_chat_completion_stream(
                client=client, model="m", messages=[], chat_conf={},
            ):
                out.append(event)
            return out

        events = _run_async(run())
        chunks = [e for e in events if e["type"] == "chunk"]
        assert len(chunks) == 1
        assert chunks[0]["text"] == "valid"
