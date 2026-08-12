"""Chat SSE request registry lifecycle and idempotency tests."""

import asyncio
from typing import AsyncIterator

import pytest
from pydantic import BaseModel

from butly_api.chat_requests import (
    ChatFinalizingSignal,
    ChatRequestRecord,
    ChatRequestRegistry,
)
from butly_api.errors import ApiException
from butly_api.schemas.chat import (
    ChatChunkData,
    ChatChunkEvent,
    ChatDone,
    ChatDoneEvent,
    ChatErrorEvent,
)
from butly_api.schemas.common import ApiError


def _run(coro):
    return asyncio.run(coro)


async def _collect(registry, record):
    return [event async for event in registry.stream(record)]


def _done(request_id: str, text: str = "ok") -> ChatDoneEvent:
    return ChatDoneEvent(
        request_id=request_id,
        data=ChatDone(full_text=text),
    )


def test_cancel_before_subscription_never_invokes_event_source():
    async def scenario():
        registry = ChatRequestRegistry()
        calls = 0

        async def source(
            record: ChatRequestRecord,
        ) -> AsyncIterator[BaseModel]:
            nonlocal calls
            calls += 1
            yield _done(record.request_id)

        record = await registry.start_or_attach(
            request_id="transport-1",
            client_request_id="logical-1",
            fingerprint="same",
            event_source_factory=source,
        )
        assert record.task is None
        await registry.cancel(record)

        assert calls == 0
        assert record.state == "cancelled"
        assert record.retryable is True
        assert record.event_source_factory is None
        assert record.start_watchdog is None
        events = await _collect(registry, record)
        assert [event.event for event in events] == ["error"]
        assert events[0].data.code == "request_cancelled"

        retry = await registry.start_or_attach(
            request_id="transport-2",
            client_request_id="logical-1",
            fingerprint="same",
            event_source_factory=source,
        )
        retry_events = await _collect(registry, retry)
        assert retry is not record
        assert retry.attempt == 2
        assert retry.state == "completed"
        assert retry_events[-1].request_id == "transport-2"
        assert calls == 1

    _run(scenario())


def test_finalizing_cancel_is_declined_and_completed_replay_is_exactly_once():
    async def scenario():
        registry = ChatRequestRegistry()
        release = asyncio.Event()
        entered_finalizing = asyncio.Event()
        drives = 0
        saves = 0

        async def source(
            record: ChatRequestRecord,
        ) -> AsyncIterator[BaseModel]:
            nonlocal drives, saves
            drives += 1
            yield ChatFinalizingSignal()
            entered_finalizing.set()
            await release.wait()
            saves += 1
            yield _done(record.request_id, "saved once")

        record = await registry.start_or_attach(
            request_id="transport-final",
            client_request_id="logical-final",
            fingerprint="same",
            event_source_factory=source,
        )
        consumer = asyncio.create_task(_collect(registry, record))
        await entered_finalizing.wait()
        await asyncio.sleep(0)

        status = (await registry.cancel(record)).public_status()
        assert status.state == "finalizing"
        assert status.cancellable is False
        assert consumer.done() is False

        release.set()
        events = await consumer
        await asyncio.sleep(0)
        assert [event.event for event in events] == ["done"]
        assert record.state == "completed"
        assert record.task is None
        assert record.event_source_factory is None
        assert saves == 1

        attached = await registry.start_or_attach(
            request_id="transport-replay",
            client_request_id="logical-final",
            fingerprint="same",
            event_source_factory=source,
        )
        assert attached is record
        replay = await _collect(registry, attached)
        assert drives == 1
        assert saves == 1
        assert replay
        assert {event.request_id for event in replay} == {"transport-final"}
        assert replay[-1].data.full_text == "saved once"

    _run(scenario())


