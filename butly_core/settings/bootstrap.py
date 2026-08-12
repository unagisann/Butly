"""Apply one data directory's typed settings to legacy runtime globals."""

from copy import deepcopy
from pathlib import Path

from butly_core.settings.root import RootSettings, get_settings


def apply_runtime_settings(data_dir: Path) -> RootSettings:
    """Make ChatService and ConnectionRegistry use ``data_dir/user_config.json``.

    The typed settings layer is the source. Legacy dictionaries are mutated in
    place because some existing modules retain references imported earlier.
    """
    settings = get_settings(data_dir / "user_config.json")

    from butly_core import config as legacy_config
    from butly_core.llm.connections import Connection, get_registry

    legacy_config.AI_CONFIG.clear()
    legacy_config.AI_CONFIG.update(deepcopy(settings.AI_CONFIG))
    legacy_config.SYSTEM_CONFIG.clear()
    legacy_config.SYSTEM_CONFIG.update(deepcopy(settings.SYSTEM_CONFIG))

    registry = get_registry()
    registry.reset_to_builtin()
    for item in settings.llm_connections:
        payload = item.model_dump(mode="python")
        connection = Connection(
            id=payload["id"],
            protocol=payload["protocol"],
            base_url=payload.get("base_url"),
            base_url_env=payload.get("base_url_env"),
            api_key_env=payload.get("api_key_env"),
            api_key_fallback_envs=tuple(
                payload.get("api_key_fallback_envs") or ()
            ),
            label=payload.get("label"),
            extra_headers=dict(payload.get("extra_headers") or {}),
            embeddings_supported=payload.get("embeddings_supported", True),
            embedding_model_env=payload.get("embedding_model_env"),
            default_embedding_model=payload.get("default_embedding_model"),
            model_name_strip_prefix=payload.get("model_name_strip_prefix"),
        )
        registry.register(connection, overwrite_user=True)
    return settings
