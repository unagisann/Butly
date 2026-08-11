"""
test_locomo_retrieval_replay.py
───────────────────────────────
検索だけを差し替える offline replay のテスト。BM25 モードは embedding を
呼ばないので、LLM/API 無しで最後まで回せる。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from butly_core.core.database import ButlyDatabase
from evals.locomo.retrieval_replay import (
    _build_gatekeeper_query_generator,
    evaluate,
    fuse_hybrid_evidence_ranks,
    load_questions,
    main,
    mirror_workspace,
)


def test_hybrid_evidence_rank_fusion_preserves_base_and_uses_evidence():
    candidate_ids, scores = fuse_hybrid_evidence_ranks(
        ["base-1", "base-2", "base-3", "evidence-1"],
        [
            {"card_id": "evidence-1", "evidence_rank": 1, "score": 0.9},
            {"card_id": "base-1", "evidence_rank": 2, "score": 0.8},
            {"card_id": "base-2", "evidence_rank": 3, "score": 0.7},
            {"card_id": "base-3", "evidence_rank": 4, "score": 0.6},
        ],
        base_weight=0.7,
    )

    assert candidate_ids[:3] == ["base-1", "evidence-1", "base-2"]
    assert scores[0]["fusion_rank"] == 1
    assert scores[0]["fusion_score"] == pytest.approx(0.85)


def _write_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "results").mkdir(parents=True)

    instance_dir = (
        run_dir / "workspace" / "butly_core" / "instances" / "conv_1"
    )
    (instance_dir / "short_term_json").mkdir(parents=True)

    db_path = instance_dir / "butly_memory.db"
    ButlyDatabase(db_path=str(db_path))
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO knowledge_cards "
        "(id, category, title, summary, episode, source_files) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "k1",
                "Life",
                "Pottery workshop",
                "made things with the kids",
                "The family made pottery mugs together.",
                json.dumps(["session_0001.json"]),
            ),
            (
                "k2",
                "Life",
                "Garden notes",
                "herbs on the balcony",
                "They planted basil and mint.",
                json.dumps(["session_0002.json"]),
            ),
        ],
    )
    conn.commit()
    conn.close()

    raw_payload = json.dumps({
            "messages": [{
                "role": "user",
                "parts": ["We made mugs."],
                "meta": {"locomo_dialog_ids": ["D1:1"]},
            }]
        })
    (instance_dir / "short_term_json" / "session_0001.json").write_text(
        raw_payload,
        encoding="utf-8",
    )
    integrated = instance_dir / "memory_archive" / "1_integrated"
    integrated.mkdir(parents=True)
    (integrated / "session_0001.json").write_text(
        raw_payload,
        encoding="utf-8",
    )
    (instance_dir / "config.json").write_text(
        json.dumps({
            "user_profile": {"user_name": "Caroline"},
            "agent_profile": {"ai_name": "Melanie", "locale": "en"},
        }),
        encoding="utf-8",
    )

    rows = [
        {
            "sample_id": "sample-1",
            "question_id": "qa-1",
            "question": "What pottery did they make?",
            "category": 2,
            "instance_name": "conv_1",
            "evidence": ["D1:1"],
            "retrieved_card_ids": [],
        },
        {
            "sample_id": "sample-1",
            # cat5 は対象外
            "question_id": "qa-2",
            "question": "Where do they live?",
            "category": 5,
            "instance_name": "conv_1",
            "evidence": ["D1:1"],
            "retrieved_card_ids": [],
        },
        {
            "sample_id": "sample-1",
            # oracle カードが無い問（evidence のファイルがどのカードにも無い）
            "question_id": "qa-3",
            "question": "Anything else?",
            "category": 2,
            "instance_name": "conv_1",
            "evidence": ["D9:9"],
            "retrieved_card_ids": [],
        },
    ]
    (run_dir / "results" / "qa_results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    (run_dir / "run_config.json").write_text(
        json.dumps({"run_id": "replay-test"}), encoding="utf-8"
    )
    return run_dir


class TestRetrievalReplay:
    def test_qa_free_manifest_is_accepted(self, tmp_path):
        run_dir = _write_run(tmp_path)
        qa_path = run_dir / "results" / "qa_results.jsonl"
        rows = [json.loads(line) for line in qa_path.read_text().splitlines()]
        qa_path.unlink()
        (run_dir / "results" / "retrieval_questions.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workflow": "retrieval_prep",
                    "question_count": len(rows),
                    "questions": rows,
                }
            ),
            encoding="utf-8",
        )

        assert load_questions(run_dir) == rows
        result = evaluate(run_dir, ["bm25"])
        assert result["oracle_questions"] == 1
        assert result["bm25"]["recall_at_3"] == pytest.approx(1.0)

    def test_bm25_mode_measures_recall(self, tmp_path):
        run_dir = _write_run(tmp_path)

        result = evaluate(run_dir, ["bm25"])

        # cat5 と oracle 無しの問は分母から外れる
        assert result["oracle_questions"] == 1
        assert result["bm25"]["recall_at_3"] == pytest.approx(1.0)
        assert result["bm25"]["hit_at_1"] == 1

    def test_source_run_is_not_modified(self, tmp_path):
        """replay は複製した DB に索引を作る。元 run は不変（R7）"""
        run_dir = _write_run(tmp_path)
        db_path = (
            run_dir / "workspace" / "butly_core" / "instances" / "conv_1"
            / "butly_memory.db"
        )
        before = db_path.read_bytes()

        evaluate(run_dir, ["bm25"])

        assert db_path.read_bytes() == before

    def test_reranked_mode_compares_vector_top20_to_effective_top3(
        self, tmp_path, monkeypatch
    ):
        run_dir = _write_run(tmp_path)

        def fake_search(
            _brain,
            _question,
            _instance,
            *,
            limit,
            threshold,
            override_config,
        ):
            assert limit == 3
            assert threshold == 0.4
            assert override_config["brain"]["search_mode"] == "vector"
            assert override_config["reranker"]["candidate_limit"] == 20
            return {
                "results": [],
                "diagnostics": {
                    "vector_candidate_ids": ["x1", "x2", "x3", "k1"],
                    "effective_candidate_ids": ["k1", "x1", "x2", "x3"],
                    "reranker": {
                        "status": "completed",
                        "fallback": False,
                        "selected_candidate_ids": ["k1", "x1", "x2"],
                        "scores": [
                            {"id": "k1", "score": 0.9},
                            {"id": "x1", "score": 0.2},
                        ],
                        "latency_ms": 25,
                        "token_usage": {
                            "prompt_tokens": 30,
                            "completion_tokens": 5,
                        },
                    },
                },
            }

        monkeypatch.setattr(
            "butly_core.core.brain.ButlyBrain.quick_vector_search_diag",
            fake_search,
        )
        result = evaluate(
            run_dir,
            ["reranked"],
            limit=20,
            override_config={
                "reranker": {
                    "model_name": "test-reranker",
                    "connection": "openai",
                }
            },
        )

        stats = result["reranked"]
        assert stats["recall_at_3"] == pytest.approx(1.0)
        assert stats["reranker"]["rescued_at_3"] == 1
        assert stats["reranker"]["harmed_at_3"] == 0
        assert stats["reranker"]["completion_rate"] == pytest.approx(1.0)
        assert stats["reranker"]["prompt_tokens_total"] == 30
        assert stats["reranker"]["selected_recall_at_3"] == pytest.approx(1.0)
        assert stats["details"][0]["question"] == "What pottery did they make?"
        assert stats["details"][0]["sample_id"] == "sample-1"
        assert stats["details"][0]["evidence"] == ["D1:1"]
        assert stats["details"][0]["selected_candidate_ids"][0] == "k1"
        assert stats["details"][0]["reranker_scores"][0]["score"] == 0.9

    def test_dual_query_reports_gatekeeper_rescue(self, tmp_path, monkeypatch):
        run_dir = _write_run(tmp_path)

        def fake_search(
            _brain,
            question,
            _instance,
            *,
            limit,
            threshold,
            override_config,
            retrieval_query,
        ):
            assert question == "What pottery did they make?"
            assert retrieval_query == "mugs made at the pottery workshop"
            assert limit == 20
            assert threshold == 0.4
            assert override_config["brain"]["search_mode"] == "dual_query"
            return {
                "results": [],
                "diagnostics": {
                    "original_candidate_ids": ["x1"],
                    "retrieval_query_candidate_ids": ["k1"],
                    "fused_candidate_ids": ["k1", "x1"],
                    "query_fusion": {
                        "executed": True,
                        "status": "completed",
                    },
                },
            }

        monkeypatch.setattr(
            "butly_core.core.brain.ButlyBrain.quick_vector_search_diag",
            fake_search,
        )
        result = evaluate(
            run_dir,
            ["dual_query"],
            limit=20,
            query_generator=lambda _row: {
                "retrieval_query": "mugs made at the pottery workshop",
                "source": "gatekeeper_replay",
                "status": "ok",
                "need_intent": "past_fact",
            },
        )

        stats = result["dual_query"]
        assert stats["recall_at_3"] == pytest.approx(1.0)
        assert stats["query_fusion"]["original_recall_at_3"] == 0.0
        assert stats["query_fusion"]["retrieval_query_recall_at_3"] == 1.0
        assert stats["query_fusion"]["rescued_at_3"] == 1
        assert stats["query_fusion"]["harmed_at_3"] == 0
        assert stats["details"][0]["retrieval_query_source"] == (
            "gatekeeper_replay"
        )
        assert stats["details"][0]["original_candidate_ids"] == ["x1"]

    def test_evidence_rerank_uses_episode_and_raw_embeddings(
        self, tmp_path, monkeypatch
    ):
        run_dir = _write_run(tmp_path)
        source_db = (
            run_dir
            / "workspace"
            / "butly_core"
            / "instances"
            / "conv_1"
            / "butly_memory.db"
        )

        def source_dump():
            connection = sqlite3.connect(
                f"{source_db.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            try:
                return "\n".join(connection.iterdump())
            finally:
                connection.close()

        source_db_before = source_dump()
        observed_search_modes = []

        def fake_search(
            _brain,
            _question,
            _instance,
            *,
            limit,
            threshold,
            override_config,
            query_embedding,
        ):
            assert limit == 20
            assert threshold == 0.4
            search_mode = override_config["brain"]["search_mode"]
            assert search_mode in {"vector", "hybrid"}
            observed_search_modes.append(search_mode)
            if "bm25_candidates" in override_config["brain"]:
                assert override_config["brain"]["bm25_candidates"] == 20
                assert override_config["brain"]["vector_candidates"] == 20
            assert list(query_embedding) == [1.0, 0.0]
            candidates = [
                {"id": "x1", "source_instance": "conv_1"},
                {"id": "x2", "source_instance": "conv_1"},
                {"id": "x3", "source_instance": "conv_1"},
                {"id": "k1", "source_instance": "conv_1"},
            ]
            return {
                "results": candidates,
                "diagnostics": {
                    "fused_candidate_ids": [item["id"] for item in candidates],
                    "vector_candidate_ids": [item["id"] for item in candidates],
                },
            }

        query_embed_calls = []

        def fake_embed(text, kind):
            lowered = text.lower()
            if kind == "query":
                query_embed_calls.append(text)
                return [1.0, 0.0]
            if "caroline: we made mugs" in lowered:
                return [1.0, 0.0]
            if "pottery" in lowered or "mug" in lowered:
                return [0.7, 0.7]
            return [0.0, 1.0]

        monkeypatch.setattr(
            "butly_core.core.brain.ButlyBrain.quick_vector_search_diag",
            fake_search,
        )
        cache_path = run_dir / "retrieval_cache" / "test.sqlite3"

        result = evaluate(
            run_dir,
            [
                "hybrid",
                "evidence_rerank",
                "hybrid_evidence_rerank",
                "hybrid_evidence_fusion",
            ],
            limit=20,
            override_config={"embedding": {"model_name": "fake"}},
            evidence_embedder=fake_embed,
            evidence_cache_path=cache_path,
        )

        stats = result["evidence_rerank"]
        evidence = stats["evidence_reranker"]
        assert stats["recall_at_3"] == pytest.approx(1.0)
        assert evidence["rescued_at_3"] == 1
        assert evidence["harmed_at_3"] == 0
        assert evidence["unit_distribution"]["episode"] == 2
        assert evidence["unit_distribution"]["raw"] == 1
        assert evidence["missing_raw_files"] == 1
        detail = stats["details"][0]
        assert detail["vector_recall_at_3"] == 0.0
        assert detail["selected_candidate_ids"][0] == "k1"
        assert detail["selected_evidence"][0]["evidence_type"] == "raw"
        assert "Caroline: We made mugs" in detail["selected_evidence"][0][
            "preview"
        ]
        assert cache_path.is_file()
        assert b"We made mugs" not in cache_path.read_bytes()
        assert source_dump() == source_db_before
        assert observed_search_modes == [
            "hybrid",
            "vector",
            "hybrid",
            "hybrid",
        ]
        assert result["hybrid"]["recall_at_3"] == pytest.approx(0.0)
        assert query_embed_calls == ["What pottery did they make?"]

        hybrid_stats = result["hybrid_evidence_rerank"]
        hybrid_evidence = hybrid_stats["evidence_reranker"]
        assert hybrid_stats["recall_at_3"] == pytest.approx(1.0)
        assert hybrid_evidence["base_search_mode"] == "hybrid"
        hybrid_detail = hybrid_stats["details"][0]
        assert hybrid_detail["base_search_mode"] == "hybrid"
        assert hybrid_detail["hybrid_recall_at_3"] == 0.0
        assert hybrid_detail["selected_candidate_ids"][0] == "k1"

        fusion_stats = result["hybrid_evidence_fusion"]
        fusion = fusion_stats["evidence_reranker"]
        assert fusion["base_search_mode"] == "hybrid"
        assert fusion["fusion"] == {
            "strategy": "weighted_reciprocal_rank",
            "base_weight": 0.7,
            "evidence_weight": pytest.approx(0.3),
            "rrf_k": 0,
        }
        fusion_detail = fusion_stats["details"][0]
        assert fusion_detail["selected_candidate_ids"][:2] == ["x1", "k1"]
        assert fusion_detail["evidence_fusion_scores"]
        assert fusion_detail["recall_at_3"] == pytest.approx(1.0)

        def fail_if_called(_text, _kind):
            raise AssertionError("cached embeddings must be reused")

        cached = evaluate(
            run_dir,
            ["evidence_rerank"],
            limit=20,
            override_config={"embedding": {"model_name": "fake"}},
            evidence_embedder=fail_if_called,
            evidence_cache_path=cache_path,
        )
        assert cached["evidence_rerank"]["evidence_reranker"]["cache"][
            "hits"
        ] >= 4

        changed_model_calls = []

        def embed_after_model_change(text, kind):
            changed_model_calls.append((text, kind))
            return fake_embed(text, kind)

        changed_model = evaluate(
            run_dir,
            ["evidence_rerank"],
            limit=20,
            override_config={"embedding": {"model_name": "fake-v2"}},
            evidence_embedder=embed_after_model_change,
            evidence_cache_path=cache_path,
        )
        changed_cache = changed_model["evidence_rerank"][
            "evidence_reranker"
        ]["cache"]
        assert changed_model_calls
        assert changed_cache["hits"] == 0
        assert changed_cache["misses"] >= 4

        changed_prefix_calls = []

        def embed_after_prefix_change(text, kind):
            changed_prefix_calls.append((text, kind))
            return fake_embed(text, kind)

        changed_prefix = evaluate(
            run_dir,
            ["evidence_rerank"],
            limit=20,
            override_config={
                "embedding": {
                    "model_name": "fake-v2",
                    "query_prefix": "query: ",
                }
            },
            evidence_embedder=embed_after_prefix_change,
            evidence_cache_path=cache_path,
        )
        prefix_cache = changed_prefix["evidence_rerank"][
            "evidence_reranker"
        ]["cache"]
        assert changed_prefix_calls
        assert prefix_cache["hits"] == 0
        assert prefix_cache["misses"] >= 4

    def test_gatekeeper_replay_ignores_query_for_non_memory_intent(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "butly_core.core.gatekeeper.context_classifier."
            "ContextClassifier.classify",
            lambda *_args, **_kwargs: {
                "need_intent": None,
                "retrieval_query": "a query the model should not have emitted",
                "retrieval_query_status": "ok",
                "classifier_status": "ok",
            },
        )
        generate = _build_gatekeeper_query_generator(
            {"gatekeeper": {"model_name": "fake-gatekeeper"}}
        )

        result = generate({"question": "Good morning", "diagnostics": {}})

        assert result["retrieval_query"] is None
        assert result["need_intent"] is None
        assert result["status"] == "ignored_non_memory_intent"

    def test_mirror_copies_only_databases(self, tmp_path):
        run_dir = _write_run(tmp_path)
        dest = mirror_workspace(run_dir, tmp_path / "mirror")

        instance = dest / "butly_core" / "instances" / "conv_1"
        assert (instance / "butly_memory.db").is_file()
        assert not (instance / "short_term_json").exists()

    def test_missing_workspace_raises(self, tmp_path):
        run_dir = tmp_path / "empty"
        (run_dir / "results").mkdir(parents=True)
        (run_dir / "results" / "qa_results.jsonl").write_text(
            json.dumps({"question_id": "q", "question": "x", "category": 1,
                        "instance_name": "conv_1", "evidence": ["D1:1"]}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            evaluate(run_dir, ["bm25"])

    def test_cli_prints_summary(self, tmp_path, capsys):
        run_dir = _write_run(tmp_path)
        out = tmp_path / "replay.json"

        assert main([
            "--run",
            str(run_dir),
            "--modes",
            "bm25",
            "--out",
            str(out),
            "--job-id",
            "job-replay",
        ]) == 0

        captured = capsys.readouterr()
        printed = captured.out
        assert "oracle" in printed
        assert "bm25" in printed
        assert "[LoCoMo" in captured.err
        assert "question=1/1" in captured.err
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert saved["bm25"]["questions"] == 1
        assert saved["status"] == "completed"
        assert saved["job_id"] == "job-replay"
        assert saved["modes"] == ["bm25"]
