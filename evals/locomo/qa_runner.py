"""Run LoCoMo questions through the existing ButlyRuntime chat path."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from butly_core.chat.types import ChatRequest
from butly_core.runtime import ButlyRuntime

from .artifacts import (
    append_jsonl,
    copy_latest_trace,
    resolve_retrieved_card_ids,
)
from .dataset import LocomoQuestion
from .workspace import EvaluationWorkspace


@dataclass(frozen=True)
class QARetryPolicy:
    """Retry policy for transient QA transport failures.

    ``max_retries`` counts retries after the initial request.  The production
    default therefore permits at most four provider calls for one question.
    Keeping this policy in the evaluation runner prevents normal chat from
    silently replaying a user turn.
    """

    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 4.0

    def delay_for_retry(self, retry_number: int) -> float:
        delay = self.base_delay_seconds * (2 ** max(retry_number - 1, 0))
        return min(delay, self.max_delay_seconds)


def _exception_chain(exc: BaseException):
    """Yield an exception and its explicit/implicit causes without cycles."""

    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(exc: BaseException) -> Optional[int]:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_transient_qa_error(exc: BaseException) -> Optional[str]:
    """Return a stable retry reason for transport/timeouts, otherwise None.

    Authentication, bad requests, unknown models, and other configuration
    errors deliberately remain non-retryable.  Provider adapters often wrap
    the useful SDK exception, so the complete cause chain is inspected.
    """

    timeout_names = {
        "apitimeouterror",
        "connecttimeout",
        "pooltimeout",
        "readtimeout",
        "timeout",
        "timeouterror",
        "writetimeout",
    }
    connection_names = {
        "apiconnectionerror",
        "connecterror",
        "connectionerror",
        "networkerror",
        "readerror",
        "remoteprotocolerror",
        "transporterror",
        "writeerror",
    }
    for current in _exception_chain(exc):
        status = _status_code(current)
        if status is not None and 400 <= status < 500:
            return None
        name = type(current).__name__.lower()
        if isinstance(current, (TimeoutError, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(current, ConnectionError):
            return "connection"
        if name in timeout_names or "timeout" in name:
            return "timeout"
        if name in connection_names:
            return "connection"
        message = str(current).lower()
        if "server disconnected without sending a response" in message:
            return "connection"
    return None


def _retry_error_summary(exc: BaseException) -> dict:
    chain = list(_exception_chain(exc))
    root = chain[-1] if chain else exc
    message = str(root) or str(exc)
    return {
        "error_type": type(root).__name__,
        "message": message[:500],
    }


def build_qa_request(
    *,
    question: str,
    instance_name: str,
    model_name: Optional[str] = None,
    connection: Optional[str] = None,
) -> ChatRequest:
    """Build the fixed evaluation request policy: RAG on, external search off."""
    return ChatRequest(
        text=question,
        instance_name=instance_name,
        model_name=model_name,
        connection=connection,
        use_rag=True,
        use_google_search=False,
        use_web_search=False,
        source="api",
        metadata={"evaluation": "locomo", "phase": 2},
    )


class QARunner:
    def __init__(
        self,
        runtime: ButlyRuntime,
        workspace: EvaluationWorkspace,
        *,
        qa_mode: Literal["independent", "sequential"],
        model_name: Optional[str] = None,
        connection: Optional[str] = None,
        instances_dir: Optional[Path] = None,
        retry_policy: Optional[QARetryPolicy] = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.runtime = runtime
        self.workspace = workspace
        self.model_name = model_name
        self.connection = connection
        self.instances_dir = (
            Path(instances_dir)
            if instances_dir is not None
            else workspace.instances_dir
        )
        self.qa_mode = qa_mode
        self.log_path = workspace.results_dir / "qa_results.jsonl"
        self.retry_log_path = workspace.results_dir / "qa_retries.jsonl"
        self.retry_policy = retry_policy or QARetryPolicy()
        self.retry_sleep = retry_sleep

    async def _chat_with_retry(
        self,
        request: ChatRequest,
        *,
        sample_id: str,
        question_id: str,
    ):
        failures = []
        attempt = 1
        while True:
            try:
                response = await self.runtime.chat(request)
                return response, {
                    "attempts": attempt,
                    "retry_count": len(failures),
                    "max_retries": self.retry_policy.max_retries,
                    "failures": failures,
                }
            except Exception as exc:
                reason = classify_transient_qa_error(exc)
                retry_number = attempt
                can_retry = (
                    reason is not None
                    and retry_number <= self.retry_policy.max_retries
                )
                summary = _retry_error_summary(exc)
                failure = {
                    "attempt": attempt,
                    "reason": reason or "non_retryable",
                    **summary,
                    "will_retry": can_retry,
                }
                if can_retry:
                    delay = self.retry_policy.delay_for_retry(retry_number)
                    failure["backoff_seconds"] = delay
                failures.append(failure)
                append_jsonl(
                    self.retry_log_path,
                    {
                        "run_id": self.workspace.run_id,
                        "sample_id": sample_id,
                        "question_id": question_id,
                        **failure,
                    },
                )
                if not can_retry:
                    raise
                print(
                    f"[LoCoMo QA retry] {sample_id} {question_id} "
                    f"attempt {attempt} failed ({reason}: "
                    f"{summary['error_type']}); retrying in {delay:g}s",
                    flush=True,
                )
                await self.retry_sleep(delay)
                attempt += 1

    async def run(
        self,
        *,
        sample_id: str,
        instance_name: str,
        question: LocomoQuestion,
    ) -> dict:
        request = build_qa_request(
            question=question.question,
            instance_name=instance_name,
            model_name=self.model_name,
            connection=self.connection,
        )
        started = time.perf_counter()
        response, retry = await self._chat_with_retry(
            request,
            sample_id=sample_id,
            question_id=question.question_id,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        instance_dir = self.instances_dir / instance_name
        copied_trace = copy_latest_trace(
            instance_dir,
            self.workspace.traces_dir,
            question.question_id,
            sample_id=sample_id,
        )
        if copied_trace is None:
            raise RuntimeError(
                f"ButlyRuntime completed QA without a Trace: {question.question_id}"
            )

        debug_info = response.debug_info or {}
        rag = debug_info.get("rag") if isinstance(debug_info.get("rag"), dict) else {}
        rag_results = rag.get("results") if isinstance(rag.get("results"), list) else []
        retrieved_card_ids = resolve_retrieved_card_ids(
            instance_dir / "butly_memory.db",
            [item for item in rag_results if isinstance(item, dict)],
        )
        trace_path = copied_trace.relative_to(self.workspace.run_dir).as_posix()
        result = {
            "run_id": self.workspace.run_id,
            "sample_id": sample_id,
            "instance_name": instance_name,
            "qa_mode": self.qa_mode,
            "question_id": question.question_id,
            "question": question.question,
            "expected_answer": question.answer,
            "prediction": response.text,
            "category": question.category,
            "evidence": question.evidence,
            "tier": response.tier,
            "need": response.need,
            "refs": response.refs,
            "sources": response.sources,
            "retrieved_card_ids": retrieved_card_ids,
            "latency_ms": latency_ms,
            "trace_path": trace_path,
            "request": {
                "use_rag": request.use_rag,
                "use_google_search": request.use_google_search,
                "use_web_search": request.use_web_search,
                "source": request.source,
                "model_name": request.model_name,
                "connection": request.connection,
            },
            "diagnostics": {
                "gatekeeper": debug_info.get("gatekeeper", {}),
                "rag": rag,
                "timing": debug_info.get("timing", {}),
                "token_usage": debug_info.get("token_usage"),
                "token_usage_total": debug_info.get("token_usage_total"),
                "provider": debug_info.get("provider"),
                "model": debug_info.get("model"),
                "connection_id": debug_info.get("connection_id"),
                "qa_retry": retry,
            },
            "error": None,
        }
        append_jsonl(self.log_path, result)
        return result
