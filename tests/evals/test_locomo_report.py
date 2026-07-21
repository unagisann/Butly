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
        "memory_reused_from_run_id": "source-v16",
        "locale": "en",
        "sample_limit": None,
        "session_limit": None,
        "question_limit": None,
    }

    summary = _render(scores, run_config, error_count=0)

    assert "- QA mode: independent" in summary
    assert "- Memory source: source-v16" in summary
    assert "- Prompt locale: en" in summary
    assert "- Scope: samples=all, sessions=all, questions=all" in summary
