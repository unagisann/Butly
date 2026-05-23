"""
tests/test_model_registry.py
----------------------------
ModelRef / ModelPreset / 推定ヘルパーの単体テスト。
"""

import pytest

from butly_core.llm.model_registry import (
    MODEL_PRESETS,
    ModelRef,
    find_preset,
    get_presets_for_role,
    get_replacement,
    infer_connection_id,
    is_deprecated,
    normalize_model_ref,
    resolve_role_model_ref,
)


# ===================================================================
# infer_connection_id
# ===================================================================

class TestInferConnectionId:
    def test_gemini(self):
        assert infer_connection_id("gemini-3.5-flash") == "google"
        assert infer_connection_id("gemini-2.5-pro") == "google"

    def test_gemini_with_models_prefix(self):
        assert infer_connection_id("models/gemini-2.5-flash") == "google"

    def test_gemini_embedding(self):
        assert infer_connection_id("gemini-embedding-2") == "google"

    def test_gpt(self):
        assert infer_connection_id("gpt-4o") == "openai"
        assert infer_connection_id("gpt-4o-mini") == "openai"
        assert infer_connection_id("gpt-5.4-mini") == "openai"  # 将来のモデル

    def test_o_series(self):
        assert infer_connection_id("o1") == "openai"
        assert infer_connection_id("o3-mini") == "openai"
        assert infer_connection_id("o4-mini") == "openai"

    def test_text_embedding(self):
        assert infer_connection_id("text-embedding-3-small") == "openai"
        assert infer_connection_id("text-embedding-3-large") == "openai"

    def test_grok(self):
        assert infer_connection_id("grok-4.3") == "xai"
        assert infer_connection_id("grok-4-1-fast-reasoning") == "xai"

    def test_xai_prefix(self):
        assert infer_connection_id("xai/grok-4.3") == "xai"

    def test_ollama(self):
        assert infer_connection_id("ollama/llama3.2") == "ollama"
        assert infer_connection_id("ollama/llava:13b") == "ollama"

    def test_unknown(self):
        assert infer_connection_id("unknown-model") is None
        assert infer_connection_id("") is None
        assert infer_connection_id(None) is None


# ===================================================================
# normalize_model_ref
# ===================================================================

class TestNormalizeModelRef:
    def test_string_legacy(self):
        ref = normalize_model_ref("gpt-4o")
        assert ref.connection_id == "openai"
        assert ref.model_name == "gpt-4o"

    def test_dict_new_format(self):
        ref = normalize_model_ref({"connection": "openai", "model_name": "gpt-4o"})
        assert ref.connection_id == "openai"
        assert ref.model_name == "gpt-4o"

    def test_dict_legacy_format(self):
        # connection 未指定 → 推定
        ref = normalize_model_ref({"model_name": "gpt-4o"})
        assert ref.connection_id == "openai"

    def test_modelref_passthrough(self):
        original = ModelRef(connection_id="xai", model_name="grok-4.3")
        ref = normalize_model_ref(original)
        assert ref is original

    def test_connection_explicit_overrides_inference(self):
        # 明示が優先 (Groq でも custom_llm でも、推定不可な model を使うシナリオ)
        ref = normalize_model_ref({"connection": "groq", "model_name": "llama-3-70b"})
        assert ref.connection_id == "groq"

    def test_default_connection_used_when_cannot_infer(self):
        ref = normalize_model_ref("custom-model-name", default_connection_id="openai")
        assert ref.connection_id == "openai"
        assert ref.model_name == "custom-model-name"

    def test_empty_model_name_raises(self):
        with pytest.raises(ValueError, match="empty"):
            normalize_model_ref("")
        with pytest.raises(ValueError, match="empty"):
            normalize_model_ref({"model_name": ""})

    def test_unknown_with_no_default_raises(self):
        with pytest.raises(ValueError, match="Cannot infer"):
            normalize_model_ref("unknown-xyz")

    def test_to_dict(self):
        ref = ModelRef(connection_id="openai", model_name="gpt-4o")
        assert ref.to_dict() == {"connection": "openai", "model_name": "gpt-4o"}


