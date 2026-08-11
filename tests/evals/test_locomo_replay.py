"""External-API-free end-to-end tests for the replay runner and resume flow."""

import asyncio
from io import StringIO
import json
import os
import re
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from butly_core.chat.types import ChatResponse
from butly_core.core.database import ButlyDatabase
from evals.locomo.checkpoint import STATUS_QA, Checkpoint
from evals.locomo.cli import main as cli_main
from evals.locomo.config import EvaluationProfile, ReplayConfig
from evals.locomo.dataset import LocomoConversation, load_dataset
from evals.locomo.qa_runner import QARunner, build_qa_request
from evals.locomo.progress import create_console_progress
from evals.locomo.replay import (
    LOCOMO_QA_PROMPT_VERSION,
    _build_qa_system_instruction,
    _configure_instance,
    _diff_card_identity,
    _discard_partial_session,
    _create_instance,
    _instance_name,
    _knowledge_card_identity,
    _resolve_instance_names,
    _write_run_metadata,
    rerun_qa_from_memory,
    resume_evaluation,
    run_evaluation,
)
from evals.locomo.sleeptime_runner import SleeptimeRunError, SleeptimeRunner
from evals.locomo.workspace import EvaluationWorkspace, WorkspaceError


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


@pytest.fixture(autouse=True)
def _run_fake_chat_workers_inline(monkeypatch):
    """Keep this fake-only suite independent of workers and tokenizer I/O.

    Production calls remain on the threadpool. These tests use immediate fake
    providers, for which two near-simultaneous worker completions can leave the
    event loop asleep even though both workers are already idle. The simple
    token counter also prevents tiktoken from downloading its vocabulary in a
    suite explicitly documented as external-API-free.
    """

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        "butly_core.chat.service.run_in_threadpool",
        inline,
    )
    monkeypatch.setattr(
        "butly_core.core.raw_memory_reader.count_tokens",
        lambda text: len(str(text).split()),
    )


def test_qa_system_instruction_is_grounded_and_has_no_unexpanded_slots():
    prompt = _build_qa_system_instruction("Maya")

    assert prompt.startswith("You are Maya")
    assert "Related Matured Memories (active nodes)" in prompt
    assert "If any of them directly answers the question" in prompt
    assert "relative to the original conversation" in prompt
    assert "Only answer 'No information available'" in prompt
    # v3: strict terse-answer format restored (grounding kept)
    assert "Answer as briefly as possible" in prompt
    assert "no full" in prompt and "sentence" in prompt
    assert "{context}" not in prompt
    assert "{question}" not in prompt


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
        if "memory_nodes" in prompt and "link_existing" in prompt:
            # Stage 3 node review: レビューカード全 id を反映した new_node を返す
            card_ids = re.findall(r'"id": "([^"]+)"', prompt)
            return json.dumps(
                {
                    "reviewed_card_ids": card_ids,
                    "link_existing": [],
                    "new_nodes": (
                        [
                            {
                                "kind": "preference",
                                "subject": "user",
                                "topic": "pottery",
                                "statement": "Maya is actively into pottery.",
                                "confidence": 0.8,
                                "source_card_ids": card_ids,
                            }
                        ]
                        if card_ids
                        else []
                    ),
                }
            )
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


