"""Unit tests for the evidence=0 breakdown (evals/locomo/evidence_analysis.py)."""

import json

import pytest

from evals.locomo.evidence_analysis import (
    analyze_run,
    analyze_scores,
    classify,
    format_text,
)


def question(
    question_id: str,
    *,
    category: int = 1,
    evidence_coverage=0.0,
    official_score: float = 0.0,
    **overrides,
) -> dict:
    """検索指標が揃った1問。overrides で分類対象のキーだけ差し替える。"""
    entry = {
        "question_id": question_id,
        "category": category,
        "evidence_coverage": evidence_coverage,
        "official_score": official_score,
        "search_executed": True,
        "oracle_available": True,
        "recall_at_3": 1.0,
        "recall_at_20": 1.0,
    }
    entry.update(overrides)
    return entry


def scores(questions: list[dict], run_id: str = "test_run") -> dict:
    return {
        "run_id": run_id,
        "questions": questions,
        "official": {"overall": 0.5},
    }


class TestClassify:
    def test_search_not_executed_is_reported_before_anything_else(self):
        entry = question("q", search_executed=False, oracle_available=None)
        assert classify(entry) == "no_search"

    def test_missing_card_is_distinguished_from_ranking_failures(self):
        entry = question(
            "q", oracle_available=False, recall_at_3=None, recall_at_20=None
        )
        assert classify(entry) == "no_card"

    def test_candidate_pool_miss(self):
        assert classify(question("q", recall_at_3=0.0, recall_at_20=0.0)) == (
            "not_in_candidates"
        )

    def test_in_pool_but_below_injection_cut(self):
        assert classify(question("q", recall_at_3=0.0, recall_at_20=1.0)) == (
            "rank_below_injection"
        )

    def test_ranked_high_but_not_injected(self):
        assert classify(question("q")) == "dropped_after_ranking"

    def test_partial_recall_counts_as_reached(self):
        entry = question("q", recall_at_3=0.0, recall_at_20=0.5)
        assert classify(entry) == "rank_below_injection"

    def test_missing_metrics_are_unclassified_not_no_search(self):
        """古い scores.json はキーごと無い。False と欠損を混同すると全問が
        no_search に化ける。"""
        legacy = {
            "question_id": "q",
            "category": 1,
            "evidence_coverage": 0.0,
            "official_score": 0.0,
        }
        assert classify(legacy) == "unclassified"

    def test_null_recall_with_executed_search_is_unclassified(self):
        entry = question("q", recall_at_3=None, recall_at_20=None)
        assert classify(entry) == "unclassified"


class TestAnalyze:
    def test_buckets_count_each_question_once(self):
        analysis = analyze_scores(
            scores(
                [
                    question("a", recall_at_3=0.0, recall_at_20=0.0),
                    question("b", recall_at_3=0.0, recall_at_20=1.0),
                    question("c", recall_at_3=0.0, recall_at_20=1.0),
                    question("d", oracle_available=False),
                ]
            )
        )
        counts = {b["key"]: b["count"] for b in analysis["buckets"]}
        assert counts["not_in_candidates"] == 1
        assert counts["rank_below_injection"] == 2
        assert counts["no_card"] == 1
        assert sum(counts.values()) == analysis["evidence_zero_count"] == 4

    def test_questions_without_evidence_turns_leave_the_denominator(self):
        analysis = analyze_scores(
            scores(
                [
                    question("a"),
                    question("b", evidence_coverage=None),
                ]
            )
        )
        assert analysis["measured_count"] == 1
        assert analysis["question_count"] == 2

    def test_adversarial_is_excluded_by_default_but_still_reported(self):
        analysis = analyze_scores(
            scores(
                [
                    question("a"),
                    question("adv", category=5),
                ]
            )
        )
        assert analysis["measured_count"] == 1
        assert analysis["adversarial_count"] == 1
        assert analysis["adversarial_evidence_zero_count"] == 1
        assert analysis["evidence_zero_count"] == 1

    def test_adversarial_can_be_included(self):
        analysis = analyze_scores(
            scores([question("a"), question("adv", category=5)]),
            include_adversarial=True,
        )
        assert analysis["measured_count"] == 2
        assert analysis["evidence_zero_count"] == 2

    def test_evidence_hit_and_zero_are_scored_separately(self):
        analysis = analyze_scores(
            scores(
                [
                    question("hit", evidence_coverage=1.0, official_score=0.8),
                    question("miss", official_score=0.2),
                ]
            )
        )
        assert analysis["evidence_hit_count"] == 1
        assert analysis["official_mean_evidence_hit"] == pytest.approx(0.8)
        assert analysis["official_mean_evidence_zero"] == pytest.approx(0.2)

    def test_share_is_relative_to_evidence_zero_questions(self):
        analysis = analyze_scores(
            scores(
                [
                    question("a", recall_at_3=0.0, recall_at_20=0.0),
                    question("b", recall_at_3=0.0, recall_at_20=0.0),
                    question("hit", evidence_coverage=1.0),
                ]
            )
        )
        bucket = next(
            b for b in analysis["buckets"] if b["key"] == "not_in_candidates"
        )
        assert bucket["share"] == pytest.approx(1.0)


class TestAnalyzeRun:
    def test_reads_scores_json_from_the_run_dir(self, tmp_path):
        (tmp_path / "scores.json").write_text(
            json.dumps(scores([question("a")], run_id="run-1")),
            encoding="utf-8",
        )
        analysis = analyze_run(tmp_path)
        assert analysis["run_id"] == "run-1"
        assert analysis["run_dir"] == str(tmp_path)

    def test_missing_scores_json_is_actionable(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="score"):
            analyze_run(tmp_path)


class TestFormatText:
    def test_reports_the_legacy_run_hint_when_unclassified(self):
        analysis = analyze_scores(
            scores(
                [
                    {
                        "question_id": "old",
                        "category": 1,
                        "evidence_coverage": 0.0,
                        "official_score": 0.0,
                    }
                ]
            )
        )
        text = format_text(analysis, list_limit=0)
        assert "score" in text
        assert "判定材料が無い" in text

    def test_baseline_adds_a_delta_column(self):
        current = analyze_scores(
            scores([question("a", recall_at_3=0.0, recall_at_20=0.0)])
        )
        baseline = analyze_scores(
            scores(
                [
                    question("a", recall_at_3=0.0, recall_at_20=0.0),
                    question("b", recall_at_3=0.0, recall_at_20=0.0),
                ],
                run_id="baseline_run",
            )
        )
        text = format_text(current, list_limit=0, baseline=baseline)
        assert "baseline_run" in text
        assert "-1" in text
