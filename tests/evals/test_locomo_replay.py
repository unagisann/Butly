"""External-API-free end-to-end test for the Phase 2 mini replay runner."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from butly_core.chat.types import ChatResponse
from evals.locomo.config import ReplayConfig
from evals.locomo.qa_runner import build_qa_request
from evals.locomo.replay import run_evaluation


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


class FakeProvider:
    """One provider surface reused by every existing Butly model role."""

    def __init__(self):
        self.generate_contexts = []

    def supports_vision(self, model_name):
        return True

    def generate(self, text, attachments, context):
        self.generate_contexts.append(context)
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
    assert qa_rows[0]["prediction"] == (
        "Maya planned a blue mug and an herb planter."
    )
    assert qa_rows[0]["request"]["use_rag"] is True
    assert qa_rows[0]["request"]["use_google_search"] is False
    assert qa_rows[0]["request"]["use_web_search"] is False
    assert qa_rows[0]["retrieved_card_ids"]

    trace_path = workspace.run_dir / qa_rows[0]["trace_path"]
    assert trace_path.is_file()
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["nodes"]

    instance_dir = workspace.instances_dir / "locomo_synthetic_conv_1"
    assert (instance_dir / "butly_memory.db").is_file()
    snapshot_root = workspace.snapshots_dir / instance_dir.name / "session_1"
    assert (snapshot_root / "before_sleeptime").is_dir()
    assert (snapshot_root / "after_sleeptime").is_dir()
    assert (workspace.run_dir / "dataset_manifest.json").is_file()
    assert (workspace.run_dir / "environment.json").is_file()

    assert fake_provider.generate_contexts
    memory_blocks = fake_provider.generate_contexts[0]["memory_blocks"]
    assert memory_blocks["rag_context"]
