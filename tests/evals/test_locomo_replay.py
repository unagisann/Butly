"""External-API-free end-to-end tests for the replay runner and resume flow."""

import asyncio
from io import StringIO
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from butly_core.chat.types import ChatResponse
from evals.locomo.checkpoint import STATUS_QA, Checkpoint
from evals.locomo.cli import main as cli_main
from evals.locomo.config import ReplayConfig
from evals.locomo.dataset import LocomoConversation
from evals.locomo.qa_runner import QARunner, build_qa_request
from evals.locomo.progress import create_console_progress
from evals.locomo.replay import (
    _discard_partial_session,
    _instance_name,
    _resolve_instance_names,
    resume_evaluation,
    run_evaluation,
)
from evals.locomo.sleeptime_runner import SleeptimeRunError, SleeptimeRunner


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


class FakeProvider:
    """One provider surface reused by every existing Butly model role."""

    def __init__(self):
        self.generate_contexts = []
        self.chronos_env_seen = "unset"

    def supports_vision(self, model_name):
        return True

    def generate(self, text, attachments, context):
        self.generate_contexts.append(context)
        self.chronos_env_seen = os.environ.get("BUTLY_CHRONOS_NOW")
        return ChatResponse(
            text="Maya planned a blue mug and an herb planter.",
            debug_info={"fake_provider": True},
        )

    def summarize(self, conversation_text, config):
        return "Synthetic summary"

    def embed(self, text, config=None):
        return [1.0, 0.5, 0.25, 0.125]

    def classify(self, prompt, config):
        if "knowledge cards" in prompt or '"ai_importance"' in prompt:
            return json.dumps(
                [
                    {
                        "category": "Hobby",
                        "title": "Maya learns pottery",
                        "tags": "pottery,Harbor Pottery Club,blue mug,herb planter",
                        "ai_importance": 7,
                        "humanity_importance": 6,
                        "summary": (
                            "Maya joined Harbor Pottery Club, planned a blue mug "
                            "and herb planter, then gave the planter to Lena."
                        ),
                        "episode": "Maya sounded proud of her first pottery projects.",
                    }
                ]
            )
        if "prefrontal cortex" in prompt:
            return json.dumps(
                {
                    "llm_scoring": {
                        "response_complexity": 0.7,
                        "emotional_weight": 0.1,
                        "continuity_need": 0.8,
                    },
                    "need_intent": "past_fact",
                }
            )
        if "state management module" in prompt:
            return json.dumps({"topic": "pottery", "mood": "neutral"})
        if "memory indexing module" in prompt:
            return json.dumps(
                {
                    "headlines": [
                        {"type": "topic", "text": "Maya's pottery projects"}
                    ]
                }
            )
        if "fact digest" in prompt or "事実ダイジェスト" in prompt:
            return (
                "[2024-04-08] Pottery plans\n"
                "- Maya joined Harbor Pottery Club and planned a blue mug and "
                "an herb planter. She scheduled her first class for April 13.\n"
                "- In the later session Maya completed both projects and gave "
                "the green herb planter to her sister Lena."
            )
        return "Synthetic snapshot"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_build_qa_request_disables_external_search():
    request = build_qa_request(
        question="What did Maya make?",
        instance_name="locomo_test",
    )

    assert request.use_rag is True
    assert request.use_google_search is False
    assert request.use_web_search is False
    assert request.source == "api"


def _empty_conversation(sample_id: str) -> LocomoConversation:
    return LocomoConversation(
        sample_id=sample_id,
        speaker_a="Alex",
        speaker_b="Maya",
        sessions=[],
        questions=[],
    )


def test_instance_name_preserves_normal_ids_and_disambiguates_lossy_slugs():
    legacy = _instance_name("synthetic-conv-1")
    underscore = _instance_name("synthetic_conv_1")
    punctuation = _instance_name("synthetic?conv?1")

    assert legacy == "locomo_synthetic_conv_1"
    assert underscore.startswith("locomo_synthetic_conv_1__")
    assert punctuation.startswith("locomo_synthetic_conv_1__")
    assert len({legacy, underscore, punctuation}) == 3
    assert _instance_name("synthetic_conv_1") == underscore
    assert _instance_name("日本語").startswith("locomo_sample__")


