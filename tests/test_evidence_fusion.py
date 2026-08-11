"""Runtime Episode/RAW evidence fusion and cache behavior."""

import numpy as np

from butly_core.core.evidence_fusion import RuntimeEvidenceFusion


def _candidates() -> list[dict]:
    return [
        {
            "id": "base-1",
            "source_instance": "Jarvis",
            "title": "First",
            "episode": "second best evidence",
        },
        {
            "id": "base-2",
            "source_instance": "Jarvis",
            "title": "Second",
            "episode": "third best evidence",
        },
        {
            "id": "base-3",
            "source_instance": "Jarvis",
            "title": "Third",
            "episode": "fourth best evidence",
        },
        {
            "id": "evidence-1",
            "source_instance": "Jarvis",
            "title": "Target",
            "episode": "needle fact from source conversation",
        },
    ]


def _embed(text: str, kind: str):
    if kind == "query" or "needle fact" in text:
        return [1.0, 0.0]
    if "second best" in text:
        return [0.9, 0.1]
    if "third best" in text:
        return [0.8, 0.2]
    return [0.7, 0.3]


def test_runtime_fusion_promotes_evidence_and_reuses_private_cache(tmp_path):
    instances = tmp_path / "instances"
    (instances / "Jarvis").mkdir(parents=True)
    cache_path = tmp_path / "cache" / "evidence.sqlite3"

    fusion = RuntimeEvidenceFusion(
        instances,
        {"model_name": "fake-embedding"},
        cache_path=cache_path,
        embedder=_embed,
    )
    try:
        output = fusion.rerank(
            "target question",
            _candidates(),
            default_instance="Jarvis",
            top_n=3,
            base_weight=0.7,
        )
        cache = fusion.cache.diagnostics()
    finally:
        fusion.close()

    assert output["status"] == "completed"
    assert output["selected_candidate_ids"] == [
        "base-1",
        "evidence-1",
        "base-2",
    ]
    assert cache["misses"] == 5
    assert cache["writes"] == 5
    assert b"needle fact from source conversation" not in cache_path.read_bytes()

    def unexpected_embed(_text: str, _kind: str):
        raise AssertionError("warm cache should avoid provider calls")

    warm = RuntimeEvidenceFusion(
        instances,
        {"model_name": "fake-embedding"},
        cache_path=cache_path,
        embedder=unexpected_embed,
    )
    try:
        repeated = warm.rerank(
            "target question",
            _candidates(),
            default_instance="Jarvis",
            top_n=3,
            base_weight=0.7,
        )
        warm_cache = warm.cache.diagnostics()
    finally:
        warm.close()

    assert repeated["selected_candidate_ids"] == output["selected_candidate_ids"]
    assert warm_cache["hits"] == 5
    assert warm_cache["misses"] == 0


def test_runtime_fusion_falls_back_to_hybrid_order_on_embedding_error(tmp_path):
    instances = tmp_path / "instances"
    (instances / "Jarvis").mkdir(parents=True)

    def broken_embed(_text: str, _kind: str):
        raise RuntimeError("embedding unavailable")

    fusion = RuntimeEvidenceFusion(
        instances,
        {"model_name": "fake-embedding"},
        cache_path=tmp_path / "evidence.sqlite3",
        embedder=broken_embed,
    )
    try:
        output = fusion.rerank(
            "target question",
            _candidates(),
            default_instance="Jarvis",
            top_n=3,
            query_vector=np.asarray([1.0, 0.0], dtype=np.float32),
        )
    finally:
        fusion.close()

    assert output["status"] == "fallback"
    assert output["fallback"] is True
    assert output["candidate_ids"] == [
        "base-1",
        "base-2",
        "base-3",
        "evidence-1",
    ]
    assert "embedding unavailable" in output["error"]
