"""
tests/test_settings_phase1.py
-----------------------------
Phase 1 pydantic-settings compatibility tests.
"""

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clear_settings_cache_between_tests():
    from butly_core.settings import clear_settings_cache

    clear_settings_cache()
    yield
    clear_settings_cache()


def test_legacy_config_globals_match_get_settings():
    import butly_core.config as cfg_mod
    from butly_core.settings import get_settings

    settings = get_settings(cfg_mod.USER_CONFIG_PATH)

    assert settings.AI_CONFIG == cfg_mod.AI_CONFIG
    assert settings.SYSTEM_CONFIG == cfg_mod.SYSTEM_CONFIG


def test_user_config_deep_merge_preserves_nested_defaults(tmp_path: Path):
    cfg_path = tmp_path / "user_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "AI_CONFIG": {
                    "chat": {
                        "generation_config": {
                            "temperature": 0.12,
                        }
                    }
                },
                "SYSTEM_CONFIG": {
                    "memory": {
                        "short_term_limit": 9,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    from butly_core.settings import get_settings

    settings = get_settings(cfg_path)
    chat = settings.AI_CONFIG["chat"]

    assert chat["generation_config"]["temperature"] == 0.12
    assert chat["generation_config"]["top_p"] == 0.95
    assert chat["generation_config"]["max_output_tokens"] == 8192
    assert settings.SYSTEM_CONFIG["memory"]["short_term_limit"] == 9
    assert settings.SYSTEM_CONFIG["memory"]["max_raw_tokens"] == 4096


def test_settings_normalizes_model_name_connection_mismatch(tmp_path: Path):
    cfg_path = tmp_path / "user_config.json"
    cfg_path.write_text(
        json.dumps({"AI_CONFIG": {"chat": {"model_name": "gpt-4o"}}}),
        encoding="utf-8",
    )

    from butly_core.settings import get_settings

    settings = get_settings(cfg_path)

    assert settings.AI_CONFIG["chat"]["model_name"] == "gpt-4o"
    assert settings.AI_CONFIG["chat"]["connection"] == "openai"


def test_settings_loads_llm_capability_manual_overrides(tmp_path: Path):
    cfg_path = tmp_path / "user_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "LLM_CAPABILITY_OVERRIDES": {
                    "gateway": {
                        "reasoner": {
                            "token_limit_parameter": (
                                "max_completion_tokens"
                            ),
                            "supports_reasoning": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    from butly_core.settings import get_settings

    settings = get_settings(cfg_path)

    assert settings.LLM_CAPABILITY_OVERRIDES == {
        "gateway": {
            "reasoner": {
                "token_limit_parameter": "max_completion_tokens",
                "supports_reasoning": True,
            }
        }
    }


def test_get_settings_cache_clear_reloads_file(tmp_path: Path):
    cfg_path = tmp_path / "user_config.json"
    cfg_path.write_text(
        json.dumps({"SYSTEM_CONFIG": {"memory": {"short_term_limit": 5}}}),
        encoding="utf-8",
    )

    from butly_core.settings import clear_settings_cache, get_settings

    first = get_settings(cfg_path)
    cfg_path.write_text(
        json.dumps({"SYSTEM_CONFIG": {"memory": {"short_term_limit": 11}}}),
        encoding="utf-8",
    )

    assert get_settings(cfg_path).SYSTEM_CONFIG["memory"]["short_term_limit"] == 5

    clear_settings_cache()
    assert get_settings(cfg_path).SYSTEM_CONFIG["memory"]["short_term_limit"] == 11
    assert get_settings.cache_clear is clear_settings_cache
    assert first is not get_settings(cfg_path)


def test_override_settings_context_manager(tmp_path: Path):
    cfg_path = tmp_path / "user_config.json"
    cfg_path.write_text("{}", encoding="utf-8")

    from butly_core.settings import RootSettings, get_settings, override_settings

    original = get_settings(cfg_path)
    override = RootSettings(
        AI_CONFIG={"chat": {"connection": "openai", "model_name": "gpt-4o"}},
        SYSTEM_CONFIG={"agent": {"agent_name": "Testly"}},
    )

    with override_settings(override):
        assert get_settings() is override
        assert get_settings(cfg_path).AI_CONFIG["chat"]["model_name"] == "gpt-4o"

    assert get_settings(cfg_path) is original
