"""
tests/test_settings_model_candidates.py
---------------------------------------
Phase 3: /settings/model_candidates エンドポイントのテスト。

候補内訳:
  1. MODEL_PRESETS (role でフィルタ)
  2. 現在 AI_CONFIG[role] に保存されている model_name
  3. 利用可能 Connection で /models 動的取得した結果
"""

import json
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def reset_registry():
    from butly_core.llm.connections import get_registry
    from routers.settings import _invalidate_model_catalog

    get_registry().reset_to_builtin()
    _invalidate_model_catalog()
    yield
    get_registry().reset_to_builtin()
    _invalidate_model_catalog()


@pytest.fixture(autouse=True)
def patch_urlopen_default(monkeypatch):
    """テスト中の偶発的な実外部呼び出しを防ぐ。"""
    from urllib.error import URLError

    def _fake(*a, **kw):
        raise URLError("blocked in tests")
    monkeypatch.setattr("urllib.request.urlopen", _fake)


class TestModelCandidatesRole:
    def test_returns_preset_for_chat_role(self):
        from routers.settings import model_candidates
        res = model_candidates(role="chat")
        assert res["role"] == "chat"
        cands = res["candidates"]
        assert len(cands) > 0
        # MODEL_PRESETS から chat role を持つもの (Gemini Flash 等) が含まれる
        names = {c["model_name"] for c in cands}
        assert "gemini-3.5-flash" in names

    def test_embedding_role_excludes_chat_only(self):
        from routers.settings import model_candidates
        res = model_candidates(role="embedding")
        # embedding capability を持つ preset のみ
        names = {c["model_name"] for c in res["candidates"]}
        assert "gemini-embedding-2" in names or "text-embedding-3-small" in names
        # chat-only model は含まれない (動的取得は外部呼び出し失敗で skip されるため確定)
        assert "gpt-4o" not in names
        assert "gemini-3.5-flash" not in names

    def test_includes_saved_ai_config_model(self, monkeypatch):
        """AI_CONFIG[role] に保存されているモデルが preset に無くても候補に入る。"""
        from butly_core import config as cfg_mod
        # AI_CONFIG["chat"] を一時的に書き換え
        original_chat = dict(cfg_mod.AI_CONFIG["chat"])
        cfg_mod.AI_CONFIG["chat"] = {
            "connection": "openai",
            "model_name": "gpt-4o-custom-fine-tuned",
        }
        try:
            from routers.settings import model_candidates
            res = model_candidates(role="chat")
            names = {c["model_name"] for c in res["candidates"]}
            assert "gpt-4o-custom-fine-tuned" in names
        finally:
            cfg_mod.AI_CONFIG["chat"] = original_chat

    def test_deprecated_flag_passthrough(self):
        from routers.settings import model_candidates
        res = model_candidates(role="chat", include_deprecated=True)
        # deprecated preset (例: grok-4-1-fast-reasoning) が含まれる
        deprecated_items = [c for c in res["candidates"] if c["deprecated"]]
        assert len(deprecated_items) >= 1
        for d in deprecated_items:
            assert d.get("replacement")

    def test_deprecated_excluded_by_default(self):
        from routers.settings import model_candidates
        res = model_candidates(role="chat", include_deprecated=False)
        deprecated_items = [c for c in res["candidates"] if c["deprecated"]]
        # 現在 AI_CONFIG が deprecated モデルでない限り 0 件
        # (AI_CONFIG はデフォルトで Gemini stable なので 0)
        assert len(deprecated_items) == 0

    def test_invalid_role_400(self):
        from fastapi import HTTPException
        from routers.settings import model_candidates
        with pytest.raises(HTTPException) as exc:
            model_candidates(role="bogus_role")
        assert exc.value.status_code == 400

    def test_no_dupes_for_same_connection_model(self):
        """preset と AI_CONFIG が同じモデルを指す → 1 件にまとまる。"""
        from routers.settings import model_candidates
        res = model_candidates(role="chat")
        seen = set()
        for c in res["candidates"]:
            key = (c["connection_id"], c["model_name"])
            assert key not in seen, f"重複: {key}"
            seen.add(key)