def test_retrieval_prep_stops_after_sleeptime_and_writes_questions(tmp_path):
    fake_provider = FakeProvider()
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="retrieval-prep",
        sample_limit=1,
        session_limit=1,
        question_limit=2,
        workflow="retrieval_prep",
    )

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        result = asyncio.run(run_evaluation(config))

    assert result.workflow == "retrieval_prep"
    assert result.answered_questions == 0
    assert result.prepared_questions == 2
    assert not (result.workspace.results_dir / "qa_results.jsonl").exists()
    assert not (result.workspace.run_dir / "scores.json").exists()
    manifest = json.loads(
        (
            result.workspace.results_dir / "retrieval_questions.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["workflow"] == "retrieval_prep"
    assert manifest["sample_ids"] == ["synthetic-conv-1"]
    assert manifest["question_count"] == 2
    assert manifest["questions"][0]["instance_name"] == (
        "locomo_synthetic_conv_1"
    )
    assert manifest["questions"][0]["evidence"]
    run_config = json.loads(
        result.workspace.run_config_path.read_text(encoding="utf-8")
    )
    assert run_config["workflow"] == "retrieval_prep"
    checkpoint = Checkpoint.load(
        "retrieval-prep", result.workspace.checkpoints_dir
    )
    assert checkpoint.status == "completed"
    progress = checkpoint.progress_for(
        "synthetic-conv-1", "locomo_synthetic_conv_1"
    )
    assert progress.qa_completed == 0
    assert fake_provider.generate_contexts == []

    (result.workspace.results_dir / "retrieval_questions.json").unlink()
    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ):
        resumed = asyncio.run(resume_evaluation(result.workspace.run_dir))
    assert resumed.answered_questions == 0
    assert resumed.prepared_questions == 2
    assert (
        resumed.workspace.results_dir / "retrieval_questions.json"
    ).is_file()
    assert not (resumed.workspace.results_dir / "qa_results.jsonl").exists()


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
    assert "Answer as briefly as possible" in system_instruction

    run_config = json.loads(
        (run_dir / "run_config.json").read_text(encoding="utf-8")
    )
    assert run_config["locale"] == "en"
    assert run_config["qa_mode"] == "independent"
    assert run_config["qa_prompt_version"] == LOCOMO_QA_PROMPT_VERSION


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


def test_rerun_qa_reuses_exact_cards_without_touching_source(tmp_path):
    source_config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="reuse-source",
        sample_limit=1,
        session_limit=1,
        question_limit=1,
        qa_mode="independent",
        workflow="retrieval_prep",
    )
    conversation = load_dataset(FIXTURE)[0]
    source_workspace = EvaluationWorkspace.create(
        tmp_path,
        run_id="reuse-source",
    )
    _write_run_metadata(source_workspace, source_config, [conversation])
    source_instance = source_workspace.instances_dir / "locomo_synthetic_conv_1"
    _create_instance(
        source_workspace.create_runtime(),
        conversation,
        source_instance.name,
        source_config,
    )
    source_instruction = source_instance / "system_instruction.txt"
    source_instruction.write_text("legacy answer prompt", encoding="utf-8")
    source_db = source_instance / "butly_memory.db"
    ButlyDatabase(source_db).register_knowledge(
        {
            "id": "same-card-1",
            "category": "Hobby",
            "title": "Maya learns pottery",
            "ai_importance": 7,
            "humanity_importance": 6,
            "summary": "Maya planned a blue mug and an herb planter.",
            "episode": "A synthetic test card.",
            "raw_reference": "session_0001.json",
        }
    )
    with sqlite3.connect(source_db) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    source_checkpoint = Checkpoint.create(
        source_workspace.run_id,
        source_workspace.checkpoints_dir,
    )
    source_progress = source_checkpoint.progress_for(
        conversation.sample_id,
        source_instance.name,
    )
    source_progress.replayed_sessions = ["session_1"]
    source_progress.sleeptime_completed = ["session_1"]
    source_progress.qa_completed = 0
    source_checkpoint.status = "completed"
    source_checkpoint.save()
    (source_workspace.results_dir / "replay_log.jsonl").write_text(
        '{"status":"succeeded"}\n',
        encoding="utf-8",
    )
    (source_workspace.results_dir / "sleeptime_log.jsonl").write_text(
        '{"status":"succeeded","knowledge_cards_created":1}\n',
        encoding="utf-8",
    )
    source_db_bytes = source_db.read_bytes()

    profile_path = tmp_path / "qa-ablation.yaml"
    profile_path.write_text(
        """locale: en
chat:
  generation_config:
    temperature: 0.0
gatekeeper:
  generation_config:
    temperature: 0.1
judge:
  connection: judge-connection
  model_name: judge-model
  generation_config:
    max_output_tokens: 2048
brain:
  use_rag: false
context_levels:
  preset: custom
  levels:
    current_time: 'off'
    mid_term: 'off'
    session_digest: high
    rag: 'off'
""",
        encoding="utf-8",
    )

    async def fake_qa_run(self, **kwargs):
        return {"question_id": kwargs["question"].question_id}

    with patch.object(
        SleeptimeRunner,
        "run",
        side_effect=AssertionError("rerun-qa must not run Sleeptime"),
    ), patch.object(QARunner, "run", fake_qa_run):
        rerun = asyncio.run(
            rerun_qa_from_memory(
                source_workspace.run_dir,
                output_dir=tmp_path,
                run_id="reuse-target",
                question_limit=2,
                profile_path=profile_path,
            )
        )

    target_instance = rerun.workspace.instances_dir / rerun.instance_names[0]
    assert rerun.replayed_sessions == 0
    assert rerun.answered_questions == 2
    assert source_db.read_bytes() == source_db_bytes
    assert (target_instance / "butly_memory.db").read_bytes() == source_db_bytes
    assert source_instruction.read_text(encoding="utf-8") == "legacy answer prompt"
    target_instruction = (
        target_instance / "system_instruction.txt"
    ).read_text(encoding="utf-8")
    assert "Related Matured Memories (active nodes)" in target_instruction
    assert "Only answer 'No information available'" in target_instruction
    assert not (source_workspace.results_dir / "qa_results.jsonl").exists()
    assert (
        rerun.workspace.results_dir / "sleeptime_log.jsonl"
    ).read_text(encoding="utf-8") == (
        source_workspace.results_dir / "sleeptime_log.jsonl"
    ).read_text(encoding="utf-8")

    target_config = json.loads(
        (target_instance / "config.json").read_text(encoding="utf-8")
    )
    assert target_config["chat"]["generation_config"]["temperature"] == 0.0
    assert (
        target_config["gatekeeper"]["generation_config"]["temperature"]
        == 0.1
    )
    assert target_config["brain"]["use_rag"] is False
    assert "judge" not in target_config
    assert target_config["context_levels"]["levels"] == {
        "current_time": "off",
        "mid_term": "off",
        "session_digest": "high",
        "rag": "off",
    }
    run_config = json.loads(
        rerun.workspace.run_config_path.read_text(encoding="utf-8")
    )
    assert run_config["memory_reused_from_run_id"] == "reuse-source"
    assert run_config["workflow"] == "full"
    assert run_config["run_sleeptime_per_session"] is False
    assert run_config["qa_prompt_version"] == LOCOMO_QA_PROMPT_VERSION
    assert run_config["judge"]["connection"] == "judge-connection"
    assert run_config["judge"]["model_name"] == "judge-model"
    assert run_config["judge"]["generation_config"] == {
        "temperature": 0.0,
        "max_output_tokens": 2048,
    }
    assert len(run_config["judge"]["config_signature"]) == 64
    checkpoint = Checkpoint.load("reuse-target", rerun.workspace.checkpoints_dir)
    progress = checkpoint.samples["synthetic-conv-1"]
    assert progress.replayed_sessions == ["session_1"]
    assert progress.sleeptime_completed == ["session_1"]
    assert progress.qa_completed == 2

    progress.sleeptime_completed = []
    checkpoint.save()
    with pytest.raises(
        WorkspaceError,
        match="refusing to execute Replay/Sleeptime",
    ):
        asyncio.run(resume_evaluation(rerun.workspace.run_dir))


