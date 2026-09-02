"""Oracle Reader resolves only annotated LoCoMo evidence turns."""

from pathlib import Path

import pytest

from evals.locomo.dataset import LocomoQuestion, load_dataset
from evals.locomo.oracle_reader import (
    OracleEvidenceError,
    OracleMemoryBlockBuilder,
    resolve_oracle_evidence,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


def test_oracle_evidence_contains_only_referenced_turns_in_source_order():
    conversation = load_dataset(FIXTURE)[0]
    question = conversation.questions[1]

    evidence = resolve_oracle_evidence(conversation, question)

    assert evidence.requested_dialog_ids == ("D1:3", "D2:1")
    assert evidence.resolved_dialog_ids == ("D1:3", "D2:1")
    assert evidence.turn_count == 2
    assert "[D1:3] Maya: The first class is April 13." in evidence.text
    assert "[D2:1] Maya: My first pottery class was on April 13." in evidence.text
    assert evidence.text.index("[D1:3]") < evidence.text.index("[D2:1]")
    assert "D1:1" not in evidence.text
    assert "13 April 2024" not in evidence.text  # gold answer is never injected
    assert evidence.chars == len(evidence.text)


def test_oracle_evidence_is_empty_for_adversarial_question():
    conversation = load_dataset(FIXTURE)[0]

    evidence = resolve_oracle_evidence(conversation, conversation.questions[4])

    assert evidence.text == ""
    assert evidence.turn_count == 0
    assert evidence.to_json_dict()["missing_dialog_ids"] == []
    assert evidence.to_json_dict()["malformed_annotations"] == []


def test_oracle_evidence_rejects_unknown_dialog_id():
    conversation = load_dataset(FIXTURE)[0]
    question = LocomoQuestion(
        question_id="broken",
        question="What happened?",
        answer="Something",
        category=1,
        evidence=["D99:1"],
    )

    with pytest.raises(OracleEvidenceError, match="D99:1"):
        resolve_oracle_evidence(conversation, question)


def test_oracle_evidence_expands_semicolon_joined_official_annotation():
    conversation = load_dataset(FIXTURE)[0]
    question = LocomoQuestion(
        question_id="compound",
        question="What happened?",
        answer="Two things",
        category=1,
        evidence=["D1:3; D2:1"],
    )

    evidence = resolve_oracle_evidence(conversation, question)

    assert evidence.requested_dialog_ids == ("D1:3", "D2:1")
    assert evidence.resolved_dialog_ids == ("D1:3", "D2:1")
    assert evidence.turn_count == 2


def test_oracle_evidence_normalizes_legacy_ids_and_records_partial_misses():
    conversation = load_dataset(FIXTURE)[0]
    question = LocomoQuestion(
        question_id="legacy",
        question="What happened?",
        answer="Two things",
        category=1,
        evidence=["D:1:3 D2:01", "D10:19", "D"],
    )

    evidence = resolve_oracle_evidence(conversation, question)

    assert evidence.requested_dialog_ids == ("D1:3", "D2:1", "D10:19")
    assert evidence.resolved_dialog_ids == ("D1:3", "D2:1")
    assert evidence.missing_dialog_ids == ("D10:19",)
    assert evidence.malformed_annotations == ("D",)


def test_oracle_memory_blocks_exclude_every_normal_memory_source():
    conversation = load_dataset(FIXTURE)[0]
    evidence = resolve_oracle_evidence(conversation, conversation.questions[0])
    builder = OracleMemoryBlockBuilder(evidence)

    blocks = builder.build(
        tier="mid",
        memory_manager=object(),
        brain=object(),
        user_input="ignored",
        instance_name="ignored",
        override_config={"agent_profile": {"locale": "en"}},
        gatekeeper_output={"need": "rag_search"},
    )

    assert blocks["rag_context"] == evidence.text
    assert blocks["rag_source_mode"] == "oracle"
    assert blocks["rag_raw_reference"]["dialog_ids"] == ["D1:3"]
    assert blocks["short_term"] == []
    assert blocks["session_digest"] == ""
    assert blocks["mid_term"] == ""
    assert blocks["glossary_hits"] == []
    assert blocks["active_nodes"] == []
    assert blocks["rag_results_raw"] == []
