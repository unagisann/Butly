"""API キー用 dotenv の読み込み元を固定する回帰テスト。"""

import os
from pathlib import Path
from unittest.mock import patch


def test_openai_compat_finds_only_dotenv():
    from butly_core.llm import _openai_compat

    with patch.object(Path, "exists", return_value=True):
        env_path = _openai_compat._find_env_path()

    assert env_path is not None
    assert Path(env_path).name == ".env"


def test_gemini_client_loads_only_dotenv():
    from butly_core.llm.providers import gemini

    with (
        patch.object(Path, "exists", return_value=True),
        patch.object(gemini, "load_dotenv") as load_dotenv,
        patch.object(gemini.genai, "Client") as client,
        patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
    ):
        gemini._get_client()

    loaded_path = load_dotenv.call_args.args[0]
    assert loaded_path.name == ".env"
    client.assert_called_once_with(api_key="test-key")
