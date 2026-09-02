"""Evaluation-only reader ceiling using LoCoMo's annotated evidence turns."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .dataset import LocomoConversation, LocomoQuestion, LocomoTurn


_EVIDENCE_DIALOG_ID = re.compile(r"D:?(\d+):(\d+)")


class OracleEvidenceError(ValueError):
    """Raised when an annotated evidence turn cannot be resolved exactly."""


@dataclass(frozen=True)
class OracleEvidence:
    """Rendered evidence plus audit metadata; never contains the gold answer."""

    text: str
    requested_dialog_ids: tuple[str, ...]
    resolved_dialog_ids: tuple[str, ...]
    missing_dialog_ids: tuple[str, ...]
    malformed_annotations: tuple[str, ...]
    turn_count: int
    chars: int

    def to_json_dict(self) -> dict:
        return {
            "requested_dialog_ids": list(self.requested_dialog_ids),
            "resolved_dialog_ids": list(self.resolved_dialog_ids),
            "turn_count": self.turn_count,
            "chars": self.chars,
            "missing_dialog_ids": list(self.missing_dialog_ids),
            "malformed_annotations": list(self.malformed_annotations),
        }


def resolve_oracle_evidence(
    conversation: LocomoConversation,
    question: LocomoQuestion,
) -> OracleEvidence:
    """Resolve only ``question.evidence`` and render it in source order."""

    # Official LoCoMo contains a handful of legacy annotation variants:
    # semicolon/space-joined IDs, ``D:11:26``, ``D30:05``, and a stray ``D``.
    # Parse recognizable IDs deterministically and retain malformed fragments
    # in diagnostics instead of guessing a source turn.
    requested_items = []
    malformed = []
    for annotation in question.evidence:
        matches = list(_EVIDENCE_DIALOG_ID.finditer(annotation))
        if not matches:
            malformed.append(annotation)
            continue
        requested_items.extend(
            f"D{int(match.group(1))}:{int(match.group(2))}"
            for match in matches
        )
        remainder = _EVIDENCE_DIALOG_ID.sub("", annotation)
        if remainder.strip(" ;,"):
            malformed.append(annotation)
    requested = tuple(dict.fromkeys(requested_items))
    requested_set = set(requested)
    all_turns = [
        turn
        for session in conversation.sessions
        for turn in session.turns
    ]
    by_id = {turn.dialog_id: turn for turn in all_turns}
    missing = tuple(
        dialog_id for dialog_id in requested if dialog_id not in by_id
    )

    selected = [turn for turn in all_turns if turn.dialog_id in requested_set]
    if requested and not selected:
        raise OracleEvidenceError(
            f"{conversation.sample_id} {question.question_id}: no annotated "
            f"evidence turn can be resolved; missing={list(missing)}"
        )
    text = _render_turns(selected)
    return OracleEvidence(
        text=text,
        requested_dialog_ids=requested,
        resolved_dialog_ids=tuple(turn.dialog_id for turn in selected),
        missing_dialog_ids=missing,
        malformed_annotations=tuple(malformed),
        turn_count=len(selected),
        chars=len(text),
    )


def _render_turns(turns: list[LocomoTurn]) -> str:
    if not turns:
        return ""

    lines = ["Source Conversation Excerpts"]
    previous_timestamp = None
    for turn in turns:
        if turn.timestamp != previous_timestamp:
            lines.append("")
            lines.append(
                turn.timestamp.strftime("Conversation date: %d %B %Y, %I:%M %p")
                .replace(" 0", " ")
            )
            previous_timestamp = turn.timestamp
        rendered_text = turn.text.replace("\n", "\n  ")
        lines.append(f"[{turn.dialog_id}] {turn.speaker}: {rendered_text}")
    return "\n".join(lines)


class OracleMemoryBlockBuilder:
    """Build memory blocks containing no data except resolved oracle turns."""

    def __init__(self, evidence: OracleEvidence):
        self.evidence = evidence

    def build(
        self,
        tier: str,
        memory_manager,
        brain=None,
        user_input: str = "",
        instance_name: str = "00_master",
        override_config: dict = None,
        gatekeeper_output: dict = None,
    ) -> dict:
        del memory_manager, brain, user_input, instance_name, gatekeeper_output
        config = override_config if isinstance(override_config, dict) else {}
        profile = config.get("agent_profile") or config.get("agent") or {}
        locale = profile.get("locale") or "en"
        raw_reference = {
            "status": "oracle",
            "chars": self.evidence.chars,
            "truncated": False,
            "file_count": 0,
            "files": [],
            "dialog_ids": list(self.evidence.resolved_dialog_ids),
            "missing_dialog_ids": list(self.evidence.missing_dialog_ids),
            "malformed_annotations": list(
                self.evidence.malformed_annotations
            ),
            "turn_count": self.evidence.turn_count,
        }
        return {
            "tier": tier,
            "topic": "",
            "locale": locale,
            "allow_user_prompt_overrides": False,
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "mid_term_digest": "",
            "mid_term_recent_snapshot": "",
            "glossary_hits": [],
            "_probe_ran": True,
            "need": "oracle_evidence" if self.evidence.turn_count else None,
            "search_targets": None,
            "rag_context": self.evidence.text,
            "rag_source_mode": "oracle",
            "rag_raw_reference": raw_reference,
            "rag_results_raw": [],
            "rag_card_ids": [],
            "active_nodes": [],
            "active_node_lookup": {
                "enabled": False,
                "attempted": False,
                "candidate_count": 0,
                "matched_count": 0,
                "reason": "oracle_reader",
            },
        }
