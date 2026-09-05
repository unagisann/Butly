"""Runtime embedding recovery, bounded fallback, and trace diagnostics."""

from unittest.mock import Mock

import numpy as np
import pytest

from butly_core.core.brain import ButlyBrain
from butly_core.core.evidence_fusion import RuntimeEvidenceFusion, default_embedder
from butly_core.llm import embedding_retry
from butly_core.trace.collector import (
    get_collected, reset_collection, start_collection,
)


class ApiError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"{code} provider error")


@pytest.fixture
def waits(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(embedding_retry.time, "sleep", sleep)
    monkeypatch.setattr(embedding_retry.random, "uniform", lambda a, b: b)
    return sleep


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_error_recovers_with_bounded_jitter(status, waits):
    call = Mock(side_effect=[ApiError(status), ApiError(status), [1.0, 0.0]])
    diag = {}
    assert embedding_retry.embed_with_retry(call, diag) == [1.0, 0.0]
    assert [c.args[0] for c in waits.call_args_list] == [1.25, 2.25]
    assert diag["retry_count"] == 2
    assert diag["retry_wait_ms"] == 3500
    assert diag["rate_limit_count"] == (2 if status == 429 else 0)
    assert diag["retry_exhausted"] is False


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors_do_not_retry(status, waits):
    call = Mock(side_effect=ApiError(status))
    with pytest.raises(ApiError):
        embedding_retry.embed_with_retry(call, {})
    assert call.call_count == 1
    waits.assert_not_called()


def test_exception_text_cannot_override_explicit_client_status(waits):
    error = ApiError(400)
    error.args = ("429 in an invalid request",)
    assert embedding_retry.transient_embedding_status(error) is None
    assert embedding_retry.transient_embedding_status(
        RuntimeError("model 429 not found")
    ) is None


@pytest.mark.parametrize("route", ["brain", "evidence"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_both_runtime_paths_retry_and_record_outcome(
    tmp_path, monkeypatch, waits, route, exhausted,
):
    provider = Mock()
    provider.pop_last_token_usage.return_value = None
    provider.embed.side_effect = (
        ApiError(429) if exhausted else [ApiError(429), [1.0, 0.0]]
    )
    conf = {"connection": "google", "model_name": "gemini-embedding-2"}
    brain = ButlyBrain(tmp_path)
    monkeypatch.setattr(brain, "_get_provider", lambda c: provider)
    monkeypatch.setattr(
        "butly_core.core.evidence_fusion.ProviderFactory.create", lambda c: provider
    )
    token = start_collection()
    try:
        if route == "brain":
            result = brain.get_embedding("question", conf)
            assert result == (None if exhausted else [1.0, 0.0])
        elif exhausted:
            with pytest.raises(ApiError):
                default_embedder(conf)("question", "query")
        else:
            assert default_embedder(conf)("question", "query") == [1.0, 0.0]
        calls = get_collected()
    finally:
        reset_collection(token)
    assert len(calls) == 1
    diag = calls[0]["metadata"]
    assert diag["retry_count"] == (2 if exhausted else 1)
    assert diag["retry_exhausted"] is exhausted
    assert bool(calls[0]["error"]) is exhausted
    assert provider.embed.call_count == (3 if exhausted else 2)
    assert provider.embed.call_args.args[0].startswith(
        "task: question answering | query: "
    )


def test_failed_evidence_document_stops_remaining_requests_and_is_not_cached(
    tmp_path, monkeypatch, waits,
):
    provider = Mock()
    provider.embed.side_effect = ApiError(503)
    monkeypatch.setattr(
        "butly_core.core.evidence_fusion.ProviderFactory.create", lambda c: provider
    )
    fusion = RuntimeEvidenceFusion(
        tmp_path, {"model_name": "gemini-embedding-2"},
        cache_path=tmp_path / "cache.sqlite3",
    )
    candidates = [
        {"id": str(i), "episode": f"Evidence {i}"} for i in range(20)
    ]
    try:
        result = fusion.rerank(
            "question", candidates, default_instance="test", top_n=3,
            query_vector=np.array([1.0, 0.0]),
        )
        assert fusion.cache.diagnostics()["writes"] == 0
    finally:
        fusion.close()
    assert provider.embed.call_count == 3
    assert result["fallback"] is True
    assert result["selected_candidate_ids"] == ["0", "1", "2"]


def test_fusion_query_exhaustion_does_not_start_second_brain_retry(
    tmp_path, monkeypatch, waits,
):
    provider = Mock()
    provider.embed.side_effect = ApiError(429)
    monkeypatch.setattr(
        "butly_core.core.evidence_fusion.ProviderFactory.create", lambda c: provider
    )
    brain = ButlyBrain(tmp_path)
    monkeypatch.setattr(
        brain, "get_embedding", Mock(side_effect=AssertionError("duplicate query"))
    )
    result = brain._hybrid_evidence_fusion_search_diag(
        "question", ["test"], default_instance="test", limit=3,
        threshold=None, brain_conf={},
        embedding_conf={"model_name": "gemini-embedding-2"},
    )
    assert provider.embed.call_count == 3
    assert result["diagnostics"]["evidence_fusion"]["fallback"] is True