def test_resolve_instance_names_reuses_legacy_checkpoint_mapping(tmp_path):
    checkpoint = Checkpoint.create("legacy", tmp_path)
    checkpoint.progress_for("conv_1", "locomo_conv_1")

    resolved = _resolve_instance_names(
        [_empty_conversation("conv_1")],
        checkpoint,
    )

    assert resolved == {"conv_1": "locomo_conv_1"}


def test_resolve_instance_names_rejects_checkpoint_collision(tmp_path):
    checkpoint = Checkpoint.create("colliding", tmp_path)
    checkpoint.progress_for("conv-1", "locomo_shared")
    checkpoint.progress_for("conv_1", "locomo_shared")

    with pytest.raises(ValueError, match="instance-name collision") as exc_info:
        _resolve_instance_names(
            [
                _empty_conversation("conv-1"),
                _empty_conversation("conv_1"),
            ],
            checkpoint,
        )

    message = str(exc_info.value)
    assert "conv-1" in message
    assert "conv_1" in message
    assert "locomo_shared" in message


def test_replay_sleeptime_qa_and_jsonl_outputs(tmp_path):
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="phase2-e2e",
        sample_limit=1,
        session_limit=2,
        question_limit=1,
    )

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        result = asyncio.run(run_evaluation(config))

    workspace = result.workspace
    replay_rows = _read_jsonl(workspace.results_dir / "replay_log.jsonl")
    sleeptime_rows = _read_jsonl(workspace.results_dir / "sleeptime_log.jsonl")
    qa_rows = _read_jsonl(workspace.results_dir / "qa_results.jsonl")

    assert len(replay_rows) == 2
    assert [row["session_id"] for row in replay_rows] == ["session_1", "session_2"]
    assert all(row["status"] == "succeeded" for row in replay_rows)
    assert all(row["saved_turn_count"] == 2 for row in replay_rows)

    assert len(sleeptime_rows) == 2
    assert all(row["stage_1_success"] is True for row in sleeptime_rows)
    assert all(row["stage_2_success"] is True for row in sleeptime_rows)
    assert sum(row["knowledge_cards_created"] for row in sleeptime_rows) == 2

    assert len(qa_rows) == 1
    assert qa_rows[0]["qa_mode"] == "independent"
    assert qa_rows[0]["prediction"] == (
        "Maya planned a blue mug and an herb planter."
    )
    assert qa_rows[0]["request"]["use_rag"] is True
    assert qa_rows[0]["request"]["use_google_search"] is False
    assert qa_rows[0]["request"]["use_web_search"] is False
    assert qa_rows[0]["retrieved_card_ids"]

    trace_path = workspace.run_dir / qa_rows[0]["trace_path"]
    assert trace_path.is_file()
    assert trace_path.parent.name == "synthetic-conv-1"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["nodes"]

    instance_dir = workspace.instances_dir / "locomo_synthetic_conv_1"
    assert (instance_dir / "butly_memory.db").is_file()
    assert list((instance_dir / "short_term_json").glob("*.json")) == []
    snapshot_root = workspace.snapshots_dir / instance_dir.name / "session_1"
    assert (snapshot_root / "before_sleeptime").is_dir()
    assert (snapshot_root / "after_sleeptime").is_dir()
    assert (workspace.run_dir / "dataset_manifest.json").is_file()
    assert (workspace.run_dir / "environment.json").is_file()

    assert fake_provider.generate_contexts
    memory_blocks = fake_provider.generate_contexts[0]["memory_blocks"]
    assert memory_blocks["rag_context"]

    checkpoint = Checkpoint.load("phase2-e2e", workspace.checkpoints_dir)
    assert checkpoint.status == "completed"
    progress = checkpoint.progress_for("synthetic-conv-1", "locomo_synthetic_conv_1")
    assert progress.sleeptime_completed == ["session_1", "session_2"]
    assert progress.qa_completed == 1


