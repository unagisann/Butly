"""
test_memory_judge.py
--------------------
MemoryJudge の単体テスト。
LLM呼び出し不要 — パース処理と config 解決のロジックを検証。
"""

import pytest

from butly_core.core.gatekeeper.memory_judge import MemoryJudge


class TestParseResponse:
    """_parse_response のテスト"""

    @pytest.fixture
    def judge(self):
        return MemoryJudge()

    def test_parse_need_and_targets(self, judge):
        raw = '```json\n{"need": "過去のプロジェクト情報", "search_targets": ["project", "deadline"]}\n```'
        result = judge._parse_response(raw)
        assert result["need"] == "過去のプロジェクト情報"
        assert result["search_targets"] == ["project", "deadline"]

    def test_parse_null_need(self, judge):
        raw = '{"need": null, "search_targets": null}'
        result = judge._parse_response(raw)
        assert result["need"] is None
        assert result["search_targets"] is None

    def test_parse_string_null_normalized(self, judge):
        """LLM が "null" を文字列で出力する場合の正規化"""
        raw = '{"need": "null", "search_targets": "null"}'
        result = judge._parse_response(raw)
        assert result["need"] is None
        assert result["search_targets"] is None

    def test_parse_string_none_normalized(self, judge):
        """LLM が "None" を文字列で出力する場合の正規化"""
        raw = '{"need": "None", "search_targets": "None"}'
        result = judge._parse_response(raw)
        assert result["need"] is None
        assert result["search_targets"] is None

    def test_parse_empty_string_normalized(self, judge):
        raw = '{"need": "", "search_targets": []}'
        result = judge._parse_response(raw)
        assert result["need"] is None
        assert result["search_targets"] is None

    def test_parse_empty_returns_default(self, judge):
        result = judge._parse_response("")
        assert result["need"] is None
        assert result["search_targets"] is None

    def test_parse_invalid_json_returns_default(self, judge):
        result = judge._parse_response("not json")
        assert result["need"] is None
        assert result["search_targets"] is None

    def test_parse_plain_json(self, judge):
        raw = '{"need": "ユーザーの好み", "search_targets": ["food", "preference"]}'
        result = judge._parse_response(raw)
        assert result["need"] == "ユーザーの好み"
        assert len(result["search_targets"]) == 2


class TestConfigResolution:
    """config 解決ロジックのテスト"""

    @pytest.fixture
    def judge(self):
        return MemoryJudge()

    def test_override_memory_judge_takes_priority(self, judge):
        override = {
            "memory_judge": {
                "model_name": "custom-judge-model",
                "generation_config": {"temperature": 0.1},
            }
        }
        model, config = judge._resolve_config(override)
        assert model == "custom-judge-model"

    def test_override_gatekeeper_fallback(self, judge):
        override = {
            "gatekeeper": {
                "model_name": "gatekeeper-model",
                "generation_config": {"temperature": 0.2},
            }
        }
        model, config = judge._resolve_config(override)
        assert model == "gatekeeper-model"

    def test_no_override_uses_default(self, judge):
        model, config = judge._resolve_config(None)
        assert model == judge.model_name

    def test_memory_judge_over_gatekeeper_in_override(self, judge):
        """override に memory_judge と gatekeeper 両方がある場合は memory_judge 優先"""
        override = {
            "memory_judge": {"model_name": "mj-model"},
            "gatekeeper": {"model_name": "gk-model"},
        }
        model, config = judge._resolve_config(override)
        assert model == "mj-model"


class TestDefaultOutput:
    """デフォルト出力のテスト"""

    def test_default_output(self):
        judge = MemoryJudge()
        output = judge._default_output()
        assert output["need"] is None
        assert output["search_targets"] is None
