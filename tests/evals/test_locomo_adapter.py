"""LoCoMo speaker-to-role replay adapter tests."""

from datetime import datetime
import json
from pathlib import Path

from butly_core.core.memory import ButlyMemory
from evals.locomo.adapter import ReplayAdapter
from evals.locomo.dataset import (
    LocomoConversation,
    LocomoSession,
    LocomoTurn,
    load_dataset,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mini_locomo.json"


def _memory(tmp_path: Path) -> ButlyMemory:
    return ButlyMemory(tmp_path, instance_name="locomo_synthetic")


def test_alternating_speakers_are_saved_as_user_assistant_pairs(tmp_path):
    conversation = load_dataset(FIXTURE)[0]
    memory = _memory(tmp_path)
    adapter = ReplayAdapter(memory, conversation)

    saved = adapter.replay_session(conversation.sessions[0])

    assert len(saved) == 2
    assert saved[0].dialog_ids == ["D1:1", "D1:2"]
    files = sorted(memory.short_term_json_dir.glob("*.json"))
    first = json.loads(files[0].read_text(encoding="utf-8"))
    assert first["timestamp"] == "2024-04-08T10:30:00"
    assert first["messages"][0]["role"] == "user"
    assert first["messages"][0]["parts"] == [conversation.sessions[0].turns[0].text]
    assert first["messages"][1] == {
        "role": "model",
        "parts": [conversation.sessions[0].turns[1].text],
    }

    meta = first["messages"][0]["meta"]
    assert meta["locomo_sample_id"] == "synthetic-conv-1"
    assert meta["locomo_session_id"] == "session_1"
    assert meta["locomo_dialog_id"] == "D1:1"
    assert meta["original_speaker"] == "Maya"
    assert meta["original_timestamp"] == "2024-04-08T10:30:00"
    assert meta["source"] == "eval"
    assert meta["lane"] == "direct"
    assert meta["locomo_dialog_ids"] == ["D1:1", "D1:2"]
    assert meta["speaker_roles"] == {"Maya": "user", "Noah": "assistant"}


def test_irregular_speaker_order_preserves_all_turns(tmp_path):
    timestamp = datetime(2024, 6, 1, 9, 0)
    turns = [
        LocomoTurn("D1:1", "A", "first A", timestamp),
        LocomoTurn("D1:2", "A", "second A", timestamp),
        LocomoTurn("D1:3", "B", "then B", timestamp),
        LocomoTurn("D1:4", "B", "another B", timestamp),
    ]
    session = LocomoSession("session_1", timestamp, turns)
    conversation = LocomoConversation("irregular", "A", "B", [session], [])
    memory = _memory(tmp_path)

    records = ReplayAdapter(memory, conversation).replay_session(session)

    assert [dialog_id for record in records for dialog_id in record.dialog_ids] == [
        "D1:1",
        "D1:2",
        "D1:3",
        "D1:4",
    ]
    actual_texts = []
    for path in sorted(memory.short_term_json_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        actual_texts.extend(
            message["parts"][0]
            for message in data["messages"]
            if message["parts"][0]
        )
    assert actual_texts == ["first A", "second A", "then B", "another B"]


def test_assistant_first_turn_keeps_its_metadata(tmp_path):
    timestamp = datetime(2024, 6, 1, 9, 0)
    session = LocomoSession(
        "session_1",
        timestamp,
        [LocomoTurn("D1:1", "B", "assistant first", timestamp)],
    )
    conversation = LocomoConversation("assistant-first", "A", "B", [session], [])
    memory = _memory(tmp_path)

    record = ReplayAdapter(memory, conversation).replay_session(session)[0]

    saved = json.loads(
        (memory.short_term_json_dir / record.file_name).read_text(encoding="utf-8")
    )
    assert saved["messages"][0]["parts"] == [""]
    assert saved["messages"][1]["parts"] == ["assistant first"]
    meta = saved["messages"][0]["meta"]
    assert meta["locomo_dialog_id"] == "D1:1"
    assert meta["original_speaker"] == "B"