def test_rerun_qa_rejects_sequential_source(tmp_path):
    source_dir = tmp_path / "sequential-source"
    source_dir.mkdir()
    (source_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "sequential-source",
                "dataset_path": str(FIXTURE),
                "output_dir": str(tmp_path),
                "qa_mode": "sequential",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="qa_mode=independent"):
        asyncio.run(rerun_qa_from_memory(source_dir))


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


# ===================================================================
# Stage 3 (knowledge maturation) — profile merge / per-session / clone A/B
# ===================================================================

STAGE3_ON_PROFILE_YAML = """name: stage3_on
sleeptime:
  update_targets:
    knowledge_maturation: true
memory:
  knowledge_maturation_enabled: true
"""


def test_configure_instance_merges_profile_recursively():
    runtime = MagicMock()
    runtime.instance_manager.get_instance_config.return_value = {}
    runtime.instance_manager.update_instance_config.return_value = (True, "ok")
    profile = EvaluationProfile(
        name=None,
        locale=None,
        sections={
            "sleeptime": {"update_targets": {"knowledge_maturation": True}},
            "memory": {"knowledge_maturation_enabled": True},
        },
    )
    config = ReplayConfig(dataset_path=FIXTURE, output_dir=Path("/tmp"))

    _configure_instance(runtime, "inst", config, profile)

    captured = runtime.instance_manager.update_instance_config.call_args[0][1]
    targets = captured["sleeptime"]["update_targets"]
    # 上書き対象の 1 キーだけ変わる
    assert targets["knowledge_maturation"] is True
    # 再帰マージ: digest / knowledge_cards 等の既定を消さない
    assert targets["digest"] is True
    assert targets["knowledge_cards"] is True
    assert captured["memory"]["knowledge_maturation_enabled"] is True


def test_configure_instance_keeps_fusion_cache_outside_qa_clones(tmp_path):
    runtime = MagicMock()
    runtime.base_dir = tmp_path / "run" / "workspace"
    runtime.instance_manager.get_instance_config.return_value = {}
    runtime.instance_manager.update_instance_config.return_value = (True, "ok")
    profile = EvaluationProfile(
        name=None,
        locale="en",
        sections={"brain": {"search_mode": "hybrid_evidence_fusion"}},
    )
    config = ReplayConfig(dataset_path=FIXTURE, output_dir=tmp_path)

    _configure_instance(runtime, "inst", config, profile)

    captured = runtime.instance_manager.update_instance_config.call_args[0][1]
    assert captured["brain"]["evidence_cache_path"] == str(
        tmp_path / "run" / "retrieval_cache" / "evidence_embeddings.sqlite3"
    )


