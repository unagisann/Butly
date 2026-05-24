"""Pydantic model for user-defined LLM connections."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class LLMConnection(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    protocol: str
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key_fallback_envs: tuple[str, ...] = Field(default_factory=tuple)
    label: Optional[str] = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    embeddings_supported: bool = True
    embedding_model_env: Optional[str] = None
    default_embedding_model: Optional[str] = None
    model_name_strip_prefix: Optional[str] = None
