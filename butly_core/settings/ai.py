"""Pydantic models for AI_CONFIG.

Phase 1 keeps role payloads as dictionaries so the legacy config dict stays
byte-for-byte compatible. Later phases can tighten these role models.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .defaults import AI_CONFIG as DEFAULT_AI_CONFIG


def _role_default(role: str) -> dict[str, Any]:
    return deepcopy(DEFAULT_AI_CONFIG[role])


class GenerationConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_output_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None


class SafetySetting(BaseModel):
    model_config = ConfigDict(extra="allow")

    category: str
    threshold: str


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    connection: Optional[str] = None
    model_name: Optional[str] = None
    generation_config: dict[str, Any] = Field(default_factory=dict)
    safety_settings: list[dict[str, Any]] = Field(default_factory=list)


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    chat: dict[str, Any] = Field(default_factory=lambda: _role_default("chat"))
    summary: dict[str, Any] = Field(default_factory=lambda: _role_default("summary"))
    knowledge: dict[str, Any] = Field(default_factory=lambda: _role_default("knowledge"))
    embedding: dict[str, Any] = Field(default_factory=lambda: _role_default("embedding"))
    gatekeeper: dict[str, Any] = Field(default_factory=lambda: _role_default("gatekeeper"))
    context_classifier: dict[str, Any] = Field(
        default_factory=lambda: _role_default("context_classifier")
    )