def test_resume_after_interruption_does_not_reingest(tmp_path):
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="resume-e2e",
        sample_limit=1,
        session_limit=2,
        question_limit=1,
    )

    original_run = SleeptimeRunner.run
    calls = {"count": 0}

    def flaky_run(self, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise SleeptimeRunError("injected interruption")
        return original_run(self, **kwargs)

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        with patch.object(SleeptimeRunner, "run", flaky_run):
            with pytest.raises(SleeptimeRunError):
                asyncio.run(run_evaluation(config))

        run_dir = tmp_path / "resume-e2e"
        interrupted = Checkpoint.load("resume-e2e", run_dir / "checkpoints")
        progress = interrupted.progress_for(
            "synthetic-conv-1", "locomo_synthetic_conv_1"
        )
        assert progress.sleeptime_completed == ["session_1"]
        assert progress.replayed_sessions == ["session_1", "session_2"]
        assert progress.qa_completed == 0

        progress_stream = StringIO()
        result = asyncio.run(
            resume_evaluation(
                run_dir,
                progress_reporter=create_console_progress(progress_stream),
            )
        )

    assert result.replayed_sessions == 1
    assert result.answered_questions == 1
    assert progress_stream.getvalue().splitlines()[0].startswith(
        "[LoCoMo  54.0%] [3/5] setup"
    )

    workspace = result.workspace
    replay_rows = _read_jsonl(workspace.results_dir / "replay_log.jsonl")
    assert [row["session_id"] for row in replay_rows] == ["session_1", "session_2"]

    instance_dir = workspace.instances_dir / "locomo_synthetic_conv_1"
    integrated = instance_dir / "memory_archive" / "1_integrated"
    knowledgeized = instance_dir / "memory_archive" / "2_knowledgeized"
    total_turn_files = len(list(integrated.glob("session_*.json"))) + len(
        list(knowledgeized.rglob("session_*.json"))
    )
    expected_turn_files = sum(row["saved_turn_count"] for row in replay_rows)
    assert total_turn_files == expected_turn_files
    # Independent QA runs in disposable clones, so the canonical instance keeps
    # neither QA turns nor replay turns after both Sleeptime passes.
    leftover_replay_turns = [
        f
        for f in (instance_dir / "short_term_json").glob("*.json")
        if "locomo_sample_id"
        in json.loads(f.read_text(encoding="utf-8"))["messages"][0].get("meta", {})
    ]
    assert leftover_replay_turns == []

    qa_rows = _read_jsonl(workspace.results_dir / "qa_results.jsonl")
    assert len(qa_rows) == 1

    final = Checkpoint.load("resume-e2e", workspace.checkpoints_dir)
    assert final.status == "completed"


def test_qa_pins_chronos_to_last_session_day(tmp_path, monkeypatch):
    """QA 時のシステム時刻が最終会話日の翌日に固定され、実行後は復元される。"""
    monkeypatch.delenv("BUTLY_CHRONOS_NOW", raising=False)
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="chronos-e2e",
        sample_limit=1,
        session_limit=2,
        question_limit=1,
    )

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        asyncio.run(run_evaluation(config))

    # fixture の最終セッションは 2024-05-20 18:45 → QA 時は翌日に固定
    assert fake_provider.chronos_env_seen == "2024-05-21T18:45:00"
    # 実行後は環境変数が元（未設定）に戻っている
    assert os.environ.get("BUTLY_CHRONOS_NOW") is None


def test_discard_partial_session_removes_only_matching_turns(tmp_path):
    short_term = tmp_path / "short_term_json"
    short_term.mkdir(parents=True)

    def _write(name: str, meta):
        message = {"role": "user", "parts": ["hello"]}
        if meta is not None:
            message["meta"] = meta
        (short_term / name).write_text(
            json.dumps({"timestamp": "2024-04-08T10:00:00", "messages": [message]}),
            encoding="utf-8",
        )

    _write(
        "session_20240408_100000_000000.json",
        {"locomo_sample_id": "conv-1", "locomo_session_id": "session_2"},
    )
    _write(
        "session_20240408_100001_000000.json",
        {"locomo_sample_id": "conv-1", "locomo_session_id": "session_1"},
    )
    _write("session_20240408_100002_000000.json", None)

    _discard_partial_session(tmp_path, "conv-1", "session_2")

    remaining = sorted(f.name for f in short_term.glob("*.json"))
    assert remaining == [
        "session_20240408_100001_000000.json",
        "session_20240408_100002_000000.json",
    ]


