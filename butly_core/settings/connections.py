"""Pydantic model for user-defined LLM connections."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from butly_core.llm.connections import (
    validate_base_url,
    validate_connection_id,
    validate_env_name,
    validate_extra_headers,
)


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

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_connection_id(value)

    @field_validator(
        "base_url_env",
        "api_key_env",
        "embedding_model_env",
    )
    @classmethod
    def validate_optional_env_name(cls, value: Optional[str]) -> Optional[str]:
        return validate_env_name(value)

    @field_validator("api_key_fallback_envs")
    @classmethod
    def validate_fallback_env_names(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        for value in values:
            validate_env_name(value)
        return values

    @field_validator("base_url")
    @classmethod
    def validate_optional_base_url(cls, value: Optional[str]) -> Optional[str]:
        return validate_base_url(value)

    @field_validator("extra_headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_extra_headers(value)
