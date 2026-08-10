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
    assert profile.judge is None


def test_load_profile_keeps_judge_out_of_instance_sections(tmp_path):
    path = tmp_path / "judge.yaml"
    path.write_text(
        "judge:\n"
        "  connection: nanogpt\n"
        "  model_name: TEE/gemma4-31b\n"
        "  generation_config:\n"
        "    max_output_tokens: 4096\n",
        encoding="utf-8",
    )

    profile = load_profile(path)

    assert profile.sections == {}
    assert profile.judge == {
        "connection": "nanogpt",
        "model_name": "TEE/gemma4-31b",
        "generation_config": {"max_output_tokens": 4096},
    }


def test_load_profile_normalizes_runtime_reranker_section(tmp_path):
    path = tmp_path / "reranker.yaml"
    path.write_text(
        "reranker:\n"
        "  connection: nanogpt-sub\n"
        "  model_name: TEE/gemma4-31b\n"
        "  candidate_limit: 20\n"
        "  max_candidate_chars: 1200\n"
        "  generation_config:\n"
        "    temperature: 0.9\n"
        "    max_output_tokens: 4096\n",
        encoding="utf-8",
    )

    profile = load_profile(path)

    assert profile.sections["reranker"] == {
        "enabled": True,
        "connection": "nanogpt-sub",
        "model_name": "TEE/gemma4-31b",
        "candidate_limit": 20,
        "max_candidate_chars": 1200,
        "generation_config": {
            "temperature": 0.0,
            "max_output_tokens": 4096,
        },
    }


def test_load_profile_accepts_reviewed_cross_encoder_reranker(tmp_path):
    path = tmp_path / "cross-encoder.yaml"
    path.write_text(
        "reranker:\n"
        "  engine: cross_encoder\n"
        "  model_name: mminilmv2\n"
        "  candidate_limit: 20\n"
        "  batch_size: 8\n"
        "  score_threshold: -0.2\n"
        "  device: cpu\n",
        encoding="utf-8",
    )

    profile = load_profile(path)

    assert profile.sections["reranker"] == {
        "enabled": True,
        "engine": "cross_encoder",
        "model_name": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        "model_revision": "9b8bd7b40e70f84c2444fa0f6545773ad74c2fa6",
        "candidate_limit": 20,
        "max_candidate_chars": 1600,
        "batch_size": 8,
        "score_threshold": -0.2,
        "device": "cpu",
    }


@pytest.mark.parametrize(
    "yaml_text",
    [
        "judge: []\n",
        "judge: {}\n",
        "judge:\n  model_name: ''\n",
        "judge:\n  model_name: judge\n  generation_config: []\n",
    ],
)
def test_load_profile_rejects_invalid_judge_section(tmp_path, yaml_text):
    path = tmp_path / "bad-judge.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ProfileError, match="judge"):
        load_profile(path)


def test_load_profile_requires_connection_for_custom_judge_model(tmp_path):
    path = tmp_path / "custom-judge.yaml"
    path.write_text(
        "judge:\n  model_name: gemma-custom-31b\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="Cannot infer connection"):
        load_profile(path)


def test_load_profile_rejects_unknown_locale(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("locale: fr\n", encoding="utf-8")

    with pytest.raises(ProfileError, match="locale"):
        load_profile(path)


def test_load_profile_accepts_sleeptime_section(tmp_path):
    path = tmp_path / "stage3.yaml"
    path.write_text(
        "name: stage3_on\n"
        "sleeptime:\n"
        "  update_targets:\n"
        "    knowledge_maturation: true\n"
        "memory:\n"
        "  knowledge_maturation_enabled: true\n",
        encoding="utf-8",
    )

    profile = load_profile(path)

    assert (
        profile.sections["sleeptime"]["update_targets"]["knowledge_maturation"]
        is True
    )
    assert profile.sections["memory"]["knowledge_maturation_enabled"] is True


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


def test_profile_context_levels_and_role_temperature_apply_to_instance(tmp_path):
    profile_path = tmp_path / "ablation.yaml"
    profile_path.write_text(
        """chat:
  generation_config:
    temperature: 0.0
context_levels:
  preset: custom
  levels:
    current_time: 'off'
    mid_term: high
    session_digest: 'off'
    rag: high
""",
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
    workspace = EvaluationWorkspace.create(tmp_path / "runs", run_id="ablation")

    _create_instance(
        workspace.create_runtime(),
        conversation,
        "locomo_ablation",
        config,
        profile,
    )

    instance_config = json.loads(
        (workspace.instances_dir / "locomo_ablation" / "config.json").read_text(
            encoding="utf-8"
        )
    )
    assert instance_config["chat"]["generation_config"]["temperature"] == 0.0
    assert instance_config["context_levels"] == {
        "preset": "custom",
        "levels": {
            "current_time": "off",
            "mid_term": "high",
            "session_digest": "off",
            "rag": "high",
        },
    }


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


def test_cli_rerun_qa_flags_are_parsed():
    args = build_parser().parse_args(
        [
            "rerun-qa",
            "--source-run",
            "/tmp/runs/source-v16",
            "--dataset",
            str(FIXTURE),
            "--output-dir",
            "/tmp/runs",
            "--run-id",
            "source-v16-no-time",
            "--all-questions",
            "--profile",
            "/tmp/no-time.yaml",
            "--locale",
            "en",
        ]
    )

    assert args.source_run == Path("/tmp/runs/source-v16")
    assert args.dataset == FIXTURE
    assert args.output_dir == Path("/tmp/runs")
    assert args.run_id == "source-v16-no-time"
    assert args.all_questions is True
    assert args.question_limit is None
    assert args.profile == Path("/tmp/no-time.yaml")
    assert args.qa_mode == "independent"


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
