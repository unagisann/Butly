"""Typed configuration and model-profile loading for the LoCoMo runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


# モデル role 5種 + instance-config にそのまま流し込む非モデルセクション
# ("memory": RAG source、"brain": 検索スコアリングの評価用オーバーライド)
PROFILE_ROLE_SECTIONS = (
    "chat",
    "gatekeeper",
    "summary",
    "knowledge",
    "embedding",
    "memory",
    "brain",
)
QA_MODES = ("independent", "sequential")
DEFAULT_EVALUATION_LOCALE = "en"
SUPPORTED_EVALUATION_LOCALES = ("en", "ja")


class ProfileError(ValueError):
    """Raised when an evaluation profile YAML cannot be applied."""


@dataclass(frozen=True)
class EvaluationProfile:
    """Typed top-level settings plus instance-config role overrides."""

    name: Optional[str]
    locale: Optional[str]
    sections: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ReplayConfig:
    dataset_path: Path
    output_dir: Path
    run_id: Optional[str] = None
    sample_ids: tuple[str, ...] = ()
    sample_limit: Optional[int] = 1
    session_limit: Optional[int] = None
    question_limit: Optional[int] = 1
    qa_mode: str = "independent"
    locale: Optional[str] = None
    model_name: Optional[str] = None
    connection: Optional[str] = None
    profile_path: Optional[Path] = None
    clean: bool = False

    def __post_init__(self) -> None:
        _validate_optional_limit("sample_limit", self.sample_limit)
        _validate_optional_limit("session_limit", self.session_limit)
        _validate_optional_limit("question_limit", self.question_limit)
        if self.qa_mode not in QA_MODES:
            raise ValueError(f"qa_mode must be one of {QA_MODES}")
        if self.locale is not None and (
            not isinstance(self.locale, str)
            or self.locale.strip() not in SUPPORTED_EVALUATION_LOCALES
        ):
            raise ValueError(
                f"locale must be one of {SUPPORTED_EVALUATION_LOCALES}"
            )

    def to_json_dict(self) -> dict:
        return {
            "dataset_path": str(Path(self.dataset_path).resolve()),
            "output_dir": str(Path(self.output_dir).resolve()),
            "run_id": self.run_id,
            "sample_ids": list(self.sample_ids),
            "sample_limit": self.sample_limit,
            "session_limit": self.session_limit,
            "question_limit": self.question_limit,
            "qa_mode": self.qa_mode,
            "locale": self.locale,
            "model_name": self.model_name,
            "connection": self.connection,
            "profile_path": (
                str(Path(self.profile_path).resolve())
                if self.profile_path is not None
                else None
            ),
            "clean": self.clean,
            "run_sleeptime_per_session": True,
            "qa_isolation": self.qa_mode,
            "external_search": False,
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> "ReplayConfig":
        """Rebuild the config persisted in run_config.json for resume runs."""
        qa_mode = payload.get("qa_mode")
        if qa_mode is None:
            legacy_isolation = payload.get("qa_isolation")
            qa_mode = (
                "sequential"
                if legacy_isolation
                in {"sequential", "sequential_without_sleeptime_phase2"}
                else "independent"
            )
        return cls(
            dataset_path=Path(payload["dataset_path"]),
            output_dir=Path(payload["output_dir"]),
            run_id=payload.get("run_id"),
            sample_ids=tuple(payload.get("sample_ids", [])),
            sample_limit=_optional_int(payload.get("sample_limit", 1)),
            session_limit=_optional_int(payload.get("session_limit")),
            question_limit=_optional_int(payload.get("question_limit", 1)),
            qa_mode=str(qa_mode),
            locale=_optional_text(payload.get("locale")),
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


def load_profile(path: Path) -> EvaluationProfile:
    """Load typed evaluation settings and instance-config role overrides."""
    profile_path = Path(path)
    try:
        payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileError(f"Profile not found: {profile_path}") from exc
    except yaml.YAMLError as exc:
        raise ProfileError(f"Invalid YAML in {profile_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileError(f"{profile_path}: profile root must be a mapping")

    raw_name = payload.get("name")
    if raw_name is not None and (
        not isinstance(raw_name, str) or not raw_name.strip()
    ):
        raise ProfileError(f"{profile_path}: name must be a non-empty string")
    raw_locale = payload.get("locale")
    if raw_locale is not None and (
        not isinstance(raw_locale, str) or not raw_locale.strip()
    ):
        raise ProfileError(f"{profile_path}: locale must be a non-empty string")
    if (
        isinstance(raw_locale, str)
        and raw_locale.strip() not in SUPPORTED_EVALUATION_LOCALES
    ):
        raise ProfileError(
            f"{profile_path}: locale must be one of "
            f"{SUPPORTED_EVALUATION_LOCALES}"
        )

    sections = {}
    for key, value in payload.items():
        if key in {"name", "locale"}:
            continue
        if key not in PROFILE_ROLE_SECTIONS:
            raise ProfileError(
                f"{profile_path}: unknown profile section {key!r}; "
                f"expected one of {PROFILE_ROLE_SECTIONS}"
            )
        if not isinstance(value, dict):
            raise ProfileError(f"{profile_path}: section {key!r} must be a mapping")
        sections[key] = value
    if not sections and raw_locale is None:
        raise ProfileError(
            f"{profile_path}: profile defines neither locale nor role sections"
        )
    return EvaluationProfile(
        name=raw_name.strip() if isinstance(raw_name, str) else None,
        locale=raw_locale.strip() if isinstance(raw_locale, str) else None,
        sections=sections,
    )


def resolve_evaluation_locale(
    cli_locale: Optional[str],
    profile_locale: Optional[str],
) -> str:
    """Resolve CLI > profile > English for reproducible evaluation runs."""
    return (
        (cli_locale.strip() if cli_locale is not None else None)
        or (profile_locale.strip() if profile_locale is not None else None)
        or DEFAULT_EVALUATION_LOCALE
    )


def _validate_optional_limit(name: str, value: Optional[int]) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer when specified")
    if value < 1:
        raise ValueError(f"{name} must be at least 1 when specified")


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("limit values must be integers or null")
    return int(value)


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