def test_cli_run_scores_reports_and_applies_profile(tmp_path, capsys):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "name: test_profile\n"
        "locale: ja\n"
        "chat:\n"
        "  connection: fake_conn\n"
        "  model_name: fake-chat\n"
        "knowledge:\n"
        "  model_name: fake-knowledge\n",
        encoding="utf-8",
    )

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=FakeProvider(),
    ), patch("sleeptime.time.sleep", return_value=None):
        exit_code = cli_main(
            [
                "run",
                "--dataset",
                str(FIXTURE),
                "--output-dir",
                str(tmp_path / "runs"),
                "--run-id",
                "cli-e2e",
                "--session-limit",
                "1",
                "--question-limit",
                "2",
                "--locale",
                "en",
                "--profile",
                str(profile_path),
            ]
        )

    assert exit_code == 0
    captured = capsys.readouterr()
    stdout_lines = captured.out.strip().splitlines()
    payload = json.loads(stdout_lines[-1])
    assert payload["run_id"] == "cli-e2e"
    assert "overall_score" in payload
    assert "setup" in captured.err
    assert "replay" in captured.err
    assert "sleeptime" in captured.err
    assert "qa" in captured.err
    assert "score" in captured.err
    assert "report" in captured.err
    assert "[LoCoMo 100.0%]" in captured.err

    run_dir = tmp_path / "runs" / "cli-e2e"
    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    assert scores["question_count"] == 2
    assert (run_dir / "summary.md").is_file()
    # Evidence coverage must be computed because run passes the dataset through.
    assert scores["butly"]["evidence_retrieval_rate"] is not None

    instance_config = json.loads(
        (
            run_dir
            / "workspace"
            / "butly_core"
            / "instances"
            / "locomo_synthetic_conv_1"
            / "config.json"
        ).read_text(encoding="utf-8")
    )
    assert instance_config["chat"]["model_name"] == "fake-chat"
    assert instance_config["chat"]["connection"] == "fake_conn"
    assert instance_config["knowledge"]["model_name"] == "fake-knowledge"
    assert instance_config["agent_profile"]["locale"] == "en"
    assert instance_config["prompts"]["allow_user_overrides"] is False
    system_instruction = (
        run_dir
        / "workspace"
        / "butly_core"
        / "instances"
        / "locomo_synthetic_conv_1"
        / "system_instruction.txt"
    ).read_text(encoding="utf-8")
    assert "Answer in English" in system_instruction

    run_config = json.loads(
        (run_dir / "run_config.json").read_text(encoding="utf-8")
    )
    assert run_config["locale"] == "en"
    assert run_config["qa_mode"] == "independent"


def test_independent_all_questions_reset_history_and_keep_canonical_clean(tmp_path):
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="independent-all",
        sample_limit=1,
        session_limit=1,
        question_limit=None,
        qa_mode="independent",
    )

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        result = asyncio.run(run_evaluation(config))

    qa_rows = _read_jsonl(result.workspace.results_dir / "qa_results.jsonl")
    assert len(qa_rows) == 5
    assert all(row["qa_mode"] == "independent" for row in qa_rows)
    baseline_history = fake_provider.generate_contexts[0]["history"]
    assert baseline_history
    assert all(
        context["history"] == baseline_history
        for context in fake_provider.generate_contexts
    )
    assert all(
        "What two pottery projects did Maya plan to make?"
        not in json.dumps(context["history"], ensure_ascii=False)
        for context in fake_provider.generate_contexts
    )

    canonical = (
        result.workspace.instances_dir / "locomo_synthetic_conv_1"
    )
    assert list((canonical / "short_term_json").glob("*.json")) == []
    assert not (canonical / "traces").exists()
    assert not (canonical / "session_state.json").exists()
    with sqlite3.connect(canonical / "butly_memory.db") as connection:
        usage_total = connection.execute(
            "SELECT COALESCE(SUM(usage_count), 0) FROM knowledge_cards"
        ).fetchone()[0]
    assert usage_total == 0


def test_sequential_mode_keeps_prior_qa_in_history(tmp_path):
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="sequential-two",
        sample_limit=1,
        session_limit=1,
        question_limit=2,
        qa_mode="sequential",
    )

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        result = asyncio.run(run_evaluation(config))

    first_history = json.dumps(
        fake_provider.generate_contexts[0]["history"],
        ensure_ascii=False,
    )
    second_history = json.dumps(
        fake_provider.generate_contexts[1]["history"],
        ensure_ascii=False,
    )
    assert "What two pottery projects did Maya plan to make?" not in first_history
    assert "What two pottery projects did Maya plan to make?" in second_history
    qa_rows = _read_jsonl(result.workspace.results_dir / "qa_results.jsonl")
    assert [row["qa_mode"] for row in qa_rows] == ["sequential", "sequential"]
    canonical = result.workspace.instances_dir / "locomo_synthetic_conv_1"
    assert len(list((canonical / "short_term_json").glob("*.json"))) == 2


