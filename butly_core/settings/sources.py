"""Settings loading helpers for the Phase 1 compatibility shim."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .defaults import AI_CONFIG as DEFAULT_AI_CONFIG
from .defaults import SYSTEM_CONFIG as DEFAULT_SYSTEM_CONFIG


def recursive_update(base_dict: dict[str, Any], update_dict: dict[str, Any]) -> None:
    for key, value in update_dict.items():
        if (
            isinstance(value, dict)
            and key in base_dict
            and isinstance(base_dict[key], dict)
        ):
            recursive_update(base_dict[key], value)
        else:
            base_dict[key] = value


def normalize_ai_config(ai_config: dict[str, Any]) -> None:
    from butly_core.llm.connections import is_builtin_connection
    from butly_core.llm.model_registry import infer_connection_id

    for role, cfg in ai_config.items():
        if not isinstance(cfg, dict):
            continue
        model_name = cfg.get("model_name")
        if not model_name:
            continue

        existing = cfg.get("connection")
        inferred = infer_connection_id(model_name)

        if not existing:
            if inferred:
                cfg["connection"] = inferred
            else:
                print(
                    f"[Config] role={role!r} model_name={model_name!r} の "
                    f"connection を推定できませんでした。明示的に "
                    f'"connection" を指定してください。'
                )
            continue

        if inferred and inferred != existing and is_builtin_connection(existing):
            print(
                f"[Config] role={role!r} connection={existing!r} と "
                f"model_name={model_name!r} の整合が取れません。"
                f"推定 connection {inferred!r} で置き換えます "
                f"(明示が必要なら user_config.json で connection を指定してください)。"
            )
            cfg["connection"] = inferred


def load_settings_data(config_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "AI_CONFIG": deepcopy(DEFAULT_AI_CONFIG),
        "SYSTEM_CONFIG": deepcopy(DEFAULT_SYSTEM_CONFIG),
        "LLM_CONNECTIONS": [],
        "LLM_CAPABILITY_OVERRIDES": {},
    }

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        normalize_ai_config(data["AI_CONFIG"])
        return data
    except json.JSONDecodeError as exc:
        print(f"[Config] Failed to load user_config.json: {exc}")
        normalize_ai_config(data["AI_CONFIG"])
        return data

    if not isinstance(raw, dict):
        print(
            f"[Config] user_config.json root must be object "
            f"(got {type(raw).__name__})"
        )
        normalize_ai_config(data["AI_CONFIG"])
        return data

    if isinstance(raw.get("AI_CONFIG"), dict):
        recursive_update(data["AI_CONFIG"], raw["AI_CONFIG"])
    if isinstance(raw.get("SYSTEM_CONFIG"), dict):
        recursive_update(data["SYSTEM_CONFIG"], raw["SYSTEM_CONFIG"])
    if "LLM_CONNECTIONS" in raw:
        data["LLM_CONNECTIONS"] = raw["LLM_CONNECTIONS"]
    if isinstance(raw.get("LLM_CAPABILITY_OVERRIDES"), dict):
        data["LLM_CAPABILITY_OVERRIDES"] = raw[
            "LLM_CAPABILITY_OVERRIDES"
        ]

    normalize_ai_config(data["AI_CONFIG"])
    return data
