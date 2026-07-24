"""Unit tests for official-compatible scoring, aggregation, and reporting."""

import json
from pathlib import Path

import pytest

from evals.locomo.report import ReportError, write_report
from evals.locomo.scorer import (
    ScoringError,
    answer_contained,
    claims_no_information,
    exact_match,
    normalize_answer,
    official_score,
    score_run,
    token_f1,
)
from evals.locomo.stemming import stem


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


class TestNormalization:
    def test_normalize_strips_articles_punctuation_and_case(self):
        assert normalize_answer("The blue, mug!") == "blue mug"

    def test_normalize_removes_and_as_article(self):
        assert normalize_answer("a mug and an planter") == "mug planter"

    def test_porter_stemmer_matches_reference_words(self):
        expected = {
            "caresses": "caress",
            "ponies": "poni",
            "running": "run",
            "relational": "relat",
            "conditional": "condit",
            "hopefulness": "hope",
            "electrical": "electr",
            "studies": "studi",
            "nursing": "nurs",
            "agreed": "agre",
        }
        assert {word: stem(word) for word in expected} == expected


class TestQuestionScores:
    def test_token_f1_uses_stemmed_overlap(self):
        assert token_f1("She was painting daily", "paint") == pytest.approx(0.4)

    def test_token_f1_zero_when_empty(self):
        assert token_f1("", "nursing") == 0.0

    def test_exact_match_compares_normalized_token_sets(self):
        assert exact_match("The mug, blue", "blue mug") is True
        assert exact_match("blue mug extra", "blue mug") is False

    def test_answer_containment(self):
        assert answer_contained("She decided to study nursing.", "nursing") is True
        assert answer_contained("She studies medicine.", "nursing") is False

    def test_category_1_averages_comma_separated_answers(self):
        score = official_score(
            "a blue mug, an herb planter", "blue mug, herb planter", 1
        )
        assert score == pytest.approx(1.0)
        partial = official_score("a blue mug", "blue mug, herb planter", 1)
        assert 0.0 < partial < 1.0

    def test_category_3_grades_only_before_semicolon(self):
        assert official_score(
            "8 April 2024", "8 April 2024; the first session", 3
        ) == pytest.approx(1.0)

    def test_category_5_detects_no_information_phrases(self):
        assert official_score("There is no information available.", "x", 5) == 1.0
        assert official_score("That was not mentioned in our chats.", "x", 5) == 1.0
        assert official_score("She studied nursing.", "x", 5) == 0.0
        assert claims_no_information("NOT MENTIONED anywhere") is True


def _qa_row(question_id: str, **overrides) -> dict:
    row = {
        "run_id": "score-test",
        "sample_id": "synthetic-conv-1",
        "instance_name": "locomo_synthetic_conv_1",
        "question_id": question_id,
        "question": "What did Maya plan to make?",
        "expected_answer": "a blue mug, an herb planter",
        "prediction": "a blue mug, an herb planter",
        "category": 1,
        "evidence": ["D1:1"],
        "latency_ms": 100,
        "retrieved_card_ids": ["k1"],
        "diagnostics": {
            "gatekeeper": {
                "tier": "mid",
                "need_intent": "past_fact",
                "classifier_status": "ok",
                "fallback_reason": None,
                "intent_floor_applied": False,
            },
            "rag": {
                "results": [
                    {
                        "title": "Maya learns pottery",
                        "episode": "Maya planned a blue mug and an herb planter.",
                    }
                ]
            },
        },
        "error": None,
    }
    row.update(overrides)
    return row


