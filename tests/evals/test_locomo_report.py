"""Run-policy metadata rendered into LoCoMo summaries."""

from evals.locomo.report import _render


def test_report_includes_qa_mode_locale_and_all_scope():
    scores = {
        "run_id": "policy-test",
        "generated_at": "2026-07-20T00:00:00+00:00",
        "question_count": 2,
        "official": {},
        "auxiliary": {},
        "butly": {},
        "questions": [],
    }
    run_config = {
        "dataset_path": "/data/locomo10.json",
        "qa_mode": "independent",
        "qa_memory_mode": "oracle",
        "memory_reused_from_run_id": "source-v16",
        "locale": "en",
        "qa_prompt_version": "grounded-memory-v2",
        "sample_limit": None,
        "session_limit": None,
        "question_limit": None,
    }

    summary = _render(scores, run_config, error_count=0)

    assert "- QA mode: independent" in summary
    assert "- QA memory mode: oracle" in summary
    assert "- Memory source: source-v16" in summary
    assert "- Prompt locale: en" in summary
    assert "- Dataset locale: unknown" in summary
    assert "- QA prompt version: grounded-memory-v2" in summary
    assert "- Scope: samples=all, sessions=all, questions=all" in summary


def test_report_labels_japanese_score_as_localized_not_official():
    scores = {
        "run_id": "ja-test",
        "generated_at": "2026-08-13T00:00:00+00:00",
        "question_count": 1,
        "scoring": {
            "locale": "ja",
            "method": "localized-ja-mixed-script-f1-v1",
            "official_compatible": False,
        },
        "official": {"overall": 1.0},
        "auxiliary": {},
        "butly": {},
        "questions": [],
    }
    run_config = {
        "dataset_path": "/data/locomo10_ja.json",
        "locale": "ja",
        "dataset_locale": "ja",
    }

    summary = _render(scores, run_config, error_count=0)

    assert "- Dataset locale: ja" in summary
    assert "official-compatible: False" in summary
    assert "Japanese-localized scores (not comparable to official English)" in summary