def test_independent_qa_resume_rebuilds_disposable_clone(tmp_path):
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="independent-resume",
        sample_limit=1,
        session_limit=1,
        question_limit=2,
        qa_mode="independent",
    )
    original_run = QARunner.run
    calls = {"count": 0}

    async def flaky_run(self, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected QA interruption")
        return await original_run(self, **kwargs)

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        with patch.object(QARunner, "run", flaky_run):
            with pytest.raises(RuntimeError, match="QA interruption"):
                asyncio.run(run_evaluation(config))

        run_dir = tmp_path / "independent-resume"
        interrupted = Checkpoint.load("independent-resume", run_dir / "checkpoints")
        progress = interrupted.progress_for(
            "synthetic-conv-1",
            "locomo_synthetic_conv_1",
        )
        assert progress.qa_completed == 1
        canonical = (
            run_dir
            / "workspace"
            / "butly_core"
            / "instances"
            / "locomo_synthetic_conv_1"
        )
        assert list((canonical / "short_term_json").glob("*.json")) == []

        result = asyncio.run(resume_evaluation(run_dir))

    assert result.answered_questions == 1
    qa_rows = _read_jsonl(result.workspace.results_dir / "qa_results.jsonl")
    assert len(qa_rows) == 2
    final = Checkpoint.load("independent-resume", result.workspace.checkpoints_dir)
    assert final.progress_for(
        "synthetic-conv-1",
        "locomo_synthetic_conv_1",
    ).qa_completed == 2


def test_sequential_resume_rolls_back_qa_before_unwritten_checkpoint(tmp_path):
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="sequential-crash-window",
        sample_limit=1,
        session_limit=1,
        question_limit=2,
        qa_mode="sequential",
    )
    original_save = Checkpoint.save
    interruption = {"raised": False}

    def fail_first_qa_commit(self):
        progress = self.samples.get("synthetic-conv-1")
        if (
            self.status == STATUS_QA
            and progress is not None
            and progress.qa_completed == 1
            and not interruption["raised"]
        ):
            interruption["raised"] = True
            raise RuntimeError("injected checkpoint interruption")
        return original_save(self)

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        with patch.object(Checkpoint, "save", fail_first_qa_commit):
            with pytest.raises(RuntimeError, match="checkpoint interruption"):
                asyncio.run(run_evaluation(config))

        run_dir = tmp_path / "sequential-crash-window"
        interrupted = Checkpoint.load(
            "sequential-crash-window",
            run_dir / "checkpoints",
        )
        progress = interrupted.progress_for(
            "synthetic-conv-1",
            "locomo_synthetic_conv_1",
        )
        assert progress.qa_completed == 0

        canonical = (
            run_dir
            / "workspace"
            / "butly_core"
            / "instances"
            / "locomo_synthetic_conv_1"
        )
        assert len(list((canonical / "short_term_json").glob("*.json"))) == 1
        assert len(_read_jsonl(run_dir / "results" / "qa_results.jsonl")) == 1
        assert any(
            (run_dir / "checkpoints" / "sequential_qa").iterdir()
        )

        result = asyncio.run(resume_evaluation(run_dir))

    assert result.answered_questions == 2
    qa_rows = _read_jsonl(result.workspace.results_dir / "qa_results.jsonl")
    assert len(qa_rows) == 2
    assert len({row["question_id"] for row in qa_rows}) == 2

    first_question = "What two pottery projects did Maya plan to make?"
    assert len(fake_provider.generate_contexts) == 3
    retried_history = json.dumps(
        fake_provider.generate_contexts[1]["history"],
        ensure_ascii=False,
    )
    second_history = json.dumps(
        fake_provider.generate_contexts[2]["history"],
        ensure_ascii=False,
    )
    assert first_question not in retried_history
    assert second_history.count(first_question) == 1

    canonical = result.workspace.instances_dir / "locomo_synthetic_conv_1"
    turn_files = list((canonical / "short_term_json").glob("*.json"))
    assert len(turn_files) == 2
    persisted_history = "\n".join(
        turn_file.read_text(encoding="utf-8") for turn_file in turn_files
    )
    assert persisted_history.count(first_question) == 1
    recovery_root = result.workspace.checkpoints_dir / "sequential_qa"
    assert not recovery_root.exists() or not any(recovery_root.iterdir())

    final = Checkpoint.load(
        "sequential-crash-window",
        result.workspace.checkpoints_dir,
    )
    assert final.progress_for(
        "synthetic-conv-1",
        "locomo_synthetic_conv_1",
    ).qa_completed == 2
