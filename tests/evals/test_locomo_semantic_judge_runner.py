"""LoCoMo semantic-judge artifacts, resume, and aggregation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.locomo.cli import build_parser
import evals.locomo.cli as locomo_cli
import evals.locomo.semantic_judge_runner as judge_runner
from evals.locomo.progress import create_console_progress
from evals.locomo.report import write_report
from evals.locomo.semantic_judge_runner import (
    LocomoJudgeError,
    build_semantic_question_details,
    resolve_judge_config,
    run_locomo_semantic_judge,
    semantic_scores_for_current_inputs,
)
from evals.semantic_judge import JudgeConfig, SemanticJudgeError


def _judge_config(model_name: str = "judge-model") -> JudgeConfig:
    config = JudgeConfig.from_mapping(
        {
            "connection": "judge-connection",
            "model_name": model_name,
            "generation_config": {
                "temperature": 0.0,
                "max_output_tokens": 2048,
            },
        }
    )
    assert config is not None
    return config


class FakeJudge:
    def __init__(self, config: JudgeConfig, outputs: list[object]):
        self.config = config
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def call(self, prompt: str, **_kwargs) -> dict:
        self.prompts.append(prompt)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return {
            "payload": output,
            "raw_response": json.dumps(output),
            "token_usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "completion_metadata": {"finish_reason": "stop"},
            "latency_ms": 5,
        }


def _question(
    question_id: str,
    *,
    category: int = 1,
    expected: str = "blue mug, herb planter",
    prediction: str = "blue mug and herb planter",
    official_score: float = 1.0,
) -> dict:
    return {
        "sample_id": "sample-1",
        "question_id": question_id,
        "category": category,
        "question": "What is the answer?",
        "expected_answer": expected,
        "prediction": prediction,
        "official_score": official_score,
    }


def _write_run(tmp_path: Path, questions: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps({"run_id": "judge-run"}),
        encoding="utf-8",
    )
    (run_dir / "scores.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "judge-run",
                "question_count": len(questions),
                "official": {"overall": 0.5, "by_category": {}},
                "auxiliary": {},
                "butly": {},
                "questions": questions,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def _complete(score: int, *, contradiction: bool = False) -> dict:
    verdict = {0: "incorrect", 1: "partial", 2: "correct"}[score]
    return {
        "verdict": verdict,
        "confidence": "high",
        "contradiction": contradiction,
        "missing_critical": score == 1,
        "reason": "brief reason",
    }


def test_judges_atomically_without_modifying_official_scores(tmp_path):
    run_dir = _write_run(
        tmp_path,
        [
            _question(
                "q-1",
                category=3,
                expected="8 April 2024; first session",
                prediction="April 8, 2024",
                official_score=0.0,
            ),
            _question(
                "q-2",
                category=4,
                expected="red",
                prediction="blue",
                official_score=1.0,
            ),
        ],
    )
    original_scores = (run_dir / "scores.json").read_bytes()
    config = _judge_config()
    fake = FakeJudge(config, [_complete(2), _complete(0, contradiction=True)])

    result = run_locomo_semantic_judge(run_dir, config, judge=fake)

    assert result["status"] == "completed"
    assert result["coverage"] == pytest.approx(1.0)
    assert result["summary"]["categories"]["3"]["judged_count"] == 1
    assert result["summary"]["official_disagreement"] == {
        "possible_false_negative_count": 1,
        "possible_false_positive_count": 1,
    }
    assert '"reference_answer": "8 April 2024"' in fake.prompts[0]
    assert "8 April 2024; first session" not in fake.prompts[0]
    assert (run_dir / "semantic_scores.json").is_file()
    assert (
        run_dir / "results" / "semantic_judge" / "sample-1" / "q-1.json"
    ).is_file()
    assert (run_dir / "scores.json").read_bytes() == original_scores


def test_matching_success_is_skipped_but_changed_input_is_rejudged(tmp_path):
    run_dir = _write_run(tmp_path, [_question("q-1")])
    config = _judge_config()
    first = FakeJudge(config, [_complete(2)])
    run_locomo_semantic_judge(run_dir, config, judge=first)

    skipped = FakeJudge(config, [])
    second = run_locomo_semantic_judge(run_dir, config, judge=skipped)
    assert second["questions"][0]["status"] == "complete"
    assert skipped.prompts == []

    scores_path = run_dir / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    scores["questions"][0]["prediction"] = "a changed answer"
    scores_path.write_text(json.dumps(scores), encoding="utf-8")
    changed = FakeJudge(config, [_complete(1)])

    third = run_locomo_semantic_judge(run_dir, config, judge=changed)

    assert len(changed.prompts) == 1
    assert third["questions"][0]["score"] == 1


def test_error_is_partial_and_retried_on_next_run(tmp_path):
    run_dir = _write_run(tmp_path, [_question("q-1")])
    config = _judge_config()
    failed = FakeJudge(
        config,
        [
            SemanticJudgeError(
                "invalid schema",
                raw_response="x" * 5000,
                token_usage={"prompt_tokens": 12},
                completion_metadata={"finish_reason": "length"},
            )
        ],
    )

    first = run_locomo_semantic_judge(run_dir, config, judge=failed)

    assert first["status"] == "partial"
    assert first["judged_count"] == 0
    assert first["error_count"] == 1
    assert first["questions"][0]["status"] == "error"
    assert len(first["questions"][0]["raw_response"]) == 4000
    assert first["questions"][0]["token_usage"] == {"prompt_tokens": 12}
    assert first["questions"][0]["completion_metadata"] == {
        "finish_reason": "length"
    }

    recovered = FakeJudge(config, [_complete(2)])
    second = run_locomo_semantic_judge(run_dir, config, judge=recovered)

    assert len(recovered.prompts) == 1
    assert second["status"] == "completed"
    assert second["error_count"] == 0


def test_config_change_invalidates_completed_artifact(tmp_path):
    run_dir = _write_run(tmp_path, [_question("q-1")])
    old_config = _judge_config("judge-v1")
    run_locomo_semantic_judge(
        run_dir,
        old_config,
        judge=FakeJudge(old_config, [_complete(2)]),
    )
    new_config = _judge_config("judge-v2")
    fake = FakeJudge(new_config, [_complete(0)])

    result = run_locomo_semantic_judge(run_dir, new_config, judge=fake)

    assert len(fake.prompts) == 1
    assert result["config_signature"] == new_config.signature()
    assert result["questions"][0]["score"] == 0


def test_resolve_config_prefers_run_snapshot_and_allows_overrides(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "judge:\n"
        "  connection: profile-connection\n"
        "  model_name: profile-model\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "judge-run",
                "profile_path": str(profile),
                "judge": {
                    "connection": "snapshot-connection",
                    "model_name": "snapshot-model",
                    "generation_config": {"max_output_tokens": 1024},
                },
            }
        ),
        encoding="utf-8",
    )

    snapshotted = resolve_judge_config(run_dir)
    overridden = resolve_judge_config(
        run_dir,
        model_name="gpt-5-mini",
        max_output_tokens=4096,
    )

    assert snapshotted is not None
    assert snapshotted.model_name == "snapshot-model"
    assert snapshotted.connection == "snapshot-connection"
    assert overridden is not None
    assert overridden.model_name == "gpt-5-mini"
    assert overridden.connection is None
    assert overridden.generation_config["max_output_tokens"] == 4096


def test_custom_model_override_requires_explicit_connection(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps({
            "judge": {
                "connection": "old-connection",
                "model_name": "old-model",
            }
        }),
        encoding="utf-8",
    )

    with pytest.raises(
        LocomoJudgeError,
        match="Cannot infer connection.*Specify 'connection' explicitly",
    ):
        resolve_judge_config(run_dir, model_name="gemma-custom-31b")


def test_explicit_connection_override_survives_model_change(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "judge": {
                    "connection": "old-connection",
                    "model_name": "old-model",
                }
            }
        ),
        encoding="utf-8",
    )

    overridden = resolve_judge_config(
        run_dir,
        model_name="new-model",
        connection="new-connection",
    )

    assert overridden is not None
    assert overridden.connection == "new-connection"


def test_resolve_config_falls_back_to_profile(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "judge:\n"
        "  connection: profile-connection\n"
        "  model_name: profile-model\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps({"run_id": "judge-run", "profile_path": str(profile)}),
        encoding="utf-8",
    )

    config = resolve_judge_config(run_dir)

    assert config is not None
    assert config.model_name == "profile-model"
    assert config.connection == "profile-connection"


def test_persisted_disabled_judge_does_not_follow_later_profile_change(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "judge:\n  connection: later\n  model_name: later-model\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "judge-run",
                "profile_path": str(profile),
                "judge": None,
            }
        ),
        encoding="utf-8",
    )

    assert resolve_judge_config(run_dir) is None


def test_report_renders_semantic_auxiliary_section(tmp_path):
    run_dir = _write_run(tmp_path, [_question("q-1")])
    config = _judge_config()
    run_locomo_semantic_judge(
        run_dir,
        config,
        judge=FakeJudge(config, [_complete(2)]),
    )

    report_path = write_report(run_dir)
    text = report_path.read_text(encoding="utf-8")

    assert "Semantic judge (auxiliary, not official)" in text
    assert "Coverage: 1/1 (1.000); errors: 0" in text
    assert "judge-model" in text


def test_official_rescore_refreshes_disagreement_without_rejudging(tmp_path):
    run_dir = _write_run(
        tmp_path,
        [_question("q-1", official_score=0.0)],
    )
    config = _judge_config()
    semantic = run_locomo_semantic_judge(
        run_dir,
        config,
        judge=FakeJudge(config, [_complete(2)]),
    )
    assert semantic["summary"]["official_disagreement"] == {
        "possible_false_negative_count": 1,
        "possible_false_positive_count": 0,
    }
    scores_path = run_dir / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    scores["questions"][0]["official_score"] = 1.0
    scores_path.write_text(json.dumps(scores), encoding="utf-8")

    current = semantic_scores_for_current_inputs(scores, semantic)
    report_text = write_report(run_dir).read_text(encoding="utf-8")

    assert current is not None
    assert current["status"] == "completed"
    assert current["summary"]["official_disagreement"] == {
        "possible_false_negative_count": 0,
        "possible_false_positive_count": 0,
    }
    assert semantic["summary"]["official_disagreement"][
        "possible_false_negative_count"
    ] == 1
    assert "false negatives / false positives: 0 / 0" in report_text


def test_changed_scores_make_semantic_artifact_stale_and_report_hides_metrics(
    tmp_path,
):
    run_dir = _write_run(tmp_path, [_question("q-1")])
    config = _judge_config()
    semantic = run_locomo_semantic_judge(
        run_dir,
        config,
        judge=FakeJudge(config, [_complete(2)]),
    )
    scores_path = run_dir / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    scores["questions"][0]["prediction"] = "changed after judging"
    scores_path.write_text(json.dumps(scores), encoding="utf-8")

    current = semantic_scores_for_current_inputs(scores, semantic)
    report_text = write_report(run_dir).read_text(encoding="utf-8")

    assert current is not None
    assert current["status"] == "stale"
    assert current["rejudge_required"] is True
    assert current["questions"] == []
    assert "Status: stale (re-judging required)" in report_text
    assert "Normalized semantic score mean" not in report_text


def test_changed_judge_config_makes_semantic_artifact_stale(tmp_path):
    run_dir = _write_run(tmp_path, [_question("q-1")])
    config = _judge_config()
    semantic = run_locomo_semantic_judge(
        run_dir,
        config,
        judge=FakeJudge(config, [_complete(2)]),
    )
    semantic["judge"] = {
        **semantic["judge"],
        "model_name": "different-model",
    }
    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))

    current = semantic_scores_for_current_inputs(scores, semantic)

    assert current is not None
    assert current["status"] == "stale"
    assert "configuration" in current["stale_reason"]


def test_question_reordering_does_not_make_semantic_artifact_stale(tmp_path):
    run_dir = _write_run(tmp_path, [_question("q-1"), _question("q-2")])
    config = _judge_config()
    semantic = run_locomo_semantic_judge(
        run_dir,
        config,
        judge=FakeJudge(config, [_complete(2), _complete(1)]),
    )
    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    scores["questions"].reverse()

    current = semantic_scores_for_current_inputs(scores, semantic)

    assert current is semantic


def test_semantic_question_details_flag_all_review_reasons(tmp_path):
    questions = [
        _question("partial", official_score=0.5),
        _question("contradiction", official_score=1.0),
        _question("low-fn", official_score=0.0),
        _question("clean", official_score=1.0),
    ]
    run_dir = _write_run(tmp_path, questions)
    config = _judge_config()
    semantic = run_locomo_semantic_judge(
        run_dir,
        config,
        judge=FakeJudge(
            config,
            [
                _complete(1),
                _complete(0, contradiction=True),
                {**_complete(2), "confidence": "low"},
                _complete(2),
            ],
        ),
    )
    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))

    current, details = build_semantic_question_details(scores, semantic)
    by_id = {item["question_id"]: item for item in details}

    assert current is semantic
    assert by_id["partial"]["review_reasons"] == [
        "partial",
        "missing_critical",
    ]
    assert by_id["contradiction"]["review_reasons"] == [
        "contradiction",
        "possible_false_positive",
    ]
    assert by_id["low-fn"]["review_reasons"] == [
        "low_confidence",
        "possible_false_negative",
    ]
    assert by_id["clean"]["review_required"] is False


def test_cli_exposes_posthoc_judge_arguments():
    args = build_parser().parse_args(
        [
            "judge",
            "--run-dir",
            "/tmp/run",
            "--judge-model-name",
            "model-x",
            "--judge-connection",
            "connection-x",
            "--judge-max-output-tokens",
            "4096",
        ]
    )

    assert args.command == "judge"
    assert args.judge_model_name == "model-x"
    assert args.judge_connection == "connection-x"
    assert args.judge_max_output_tokens == 4096


def test_normal_finish_reports_partial_judge_then_fails(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report_calls = []
    monkeypatch.setattr(
        "evals.locomo.scorer.score_run",
        lambda *args, **kwargs: {"official": {"overall": 0.5}},
    )
    monkeypatch.setattr(
        locomo_cli,
        "_run_configured_judge",
        lambda *args, **kwargs: {
            "status": "partial",
            "error_count": 1,
            "question_count": 2,
            "judged_count": 1,
        },
    )

    def fake_report(path):
        report_calls.append(Path(path))
        return Path(path) / "summary.md"

    monkeypatch.setattr("evals.locomo.report.write_report", fake_report)
    result = SimpleNamespace(
        workspace=SimpleNamespace(run_id="run", run_dir=run_dir),
        sample_ids=["sample-1"],
        replayed_sessions=0,
        answered_questions=2,
    )

    with pytest.raises(ValueError, match="semantic judging is partial"):
        locomo_cli._finish(
            result,
            dataset=tmp_path / "dataset.json",
            skip_scoring=False,
            progress_reporter=create_console_progress(),
        )

    assert report_calls == [run_dir]


def test_posthoc_command_reports_partial_judge_then_fails(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report_calls = []
    config = _judge_config()
    monkeypatch.setattr(
        judge_runner,
        "resolve_judge_config",
        lambda *args, **kwargs: config,
    )
    monkeypatch.setattr(
        judge_runner,
        "run_locomo_semantic_judge",
        lambda *args, **kwargs: {
            "run_id": "run",
            "status": "partial",
            "error_count": 1,
            "question_count": 2,
            "judged_count": 1,
            "coverage": 0.5,
        },
    )

    def fake_report(path):
        report_calls.append(Path(path))
        return Path(path) / "summary.md"

    monkeypatch.setattr("evals.locomo.report.write_report", fake_report)
    args = build_parser().parse_args(
        ["judge", "--run-dir", str(run_dir), "--judge-model-name", "model"]
    )

    with pytest.raises(ValueError, match="semantic judging is partial"):
        locomo_cli._command_judge(args)

    assert report_calls == [run_dir]
