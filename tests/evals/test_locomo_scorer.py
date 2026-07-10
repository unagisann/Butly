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
        "question_id": question_id,
        "question": "What did Maya plan to make?",
        "expected_answer": "a blue mug, an herb planter",
        "prediction": "a blue mug, an herb planter",
        "category": 1,
        "evidence": ["D1:1"],
        "latency_ms": 100,
        "retrieved_card_ids": ["k1"],
        "diagnostics": {
            "gatekeeper": {"tier": "mid", "need_intent": "past_fact"},
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


def _write_run(tmp_path: Path, qa_rows: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    results = run_dir / "results"
    results.mkdir(parents=True)
    (results / "qa_results.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in qa_rows), encoding="utf-8"
    )
    (results / "sleeptime_log.jsonl").write_text(
        json.dumps({"knowledge_cards_created": 3, "error": None})
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

        assert (run_dir / "scores.json").is_file()
        errors = (run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(errors) == 1
        assert json.loads(errors[0])["source"] == "sleeptime"

    def test_deduplicates_resumed_question_records(self, tmp_path):
        rows = [
            _qa_row("qa-1", prediction="wrong answer entirely"),
            _qa_row("qa-1"),
        ]
        run_dir = _write_run(tmp_path, rows)

        scores = score_run(run_dir)

        assert scores["question_count"] == 1
        assert scores["official"]["overall"] == pytest.approx(1.0)

    def test_evidence_coverage_requires_dataset(self, tmp_path):
        run_dir = _write_run(tmp_path, [_qa_row("qa-1")])

        without_dataset = score_run(run_dir)
        assert without_dataset["butly"]["evidence_retrieval_rate"] is None

        with_dataset = score_run(run_dir, dataset_path=FIXTURE)
        rate = with_dataset["butly"]["evidence_retrieval_rate"]
        assert rate is not None
        assert 0.0 <= rate <= 1.0


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
        assert "| 1 (multi-answer) | 1.000 | 1 |" in text
        assert "Lowest-scoring questions" in text
        assert "qa-2" in text
        assert "CC BY-NC 4.0" in text