def _write_workspace(run_dir: Path) -> None:
    """provenance 判定用の最小 workspace (カード DB + 保存ターンファイル) を作る。"""
    import sqlite3

    instance_dir = (
        run_dir / "workspace" / "butly_core" / "instances" / "locomo_synthetic_conv_1"
    )
    (instance_dir / "short_term_json").mkdir(parents=True)

    conn = sqlite3.connect(instance_dir / "butly_memory.db")
    conn.execute("CREATE TABLE knowledge_cards (id TEXT, source_files TEXT)")
    conn.execute(
        "INSERT INTO knowledge_cards VALUES (?, ?)",
        ("k1", json.dumps(["session_0001.json", "session_0002.json"])),
    )
    conn.commit()
    conn.close()

    (instance_dir / "short_term_json" / "session_0001.json").write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "parts": ["I want to make a blue mug."],
                        "meta": {"locomo_dialog_ids": ["D1:1", "D1:2"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_run(tmp_path: Path, qa_rows: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    results = run_dir / "results"
    results.mkdir(parents=True)
    (results / "qa_results.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in qa_rows), encoding="utf-8"
    )
    (results / "sleeptime_log.jsonl").write_text(
        json.dumps(
            {
                "knowledge_cards_created": 3,
                "knowledge_chunks": 2,
                "knowledge_chunk_failures": 1,
                "llm_prompt_tokens": 9000,
                "llm_completion_tokens": 1500,
                "llm_calls": 4,
                "error": None,
            }
        )
        + "\n"
        + json.dumps({"knowledge_cards_created": 0, "error": "Boom"})
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "run_config.json").write_text(
        json.dumps({"run_id": "score-test", "dataset_path": str(FIXTURE)}),
        encoding="utf-8",
    )
    return run_dir


class TestScoreRun:
    def test_missing_qa_results_raises(self, tmp_path):
        (tmp_path / "results").mkdir()
        with pytest.raises(ScoringError):
            score_run(tmp_path)

    def test_aggregates_categories_and_butly_metrics(self, tmp_path):
        rows = [
            _qa_row("qa-1"),
            _qa_row(
                "qa-2",
                category=5,
                expected_answer="No information available",
                prediction="No information available in my memories.",
                latency_ms=300,
                retrieved_card_ids=[],
                diagnostics={"gatekeeper": {"tier": "low"}, "rag": {"results": []}},
            ),
            _qa_row(
                "qa-3",
                category=2,
                expected_answer="nursing",
                prediction="She never said.",
                latency_ms=200,
            ),
        ]
        run_dir = _write_run(tmp_path, rows)

        scores = score_run(run_dir)

        assert scores["run_id"] == "score-test"
        assert scores["question_count"] == 3
        official = scores["official"]
        assert official["by_category"]["1"]["score"] == pytest.approx(1.0)
        assert official["by_category"]["2"]["score"] == pytest.approx(0.0)
        assert official["no_information_accuracy"] == pytest.approx(1.0)
        assert official["overall"] == pytest.approx(2 / 3)

        butly = scores["butly"]
        assert butly["rag_trigger_rate"] == pytest.approx(2 / 3)
        assert butly["rag_trigger_rate_when_incorrect"] == pytest.approx(1.0)
        assert butly["latency_ms_p50"] == pytest.approx(200)
        assert butly["tier_distribution"] == {"low": 1, "mid": 2}
        assert butly["knowledge_cards_created"] == 3
        assert butly["sleeptime_failures"] == 1
        assert butly["stage2_chunks"] == 2
        assert butly["stage2_chunk_failures"] == 1

        assert (run_dir / "scores.json").is_file()
        errors = (run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(errors) == 1
        assert json.loads(errors[0])["source"] == "sleeptime"

    def test_raw_reference_metrics_aggregated(self, tmp_path):
        """rag.raw_reference の status/chars/truncated が集計される"""
        rows = [
            _qa_row(
                "qa-1",
                diagnostics={
                    "gatekeeper": {"tier": "mid"},
                    "rag": {
                        "results": [{"title": "t"}],
                        "source_mode": "both",
                        "raw_reference": {
                            "status": "ok",
                            "files": ["s1.json"],
                            "file_count": 1,
                            "chars": 500,
                            "truncated": False,
                            "top_k": 1,
                        },
                    },
                },
            ),
            _qa_row(
                "qa-2",
                diagnostics={
                    "gatekeeper": {"tier": "mid"},
                    "rag": {
                        "results": [{"title": "t"}],
                        "source_mode": "both",
                        "raw_reference": {
                            "status": "fallback_cards",
                            "files": [],
                            "chars": 0,
                            "truncated": False,
                        },
                    },
                },
            ),
        ]
        run_dir = _write_run(tmp_path, rows)

        butly = score_run(run_dir)["butly"]
        assert butly["rag_source_mode_distribution"] == {"both": 2}
        assert butly["raw_reference_status_distribution"] == {
            "fallback_cards": 1,
            "ok": 1,
        }
        assert butly["raw_reference_chars_mean"] == pytest.approx(250)
        assert butly["raw_reference_truncated_rate"] == pytest.approx(0.0)
        # file_count は ok の行のみ（fallback_cards は None で除外）→ mean=1.0
        assert butly["raw_reference_files_mean"] == pytest.approx(1.0)

    def test_raw_reference_metrics_absent_for_cards_mode(self, tmp_path):
        """raw_reference 無し（cards モード・旧 run）でも集計が壊れない"""
        run_dir = _write_run(tmp_path, [_qa_row("qa-1")])

        butly = score_run(run_dir)["butly"]
        assert butly["raw_reference_status_distribution"] == {}
        assert butly["raw_reference_chars_mean"] is None

    def test_token_usage_aggregated(self, tmp_path):
        """diagnostics.token_usage（API 実測）が平均・合計に集計される"""
        rows = [
            _qa_row(
                "qa-1",
                diagnostics={
                    "gatekeeper": {"tier": "mid"},
                    "rag": {"results": []},
                    "token_usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 40,
                        "source": "api",
                    },
                    "token_usage_total": {
                        "prompt_tokens": 1600,
                        "completion_tokens": 90,
                        "calls": 3,
                    },
                },
            ),
            _qa_row(
                "qa-2",
                diagnostics={
                    "gatekeeper": {"tier": "mid"},
                    "rag": {"results": []},
                    "token_usage": {
                        "prompt_tokens": 3000,
                        "completion_tokens": 60,
                        "source": "api",
                    },
                    "token_usage_total": {
                        "prompt_tokens": 3800,
                        "completion_tokens": 110,
                        "calls": 3,
                    },
                },
            ),
        ]
        run_dir = _write_run(tmp_path, rows)

        butly = score_run(run_dir)["butly"]
        assert butly["prompt_tokens_mean"] == pytest.approx(2000)
        assert butly["prompt_tokens_total"] == 4000
        assert butly["completion_tokens_total"] == 100
        # QA ターン全体（chat + 補助呼び出し）の合算
        assert butly["qa_all_calls_prompt_tokens_mean"] == pytest.approx(2700)
        assert butly["qa_all_calls_prompt_tokens_total"] == 5400
        assert butly["qa_all_calls_completion_tokens_total"] == 200
        # Sleeptime 側（_write_run の 1 行目に 9000/1500 を記録済み）
        assert butly["sleeptime_prompt_tokens_total"] == 9000
        assert butly["sleeptime_completion_tokens_total"] == 1500

    def test_token_usage_absent_for_old_runs(self, tmp_path):
        """token_usage 無しの旧 run でも None で集計が壊れない"""
        run_dir = _write_run(tmp_path, [_qa_row("qa-1")])

        butly = score_run(run_dir)["butly"]
        assert butly["prompt_tokens_mean"] is None
        assert butly["prompt_tokens_total"] is None

    def test_deduplicates_resumed_question_records(self, tmp_path):
        rows = [
            _qa_row("qa-1", prediction="wrong answer entirely"),
            _qa_row("qa-1"),
        ]
        run_dir = _write_run(tmp_path, rows)

        scores = score_run(run_dir)

        assert scores["question_count"] == 1
        assert scores["official"]["overall"] == pytest.approx(1.0)

    def test_same_question_id_in_different_samples_is_not_deduplicated(
        self,
        tmp_path,
    ):
        rows = [
            _qa_row("qa-1", sample_id="conv-a"),
            _qa_row("qa-1", sample_id="conv-b"),
        ]
        run_dir = _write_run(tmp_path, rows)

        scores = score_run(run_dir)

        assert scores["question_count"] == 2
        assert {row["sample_id"] for row in scores["questions"]} == {
            "conv-a",
            "conv-b",
        }

    def test_evidence_coverage_none_without_workspace(self, tmp_path):
        """workspace が無い run では provenance を組めず None (n/a) になる"""
        run_dir = _write_run(tmp_path, [_qa_row("qa-1")])

        scores = score_run(run_dir)
        assert scores["butly"]["evidence_retrieval_rate"] is None
        assert scores["questions"][0]["evidence_coverage"] is None

    def test_evidence_coverage_from_provenance(self, tmp_path):
        """retrieved card → source_files → locomo_dialog_ids の連鎖で判定する"""
        rows = [
            _qa_row("qa-1"),  # evidence D1:1, retrieved k1 → covered
            _qa_row("qa-2", evidence=["D9:9"]),  # 存在しない evidence → 0.0
            _qa_row("qa-3", retrieved_card_ids=[]),  # RAG 不発火 → 0.0
        ]
        run_dir = _write_run(tmp_path, rows)
        _write_workspace(run_dir)

        scores = score_run(run_dir)

        by_id = {q["question_id"]: q for q in scores["questions"]}
        assert by_id["qa-1"]["evidence_coverage"] == pytest.approx(1.0)
        assert by_id["qa-2"]["evidence_coverage"] == pytest.approx(0.0)
        assert by_id["qa-3"]["evidence_coverage"] == pytest.approx(0.0)
        assert scores["butly"]["evidence_retrieval_rate"] == pytest.approx(1 / 3)
        assert scores["butly"]["evidence_metric"] == "provenance_chunk_level"

    def test_classifier_fallback_and_floor_rates(self, tmp_path):
        rows = [
            _qa_row("qa-1"),  # status ok / floor False
            _qa_row(
                "qa-2",
                diagnostics={
                    "gatekeeper": {
                        "tier": "mid",
                        "need_intent": "past_fact",
                        "classifier_status": "fallback",
                        "fallback_reason": "parse_error",
                        "intent_floor_applied": True,
                    },
                    "rag": {"results": []},
                },
            ),
            # classifier フィールドの無い旧形式 row は集計から除外される
            _qa_row(
                "qa-3",
                diagnostics={"gatekeeper": {"tier": "mid"}, "rag": {"results": []}},
            ),
        ]
        run_dir = _write_run(tmp_path, rows)

        butly = score_run(run_dir)["butly"]
        assert butly["classifier_fallback_rate"] == pytest.approx(0.5)
        assert butly["classifier_fallback_reasons"] == {"parse_error": 1}
        assert butly["intent_floor_rate"] == pytest.approx(0.5)


class TestReport:
    def test_report_requires_scores(self, tmp_path):
        with pytest.raises(ReportError):
            write_report(tmp_path)

    def test_summary_contains_scores_and_worst_questions(self, tmp_path):
        rows = [
            _qa_row("qa-1"),
            _qa_row(
                "qa-2",
                category=2,
                expected_answer="nursing",
                prediction="She never said.",
            ),
        ]
        run_dir = _write_run(tmp_path, rows)
        score_run(run_dir)

        summary_path = write_report(run_dir)

        text = summary_path.read_text(encoding="utf-8")
        assert "LoCoMo Evaluation Summary — score-test" in text
        assert "Overall score: 0.500" in text
        assert "| 1 (multi-hop) | 1.000 | 1 |" in text
        assert "Lowest-scoring questions" in text
        assert "qa-2" in text
        assert "CC BY-NC 4.0" in text
