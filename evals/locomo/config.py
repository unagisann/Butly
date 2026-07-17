"""Typed configuration and model-profile loading for the LoCoMo runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


# モデル role 5種 + instance-config にそのまま流し込む非モデルセクション
# ("memory": rag_source_mode / rag_raw_max_chars 等の記憶設定オーバーライド)
PROFILE_ROLE_SECTIONS = (
    "chat",
    "gatekeeper",
    "summary",
    "knowledge",
    "embedding",
    "memory",
)


class ProfileError(ValueError):
    """Raised when an evaluation profile YAML cannot be applied."""


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
    profile_path: Optional[Path] = None
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
            "profile_path": (
                str(Path(self.profile_path).resolve())
                if self.profile_path is not None
                else None
            ),
            "clean": self.clean,
            "run_sleeptime_per_session": True,
            "qa_isolation": "sequential_without_sleeptime_phase2",
            "external_search": False,
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> "ReplayConfig":
        """Rebuild the config persisted in run_config.json for resume runs."""
        return cls(
            dataset_path=Path(payload["dataset_path"]),
            output_dir=Path(payload["output_dir"]),
            run_id=payload.get("run_id"),
            sample_ids=tuple(payload.get("sample_ids", [])),
            sample_limit=payload.get("sample_limit"),
            session_limit=payload.get("session_limit"),
            question_limit=int(payload.get("question_limit", 1)),
            model_name=payload.get("model_name"),
            connection=payload.get("connection"),
            profile_path=(
                Path(payload["profile_path"])
                if payload.get("profile_path")
                else None
            ),
            # Resume must never re-trigger a workspace wipe.
            clean=False,
        )


def load_profile(path: Path) -> dict[str, dict[str, Any]]:
    """Load a model-profile YAML: role section -> instance-config overrides."""
    profile_path = Path(path)
    try:
        payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileError(f"Profile not found: {profile_path}") from exc
    except yaml.YAMLError as exc:
        raise ProfileError(f"Invalid YAML in {profile_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileError(f"{profile_path}: profile root must be a mapping")

    sections = {}
    for key, value in payload.items():
        if key == "name":
            continue
        if key not in PROFILE_ROLE_SECTIONS:
            raise ProfileError(
                f"{profile_path}: unknown profile section {key!r}; "
                f"expected one of {PROFILE_ROLE_SECTIONS}"
            )
        if not isinstance(value, dict):
            raise ProfileError(f"{profile_path}: section {key!r} must be a mapping")
        sections[key] = value
    if not sections:
        raise ProfileError(f"{profile_path}: profile defines no role sections")
    return sections


def _validate_optional_limit(name: str, value: Optional[int]) -> None:
    if value is not None and value < 1:
        raise ValueError(f"{name} must be at least 1 when specified")
