"""
tests/test_settings_ollama_url.py
---------------------------------
/settings/ollama_url の GET/POST。

別PCの Ollama を指定しても保存できず、UI が常に localhost を表示していた
問題の回帰テスト。built-in connection は上書き不可なので、正規の逃げ道である
``base_url_env`` (= OLLAMA_BASE_URL) へ永続化する。

route 関数を直接呼び、.env の書き込み先は tmp へ差し替える。
"""

import os
from pathlib import Path

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def reset_registry():
    from butly_core.llm.connections import get_registry

    get_registry().reset_to_builtin()
    yield
    get_registry().reset_to_builtin()


@pytest.fixture
def env_path(tmp_path: Path, monkeypatch):
    import routers.settings as settings_mod

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(settings_mod.deps, "DATA_DIR", data_dir)
    original = os.environ.pop("OLLAMA_BASE_URL", None)
    yield data_dir / ".env"
    # set_ollama_url は os.environ を直接書くため monkeypatch では戻らない。
    # 他テストへ漏らさないよう明示的に復元する。
    os.environ.pop("OLLAMA_BASE_URL", None)
    if original is not None:
        os.environ["OLLAMA_BASE_URL"] = original


class TestGetOllamaUrl:
    def test_defaults_to_localhost_root_form(self, env_path):
        from routers.settings import get_ollama_url

        result = get_ollama_url()

        assert result["url"] == "http://localhost:11434"
        assert result["source"] == "default"

    def test_reports_env_override(self, env_path, monkeypatch):
        from routers.settings import get_ollama_url

        monkeypatch.setenv("OLLAMA_BASE_URL", "http://192.168.10.101:11434/v1")

        result = get_ollama_url()

        # UI と接続テストは root 形を使う
        assert result["url"] == "http://192.168.10.101:11434"
        assert result["source"] == "env"


class TestSetOllamaUrl:
    def test_persists_remote_host(self, env_path):
        from butly_core.llm.connections import get_connection
        from routers.settings import set_ollama_url

        result = set_ollama_url(url="http://192.168.10.101:11434")

        assert result["url"] == "http://192.168.10.101:11434"
        # OpenAI 互換ベースとして /v1 付きで保存する
        assert os.environ["OLLAMA_BASE_URL"] == "http://192.168.10.101:11434/v1"
        assert "OLLAMA_BASE_URL=http://192.168.10.101:11434/v1" in (
            env_path.read_text(encoding="utf-8")
        )
        # 再起動なしで connection の実効 URL が変わる
        assert get_connection("ollama").resolve_base_url() == (
            "http://192.168.10.101:11434/v1"
        )

    def test_accepts_url_that_already_has_v1(self, env_path):
        from routers.settings import set_ollama_url

        set_ollama_url(url="http://192.168.10.101:11434/v1")

        assert os.environ["OLLAMA_BASE_URL"] == "http://192.168.10.101:11434/v1"

    def test_strips_trailing_slash(self, env_path):
        from routers.settings import set_ollama_url

        set_ollama_url(url="http://192.168.10.101:11434/")

        assert os.environ["OLLAMA_BASE_URL"] == "http://192.168.10.101:11434/v1"

    def test_roundtrip_through_get(self, env_path):
        from routers.settings import get_ollama_url, set_ollama_url

        set_ollama_url(url="http://192.168.10.101:11434")

        assert get_ollama_url() == {
            "url": "http://192.168.10.101:11434",
            "source": "env",
        }

    def test_overwrites_previous_value(self, env_path):
        from routers.settings import set_ollama_url

        set_ollama_url(url="http://192.168.10.101:11434")
        set_ollama_url(url="http://192.168.10.55:11434")

        assert os.environ["OLLAMA_BASE_URL"] == "http://192.168.10.55:11434/v1"
        body = env_path.read_text(encoding="utf-8")
        assert body.count("OLLAMA_BASE_URL=") == 1
        assert "192.168.10.55" in body

    @pytest.mark.parametrize(
        "bad_url",
        ["", "   ", "not-a-url", "ftp://192.168.10.101", "http://a\nb:11434"],
    )
    def test_rejects_invalid_url(self, env_path, bad_url):
        from routers.settings import set_ollama_url

        with pytest.raises(HTTPException) as exc:
            set_ollama_url(url=bad_url)

        assert exc.value.status_code == 400
        assert "OLLAMA_BASE_URL" not in os.environ

    def test_https_host_is_allowed(self, env_path):
        from routers.settings import set_ollama_url

        set_ollama_url(url="https://ollama.example.com")

        assert os.environ["OLLAMA_BASE_URL"] == "https://ollama.example.com/v1"
