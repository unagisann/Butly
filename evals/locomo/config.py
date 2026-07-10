"""Typed configuration for the Phase 2 LoCoMo replay CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ReplayConfig:
    dataset_path: Path
    output_dir: Path
    run_id: Optional[str] = None
    sample_ids: tuple[str, ...] = ()
    sample_limit: Optional[int] = 1
    session_limit: Optional[int] = None
    question_limit: int = 1
    model_name: Optional[str] = None
    connection: Optional[str] = None
    clean: bool = False

    def __post_init__(self) -> None:
        _validate_optional_limit("sample_limit", self.sample_limit)
        _validate_optional_limit("session_limit", self.session_limit)
        if self.question_limit < 1:
            raise ValueError("question_limit must be at least 1")

    def to_json_dict(self) -> dict:
        return {
            "dataset_path": str(Path(self.dataset_path).resolve()),
            "output_dir": str(Path(self.output_dir).resolve()),
            "run_id": self.run_id,
            "sample_ids": list(self.sample_ids),
            "sample_limit": self.sample_limit,
            "session_limit": self.session_limit,
            "question_limit": self.question_limit,
            "model_name": self.model_name,
            "connection": self.connection,
            "clean": self.clean,
            "run_sleeptime_per_session": True,
            "qa_isolation": "sequential_without_sleeptime_phase2",
            "external_search": False,
        }


def _validate_optional_limit(name: str, value: Optional[int]) -> None:
    if value is not None and value < 1:
        raise ValueError(f"{name} must be at least 1 when specified")
