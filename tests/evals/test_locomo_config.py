"""Configuration, profile, and CLI contract tests for LoCoMo runs."""

import json
from pathlib import Path

import pytest

from evals.locomo.cli import _replay_config_from_args, build_parser
from evals.locomo.config import (
    EvaluationProfile,
    ProfileError,
    ReplayConfig,
    load_profile,
    resolve_evaluation_locale,
)
from evals.locomo.dataset import load_dataset
from evals.locomo.replay import _create_instance, _resolve_config_and_profile
from evals.locomo.workspace import EvaluationWorkspace


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


def test_replay_config_roundtrip_preserves_all_limits_and_mode(tmp_path):
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        sample_limit=None,
        session_limit=None,
        question_limit=None,
        qa_mode="independent",
        locale="ja",
    )

    restored = ReplayConfig.from_json_dict(config.to_json_dict())

    assert restored.sample_limit is None
    assert restored.session_limit is None
    assert restored.question_limit is None
    assert restored.qa_mode == "independent"
    assert restored.locale == "ja"


def test_old_run_config_resumes_in_sequential_mode(tmp_path):
    restored = ReplayConfig.from_json_dict(
        {
            "dataset_path": str(FIXTURE),
            "output_dir": str(tmp_path),
            "question_limit": 10,
            "qa_isolation": "sequential_without_sleeptime_phase2",
        }
    )

    assert restored.qa_mode == "sequential"


@pytest.mark.parametrize("field", ["sample_limit", "session_limit", "question_limit"])
def test_replay_config_rejects_non_positive_limits(tmp_path, field):
    values = {field: 0}

    with pytest.raises(ValueError, match=field):
        ReplayConfig(dataset_path=FIXTURE, output_dir=tmp_path, **values)


@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_replay_config_rejects_non_integer_limits(tmp_path, value):
    with pytest.raises(ValueError, match="question_limit"):
        ReplayConfig(
            dataset_path=FIXTURE,
            output_dir=tmp_path,
            question_limit=value,
        )


def test_replay_config_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="qa_mode"):
        ReplayConfig(
            dataset_path=FIXTURE,
            output_dir=tmp_path,
            qa_mode="shared",
        )


@pytest.mark.parametrize("locale", ["fr", 123])
def test_replay_config_rejects_unknown_or_non_string_locale(tmp_path, locale):
    with pytest.raises(ValueError, match="locale"):
        ReplayConfig(
            dataset_path=FIXTURE,
            output_dir=tmp_path,
            locale=locale,
        )


def test_load_profile_returns_typed_locale_only_profile(tmp_path):
    path = tmp_path / "locale.yaml"
    path.write_text("name: japanese\nlocale: ja\n", encoding="utf-8")

    profile = load_profile(path)

    assert isinstance(profile, EvaluationProfile)
    assert profile.name == "japanese"
    assert profile.locale == "ja"
    assert profile.sections == {}


def test_load_profile_rejects_unknown_locale(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("locale: fr\n", encoding="utf-8")

    with pytest.raises(ProfileError, match="locale"):
        load_profile(path)


def test_profile_brain_override_applies_to_instance(tmp_path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "locale: en\nbrain:\n  time_decay_rate: 0.0\n",
        encoding="utf-8",
    )
    config, profile = _resolve_config_and_profile(
        ReplayConfig(
            dataset_path=FIXTURE,
            output_dir=tmp_path,
            profile_path=profile_path,
        )
    )
    conversation = load_dataset(FIXTURE)[0]
    workspace = EvaluationWorkspace.create(tmp_path / "runs", run_id="no-decay")

    _create_instance(
        workspace.create_runtime(),
        conversation,
        "locomo_no_decay",
        config,
        profile,
    )

    instance_config = json.loads(
        (
            workspace.instances_dir
            / "locomo_no_decay"
            / "config.json"
        ).read_text(encoding="utf-8")
    )
    assert instance_config["brain"]["time_decay_rate"] == 0.0


def test_locale_resolution_prefers_cli_then_profile_then_english():
    assert resolve_evaluation_locale(" ja ", "en") == "ja"
    assert resolve_evaluation_locale(None, "ja") == "ja"
    assert resolve_evaluation_locale(None, None) == "en"


def test_run_config_resolves_and_persists_profile_locale(tmp_path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("locale: ja\n", encoding="utf-8")
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        profile_path=profile_path,
    )

    resolved, profile = _resolve_config_and_profile(config)

    assert profile is not None
    assert resolved.locale == "ja"
    assert resolved.to_json_dict()["locale"] == "ja"


def test_profile_locale_applies_to_instance_without_cli_override(tmp_path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("locale: ja\n", encoding="utf-8")
    config, profile = _resolve_config_and_profile(
        ReplayConfig(
            dataset_path=FIXTURE,
            output_dir=tmp_path,
            profile_path=profile_path,
        )
    )
    conversation = load_dataset(FIXTURE)[0]
    workspace = EvaluationWorkspace.create(tmp_path / "runs", run_id="profile-ja")

    _create_instance(
        workspace.create_runtime(),
        conversation,
        "locomo_profile_ja",
        config,
        profile,
    )

    instance_config = json.loads(
        (
            workspace.instances_dir
            / "locomo_profile_ja"
            / "config.json"
        ).read_text(encoding="utf-8")
    )
    assert instance_config["agent_profile"]["locale"] == "ja"


def test_cli_all_flags_and_qa_mode_are_parsed():
    args = build_parser().parse_args(
        [
            "run",
            "--dataset",
            str(FIXTURE),
            "--output-dir",
            "/tmp/runs",
            "--all-samples",
            "--all-sessions",
            "--all-questions",
            "--qa-mode",
            "sequential",
            "--locale",
            "ja",
        ]
    )

    assert args.all_samples is True
    assert args.all_sessions is True
    assert args.all_questions is True
    assert args.qa_mode == "sequential"
    assert args.locale == "ja"
    config = _replay_config_from_args(args)
    assert config.sample_limit is None
    assert config.session_limit is None
    assert config.question_limit is None
    assert config.qa_mode == "sequential"
    assert config.locale == "ja"


@pytest.mark.parametrize(
    ("limit_flag", "all_flag"),
    [
        ("--sample-limit", "--all-samples"),
        ("--session-limit", "--all-sessions"),
        ("--question-limit", "--all-questions"),
    ],
)
def test_cli_rejects_limit_with_matching_all_flag(
    limit_flag,
    all_flag,
):
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--dataset",
                str(FIXTURE),
                "--output-dir",
                "/tmp/runs",
                limit_flag,
                "2",
                all_flag,
            ]
        )
