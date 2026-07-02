"""Pydantic model for SYSTEM_CONFIG.

Phase 1 keeps section payloads as dictionaries to preserve legacy dict output.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .defaults import SYSTEM_CONFIG as DEFAULT_SYSTEM_CONFIG


def _section_default(section: str) -> dict[str, Any]:
    return deepcopy(DEFAULT_SYSTEM_CONFIG[section])


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent: dict[str, Any] = Field(default_factory=lambda: _section_default("agent"))
    paths: dict[str, Any] = Field(default_factory=lambda: _section_default("paths"))
    memory: dict[str, Any] = Field(default_factory=lambda: _section_default("memory"))
    brain: dict[str, Any] = Field(default_factory=lambda: _section_default("brain"))
    backup: dict[str, Any] = Field(default_factory=lambda: _section_default("backup"))
    search: dict[str, Any] = Field(default_factory=lambda: _section_default("search"))
    memory_probe: dict[str, Any] = Field(
        default_factory=lambda: _section_default("memory_probe")
    )
    gatekeeper: dict[str, Any] = Field(
        default_factory=lambda: _section_default("gatekeeper")
    )
    chat: dict[str, Any] = Field(default_factory=lambda: _section_default("chat"))
    glossary: dict[str, Any] = Field(default_factory=lambda: _section_default("glossary"))
    trace: dict[str, Any] = Field(default_factory=lambda: _section_default("trace"))
