"""LoCoMo dataset parser tests using a fully synthetic fixture."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from evals.locomo.dataset import (
    LocomoDatasetError,
    load_dataset,
    parse_dataset,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


def test_loads_official_shape_and_sorts_sessions_numerically():
    conversations = load_dataset(FIXTURE)

    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation.sample_id == "synthetic-conv-1"
    assert conversation.speaker_a == "Maya"
    assert conversation.speaker_b == "Noah"
    assert [session.session_id for session in conversation.sessions] == [
        "session_1",
        "session_2",
    ]
    assert conversation.sessions[0].timestamp == datetime(2024, 4, 8, 10, 30)
    assert conversation.sessions[1].timestamp == datetime(2024, 5, 20, 18, 45)


def test_image_caption_is_preserved_and_added_to_turn_text():
    conversation = load_dataset(FIXTURE)[0]
    image_turn = conversation.sessions[0].turns[2]

    assert image_turn.dialog_id == "D1:3"
    assert image_turn.image_caption == (
        "two unfinished clay pieces beside a small bowl of blue glaze"
    )
    assert "[Image: two unfinished clay pieces" in image_turn.text


def test_questions_get_stable_ids_and_normalized_answers():
    questions = load_dataset(FIXTURE)[0].questions

    assert [question.question_id for question in questions] == [
        f"synthetic-conv-1_qa_{index:03d}" for index in range(1, 6)
    ]
    assert [question.category for question in questions] == [1, 2, 3, 4, 5]
    assert questions[4].answer == "No information available"
    assert questions[4].evidence == []


def test_parse_does_not_mutate_source_payload():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    original = deepcopy(payload)

    parse_dataset(payload, source="synthetic fixture")

    assert payload == original


def test_numeric_answer_is_normalized_to_string():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload[0]["qa"][0]["answer"] = 2024

    question = parse_dataset(payload)[0].questions[0]

    assert question.answer == "2024"


def test_missing_session_datetime_has_contextual_error():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del payload[0]["conversation"]["session_1_date_time"]

    with pytest.raises(
        LocomoDatasetError,
        match=r"synthetic-conv-1.*session_1.*session_1_date_time",
    ):
        parse_dataset(payload, source="broken fixture")


def test_unknown_speaker_has_contextual_error():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload[0]["conversation"]["session_1"][0]["speaker"] = "Unknown"

    with pytest.raises(
        LocomoDatasetError,
        match=r"synthetic-conv-1.*session_1.*D1:1.*Unknown",
    ):
        parse_dataset(payload)
