import json
from pathlib import Path

import pytest

from evals.dialogue_ab import (
    DialogueABError,
    build_dialogue_scores,
    load_dialogue_dataset,
)


FIXTURE = Path(__file__).parents[1] / "data" / "ja_dialogue_ab_prompts_v1.json"


def test_loads_japanese_dialogue_fixture():
    dataset = load_dialogue_dataset(FIXTURE)

    assert dataset.dataset_id == "ja-dialogue-ab-prompts-v1"
    assert dataset.locale == "ja"
    assert len(dataset.memory_seed) == 10
    assert len(dataset.prompts) == 30
    assert {
        prompt.category for prompt in dataset.prompts
    } == {
        "memory_required",
        "memory_irrelevant",
        "memory_optional",
    }


def test_rejects_unknown_target_memory(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["prompts"][0]["target_memory_ids"] = ["missing"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DialogueABError, match="unknown memory seeds"):
        load_dialogue_dataset(path)


def test_build_scores_compares_policy_arms():
    dataset = load_dialogue_dataset(FIXTURE)
    prompt = dataset.prompts[0]
    results = [
        {
            "policy": "intent_gated",
            "prompt_id": prompt.prompt_id,
            "category": prompt.category,
            "response": "分かりません",
            "rag_triggered": False,
            "search_executed": True,
            "prompt_tokens": 100,
            "total_prompt_tokens": 150,
            "latency_ms": 1000,
            "target_term_recall": 0.0,
            "seed_term_mentions": [],
        },
        {
            "policy": "candidates",
            "prompt_id": prompt.prompt_id,
            "category": prompt.category,
            "response": "こむぎは三毛猫の女の子です",
            "rag_triggered": True,
            "search_executed": True,
            "prompt_tokens": 160,
            "total_prompt_tokens": 220,
            "latency_ms": 1200,
            "target_term_recall": 1.0,
            "seed_term_mentions": ["こむぎ", "三毛猫", "女の子"],
        },
    ]

    scores = build_dialogue_scores(
        dataset,
        results,
        run_id="dialogue-v1",
        knowledge_cards=10,
    )

    assert scores["run_type"] == "dialogue_ab"
    assert scores["comparison"]["prompt_tokens_mean_delta"] == 60.0
    assert scores["comparison"]["required_target_recall_delta"] == 1.0
    first = scores["prompts"][0]
    assert first["prompt_tokens_delta"] == 60.0
    assert first["response_changed"] is True
