"""Focused tests for LoCoMo-only transient QA retries."""

import asyncio
import json

import pytest

from butly_core.chat.types import ChatResponse
from evals.locomo.dataset import LocomoQuestion
from evals.locomo.qa_runner import (
    QARetryPolicy,
    QARunner,
    classify_transient_qa_error,
)
from evals.locomo.workspace import EvaluationWorkspace


class _Runtime:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def chat(self, request):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _HttpError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def _wrapped(cause):
    try:
        raise cause
    except Exception as exc:
        try:
            raise RuntimeError("Provider generation failed") from exc
        except RuntimeError as wrapped:
            return wrapped


def _question():
    return LocomoQuestion(
        question_id="qa-1",
        question="When?",
        answer="Monday",
        category=2,
        evidence=["D1:1"],
    )


def _success():
    return ChatResponse(
        text="Monday",
        debug_info={"rag": {"results": []}},
    )


def _workspace(tmp_path):
    workspace = EvaluationWorkspace.create(tmp_path, run_id="qa-retry")
    trace_dir = workspace.instances_dir / "locomo_sample" / "traces"
    trace_dir.mkdir(parents=True)
    (trace_dir / "latest.json").write_text("{}", encoding="utf-8")
    return workspace


def test_transient_nested_connection_error_retries_and_records(tmp_path):
    runtime = _Runtime(
        [
            _wrapped(ConnectionError("disconnected")),
            _wrapped(TimeoutError()),
            _success(),
        ]
    )
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    runner = QARunner(
        runtime,
        _workspace(tmp_path),
        qa_mode="independent",
        retry_sleep=fake_sleep,
    )

    result = asyncio.run(
        runner.run(
            sample_id="sample",
            instance_name="locomo_sample",
            question=_question(),
        )
    )

    assert runtime.calls == 3
    assert delays == [1.0, 2.0]
    retry = result["diagnostics"]["qa_retry"]
    assert retry["attempts"] == 3
    assert retry["retry_count"] == 2
    assert [item["reason"] for item in retry["failures"]] == [
        "connection",
        "timeout",
    ]
    retry_rows = [
        json.loads(line)
        for line in runner.retry_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["will_retry"] for row in retry_rows] == [True, True]


def test_retry_exhaustion_is_four_calls_and_last_event_is_terminal(tmp_path):
    runtime = _Runtime([_wrapped(ConnectionError("down")) for _ in range(4)])
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    runner = QARunner(
        runtime,
        _workspace(tmp_path),
        qa_mode="independent",
        retry_sleep=fake_sleep,
    )

    with pytest.raises(RuntimeError, match="Provider generation failed"):
        asyncio.run(
            runner.run(
                sample_id="sample",
                instance_name="locomo_sample",
                question=_question(),
            )
        )

    assert runtime.calls == 4
    assert delays == [1.0, 2.0, 4.0]
    rows = [
        json.loads(line)
        for line in runner.retry_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 4
    assert rows[-1]["will_retry"] is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_client_and_configuration_errors_are_not_retryable(status):
    error = _wrapped(_HttpError("bad request", status))
    assert classify_transient_qa_error(error) is None


def test_plain_provider_error_is_not_assumed_transient():
    error = RuntimeError("Provider generation failed")
    assert classify_transient_qa_error(error) is None


def test_retry_policy_uses_capped_exponential_backoff():
    policy = QARetryPolicy(base_delay_seconds=1.0, max_delay_seconds=4.0)
    assert [policy.delay_for_retry(index) for index in range(1, 5)] == [
        1.0,
        2.0,
        4.0,
        4.0,
    ]
