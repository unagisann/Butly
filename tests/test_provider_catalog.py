"""OpenAI-compatible provider template catalog tests."""

from urllib.parse import urlparse

from butly_core.llm.provider_catalog import (
    get_provider_template,
    list_provider_templates,
)


def test_catalog_ids_are_unique_and_templates_are_well_formed():
    templates = list_provider_templates()
    ids = [template.id for template in templates]

    assert len(ids) == len(set(ids))
    assert {"nanogpt", "nanogpt-sub"}.issubset(ids)
    for template in templates:
        parsed_url = urlparse(template.base_url)
        assert parsed_url.scheme == "https"
        assert parsed_url.netloc
        assert template.protocol == "openai_compat"
        assert template.api_key_env


def test_nanogpt_subscription_template_uses_subscription_endpoint():
    template = get_provider_template("nanogpt-sub")

    assert template is not None
    assert template.label == "NanoGPT Pro (Subscription)"
    assert template.base_url == "https://nano-gpt.com/api/subscription/v1"
    assert template.api_key_env == "NANOGPT_API_KEY"
    assert template.embeddings_supported is False
    assert template.extra_headers == {}
    assert template.notes is not None
    assert "provider-selection headers" in template.notes


def test_nanogpt_payg_template_is_separate_and_supports_embeddings():
    template = get_provider_template("nanogpt")

    assert template is not None
    assert template.label == "NanoGPT (Pay-as-you-go)"
    assert template.base_url == "https://nano-gpt.com/api/v1"
    assert template.api_key_env == "NANOGPT_API_KEY"
    assert template.embeddings_supported is True
    assert template.extra_headers == {}


def test_unknown_provider_template_returns_none():
    assert get_provider_template("missing-provider") is None


def test_list_result_and_serialized_template_are_independent():
    templates = list_provider_templates()
    original_count = len(templates)
    templates.pop()

    assert len(list_provider_templates()) == original_count

    template = get_provider_template("nanogpt-sub")
    assert template is not None
    payload = template.to_dict()
    payload["extra_headers"]["unexpected"] = "value"

    assert template.extra_headers == {}


def test_nanogpt_subscription_uses_generic_openai_compatible_adapter():
    from butly_core.llm.connections import (
        Connection,
        get_registry,
        register_connection,
    )
    from butly_core.llm.factory import ProviderFactory
    from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter

    template = get_provider_template("nanogpt-sub")
    assert template is not None
    connection = Connection(
        id=template.id,
        protocol=template.protocol,
        base_url=template.base_url,
        api_key_env=template.api_key_env,
        embeddings_supported=template.embeddings_supported,
        extra_headers=template.extra_headers,
        label=template.label,
    )

    registry = get_registry()
    registry.reset_to_builtin()
    try:
        register_connection(connection)
        provider = ProviderFactory.create({
            "connection": "nanogpt-sub",
            "model_name": "Qwen/Qwen3-14B",
        })
        assert isinstance(provider, OpenAICompatAdapter)
    finally:
        registry.reset_to_builtin()
