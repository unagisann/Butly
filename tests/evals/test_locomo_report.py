"""Run-policy metadata rendered into LoCoMo summaries."""

import json

from evals.locomo.report import _render, write_report


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


def test_report_uses_chat_model_recorded_by_qa(tmp_path):
    run_dir = tmp_path / "recorded-model"
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True)
    (run_dir / "scores.json").write_text(
        json.dumps(
            {
                "run_id": "recorded-model",
                "generated_at": "2026-09-02T00:00:00+00:00",
                "question_count": 0,
                "official": {},
                "auxiliary": {},
                "butly": {},
                "questions": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run_config.json").write_text(
        json.dumps({"dataset_path": "/data/locomo10.json"}),
        encoding="utf-8",
    )
    (results_dir / "qa_results.jsonl").write_text(
        json.dumps(
            {
                "diagnostics": {
                    "model": "gpt-4o-mini",
                    "connection_id": "openai",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = write_report(run_dir).read_text(encoding="utf-8")

    assert "- Chat model: gpt-4o-mini (connection: openai)" in summary


def test_report_falls_back_to_chat_profile(tmp_path):
    run_dir = tmp_path / "profile-model"
    run_dir.mkdir()
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "chat:\n  model_name: profile-chat\n  connection: profile-connection\n",
        encoding="utf-8",
    )
    (run_dir / "scores.json").write_text(
        json.dumps(
            {
                "run_id": "profile-model",
                "generated_at": "2026-09-02T00:00:00+00:00",
                "question_count": 0,
                "official": {},
                "auxiliary": {},
                "butly": {},
                "questions": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "dataset_path": "/data/locomo10.json",
                "profile_path": str(profile_path),
            }
        ),
        encoding="utf-8",
    )

    summary = write_report(run_dir).read_text(encoding="utf-8")

    assert (
        "- Chat model: profile-chat (connection: profile-connection)"
        in summary
    )


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
