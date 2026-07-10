"""Convert fixed LoCoMo turns into Butly's persisted user/model turn format."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from butly_core.core.memory import ButlyMemory

from .dataset import LocomoConversation, LocomoSession, LocomoTurn


@dataclass(frozen=True)
class SavedReplayTurn:
    file_name: str
    dialog_ids: list[str]
    user_dialog_id: Optional[str]
    assistant_dialog_id: Optional[str]


class ReplayAdapter:
    """Map ``speaker_a`` to user and ``speaker_b`` to assistant.

    Consecutive utterances from the same role are flushed with an empty opposite
    side. This keeps source order and every dialog id while still using
    ``ButlyMemory.save_single_turn`` as the canonical writer.
    """

    def __init__(
        self,
        memory: ButlyMemory,
        conversation: LocomoConversation,
    ):
        self.memory = memory
        self.conversation = conversation

    @property
    def speaker_roles(self) -> dict[str, str]:
        return {
            self.conversation.speaker_a: "user",
            self.conversation.speaker_b: "assistant",
        }

    def replay_session(self, session: LocomoSession) -> list[SavedReplayTurn]:
        """Persist one session in source order and return saved-file metadata."""
        saved = []
        pending_user: Optional[LocomoTurn] = None

        for turn in session.turns:
            if turn.speaker == self.conversation.speaker_a:
                if pending_user is not None:
                    saved.append(self._save_pair(session, pending_user, None))
                pending_user = turn
                continue

            if turn.speaker != self.conversation.speaker_b:
                raise ValueError(
                    f"Unknown speaker {turn.speaker!r} in {session.session_id}"
                )
            if pending_user is None:
                saved.append(self._save_pair(session, None, turn))
            else:
                saved.append(self._save_pair(session, pending_user, turn))
                pending_user = None

        if pending_user is not None:
            saved.append(self._save_pair(session, pending_user, None))
        return saved

    def _save_pair(
        self,
        session: LocomoSession,
        user_turn: Optional[LocomoTurn],
        assistant_turn: Optional[LocomoTurn],
    ) -> SavedReplayTurn:
        source_turns = [
            turn for turn in (user_turn, assistant_turn) if turn is not None
        ]
        anchor = source_turns[0]
        timestamp = anchor.timestamp
        meta = {
            "locomo_sample_id": self.conversation.sample_id,
            "locomo_session_id": session.session_id,
            "locomo_dialog_id": anchor.dialog_id,
            "original_speaker": anchor.speaker,
            "original_timestamp": timestamp.isoformat(),
            "source": "eval",
            "lane": "direct",
            "person_id": _person_id(self.conversation.sample_id, anchor.speaker),
            "display_name": anchor.speaker,
            "locomo_dialog_ids": [turn.dialog_id for turn in source_turns],
            "speaker_roles": self.speaker_roles,
            "locomo_turns": [
                {
                    "dialog_id": turn.dialog_id,
                    "speaker": turn.speaker,
                    "role": self.speaker_roles[turn.speaker],
                    "timestamp": turn.timestamp.isoformat(),
                }
                for turn in source_turns
            ],
        }
        file_name = self.memory.save_single_turn(
            user_turn.text if user_turn is not None else "",
            assistant_turn.text if assistant_turn is not None else "",
            meta=meta,
            created_at=timestamp,
        )
        if file_name is None:
            raise RuntimeError(
                f"Failed to save replay pair for {session.session_id}: "
                f"{meta['locomo_dialog_ids']}"
            )
        return SavedReplayTurn(
            file_name=file_name,
            dialog_ids=meta["locomo_dialog_ids"],
            user_dialog_id=user_turn.dialog_id if user_turn is not None else None,
            assistant_dialog_id=(
                assistant_turn.dialog_id if assistant_turn is not None else None
            ),
        )


def _person_id(sample_id: str, speaker: str) -> str:
    raw = f"{sample_id}_{speaker}".lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return f"p_locomo_{slug}"