# ===================================================================
# resolve_role_model_ref
# ===================================================================

class TestResolveRoleModelRef:
    def test_legacy_role_config(self):
        ref = resolve_role_model_ref({"model_name": "gpt-4o"})
        assert ref.connection_id == "openai"

    def test_new_role_config(self):
        ref = resolve_role_model_ref({"connection": "google", "model_name": "gemini-3.5-flash"})
        assert ref.connection_id == "google"

    def test_fallback_model_name(self):
        ref = resolve_role_model_ref({}, fallback_model_name="gpt-4o")
        assert ref.connection_id == "openai"
        assert ref.model_name == "gpt-4o"

    def test_no_model_name_raises(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_role_model_ref({})

    def test_fallback_connection_used(self):
        # explicit connection wins
        ref = resolve_role_model_ref(
            {"connection": "openai", "model_name": "custom-x"},
            fallback_connection_id="xai",
        )
        assert ref.connection_id == "openai"


# ===================================================================
# Preset lookup
# ===================================================================

class TestPresetLookup:
    def test_chat_presets_exclude_deprecated_by_default(self):
        chats = get_presets_for_role("chat")
        for p in chats:
            assert p.deprecated is False

    def test_chat_presets_include_deprecated_when_flagged(self):
        chats = get_presets_for_role("chat", include_deprecated=True)
        deprecated_ids = [p.model_name for p in chats if p.deprecated]
        assert "grok-4-1-fast-reasoning" in deprecated_ids

    def test_embedding_role_excludes_chat_only(self):
        embeddings = get_presets_for_role("embedding")
        for p in embeddings:
            assert "embedding" in p.capabilities
        names = [p.model_name for p in embeddings]
        assert "gpt-4o-mini" not in names  # chat-only
        assert "grok-4.20-0309-reasoning" not in names

    def test_embedding_presets_have_correct_capabilities(self):
        embeddings = get_presets_for_role("embedding")
        assert any(p.model_name == "gemini-embedding-2" for p in embeddings)
        assert any(p.model_name == "text-embedding-3-small" for p in embeddings)

    def test_find_preset(self):
        p = find_preset("google", "gemini-3.5-flash")
        assert p is not None
        assert p.stable is True
        assert "chat" in p.roles

    def test_find_preset_missing(self):
        assert find_preset("google", "nonexistent-model") is None

    def test_is_deprecated(self):
        assert is_deprecated("xai", "grok-4-1-fast-reasoning") is True
        assert is_deprecated("openai", "gpt-4o") is False
        assert is_deprecated("openai", "nonexistent") is False

    def test_get_replacement(self):
        rep = get_replacement("xai", "grok-4-1-fast-reasoning")
        assert rep == "grok-4.20-0309-reasoning"
        assert get_replacement("openai", "gpt-4o") is None

    def test_stable_first_order(self):
        chats = get_presets_for_role("chat")
        # stable のもののあと preview が来る
        stable_idx = [i for i, p in enumerate(chats) if p.stable]
        preview_idx = [i for i, p in enumerate(chats) if p.preview]
        if stable_idx and preview_idx:
            assert max(stable_idx) < min(preview_idx)


# ===================================================================
# MODEL_PRESETS sanity
# ===================================================================

class TestModelPresetsSanity:
    def test_all_connection_ids_are_known_builtin(self):
        from butly_core.llm.connections import is_builtin_connection
        for p in MODEL_PRESETS:
            # built-in connection に紐づくはず (user 定義は preset には載らない)
            assert is_builtin_connection(p.connection_id), (
                f"Preset {p.model_name} refers to non-builtin connection {p.connection_id}"
            )

    def test_no_duplicate_connection_model_pair(self):
        seen: set[tuple[str, str]] = set()
        for p in MODEL_PRESETS:
            key = (p.connection_id, p.model_name)
            assert key not in seen, f"Duplicate preset: {key}"
            seen.add(key)

    def test_deprecated_has_replacement(self):
        for p in MODEL_PRESETS:
            if p.deprecated:
                assert p.replacement is not None, (
                    f"Deprecated preset {p.model_name} must specify replacement"
                )