def test_retryable_failure_starts_new_attempt_with_same_logical_id():
    async def scenario():
        registry = ChatRequestRegistry()
        drives = 0

        async def source(
            record: ChatRequestRecord,
        ) -> AsyncIterator[BaseModel]:
            nonlocal drives
            drives += 1
            if drives == 1:
                yield ChatErrorEvent(
                    request_id=record.request_id,
                    data=ApiError(
                        code="temporary_failure",
                        message="Temporary failure.",
                        request_id=record.request_id,
                    ),
                    recoverable=True,
                )
                return
            yield _done(record.request_id, "retry ok")

        first = await registry.start_or_attach(
            request_id="transport-attempt-1",
            client_request_id="logical-retry",
            fingerprint="same",
            event_source_factory=source,
        )
        first_events = await _collect(registry, first)
        assert first.state == "failed"
        assert first.retryable is True
        assert first_events[-1].recoverable is True

        second = await registry.start_or_attach(
            request_id="transport-attempt-2",
            client_request_id="logical-retry",
            fingerprint="same",
            event_source_factory=source,
        )
        second_events = await _collect(registry, second)
        assert second is not first
        assert second.attempt == 2
        assert second.state == "completed"
        assert second_events[-1].request_id == "transport-attempt-2"
        assert drives == 2

    _run(scenario())


def test_nonretryable_failure_is_replayed_without_redrive():
    async def scenario():
        registry = ChatRequestRegistry()
        drives = 0

        async def source(
            record: ChatRequestRecord,
        ) -> AsyncIterator[BaseModel]:
            nonlocal drives
            drives += 1
            yield ChatErrorEvent(
                request_id=record.request_id,
                data=ApiError(
                    code="configuration_error",
                    message="Generation failed.",
                    request_id=record.request_id,
                ),
                recoverable=False,
            )

        first = await registry.start_or_attach(
            request_id="transport-failed",
            client_request_id="logical-failed",
            fingerprint="same",
            event_source_factory=source,
        )
        await _collect(registry, first)
        assert first.state == "failed"
        assert first.retryable is False

        attached = await registry.start_or_attach(
            request_id="transport-never-used",
            client_request_id="logical-failed",
            fingerprint="same",
            event_source_factory=source,
        )
        replay = await _collect(registry, attached)
        assert attached is first
        assert drives == 1
        assert replay[-1].request_id == "transport-failed"

    _run(scenario())


def test_response_over_replay_limit_fails_without_done():
    async def scenario():
        registry = ChatRequestRegistry()

        async def source(
            record: ChatRequestRecord,
        ) -> AsyncIterator[BaseModel]:
            yield ChatChunkEvent(
                request_id=record.request_id,
                sequence=0,
                data=ChatChunkData(text="x" * (2 * 1024 * 1024 + 1)),
            )
            yield _done(record.request_id, "must not be published")

        record = await registry.start_or_attach(
            request_id="transport-large",
            client_request_id=None,
            fingerprint="large",
            event_source_factory=source,
        )
        events = await _collect(registry, record)
        assert [event.event for event in events] == ["error"]
        assert events[0].data.code == "response_too_large"
        assert events[0].recoverable is False
        assert record.state == "failed"
        assert record.retryable is False

    _run(scenario())


def test_source_exhaustion_after_finalizing_is_never_retried():
    async def scenario():
        registry = ChatRequestRegistry()
        drives = 0
        committed_side_effects = 0

        async def source(
            _record: ChatRequestRecord,
        ) -> AsyncIterator[BaseModel]:
            nonlocal drives, committed_side_effects
            drives += 1
            yield ChatFinalizingSignal()
            committed_side_effects += 1
            # Simulates an ambiguous post-commit source failure without done.

        first = await registry.start_or_attach(
            request_id="transport-ambiguous",
            client_request_id="logical-ambiguous",
            fingerprint="same",
            event_source_factory=source,
        )
        events = await _collect(registry, first)
        assert first.state == "failed"
        assert first.retryable is False
        assert events[-1].event == "error"
        assert events[-1].data.code == "finalization_interrupted"
        assert events[-1].recoverable is False

        attached = await registry.start_or_attach(
            request_id="transport-second",
            client_request_id="logical-ambiguous",
            fingerprint="same",
            event_source_factory=source,
        )
        assert attached is first
        await _collect(registry, attached)
        assert drives == 1
        assert committed_side_effects == 1

    _run(scenario())


