"""Resolve and persist the public memory-retrieval settings contract."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any, Mapping, Optional

from butly_core.io_utils import atomic_write_text
from butly_core.settings.defaults import SYSTEM_CONFIG as DEFAULT_SYSTEM_CONFIG
from butly_core.settings.sources import recursive_update


MEMORY_RETRIEVAL_FIELDS: dict[str, tuple[str, str]] = {
    "search_mode": ("brain", "search_mode"),
    "vector_search_limit": ("memory_probe", "vector_search_limit"),
    "evidence_fusion_base_weight": (
        "brain",
        "evidence_fusion_base_weight",
    ),
    "evidence_raw_chunk_chars": ("brain", "evidence_raw_chunk_chars"),
    "vector_candidates": ("brain", "vector_candidates"),
    "bm25_candidates": ("brain", "bm25_candidates"),
    "rag_source_mode": ("memory", "rag_source_mode"),
    "rag_raw_top_k": ("memory", "rag_raw_top_k"),
    "rag_raw_max_chars": ("memory", "rag_raw_max_chars"),
    "rag_raw_neighbor_radius": ("memory", "rag_raw_neighbor_radius"),
}

SEARCH_MODES = {"vector", "hybrid", "hybrid_evidence_fusion"}
RAG_SOURCE_MODES = {"cards", "raw", "both"}
_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "vector_search_limit": (1, 10),
    "evidence_raw_chunk_chars": (200, 10000),
    "vector_candidates": (3, 100),
    "bm25_candidates": (3, 100),
    "rag_raw_top_k": (0, 20),
    "rag_raw_max_chars": (0, 50000),
    "rag_raw_neighbor_radius": (0, 10),
}


class MemoryRetrievalSettingsError(ValueError):
    """Raised when persisted or proposed retrieval settings are invalid."""

    def __init__(self, message: str, *, issues: Optional[list[dict]] = None):
        super().__init__(message)
        self.issues = issues or []


def default_memory_retrieval_values() -> dict[str, Any]:
    """Return public defaults using ``defaults.py`` as the only source."""
    return {
        field: deepcopy(DEFAULT_SYSTEM_CONFIG[section][key])
        for field, (section, key) in MEMORY_RETRIEVAL_FIELDS.items()
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MemoryRetrievalSettingsError(
            f"Failed to read {path.name}."
        ) from exc
    if not isinstance(value, dict):
        raise MemoryRetrievalSettingsError(f"{path.name} root must be an object.")
    return value


def _extract_overrides(
    config: Mapping[str, Any],
    *,
    global_config: bool,
) -> dict[str, Any]:
    root = config.get("SYSTEM_CONFIG") if global_config else config
    if not isinstance(root, Mapping):
        return {}
    overrides: dict[str, Any] = {}
    for field, (section, key) in MEMORY_RETRIEVAL_FIELDS.items():
        section_data = root.get(section)
        if isinstance(section_data, Mapping) and key in section_data:
            overrides[field] = deepcopy(section_data[key])
    return overrides


def validate_memory_retrieval_values(values: Mapping[str, Any]) -> None:
    """Validate one fully resolved public settings snapshot."""
    issues: list[dict] = []
    search_mode = values.get("search_mode")
    if search_mode not in SEARCH_MODES:
        issues.append(
            {"field": "search_mode", "message": "unsupported search mode"}
        )
    source_mode = values.get("rag_source_mode")
    if source_mode not in RAG_SOURCE_MODES:
        issues.append(
            {"field": "rag_source_mode", "message": "unsupported source mode"}
        )

    for field, (minimum, maximum) in _INTEGER_RANGES.items():
        value = values.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            issues.append({"field": field, "message": "must be an integer"})
        elif not minimum <= value <= maximum:
            issues.append(
                {
                    "field": field,
                    "message": f"must be between {minimum} and {maximum}",
                }
            )

    weight = values.get("evidence_fusion_base_weight")
    if (
        isinstance(weight, bool)
        or not isinstance(weight, (int, float))
        or not math.isfinite(float(weight))
        or not 0.0 <= float(weight) <= 1.0
    ):
        issues.append(
            {
                "field": "evidence_fusion_base_weight",
                "message": "must be a finite number between 0 and 1",
            }
        )

    injection_limit = values.get("vector_search_limit")
    if isinstance(injection_limit, int) and not isinstance(injection_limit, bool):
        for field in ("vector_candidates", "bm25_candidates"):
            candidate_limit = values.get(field)
            if (
                isinstance(candidate_limit, int)
                and not isinstance(candidate_limit, bool)
                and candidate_limit < injection_limit
            ):
                issues.append(
                    {
                        "field": field,
                        "message": "must be at least vector_search_limit",
                    }
                )

    if issues:
        raise MemoryRetrievalSettingsError(
            "Memory retrieval settings are invalid.",
            issues=issues,
        )


def _resolved_global(data_dir: Path) -> dict[str, Any]:
    defaults = default_memory_retrieval_values()
    raw = _read_json_object(data_dir / "user_config.json")
    overrides = _extract_overrides(raw, global_config=True)
    effective = {**defaults, **overrides}
    validate_memory_retrieval_values(effective)
    origins = {
        field: "global" if field in overrides else "default"
        for field in MEMORY_RETRIEVAL_FIELDS
    }
    return {
        "defaults": defaults,
        "global_override": overrides,
        "effective": effective,
        "origins": origins,
    }


def resolve_global_memory_retrieval(data_dir: Path) -> dict[str, Any]:
    """Resolve defaults, explicit global overrides, effective values and origin."""
    return _resolved_global(Path(data_dir))


def resolve_instance_memory_retrieval(
    data_dir: Path,
    instance_dir: Path,
) -> dict[str, Any]:
    """Resolve one instance on top of the global effective settings."""
    global_state = _resolved_global(Path(data_dir))
    raw = _read_json_object(Path(instance_dir) / "config.json")
    overrides = _extract_overrides(raw, global_config=False)
    effective = {**global_state["effective"], **overrides}
    validate_memory_retrieval_values(effective)
    origins = {
        field: (
            "instance"
            if field in overrides
            else global_state["origins"][field]
        )
        for field in MEMORY_RETRIEVAL_FIELDS
    }
    return {
        "defaults": global_state["defaults"],
        "global_override": global_state["global_override"],
        "global_effective": global_state["effective"],
        "instance_override": overrides,
        "effective": effective,
        "origins": origins,
    }


def _set_nested_value(root: dict[str, Any], section: str, key: str, value: Any) -> None:
    current = root.get(section)
    if current is None:
        current = {}
        root[section] = current
    if not isinstance(current, dict):
        raise MemoryRetrievalSettingsError(
            f"The {section} settings section must be an object."
        )
    current[key] = value


def _remove_nested_value(root: dict[str, Any], section: str, key: str) -> None:
    current = root.get(section)
    if not isinstance(current, dict):
        return
    current.pop(key, None)
    if not current:
        root.pop(section, None)


def patch_global_memory_retrieval(
    data_dir: Path,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically apply an allowlisted partial update to ``user_config.json``."""
    unknown = set(updates) - set(MEMORY_RETRIEVAL_FIELDS)
    if unknown:
        raise MemoryRetrievalSettingsError(
            "Unknown memory retrieval setting.",
            issues=[{"field": key, "message": "unknown setting"} for key in sorted(unknown)],
        )
    if any(value is None for value in updates.values()):
        raise MemoryRetrievalSettingsError(
            "Global memory retrieval settings cannot be null."
        )

    data_dir = Path(data_dir)
    path = data_dir / "user_config.json"
    raw = _read_json_object(path)
    system = raw.get("SYSTEM_CONFIG")
    if system is None:
        system = {}
        raw["SYSTEM_CONFIG"] = system
    if not isinstance(system, dict):
        raise MemoryRetrievalSettingsError("SYSTEM_CONFIG must be an object.")

    current = _resolved_global(data_dir)["effective"]
    proposed = {**current, **deepcopy(dict(updates))}
    validate_memory_retrieval_values(proposed)
    for field, value in updates.items():
        section, key = MEMORY_RETRIEVAL_FIELDS[field]
        _set_nested_value(system, section, key, value)

    atomic_write_text(
        path,
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
    )
    _synchronize_runtime_settings(data_dir)
    return _resolved_global(data_dir)


