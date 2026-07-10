"""Parse the official LoCoMo JSON shape into stable evaluation DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Optional


_SESSION_KEY = re.compile(r"^session_(\d+)$")
_OFFICIAL_DATETIME_FORMATS = (
    "%I:%M %p on %d %B, %Y",
    "%I:%M %p on %d %B %Y",
)
_NO_INFORMATION_ANSWER = "No information available"


class LocomoDatasetError(ValueError):
    """Raised when LoCoMo input cannot be converted without ambiguity."""


@dataclass(frozen=True)
class LocomoTurn:
    dialog_id: str
    speaker: str
    text: str
    timestamp: datetime
    image_caption: Optional[str] = None


@dataclass(frozen=True)
class LocomoSession:
    session_id: str
    timestamp: datetime
    turns: list[LocomoTurn]


@dataclass(frozen=True)
class LocomoQuestion:
    question_id: str
    question: str
    answer: str
    category: int
    evidence: list[str]


@dataclass(frozen=True)
class LocomoConversation:
    sample_id: str
    speaker_a: str
    speaker_b: str
    sessions: list[LocomoSession]
    questions: list[LocomoQuestion]


def load_dataset(path: Path) -> list[LocomoConversation]:
    """Load a LoCoMo JSON file and convert it to immutable DTOs."""
    source_path = Path(path)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LocomoDatasetError(f"LoCoMo dataset not found: {source_path}") from exc
    except json.JSONDecodeError as exc:
        raise LocomoDatasetError(
            f"Invalid JSON in {source_path}: line {exc.lineno}, column {exc.colno}"
        ) from exc
    return parse_dataset(payload, source=str(source_path))


def parse_dataset(
    payload: Any,
    *,
    source: str = "LoCoMo payload",
) -> list[LocomoConversation]:
    """Convert an in-memory LoCoMo payload without mutating the input."""
    if not isinstance(payload, list):
        raise LocomoDatasetError(f"{source}: root must be a JSON array")
    if not payload:
        raise LocomoDatasetError(f"{source}: dataset must contain at least one sample")

    conversations = []
    seen_sample_ids = set()
    for sample_index, raw_sample in enumerate(payload, start=1):
        context = f"{source}: sample[{sample_index}]"
        sample = _require_dict(raw_sample, context)
        sample_id = _require_text(sample.get("sample_id"), f"{context}.sample_id")
        if sample_id in seen_sample_ids:
            raise LocomoDatasetError(f"{context}: duplicate sample_id {sample_id!r}")
        seen_sample_ids.add(sample_id)
        conversations.append(_parse_conversation(sample, sample_id, source))
    return conversations


def _parse_conversation(
    sample: dict[str, Any],
    sample_id: str,
    source: str,
) -> LocomoConversation:
    context = f"{source}: {sample_id}"
    raw_conversation = _require_dict(
        sample.get("conversation"), f"{context}.conversation"
    )
    speaker_a = _require_text(
        raw_conversation.get("speaker_a"), f"{context}.conversation.speaker_a"
    )
    speaker_b = _require_text(
        raw_conversation.get("speaker_b"), f"{context}.conversation.speaker_b"
    )
    if speaker_a == speaker_b:
        raise LocomoDatasetError(f"{context}: speaker_a and speaker_b must differ")

    session_entries = []
    for key, value in raw_conversation.items():
        match = _SESSION_KEY.fullmatch(key)
        if match:
            session_entries.append((int(match.group(1)), key, value))
    if not session_entries:
        raise LocomoDatasetError(f"{context}: no session_<number> arrays found")

    sessions = []
    seen_dialog_ids = set()
    for _, session_id, raw_turns in sorted(session_entries):
        date_key = f"{session_id}_date_time"
        if date_key not in raw_conversation:
            raise LocomoDatasetError(
                f"{context} {session_id}: missing required {date_key}"
            )
        timestamp = _parse_datetime(
            raw_conversation[date_key], f"{context} {session_id} {date_key}"
        )
        turns = _parse_turns(
            raw_turns,
            sample_id=sample_id,
            session_id=session_id,
            timestamp=timestamp,
            allowed_speakers={speaker_a, speaker_b},
            seen_dialog_ids=seen_dialog_ids,
        )
        sessions.append(
            LocomoSession(
                session_id=session_id,
                timestamp=timestamp,
                turns=turns,
            )
        )

    raw_questions = sample.get("qa")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise LocomoDatasetError(f"{context}.qa must be a non-empty array")
    questions = [
        _parse_question(raw, sample_id, index)
        for index, raw in enumerate(raw_questions, start=1)
    ]
    return LocomoConversation(
        sample_id=sample_id,
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        sessions=sessions,
        questions=questions,
    )


def _parse_turns(
    raw_turns: Any,
    *,
    sample_id: str,
    session_id: str,
    timestamp: datetime,
    allowed_speakers: set[str],
    seen_dialog_ids: set[str],
) -> list[LocomoTurn]:
    context = f"{sample_id} {session_id}"
    if not isinstance(raw_turns, list) or not raw_turns:
        raise LocomoDatasetError(f"{context}: session must be a non-empty array")

    turns = []
    for turn_index, raw_turn in enumerate(raw_turns, start=1):
        turn_context = f"{context} turn[{turn_index}]"
        turn = _require_dict(raw_turn, turn_context)
        dialog_id = _require_text(turn.get("dia_id"), f"{turn_context}.dia_id")
        if dialog_id in seen_dialog_ids:
            raise LocomoDatasetError(
                f"{turn_context}: duplicate dialog id {dialog_id!r}"
            )
        seen_dialog_ids.add(dialog_id)
        speaker = _require_text(turn.get("speaker"), f"{turn_context} {dialog_id}.speaker")
        if speaker not in allowed_speakers:
            raise LocomoDatasetError(
                f"{turn_context} {dialog_id}: unknown speaker {speaker!r}; "
                f"expected one of {sorted(allowed_speakers)!r}"
            )

        raw_text = turn.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        raw_caption = turn.get("blip_caption")
        image_caption = (
            raw_caption.strip()
            if isinstance(raw_caption, str) and raw_caption.strip()
            else None
        )
        if image_caption:
            image_text = f"[Image: {image_caption}]"
            text = f"{text}\n{image_text}" if text else image_text
        if not text:
            raise LocomoDatasetError(
                f"{turn_context} {dialog_id}: turn requires text or blip_caption"
            )
        turns.append(
            LocomoTurn(
                dialog_id=dialog_id,
                speaker=speaker,
                text=text,
                timestamp=timestamp,
                image_caption=image_caption,
            )
        )
    return turns


def _parse_question(
    raw_question: Any,
    sample_id: str,
    index: int,
) -> LocomoQuestion:
    context = f"{sample_id} qa[{index}]"
    item = _require_dict(raw_question, context)
    question = _require_text(item.get("question"), f"{context}.question")

    category = item.get("category")
    if isinstance(category, bool) or not isinstance(category, int):
        raise LocomoDatasetError(f"{context}.category must be an integer")
    if category not in {1, 2, 3, 4, 5}:
        raise LocomoDatasetError(f"{context}.category must be between 1 and 5")

    raw_answer = item.get("answer")
    if raw_answer is None:
        if category != 5:
            raise LocomoDatasetError(
                f"{context}.answer may be null only for category 5"
            )
        answer = _NO_INFORMATION_ANSWER
    elif isinstance(raw_answer, bool) or not isinstance(raw_answer, (str, int, float)):
        raise LocomoDatasetError(
            f"{context}.answer must be text, number, or null for category 5"
        )
    else:
        answer = str(raw_answer).strip()
        if not answer:
            raise LocomoDatasetError(f"{context}.answer must not be empty")

    raw_evidence = item.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raise LocomoDatasetError(f"{context}.evidence must be an array")
    evidence = []
    for evidence_index, value in enumerate(raw_evidence, start=1):
        evidence.append(
            _require_text(value, f"{context}.evidence[{evidence_index}]")
        )

    raw_id = item.get("question_id", item.get("qa_id", item.get("id")))
    question_id = (
        _require_text(raw_id, f"{context}.question_id")
        if raw_id is not None
        else f"{sample_id}_qa_{index:03d}"
    )
    return LocomoQuestion(
        question_id=question_id,
        question=question,
        answer=answer,
        category=category,
        evidence=evidence,
    )


def _parse_datetime(value: Any, context: str) -> datetime:
    text = _require_text(value, context)
    for date_format in _OFFICIAL_DATETIME_FORMATS:
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        formats = ", ".join(_OFFICIAL_DATETIME_FORMATS)
        raise LocomoDatasetError(
            f"{context}: unsupported datetime {text!r}; expected {formats} or ISO-8601"
        ) from exc


def _require_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocomoDatasetError(f"{context} must be a JSON object")
    return value


def _require_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocomoDatasetError(f"{context} must be a non-empty string")
    return value.strip()