def test_active_generation_capacity_is_separate_from_replay_retention():
    async def scenario():
        registry = ChatRequestRegistry(max_active=2, max_entries=8)

        async def source(record):
            yield _done(record.request_id)

        first = await registry.start_or_attach(
            request_id="active-1",
            client_request_id="logical-active-1",
            fingerprint="one",
            event_source_factory=source,
        )
        await registry.start_or_attach(
            request_id="active-2",
            client_request_id="logical-active-2",
            fingerprint="two",
            event_source_factory=source,
        )
        with pytest.raises(ApiException) as caught:
            await registry.start_or_attach(
                request_id="active-3",
                client_request_id="logical-active-3",
                fingerprint="three",
                event_source_factory=source,
            )
        assert caught.value.status_code == 429
        assert caught.value.code == "chat_generation_capacity_exceeded"

        # A completed request remains replayable but no longer occupies an
        # active generation slot.
        await _collect(registry, first)
        third = await registry.start_or_attach(
            request_id="active-3",
            client_request_id="logical-active-3",
            fingerprint="three",
            event_source_factory=source,
        )
        assert third.state == "running"
        await registry.cancel(third)
        second = await registry.get("active-2")
        await registry.cancel(second)

    _run(scenario())


def test_shutdown_drains_finalizing_request_instead_of_cancelling_it():
    async def scenario():
        registry = ChatRequestRegistry()
        release = asyncio.Event()
        entered = asyncio.Event()
        commits = 0

        async def source(record):
            nonlocal commits
            yield ChatFinalizingSignal()
            entered.set()
            await release.wait()
            commits += 1
            yield _done(record.request_id)

        record = await registry.start_or_attach(
            request_id="shutdown-finalizing",
            client_request_id="logical-shutdown",
            fingerprint="same",
            event_source_factory=source,
        )
        consumer = asyncio.create_task(_collect(registry, record))
        await entered.wait()

        shutdown = asyncio.create_task(registry.cancel_all())
        await asyncio.sleep(0)
        assert shutdown.done() is False
        assert record.state == "finalizing"
        release.set()
        await shutdown
        await consumer
        assert commits == 1
        assert record.state == "completed"

    _run(scenario())


def test_last_subscriber_disconnect_cancels_before_persistence():
    async def scenario():
        registry = ChatRequestRegistry()
        source_cancelled = asyncio.Event()
        committed = 0

        async def source(record):
            nonlocal committed
            try:
                yield ChatChunkEvent(
                    request_id=record.request_id,
                    sequence=0,
                    data=ChatChunkData(text="partial"),
                )
                await asyncio.Event().wait()
                yield ChatFinalizingSignal()
                committed += 1
                yield _done(record.request_id)
            finally:
                source_cancelled.set()

        record = await registry.start_or_attach(
            request_id="disconnect-running",
            client_request_id="logical-disconnect",
            fingerprint="same",
            event_source_factory=source,
        )
        consumer = registry.stream(record)
        first = await anext(consumer)
        assert first.event == "chunk"
        await consumer.aclose()

        await source_cancelled.wait()
        assert committed == 0
        assert record.state == "cancelled"
        assert record.retryable is True
        assert record.error.code == "client_disconnected"
        assert record.subscribers == 0

    _run(scenario())


def test_oversized_done_metadata_is_shed_before_replay():
    async def scenario():
        registry = ChatRequestRegistry()

        async def source(record):
            yield ChatDoneEvent(
                request_id=record.request_id,
                data=ChatDone(
                    full_text="ok",
                    sources=[
                        {"title": "t" * 1000, "url": "u" * 1000}
                        for _ in range(4000)
                    ],
                ),
            )

        record = await registry.start_or_attach(
            request_id="oversized-done",
            client_request_id="logical-oversized-done",
            fingerprint="same",
            event_source_factory=source,
        )
        events = await _collect(registry, record)
        assert record.state == "completed"
        assert events[-1].event == "done"
        assert events[-1].data.full_text == "ok"
        assert events[-1].data.sources == []
        assert record.replay_bytes <= 6 * 1024 * 1024

    _run(scenario())