def patch_instance_memory_retrieval(
    data_dir: Path,
    instance_dir: Path,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply values or ``None`` (inherit) without replacing unrelated config."""
    unknown = set(updates) - set(MEMORY_RETRIEVAL_FIELDS)
    if unknown:
        raise MemoryRetrievalSettingsError(
            "Unknown memory retrieval setting.",
            issues=[{"field": key, "message": "unknown setting"} for key in sorted(unknown)],
        )

    data_dir = Path(data_dir)
    instance_dir = Path(instance_dir)
    path = instance_dir / "config.json"
    raw = _read_json_object(path)
    global_effective = _resolved_global(data_dir)["effective"]
    current_overrides = _extract_overrides(raw, global_config=False)
    proposed_overrides = dict(current_overrides)
    for field, value in updates.items():
        if value is None:
            proposed_overrides.pop(field, None)
        else:
            proposed_overrides[field] = deepcopy(value)
    validate_memory_retrieval_values({**global_effective, **proposed_overrides})

    for field, value in updates.items():
        section, key = MEMORY_RETRIEVAL_FIELDS[field]
        if value is None:
            _remove_nested_value(raw, section, key)
        else:
            _set_nested_value(raw, section, key, value)

    atomic_write_text(
        path,
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
    )
    return resolve_instance_memory_retrieval(data_dir, instance_dir)


def runtime_memory_retrieval_snapshot(
    system_config: Mapping[str, Any],
    instance_config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Resolve a trace-safe snapshot from the live config dictionaries."""
    merged = deepcopy(dict(system_config))
    if instance_config:
        for section in {section for section, _key in MEMORY_RETRIEVAL_FIELDS.values()}:
            override = instance_config.get(section)
            if isinstance(override, dict):
                target = merged.setdefault(section, {})
                if isinstance(target, dict):
                    recursive_update(target, override)
    values: dict[str, Any] = {}
    for field, (section, key) in MEMORY_RETRIEVAL_FIELDS.items():
        section_data = merged.get(section)
        if isinstance(section_data, Mapping):
            values[field] = deepcopy(section_data.get(key))
    return values


def _synchronize_runtime_settings(data_dir: Path) -> None:
    from butly_core import config as legacy_config
    from butly_core.settings import clear_settings_cache, get_settings

    clear_settings_cache()
    settings = get_settings(data_dir / "user_config.json")
    legacy_config.SYSTEM_CONFIG.clear()
    legacy_config.SYSTEM_CONFIG.update(deepcopy(settings.SYSTEM_CONFIG))
