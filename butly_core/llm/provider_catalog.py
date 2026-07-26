"""Templates for user-defined OpenAI-compatible Connections."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ProviderTemplate:
    id: str
    label: str
    base_url: str
    api_key_env: str
    protocol: str = "openai_compat"
    embeddings_supported: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PROVIDER_TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        id="nanogpt-sub",
        label="NanoGPT Pro (Subscription)",
        base_url="https://nano-gpt.com/api/subscription/v1",
        api_key_env="NANOGPT_API_KEY",
        embeddings_supported=False,
        notes=(
            "Subscription-only endpoint. Do not add X-Provider, pay-as-you-go "
            "billing overrides, provider-selection headers, or "
            "provider-routing model suffixes."
        ),
    ),
    ProviderTemplate(
        id="nanogpt",
        label="NanoGPT (Pay-as-you-go)",
        base_url="https://nano-gpt.com/api/v1",
        api_key_env="NANOGPT_API_KEY",
        embeddings_supported=True,
    ),
    ProviderTemplate(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
    ),
    ProviderTemplate(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    ),
    ProviderTemplate(
        id="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        api_key_env="TOGETHER_API_KEY",
        embeddings_supported=True,
    ),
    ProviderTemplate(
        id="deepinfra",
        label="DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
        api_key_env="DEEPINFRA_API_KEY",
        embeddings_supported=True,
    ),
)


def list_provider_templates() -> list[ProviderTemplate]:
    return list(_PROVIDER_TEMPLATES)


def get_provider_template(template_id: str) -> Optional[ProviderTemplate]:
    return next(
        (template for template in _PROVIDER_TEMPLATES if template.id == template_id),
        None,
    )
