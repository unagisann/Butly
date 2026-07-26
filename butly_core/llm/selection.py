"""Pure model/Connection selection helpers used by settings UIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ModelChoice:
    connection_id: Optional[str]
    model_name: str


def normalize_candidates(candidates: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Optional[str], str]] = set()
    for candidate in candidates:
        if isinstance(candidate, str):
            model_name = candidate.strip()
            item = {
                "connection_id": None,
                "model_name": model_name,
                "label": model_name,
            }
        elif isinstance(candidate, dict):
            model_name = str(candidate.get("model_name") or "").strip()
            item = dict(candidate)
            item["model_name"] = model_name
            item.setdefault("connection_id", None)
            item.setdefault("label", model_name)
        else:
            continue
        if not model_name:
            continue
        key = candidate_key(item)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


def candidate_key(candidate: dict[str, Any]) -> tuple[Optional[str], str]:
    return candidate.get("connection_id"), str(candidate.get("model_name") or "")


def ensure_current_in_candidates(
    candidates: list[dict[str, Any]],
    current: ModelChoice,
) -> list[dict[str, Any]]:
    result = [dict(candidate) for candidate in candidates]
    if not current.model_name:
        return result
    key = (current.connection_id, current.model_name)
    if any(candidate_key(candidate) == key for candidate in result):
        return result
    result.append(
        {
            "connection_id": current.connection_id,
            "model_name": current.model_name,
            "label": current.model_name,
            "source": "saved",
        }
    )
    return result


def find_current_index(
    candidates: list[dict[str, Any]],
    current: ModelChoice,
) -> int:
    exact = (current.connection_id, current.model_name)
    for index, candidate in enumerate(candidates):
        if candidate_key(candidate) == exact:
            return index
    for index, candidate in enumerate(candidates):
        if candidate.get("model_name") == current.model_name:
            return index
    return 0


def set_model_choice(target: dict[str, Any], choice: ModelChoice) -> None:
    target["model_name"] = choice.model_name
    if choice.connection_id:
        target["connection"] = choice.connection_id
    else:
        from butly_core.llm.model_registry import infer_connection_id

        inferred = infer_connection_id(choice.model_name)
        if inferred:
            target["connection"] = inferred
        else:
            target.pop("connection", None)
