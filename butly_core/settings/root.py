"""Root settings object and test hooks."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .ai import AIConfig
from .connections import LLMConnection
from .sources import load_settings_data
from .system import SystemConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_USER_CONFIG_PATH = PROJECT_ROOT / "user_config.json"

_SETTINGS_OVERRIDE: Optional["RootSettings"] = None


def _default_ai() -> AIConfig:
    return AIConfig()


def _default_system() -> SystemConfig:
    return SystemConfig()


class RootSettings(BaseSettings):
    """Butly-wide settings.

    Phase 1 intentionally preserves the legacy dict shape while introducing a
    cached settings object and typed import surface.
    """

    model_config = SettingsConfigDict(
        env_prefix="BUTLY_",
        env_nested_delimiter="__",
        env_file=(".env", "APIkey.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    ai: AIConfig = Field(default_factory=_default_ai, alias="AI_CONFIG")
    system: SystemConfig = Field(
        default_factory=_default_system, alias="SYSTEM_CONFIG"
    )
    llm_connections: list[LLMConnection] = Field(
        default_factory=list, alias="LLM_CONNECTIONS"
    )
    llm_capability_overrides: dict[str, dict[str, dict[str, Any]]] = Field(
        default_factory=dict,
        alias="LLM_CAPABILITY_OVERRIDES",
    )

    @property
    def AI_CONFIG(self) -> dict:
        return deepcopy(self.ai.model_dump(mode="python"))

    @property
    def SYSTEM_CONFIG(self) -> dict:
        return deepcopy(self.system.model_dump(mode="python"))

    @property
    def LLM_CONNECTIONS(self) -> list[dict]:
        return [
            conn.model_dump(mode="python", exclude_none=True)
            for conn in self.llm_connections
        ]

    @property
    def LLM_CAPABILITY_OVERRIDES(self) -> dict[str, dict[str, dict[str, Any]]]:
        return deepcopy(self.llm_capability_overrides)


@lru_cache(maxsize=None)
def _get_settings_cached(config_path_str: str) -> RootSettings:
    data = load_settings_data(Path(config_path_str))
    return RootSettings(**data)


def get_settings(config_path: Optional[Union[Path, str]] = None) -> RootSettings:
    """Return cached settings, optionally loaded from a specific user_config."""

    if _SETTINGS_OVERRIDE is not None:
        return _SETTINGS_OVERRIDE

    path = Path(config_path) if config_path is not None else DEFAULT_USER_CONFIG_PATH
    return _get_settings_cached(str(path))


def clear_settings_cache() -> None:
    """Official test hook for reloading settings after file/env changes."""

    _get_settings_cached.cache_clear()


@contextmanager
def override_settings(settings: RootSettings) -> Iterator[RootSettings]:
    """Temporarily force get_settings() to return a provided settings object."""

    global _SETTINGS_OVERRIDE
    previous = _SETTINGS_OVERRIDE
    _SETTINGS_OVERRIDE = settings
    try:
        yield settings
    finally:
        _SETTINGS_OVERRIDE = previous


# Keep the familiar cache_clear affordance on get_settings for tests.
get_settings.cache_clear = clear_settings_cache  # type: ignore[attr-defined]