class TestModelCandidatesDynamicDiscovery:
    """連結成功した Connection の動的モデル発見をモック検証。"""

    def test_dynamic_models_appended(self, monkeypatch):
        from butly_core.llm.connections import Connection, register_connection
        register_connection(Connection(
            id="groq", protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
        ))
        monkeypatch.setenv("GROQ_API_KEY", "test-key")

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                return json.dumps({
                    "data": [
                        {"id": "llama-3.3-70b-versatile"},
                        {"id": "mixtral-8x7b"},
                    ]
                }).encode()

        def _fake_urlopen(req, timeout=5):
            if "groq.com" in req.full_url:
                return _FakeResp()
            from urllib.error import URLError
            raise URLError("not stubbed")

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        from routers.settings import model_candidates
        res = model_candidates(role="chat")
        names_by_conn = {
            (c["connection_id"], c["model_name"]) for c in res["candidates"]
        }
        assert ("groq", "llama-3.3-70b-versatile") in names_by_conn
        assert ("groq", "mixtral-8x7b") in names_by_conn

    def test_connection_without_api_key_skipped(self, monkeypatch):
        """auth 必須 connection で env 未設定なら動的取得スキップ (preset は出る)。"""
        from butly_core.llm.connections import Connection, register_connection
        register_connection(Connection(
            id="needs_auth", protocol="openai_compat",
            base_url="https://example.com/v1", api_key_env="MISSING_KEY",
        ))
        # MISSING_KEY は意図的に未設定
        monkeypatch.delenv("MISSING_KEY", raising=False)

        from routers.settings import model_candidates
        res = model_candidates(role="chat")
        # needs_auth 由来の候補は出ない
        for c in res["candidates"]:
            assert c["connection_id"] != "needs_auth"

    def test_catalog_is_reused_across_roles(self, monkeypatch):
        """5 roleの候補取得でConnectionの/modelsを重複取得しない。"""
        from butly_core.llm.connections import Connection, register_connection

        register_connection(Connection(
            id="cached-provider",
            protocol="openai_compat",
            base_url="https://cached.example/v1",
            api_key_env="CACHED_PROVIDER_API_KEY",
            embeddings_supported=True,
        ))
        monkeypatch.setenv("CACHED_PROVIDER_API_KEY", "test-key")
        calls = 0

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({
                    "data": [
                        {"id": "cached-chat-model"},
                        {"id": "cached-embedding-model"},
                    ]
                }).encode()

        def _fake_urlopen(request, timeout=5):
            nonlocal calls
            if "cached.example" in request.full_url:
                calls += 1
                return _FakeResp()
            from urllib.error import URLError
            raise URLError("not stubbed")

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        from routers.settings import model_candidates

        chat = model_candidates(role="chat")
        summary = model_candidates(role="summary")
        embedding = model_candidates(role="embedding")

        assert calls == 1
        assert any(
            item["model_name"] == "cached-chat-model"
            for item in chat["candidates"]
        )
        assert any(
            item["model_name"] == "cached-chat-model"
            for item in summary["candidates"]
        )
        assert any(
            item["model_name"] == "cached-embedding-model"
            for item in embedding["candidates"]
        )
        assert chat["catalog_ttl_seconds"] == 600.0

    def test_manual_refresh_invalidates_one_connection(self, monkeypatch):
        from butly_core.llm.connections import Connection, register_connection

        register_connection(Connection(
            id="refreshable",
            protocol="openai_compat",
            base_url="https://refreshable.example/v1",
            api_key_env="REFRESHABLE_API_KEY",
        ))
        monkeypatch.setenv("REFRESHABLE_API_KEY", "test-key")
        calls = 0

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({
                    "data": [{"id": f"model-v{calls}"}],
                }).encode()

        def _fake_urlopen(request, timeout=5):
            nonlocal calls
            if "refreshable.example" in request.full_url:
                calls += 1
                return _FakeResp()
            from urllib.error import URLError
            raise URLError("not stubbed")

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        from routers.settings import model_candidates, refresh_model_catalog

        first = model_candidates(role="chat")
        cached = model_candidates(role="chat")
        refreshed = refresh_model_catalog(connection_id="refreshable")
        second = model_candidates(role="chat")

        assert calls == 2
        assert refreshed["connection_id"] == "refreshable"
        assert any(
            item["model_name"] == "model-v1"
            for item in first["candidates"]
        )
        assert any(
            item["model_name"] == "model-v1"
            for item in cached["candidates"]
        )
        assert any(
            item["model_name"] == "model-v2"
            for item in second["candidates"]
        )

    def test_connection_filter_discovers_only_selected_provider(
        self,
        monkeypatch,
    ):
        from butly_core.llm.connections import Connection, register_connection

        for connection_id in ("selected-provider", "hidden-provider"):
            register_connection(Connection(
                id=connection_id,
                protocol="openai_compat",
                base_url=f"https://{connection_id}.example/v1",
                api_key_env="SHARED_PROVIDER_API_KEY",
            ))
        monkeypatch.setenv("SHARED_PROVIDER_API_KEY", "test-key")
        calls = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({
                    "data": [{"id": "selected-chat-model"}],
                }).encode()

        def _fake_urlopen(request, timeout=5):
            calls.append(request.full_url)
            if "selected-provider.example" in request.full_url:
                return _FakeResp()
            raise AssertionError("unselected Connection was queried")

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        from routers.settings import model_candidates

        result = model_candidates(
            role="chat",
            connection_id="selected-provider",
        )

        assert calls == ["https://selected-provider.example/v1/models"]
        assert any(
            item["connection_id"] == "selected-provider"
            and item["model_name"] == "selected-chat-model"
            for item in result["candidates"]
        )