def test_stage3_profile_runs_per_session(tmp_path):
    """§10.1: profile で Stage 3 を有効化すると Stage 2 成功後に走り、
    sleeptime_log に Stage 3 統計が分離記録される。"""
    fake_provider = FakeProvider()
    profile_path = tmp_path / "stage3_on.yaml"
    profile_path.write_text(STAGE3_ON_PROFILE_YAML, encoding="utf-8")
    config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="stage3-per-session",
        sample_limit=1,
        session_limit=2,
        question_limit=1,
        profile_path=profile_path,
    )

    with patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        result = asyncio.run(run_evaluation(config))

    sleeptime_rows = _read_jsonl(
        result.workspace.results_dir / "sleeptime_log.jsonl"
    )
    assert [row["stage_3_status"] for row in sleeptime_rows] == [
        "completed",
        "completed",
    ]
    assert sleeptime_rows[0]["stage_3_created_nodes"] >= 1
    assert sleeptime_rows[0]["stage_3_reviewed_cards"] >= 1
    assert sleeptime_rows[0]["stage_3_llm_calls"] >= 1
    # Stage 2 の成功判定に Stage 3 が紛れ込まない
    assert all(row["stage_2_success"] is True for row in sleeptime_rows)

    instance_dir = result.workspace.instances_dir / "locomo_synthetic_conv_1"
    with sqlite3.connect(instance_dir / "butly_memory.db") as connection:
        node_count = connection.execute(
            "SELECT COUNT(*) FROM memory_nodes"
        ).fetchone()[0]
        unstamped = connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_cards
            WHERE COALESCE(is_archived, 0) = 0
              AND (last_matured_content_hash IS NULL
                   OR last_matured_content_hash <> content_hash)
            """
        ).fetchone()[0]
    assert node_count >= 1
    assert unstamped == 0  # キューは drain 済み

    instance_config = json.loads(
        (instance_dir / "config.json").read_text(encoding="utf-8")
    )
    assert instance_config["sleeptime"]["update_targets"]["digest"] is True
    assert (
        instance_config["sleeptime"]["update_targets"]["knowledge_maturation"]
        is True
    )


def test_knowledge_card_identity_detects_content_changes(tmp_path):
    def _card(summary):
        return {
            "id": "card-1",
            "category": "Hobby",
            "title": "t",
            "ai_importance": 5,
            "humanity_importance": 5,
            "summary": summary,
            "episode": "e",
            "raw_reference": "raw",
        }

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    ButlyDatabase(str(db_a)).register_knowledge(_card("same summary"))
    ButlyDatabase(str(db_b)).register_knowledge(_card("different summary"))

    identity_a = _knowledge_card_identity(db_a)
    identity_b = _knowledge_card_identity(db_b)
    assert identity_a["count"] == identity_b["count"] == 1
    assert identity_a["digest"] != identity_b["digest"]
    assert "changed=['card-1']" in _diff_card_identity(identity_a, identity_b)

    # 同一内容なら digest 一致
    db_c = tmp_path / "c.db"
    ButlyDatabase(str(db_c)).register_knowledge(_card("same summary"))
    assert _knowledge_card_identity(db_c)["digest"] == identity_a["digest"]


def test_rerun_qa_stage3_bootstrap_builds_nodes_on_identical_cards(tmp_path):
    """§10.2 ON 側: カード同一性検証 → bootstrap → 不変再検証 → QA。"""
    source_config = ReplayConfig(
        dataset_path=FIXTURE,
        output_dir=tmp_path,
        run_id="ab-source",
        sample_limit=1,
        session_limit=1,
        question_limit=1,
        qa_mode="independent",
    )
    conversation = load_dataset(FIXTURE)[0]
    source_workspace = EvaluationWorkspace.create(tmp_path, run_id="ab-source")
    _write_run_metadata(source_workspace, source_config, [conversation])
    source_instance = source_workspace.instances_dir / "locomo_synthetic_conv_1"
    _create_instance(
        source_workspace.create_runtime(),
        conversation,
        source_instance.name,
        source_config,
    )
    source_db = source_instance / "butly_memory.db"
    db = ButlyDatabase(str(source_db))
    for index in (1, 2):
        db.register_knowledge(
            {
                "id": f"ab-card-{index}",
                "category": "Hobby",
                "title": f"Pottery fact {index}",
                "ai_importance": 6,
                "humanity_importance": 6,
                "summary": f"Maya pottery detail {index}.",
                "episode": "Synthetic evidence.",
                "raw_reference": "session_0001.json",
            }
        )
    with sqlite3.connect(source_db) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    source_checkpoint = Checkpoint.create(
        source_workspace.run_id, source_workspace.checkpoints_dir
    )
    source_progress = source_checkpoint.progress_for(
        conversation.sample_id, source_instance.name
    )
    source_progress.replayed_sessions = ["session_1"]
    source_progress.sleeptime_completed = ["session_1"]
    source_progress.qa_completed = 1
    source_checkpoint.status = "completed"
    source_checkpoint.save()
    (source_workspace.results_dir / "replay_log.jsonl").write_text(
        '{"status":"succeeded"}\n', encoding="utf-8"
    )
    (source_workspace.results_dir / "sleeptime_log.jsonl").write_text(
        '{"status":"succeeded"}\n', encoding="utf-8"
    )
    source_identity_before = _knowledge_card_identity(source_db)

    profile_path = tmp_path / "stage3_on.yaml"
    profile_path.write_text(STAGE3_ON_PROFILE_YAML, encoding="utf-8")

    async def fake_qa_run(self, **kwargs):
        return {"question_id": kwargs["question"].question_id}

    class UsageReportingFakeProvider(FakeProvider):
        """A/B コスト回収を検証するため実測トークンを 1-slot で返す。"""

        def pop_last_token_usage(self):
            return {"prompt_tokens": 100, "completion_tokens": 20}

    fake_provider = UsageReportingFakeProvider()
    with patch.object(
        SleeptimeRunner,
        "run",
        side_effect=AssertionError("rerun-qa must not run per-session Sleeptime"),
    ), patch.object(QARunner, "run", fake_qa_run), patch(
        "butly_core.llm.factory.ProviderFactory.create",
        return_value=fake_provider,
    ), patch("sleeptime.time.sleep", return_value=None):
        rerun = asyncio.run(
            rerun_qa_from_memory(
                source_workspace.run_dir,
                output_dir=tmp_path,
                run_id="ab-on",
                stage3_bootstrap=True,
                profile_path=profile_path,
            )
        )

    target_instance = rerun.workspace.instances_dir / rerun.instance_names[0]

    identity = json.loads(
        (rerun.workspace.run_dir / "card_identity.json").read_text(
            encoding="utf-8"
        )
    )
    sample_report = identity["synthetic-conv-1"]
    assert sample_report["card_count"] == 2
    assert sample_report["stage3_bootstrap"]["status"] == "completed"
    assert (
        sample_report["stage3_bootstrap"]["post_bootstrap_digest"]
        == sample_report["digest"]
    )

    boot_rows = _read_jsonl(
        rerun.workspace.results_dir / "stage3_bootstrap_log.jsonl"
    )
    assert boot_rows[0]["status"] == "completed"
    assert boot_rows[0]["applied_cards"] == 2
    # A/B コスト比較用に prompt/completion token が回収される (§10.3)
    assert boot_rows[0]["prompt_tokens"] == 100
    assert boot_rows[0]["completion_tokens"] == 20
    assert sample_report["stage3_bootstrap"]["prompt_tokens"] == 100
    assert sample_report["stage3_bootstrap"]["completion_tokens"] == 20

    with sqlite3.connect(target_instance / "butly_memory.db") as connection:
        node_count = connection.execute(
            "SELECT COUNT(*) FROM memory_nodes"
        ).fetchone()[0]
    assert node_count >= 1

    # source / clone ともカード集合は post-Stage 2 正本と不変
    assert (
        _knowledge_card_identity(source_db)["digest"]
        == source_identity_before["digest"]
    )
    assert (
        _knowledge_card_identity(target_instance / "butly_memory.db")["digest"]
        == source_identity_before["digest"]
    )

    run_config = json.loads(
        rerun.workspace.run_config_path.read_text(encoding="utf-8")
    )
    assert run_config["stage3_bootstrap"] is True
    # OFF 側 (stage3_bootstrap 無し) と同じ導線でカード同一性 artifact が出る
    assert rerun.answered_questions == 1

    # Colab 切断などで durable completion proof が残らなかった ON clone は、
    # 部分 node のまま QA へ進めず新しい run ID での再実行を要求する。
    (rerun.workspace.run_dir / "card_identity.json").unlink()
    with pytest.raises(
        WorkspaceError,
        match="potentially partial ON clone",
    ):
        asyncio.run(resume_evaluation(rerun.workspace.run_dir))
