"""
test_json_extract.py
--------------------
extract_json_str() の単体テスト。
ContextClassifier / StateUpdater / Stage 3 で共用する JSON 抽出ヘルパー。
"""

import json

import pytest

from butly_core.core.json_extract import extract_json_str

PAYLOAD = '{"topic": "camping", "mood": "happy"}'


class TestExtractJsonStr:

    def test_closed_json_fence(self):
        raw = f"```json\n{PAYLOAD}\n```"
        assert json.loads(extract_json_str(raw)) == json.loads(PAYLOAD)

    def test_closed_plain_fence(self):
        raw = f"```\n{PAYLOAD}\n```"
        assert json.loads(extract_json_str(raw)) == json.loads(PAYLOAD)

    def test_unclosed_fence(self):
        """閉じ ``` 欠落（max_output_tokens 到達で切れた出力）"""
        raw = f"```json\n{PAYLOAD}"
        assert json.loads(extract_json_str(raw)) == json.loads(PAYLOAD)

    def test_bare_json(self):
        assert json.loads(extract_json_str(PAYLOAD)) == json.loads(PAYLOAD)

    def test_prose_wrapped_json(self):
        raw = f"Sure! Here is the JSON:\n{PAYLOAD}\nLet me know."
        assert json.loads(extract_json_str(raw)) == json.loads(PAYLOAD)

    def test_reasoning_prefix_with_fence(self):
        """思考テキストの後にフェンス付き JSON が続くケース"""
        raw = f"Let me think about the topic first...\n```json\n{PAYLOAD}\n```"
        assert json.loads(extract_json_str(raw)) == json.loads(PAYLOAD)

    def test_nested_braces(self):
        nested = '{"llm_scoring": {"response_complexity": 0.5}, "need_intent": null}'
        raw = f"```json\n{nested}\n```"
        assert json.loads(extract_json_str(raw)) == json.loads(nested)

    @pytest.mark.parametrize("raw", ["", None])
    def test_empty_input_returns_empty(self, raw):
        assert extract_json_str(raw) == ""

    def test_no_json_returns_stripped_raw(self):
        assert extract_json_str("  not json at all  ") == "not json at all"
