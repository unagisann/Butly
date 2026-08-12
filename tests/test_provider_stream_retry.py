"""
test_provider_stream_retry.py
-----------------------------
OpenAI 互換ストリームの「出力前に即座に失敗したら 1 回だけ再試行」の検証。

実際に起きた事象が出発点: NanoGPT が SSE を開いたまま本文を返さず、
``openai.APIError: Upstream returned an empty response`` で落ちた。同じ入力の
手動再送で成功したので transient と判断できる。手動再送は Gatekeeper からやり直しに
なるため、provider 層で 1 回だけ引き直すほうが体感が軽い。

再試行してよいのは「待ち時間をほとんど増やさない」種類だけで、timeout と 429 は
対象外。1 文字でも送出済みなら二重生成になるので再試行しない。
"""

import asyncio
from unittest.mock import MagicMock

import openai
import pytest

from butly_core.llm._openai_compat import (
    MAX_STREAM_ATTEMPTS,
    async_chat_completion_stream,
    is_retryable_stream_error,
)


def _chunk(text: str):
    chunk = MagicMock()
    choice = MagicMock()
    choice.delta.content = text
    chunk.choices = [choice]
    return chunk


def _empty_response_error() -> openai.APIError:
    """上流が本文なしでストリームを閉じたときに SDK が投げる形。"""
    return openai.APIError(
        "Upstream returned an empty response with no content or tool call.",
        request=MagicMock(),
        body=None,
    )


def _status_error(status: int) -> openai.APIStatusError:
    response = MagicMock()
    response.status_code = status
    return openai.APIStatusError("boom", response=response, body=None)


async def _collect(client) -> list:
    events = []
    async for event in async_chat_completion_stream(
        client=client,
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        chat_conf={},
    ):
        events.append(event)
    return events


class TestRetryPredicate:
    def test_empty_upstream_response_is_retryable(self):
        assert is_retryable_stream_error(_empty_response_error()) is True

    def test_connection_error_is_retryable(self):
        error = openai.APIConnectionError(request=MagicMock())
        assert is_retryable_stream_error(error) is True

    def test_timeout_is_not_retryable(self):
        """timeout の再試行は待ち時間が倍になる。体感を最も損なうので除外する。"""
        error = openai.APITimeoutError(request=MagicMock())
        assert isinstance(error, openai.APIConnectionError)
        assert is_retryable_stream_error(error) is False

    def test_rate_limit_is_not_retryable(self):
        assert is_retryable_stream_error(_status_error(429)) is False

    def test_auth_error_is_not_retryable(self):
        assert is_retryable_stream_error(_status_error(401)) is False

    def test_server_error_is_retryable(self):
        response = MagicMock()
        response.status_code = 500
        error = openai.InternalServerError("boom", response=response, body=None)
        assert is_retryable_stream_error(error) is True


class TestStreamRetry:
    def test_retries_once_and_recovers_without_surfacing_the_error(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _empty_response_error(),
            iter([_chunk("こん"), _chunk("にちは")]),
        ]

        events = asyncio.run(_collect(client))

        assert [e["type"] for e in events] == ["chunk", "chunk", "done"]
        assert events[-1]["full_text"] == "こんにちは"
        assert client.chat.completions.create.call_count == 2

    def test_gives_up_after_one_retry(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _empty_response_error(),
            _empty_response_error(),
        ]

        events = asyncio.run(_collect(client))

        assert [e["type"] for e in events] == ["error"]
        assert client.chat.completions.create.call_count == MAX_STREAM_ATTEMPTS

    def test_does_not_retry_after_the_first_chunk_was_sent(self):
        """送出済みの本文があると再試行は二重生成になる。"""

        def _fail_midway():
            yield _chunk("途中まで")
            raise _empty_response_error()

        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _fail_midway(),
            iter([_chunk("やり直し")]),
        ]

        events = asyncio.run(_collect(client))

        assert [e["type"] for e in events] == ["chunk", "error"]
        assert events[0]["text"] == "途中まで"
        assert client.chat.completions.create.call_count == 1

    def test_does_not_retry_a_timeout(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            openai.APITimeoutError(request=MagicMock()),
            iter([_chunk("使われない")]),
        ]

        events = asyncio.run(_collect(client))

        assert [e["type"] for e in events] == ["error"]
        assert client.chat.completions.create.call_count == 1

    def test_successful_stream_is_not_retried(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [iter([_chunk("ok")])]

        events = asyncio.run(_collect(client))

        assert [e["type"] for e in events] == ["chunk", "done"]
        assert client.chat.completions.create.call_count == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_client_side_failures_are_surfaced_immediately(status):
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _status_error(status),
        iter([_chunk("使われない")]),
    ]

    events = asyncio.run(_collect(client))

    assert [e["type"] for e in events] == ["error"]
    assert client.chat.completions.create.call_count == 1
