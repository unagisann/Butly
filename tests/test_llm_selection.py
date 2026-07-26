"""Pure model/Connection selection helper tests."""

from dataclasses import FrozenInstanceError

import pytest

from butly_core.llm.selection import (
    ModelChoice,
    candidate_key,
    ensure_current_in_candidates,
    find_current_index,
    normalize_candidates,
    set_model_choice,
)


class TestModelChoice:
    def test_is_immutable(self):
        choice = ModelChoice(connection_id="openai", model_name="gpt-4o")

        with pytest.raises(FrozenInstanceError):
            choice.model_name = "changed"  # type: ignore[misc]


class TestNormalizeCandidates:
    def test_normalizes_legacy_strings_and_structured_candidates(self):
        candidates = normalize_candidates(
            [
                " gpt-4o ",
                {
                    "connection_id": "google",
                    "model_name": " gemini-3.5-flash ",
                    "label": "Gemini Flash",
                    "source": "api",
                },
            ]
        )

        assert candidates == [
            {
                "connection_id": None,
                "model_name": "gpt-4o",
                "label": "gpt-4o",
            },
            {
                "connection_id": "google",
                "model_name": "gemini-3.5-flash",
                "label": "Gemini Flash",
                "source": "api",
            },
        ]

    def test_deduplicates_by_connection_and_model_pair(self):
        candidates = normalize_candidates(
            [
                {"connection_id": "openai", "model_name": "shared-model"},
                {"connection_id": "openai", "model_name": "shared-model"},
                {"connection_id": "nanogpt", "model_name": "shared-model"},
            ]
        )

        assert [
            candidate_key(candidate) for candidate in candidates
        ] == [
            ("openai", "shared-model"),
            ("nanogpt", "shared-model"),
        ]

    def test_ignores_empty_and_unsupported_values(self):
        candidates = normalize_candidates(
            ["", "   ", None, 123, {}, {"model_name": None}]
        )

        assert candidates == []


class TestCurrentCandidate:
    def test_adds_saved_pair_even_when_model_name_exists_elsewhere(self):
        candidates = [
            {
                "connection_id": "openai",
                "model_name": "shared-model",
                "label": "OpenAI",
            }
        ]

        result = ensure_current_in_candidates(
            candidates,
            ModelChoice("nanogpt", "shared-model"),
        )

        assert result[-1] == {
            "connection_id": "nanogpt",
            "model_name": "shared-model",
            "label": "shared-model",
            "source": "saved",
        }
        assert len(result) == 2
        assert candidates == [
            {
                "connection_id": "openai",
                "model_name": "shared-model",
                "label": "OpenAI",
            }
        ]

    def test_does_not_duplicate_exact_saved_pair(self):
        candidates = [
            {"connection_id": "nanogpt", "model_name": "qwen3-14b"}
        ]

        result = ensure_current_in_candidates(
            candidates,
            ModelChoice("nanogpt", "qwen3-14b"),
        )

        assert result == candidates
        assert result is not candidates

    def test_empty_current_model_does_not_add_candidate(self):
        candidates = [{"connection_id": "openai", "model_name": "gpt-4o"}]

        result = ensure_current_in_candidates(
            candidates,
            ModelChoice("openai", ""),
        )

        assert result == candidates

    def test_find_index_prefers_exact_connection_pair(self):
        candidates = [
            {"connection_id": "openai", "model_name": "shared-model"},
            {"connection_id": "nanogpt", "model_name": "shared-model"},
        ]

        assert (
            find_current_index(
                candidates,
                ModelChoice("nanogpt", "shared-model"),
            )
            == 1
        )

    def test_find_index_falls_back_to_legacy_model_name(self):
        candidates = [
            {"connection_id": "openai", "model_name": "gpt-4o"},
            {"connection_id": "google", "model_name": "gemini-3.5-flash"},
        ]

        assert (
            find_current_index(
                candidates,
                ModelChoice(None, "gemini-3.5-flash"),
            )
            == 1
        )

    def test_find_index_defaults_to_first_candidate(self):
        candidates = [
            {"connection_id": "openai", "model_name": "gpt-4o"}
        ]

        assert (
            find_current_index(
                candidates,
                ModelChoice("nanogpt", "missing"),
            )
            == 0
        )


class TestSetModelChoice:
    def test_persists_explicit_connection(self):
        target = {"temperature": 0.3}

        set_model_choice(target, ModelChoice("nanogpt", "qwen3-14b"))

        assert target == {
            "temperature": 0.3,
            "connection": "nanogpt",
            "model_name": "qwen3-14b",
        }

    def test_infers_builtin_connection_for_legacy_choice(self):
        target = {"connection": "stale"}

        set_model_choice(target, ModelChoice(None, "gemini-3.5-flash"))

        assert target == {
            "connection": "google",
            "model_name": "gemini-3.5-flash",
        }

    def test_removes_stale_connection_when_model_cannot_be_inferred(self):
        target = {"connection": "stale", "temperature": 0.2}

        set_model_choice(target, ModelChoice(None, "custom-model"))

        assert target == {
            "model_name": "custom-model",
            "temperature": 0.2,
        }
