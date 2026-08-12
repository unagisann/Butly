"""Process-local chat request lifecycle / idempotency registry."""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, AsyncIterator, Callable, Dict, List, Optional

from pydantic import BaseModel

from butly_api.errors import ApiException
from butly_api.schemas.chat import (
    ChatChunkData,
    ChatChunkEvent,
    ChatDone,
    ChatDoneEvent,
    ChatErrorEvent,
    ChatRequestStatus,
)
from butly_api.schemas.common import ApiError

EventSourceFactory = Callable[["ChatRequestRecord"], AsyncIterator[BaseModel]]
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_MAX_STREAM_TEXT_BYTES = 2 * 1024 * 1024
_MAX_REPLAY_BYTES = 6 * 1024 * 1024
_MAX_DONE_EVENT_BYTES = 3 * 1024 * 1024


class ReplayBufferExceeded(Exception):
    pass


class ChatFinalizingSignal(BaseModel):
    """Internal-only barrier emitted immediately before persistence."""

    event: str = "__finalizing"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ChatRequestRecord:
    request_id: str
    client_request_id: Optional[str]
    fingerprint: str
    attempt: int
    created_at: datetime = field(default_factory=_utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    state: str = "running"
    retryable: bool = False
    error: Optional[ApiError] = None
    events: List[BaseModel] = field(default_factory=list)
    task: Optional[asyncio.Task[None]] = None
    event_source_factory: Optional[EventSourceFactory] = None
    start_watchdog: Optional[asyncio.Task[None]] = None
    ever_subscribed: bool = False
    finalizing: bool = False
    stream_text_bytes: int = 0
    replay_bytes: int = 0
    cancel_code: str = "request_cancelled"
    cancel_message: str = "Generation was cancelled."
    subscribers: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    def public_status(self) -> ChatRequestStatus:
        return ChatRequestStatus(
            request_id=self.request_id,
            client_request_id=self.client_request_id,
            attempt=self.attempt,
            state=self.state,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            retryable=self.retryable,
            cancellable=self.state == "running" and not self.finalizing,
            error=self.error,
        )


class ChatRequestRegistry:
    """Bounded registry used for cancel, status, replay, and retry attempts."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_entries: int = 64,
        max_active: int = 4,
    ):
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_entries = max_entries
        self._max_active = max_active
        self._records: Dict[str, ChatRequestRecord] = {}
        self._by_client_id: Dict[str, ChatRequestRecord] = {}
        self._lock = asyncio.Lock()

    def _remove(self, record: ChatRequestRecord) -> None:
        self._records.pop(record.request_id, None)
        client_id = record.client_request_id
        if client_id and self._by_client_id.get(client_id) is record:
            self._by_client_id.pop(client_id, None)

    @staticmethod
    def _clear_watchdog(record: ChatRequestRecord) -> None:
        watchdog = record.start_watchdog
        record.start_watchdog = None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()

    def _purge(self) -> None:
        cutoff = _utcnow() - self._ttl
        expired = [
            record
            for record in self._records.values()
            if record.finished_at is not None and record.finished_at < cutoff
        ]
        for record in expired:
            self._remove(record)

        if len(self._records) < self._max_entries:
            return
        terminal = sorted(
            (r for r in self._records.values() if r.finished_at is not None),
            key=lambda r: r.finished_at or r.created_at,
        )
        while terminal and len(self._records) >= self._max_entries:
            self._remove(terminal.pop(0))

    @staticmethod
    def _compact_completed_events(record: ChatRequestRecord) -> None:
        """多数 chunk を単一 chunk に畳み、replay の不変条件を保つ。"""
        if record.state != "completed" or record.subscribers:
            return
        metadata = [
            event for event in record.events if getattr(event, "event", None) == "metadata"
        ]
        done = next(
            (
                event
                for event in reversed(record.events)
                if isinstance(event, ChatDoneEvent)
            ),
            None,
        )
        if done is None:
            return
        compacted: List[BaseModel] = list(metadata[:1])
        if done.data.full_text:
            compacted.append(
                ChatChunkEvent(
                    request_id=record.request_id,
                    sequence=0,
                    data=ChatChunkData(text=done.data.full_text),
                )
            )
        compacted.append(done)
        record.events = compacted
        record.replay_bytes = sum(
            len(event.model_dump_json().encode("utf-8")) for event in compacted
        )

    async def start_or_attach(
        self,
        *,
        request_id: str,
        client_request_id: Optional[str],
        fingerprint: str,
        event_source_factory: EventSourceFactory,
    ) -> ChatRequestRecord:
        async with self._lock:
            self._purge()
            previous = (
                self._by_client_id.get(client_request_id)
                if client_request_id
                else None
            )
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ApiException(
                        409,
                        "idempotency_conflict",
                        "client_request_id was already used with a different payload.",
                    )
                if previous.state in {"running", "finalizing", "completed"}:
                    return previous
                if not previous.retryable:
                    return previous
                attempt = previous.attempt + 1
                retrying_terminal = True
            else:
                attempt = 1
                retrying_terminal = False

            active_count = sum(
                record.state in {"running", "finalizing"}
                for record in self._records.values()
            )
            if active_count >= self._max_active:
                raise ApiException(
                    429,
                    "chat_generation_capacity_exceeded",
                    "Too many chat generations are active. Try again later.",
                )

            if request_id in self._records:
                if retrying_terminal:
                    request_id = str(uuid.uuid4())
                else:
                    raise ApiException(
                        409,
                        "request_id_conflict",
                        "X-Request-ID is already in use by another chat request.",
                    )

            self._purge()
            if len(self._records) >= self._max_entries:
                raise ApiException(
                    429,
                    "chat_request_capacity_exceeded",
                    "Too many chat requests are being tracked. Try again later.",
                )

            record = ChatRequestRecord(
                request_id=request_id,
                client_request_id=client_request_id,
                fingerprint=fingerprint,
                attempt=attempt,
                event_source_factory=event_source_factory,
            )
            self._records[request_id] = record
            if client_request_id:
                self._by_client_id[client_request_id] = record
            record.start_watchdog = asyncio.create_task(
                self._cancel_if_never_subscribed(record),
                name=f"butly-chat-watchdog-{request_id}",
            )
            return record

    async def _cancel_if_never_subscribed(
        self, record: ChatRequestRecord
    ) -> None:
        await asyncio.sleep(5)
        if record.state == "running" and not record.ever_subscribed:
            await self.cancel(
                record,
                code="client_disconnected",
                message="The client disconnected before generation started.",
            )

    def _ensure_started(self, record: ChatRequestRecord) -> None:
        if record.task is not None or record.state != "running":
            return
        factory = record.event_source_factory
        if factory is None:
            return
        record.event_source_factory = None
        record.task = asyncio.create_task(
            self._drive(record, factory),
            name=f"butly-chat-{record.request_id}",
        )
        record.task.add_done_callback(lambda _task: setattr(record, "task", None))

    async def _publish(
        self, record: ChatRequestRecord, event: BaseModel
    ) -> None:
        if isinstance(event, ChatChunkEvent):
            chunk_bytes = len(event.data.text.encode("utf-8"))
            if record.stream_text_bytes + chunk_bytes > _MAX_STREAM_TEXT_BYTES:
                raise ReplayBufferExceeded
            record.stream_text_bytes += chunk_bytes
        event_bytes = len(event.model_dump_json().encode("utf-8"))
        reserve = _MAX_DONE_EVENT_BYTES if isinstance(event, ChatChunkEvent) else 0
        if record.replay_bytes + event_bytes + reserve > _MAX_REPLAY_BYTES:
            raise ReplayBufferExceeded
        async with record.condition:
            record.events.append(event)
            record.replay_bytes += event_bytes
            record.condition.notify_all()
        self._compact_completed_events(record)

    async def _finish(
        self,
        record: ChatRequestRecord,
        *,
        state: str,
        retryable: bool,
        error: Optional[ApiError] = None,
    ) -> None:
        self._clear_watchdog(record)
        record.event_source_factory = None
        async with record.condition:
            record.state = state
            record.retryable = retryable
            record.error = error
            record.finished_at = _utcnow()
            record.condition.notify_all()
        self._compact_completed_events(record)

    async def _drive(
        self, record: ChatRequestRecord, event_source_factory: EventSourceFactory
    ) -> None:
        record.started_at = _utcnow()
        terminal_seen = False
        try:
            async for event in event_source_factory(record):
                if isinstance(event, ChatFinalizingSignal):
                    record.finalizing = True
                    record.state = "finalizing"
                    continue
                event_name = getattr(event, "event", None)
                if event_name == "done":
                    terminal_seen = True
                    # ChatService persists immediately before yielding done.
                    # Commit state synchronously before the next await so a cancel
                    # cannot turn a persisted request into a retryable cancellation.
                    event_bytes = len(event.model_dump_json().encode("utf-8"))
                    if (
                        event_bytes > _MAX_DONE_EVENT_BYTES
                        or record.replay_bytes + event_bytes > _MAX_REPLAY_BYTES
                    ):
                        # Sources/debug are optional replay metadata. Preserve
                        # full_text and the success terminal after commit while
                        # shedding an oversized provider payload.
                        event = ChatDoneEvent(
                            request_id=record.request_id,
                            data=ChatDone(full_text=event.data.full_text),
                        )
                        event_bytes = len(
                            event.model_dump_json().encode("utf-8")
                        )
                    if record.replay_bytes + event_bytes > _MAX_REPLAY_BYTES:
                        raise ReplayBufferExceeded
                    record.events.append(event)
                    record.replay_bytes += event_bytes
                    record.state = "completed"
                    record.retryable = False
                    record.finished_at = _utcnow()
                    async with record.condition:
                        record.condition.notify_all()
                    self._compact_completed_events(record)
                    return
                await self._publish(record, event)
                if event_name == "error":
                    terminal_seen = True
                    api_error = getattr(event, "data", None)
                    recoverable = bool(getattr(event, "recoverable", False))
                    if record.finalizing:
                        recoverable = False
                    await self._finish(
                        record,
                        state="failed",
                        retryable=recoverable,
                        error=api_error,
                    )
                    return
            if not terminal_seen:
                finalization_interrupted = record.finalizing
                error = ApiError(
                    code=(
                        "finalization_interrupted"
                        if finalization_interrupted
                        else "stream_interrupted"
                    ),
                    message=(
                        "Generation finalization was interrupted."
                        if finalization_interrupted
                        else "Generation ended before a terminal event."
                    ),
                    request_id=record.request_id,
                )
                await self._publish(
                    record,
                    ChatErrorEvent(
                        request_id=record.request_id,
                        data=error,
                        recoverable=not finalization_interrupted,
                    ),
                )
                await self._finish(
                    record,
                    state="failed",
                    retryable=not finalization_interrupted,
                    error=error,
                )
        except ReplayBufferExceeded:
            error = ApiError(
                code="response_too_large",
                message="Generated response exceeded the replay safety limit.",
                request_id=record.request_id,
            )
            event = ChatErrorEvent(
                request_id=record.request_id,
                data=error,
                recoverable=False,
            )
            async with record.condition:
                record.events.append(event)
                record.replay_bytes += len(
                    event.model_dump_json().encode("utf-8")
                )
                record.condition.notify_all()
            await self._finish(
                record, state="failed", retryable=False, error=error
            )
        except asyncio.CancelledError:
            if record.state == "completed":
                async with record.condition:
                    record.condition.notify_all()
                return
            if record.finalizing:
                error = ApiError(
                    code="finalization_interrupted",
                    message="Generation finalization was interrupted.",
                    request_id=record.request_id,
                )
                await self._publish(
                    record,
                    ChatErrorEvent(
                        request_id=record.request_id,
                        data=error,
                        recoverable=False,
                    ),
                )
                await self._finish(
                    record, state="failed", retryable=False, error=error
                )
                return
            error = ApiError(
                code=record.cancel_code,
                message=record.cancel_message,
                request_id=record.request_id,
            )
            await self._publish(
                record,
                ChatErrorEvent(
                    request_id=record.request_id,
                    data=error,
                    recoverable=True,
                ),
            )
            await self._finish(
                record, state="cancelled", retryable=True, error=error
            )
        except Exception:
            error = ApiError(
                code="internal_error",
                message="An internal error occurred.",
                request_id=record.request_id,
            )
            await self._publish(
                record,
                ChatErrorEvent(
                    request_id=record.request_id,
                    data=error,
                    recoverable=False,
                ),
            )
            await self._finish(
                record, state="failed", retryable=False, error=error
            )

    async def get(self, request_id: str) -> ChatRequestRecord:
        async with self._lock:
            self._purge()
            record = self._records.get(request_id)
        if record is None:
            raise ApiException(
                404,
                "chat_request_not_found",
                "Chat request was not found or has expired.",
            )
        return record

    async def cancel(
        self,
        record: ChatRequestRecord,
        *,
        code: str = "request_cancelled",
        message: str = "Generation was cancelled.",
    ) -> ChatRequestRecord:
        if record.state != "running":
            return record
        if record.task is None:
            record.event_source_factory = None
            error = ApiError(
                code=code,
                message=message,
                request_id=record.request_id,
            )
            await self._publish(
                record,
                ChatErrorEvent(
                    request_id=record.request_id,
                    data=error,
                    recoverable=True,
                ),
            )
            await self._finish(
                record, state="cancelled", retryable=True, error=error
            )
            return record
        record.cancel_code = code
        record.cancel_message = message
        task = record.task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return record

    async def stream(
        self, record: ChatRequestRecord
    ) -> AsyncGenerator[BaseModel, None]:
        index = 0
        async with record.condition:
            record.subscribers += 1
            record.ever_subscribed = True
            if record.start_watchdog is not None:
                self._clear_watchdog(record)
            self._ensure_started(record)
        try:
            while True:
                async with record.condition:
                    await record.condition.wait_for(
                        lambda: index < len(record.events)
                        or record.state in _TERMINAL_STATES
                    )
                    pending = list(record.events[index:])
                    terminal = record.state in _TERMINAL_STATES
                for event in pending:
                    index += 1
                    yield event
                if terminal and index >= len(record.events):
                    return
        finally:
            should_cancel = False
            async with record.condition:
                record.subscribers = max(0, record.subscribers - 1)
                should_cancel = (
                    record.subscribers == 0 and record.state == "running"
                )
                if record.state == "completed" and record.subscribers == 0:
                    self._compact_completed_events(record)
            if should_cancel:
                await self.cancel(
                    record,
                    code="client_disconnected",
                    message="The client disconnected before generation completed.",
                )

    async def cancel_all(self) -> None:
        records = list(self._records.values())
        await asyncio.gather(
            *(
                self.cancel(
                    record,
                    code="backend_shutting_down",
                    message="The backend is shutting down.",
                )
                for record in records
                if record.state == "running"
            ),
            return_exceptions=True,
        )
        # A finalizing request may already have committed state/history and is
        # deliberately non-cancellable. Graceful shutdown drains these tasks
        # so process teardown cannot interrupt the exactly-once commit phase.
        finalizing_tasks = [
            record.task
            for record in self._records.values()
            if record.state == "finalizing" and record.task is not None
        ]
        if finalizing_tasks:
            await asyncio.gather(*finalizing_tasks, return_exceptions=True)
