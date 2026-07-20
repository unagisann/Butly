"""Per-instance locale propagation and evaluation prompt isolation."""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from butly_core.core.brain import ButlyBrain
from butly_core.core.chronos import ButlyChronos
from butly_core.core.gatekeeper import Gatekeeper, MemoryBlockBuilder
from butly_core.core.gatekeeper.context_classifier import ContextClassifier
from butly_core.core.gatekeeper.state_updater import StateUpdater
from butly_core.core.memory import _format_relative_time
from butly_core.core.raw_memory_reader import format_sessions
from butly_core.prompts import (
    PromptLoader,
    resolve_prompt_locale,
    user_prompt_overrides_enabled,
)
from sleeptime import ButlySleeptime


def test_prompt_settings_resolve_from_instance_profile():
    config = {
        "agent_profile": {"locale": "en"},
        "prompts": {"allow_user_overrides": False},
    }

    assert resolve_prompt_locale(config) == "en"
    assert user_prompt_overrides_enabled(config) is False


def test_prompt_loader_can_ignore_project_user_overrides(tmp_path, monkeypatch):
    from butly_core import prompts

    override_path = tmp_path / "user_prompts.json"
    override_path.write_text(
        json.dumps({"SLEEPTIME_SUMMARIZE_PROMPT": "LOCAL OVERRIDE"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompts, "_USER_PROMPTS_PATH", override_path)

    enabled = PromptLoader(locale="en")
    disabled = PromptLoader(locale="en", allow_user_overrides=False)

    assert enabled.get_template("sleeptime_summarize") == "LOCAL OVERRIDE"
    assert disabled.get_template("sleeptime_summarize") != "LOCAL OVERRIDE"


def test_sleeptime_loader_uses_instance_locale_and_override_policy(tmp_path):
    instances_dir = tmp_path / "instances"
    instance_dir = instances_dir / "eval_instance"
    instance_dir.mkdir(parents=True)
    (instance_dir / "config.json").write_text(
        json.dumps(
            {
                "agent_profile": {"locale": "en"},
                "prompts": {"allow_user_overrides": False},
            }
        ),
        encoding="utf-8",
    )
    sleeptime = ButlySleeptime(
        base_dir=tmp_path / "workspace",
        instances_dir=instances_dir,
    )

    loader = sleeptime.get_instance_prompt_loader("eval_instance")

    assert loader.locale == "en"
    assert loader.allow_user_overrides is False


def test_memory_builder_renders_english_rag_from_instance_config(
    memory_manager,
    mock_brain,
):
    output = {
        "need": "past_fact",
        "memory_probe": {
            "candidates": [
                {
                    "id": "card-1",
                    "title": "Camping plan",
                    "summary": "The trip is scheduled for June.",
                    "source_date": "2023-05-08",
                }
            ],
            "glossary_hits": [],
        },
    }

    blocks = MemoryBlockBuilder().build(
        tier="reflex",
        memory_manager=memory_manager,
        brain=mock_brain,
        user_input="When is the camping trip?",
        override_config={
            "agent_profile": {"locale": "en"},
            "prompts": {"allow_user_overrides": False},
        },
        gatekeeper_output=output,
    )

    assert blocks["locale"] == "en"
    assert blocks["allow_user_prompt_overrides"] is False
    assert blocks["rag_context"].startswith("[Past Memories (RAG)]")
    assert "\n- [2023-05-08] Camping plan:" in blocks["rag_context"]
    assert "【" not in blocks["rag_context"]


def test_brain_passes_locale_name_and_prompt_policy_to_summary_provider(
    tmp_path,
    monkeypatch,
):
    provider = MagicMock()
    provider.summarize.return_value = "summary"
    brain = ButlyBrain(tmp_path)
    monkeypatch.setattr(brain, "_get_provider", lambda config: provider)

    result = brain.summarize_conversation(
        "User: hello",
        override_config={
            "agent_profile": {"ai_name": "Melanie", "locale": "en"},
            "prompts": {"allow_user_overrides": False},
        },
    )

    assert result == "summary"
    summary_config = provider.summarize.call_args.args[1]
    assert summary_config["locale"] == "en"
    assert summary_config["agent_name"] == "Melanie"
    assert summary_config["allow_user_prompt_overrides"] is False


def test_english_time_and_gatekeeper_labels(monkeypatch):
    monkeypatch.setenv("BUTLY_CHRONOS_NOW", "2024-05-21T18:45:00")

    assert "(Tue)" in ButlyChronos(locale="en").get_system_note()
    now = datetime(2026, 5, 17, 22, 0, 0)
    assert (
        _format_relative_time(now - timedelta(minutes=30), now, locale="en")
        == "about 30 minutes ago"
    )

    history = [{"role": "user", "parts": ["When is the trip?"]}]
    assert ContextClassifier()._format_history(history, locale="en").startswith(
        "User:"
    )
    assert StateUpdater()._format_history([], locale="en") == "(no history)"


def test_english_raw_memory_uses_plain_speaker_labels():
    sessions = [
        {
            "timestamp": "2023-05-08 09:00:00",
            "messages": [
                {
                    "role": "user",
                    "parts": ["Hello"],
                    "meta": {"person_id": "p_1", "display_name": "Caroline"},
                },
                {
                    "role": "user",
                    "parts": ["Hi"],
                    "meta": {"person_id": "p_2", "display_name": "Alex"},
                },
            ],
        }
    ]

    rendered = format_sessions(sessions, user_name="User", locale="en")

    assert "Caroline: Hello" in rendered
    assert "「" not in rendered


def test_gatekeeper_resolves_current_agent_profile_name(tmp_path):
    instance_dir = tmp_path / "eval_instance"
    instance_dir.mkdir()
    (instance_dir / "config.json").write_text(
        json.dumps({"agent_profile": {"ai_name": "Melanie"}}),
        encoding="utf-8",
    )

    gatekeeper = Gatekeeper(tmp_path)

    assert gatekeeper._resolve_agent_name(instance_dir) == "Melanie"
    assert (
        gatekeeper._resolve_agent_name(
            instance_dir,
            {"agent_profile": {"ai_name": "Override"}},
        )
        == "Override"
    )
