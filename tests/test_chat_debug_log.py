"""
test_chat_debug_log.py
----------------------
ChatService._save_debug_log() のユニットテスト。
instance_dir/debug_logs/latest.json + history/ のローテーションを検証する。
"""

import json
import time
from pathlib import Path

import pytest

from butly_core.chat.service import (
    _build_active_node_trace,
    _build_prompt_full,
    _collect_prompt_text,
    _save_debug_log,
)


class TestBuildPromptFull:
    """prompt_full に history を挿入するヘルパーの動作確認"""

    def test_full_order(self):
        history = [
            {"role": "user", "parts": ["先のユーザー発話"]},
            {"role": "model", "parts": ["先のAI応答"]},
            {"role": "user", "parts": ["直前ユーザー発話"]},
            {"role": "model", "parts": ["直前AI応答"]},
        ]
        items = _build_prompt_full("SYS", "CTX", history, "今のユーザー発話")
        roles = [i["role"] for i in items]
        contents = [i["content"] for i in items]
        # system → context → history(user→assistant→user→assistant) → current user
        assert roles == ["system", "user", "user", "assistant", "user", "assistant", "user"]
        assert contents[0] == "SYS"
        assert contents[1] == "CTX"
        assert contents[2] == "先のユーザー発話"
        assert contents[3] == "先のAI応答"
        assert contents[-1] == "今のユーザー発話"

    def test_normalizes_model_to_assistant(self):
        history = [{"role": "model", "parts": ["AI発話"]}]
        items = _build_prompt_full("S", "C", history, "U")
        assert items[2]["role"] == "assistant"

    def test_handles_dict_parts(self):
        history = [{"role": "user", "parts": [{"text": "ネストされた発話"}]}]
        items = _build_prompt_full("", "", history, "")
        assert items[0]["content"] == "ネストされた発話"

    def test_empty_history(self):
        items = _build_prompt_full("S", "C", [], "U")
        roles = [i["role"] for i in items]
        assert roles == ["system", "user", "user"]
        assert items[-1]["content"] == "U"

    def test_skips_empty_sections(self):
        items = _build_prompt_full("", "", [], "")
        assert items == []


class TestActiveNodeTrace:
    def _blocks(self):
        return {
            "rag_context": "cards",
            "active_node_lookup": {
                "enabled": True,
                "attempted": True,
                "candidate_count": 3,
                "matched_count": 1,
                "reason": "completed",
            },
            "active_nodes": [
                {
                    "id": "node-1",
                    "kind": "event",
                    "topic": "camping",
                    "statement": "The family camping trip was planned for June 2023.",
                    "confidence": 1.0,
                    "source_instance": "locomo_1",
                    "matched_card_ids": ["card-1", "card-2"],
                }
            ],
        }

    def test_confirms_exact_rendered_fragment_in_gemini_prompt(self):
        statement = "The family camping trip was planned for June 2023."
        prompt = _collect_prompt_text(
            {
                "system_instruction_full": "system",
                "context_prefix_full": f"Active nodes\n- {statement} (conf=1.00)",
                "user_input": "When was the trip?",
            }
        )

        trace = _build_active_node_trace(
            self._blocks(),
            prompt,
            rag_level="high",
        )

        assert trace["injection_status"] == "confirmed"
        assert trace["prompt_included_count"] == 1
        assert trace["nodes"][0]["prompt_included"] is True
        assert trace["nodes"][0]["matched_card_ids"] == ["card-1", "card-2"]

    def test_openai_multimodal_prompt_text_is_observable(self):
        statement = "The family camping trip was planned for June 2023."
        prompt = _collect_prompt_text(
            {
                "messages_full": [
                    {"role": "system", "content": "system"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"{statement} (conf=1.00)"},
                            {"type": "image_url", "image_url": {"url": "data:"}},
                        ],
                    },
                ]
            }
        )

        trace = _build_active_node_trace(
            self._blocks(),
            prompt,
            rag_level="high",
        )

        assert trace["injection_status"] == "confirmed"
        assert statement in prompt

    def test_distinguishes_context_exclusion_from_missing_prompt_content(self):
        excluded = _build_active_node_trace(
            self._blocks(),
            "observed prompt",
            rag_level="low",
        )
        missing = _build_active_node_trace(
            self._blocks(),
            "observed prompt without the node",
            rag_level="high",
        )

        assert excluded["injection_status"] == "context_level_excluded"
        assert excluded["nodes"][0]["prompt_included"] is None
        assert missing["injection_status"] == "missing"
        assert missing["nodes"][0]["prompt_included"] is False


@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "TestInstance"
    d.mkdir()
    return d


class TestSaveDebugLog:
    def test_creates_debug_dir_and_latest(self, instance_dir):
        payload = {"timestamp": "2026-05-17T12:00:00", "instance": "TestInstance",
                   "user_input": "hello", "assistant_response": "hi"}
        _save_debug_log(instance_dir, payload)

        debug_dir = instance_dir / "debug_logs"
        assert debug_dir.exists()
        latest = debug_dir / "latest.json"
        assert latest.exists()

        loaded = json.loads(latest.read_text(encoding="utf-8"))
        assert loaded["user_input"] == "hello"
        assert loaded["assistant_response"] == "hi"

    def test_history_file_created(self, instance_dir):
        payload = {"user_input": "test"}
        _save_debug_log(instance_dir, payload)

        history_dir = instance_dir / "debug_logs" / "history"
        assert history_dir.exists()
        files = list(history_dir.glob("*.json"))
        assert len(files) == 1

    def test_latest_overwritten_on_repeat(self, instance_dir):
        _save_debug_log(instance_dir, {"turn": 1})
        _save_debug_log(instance_dir, {"turn": 2})

        latest = json.loads((instance_dir / "debug_logs" / "latest.json").read_text())
        assert latest["turn"] == 2

    def test_history_rotation(self, instance_dir):
        """max_history を超えたら古いものから削除"""
        for i in range(5):
            _save_debug_log(instance_dir, {"turn": i}, max_history=3)
            time.sleep(0.01)  # mtime 差を確保

        history_dir = instance_dir / "debug_logs" / "history"
        files = list(history_dir.glob("*.json"))
        assert len(files) == 3

        # 残ったファイルは新しい 3 件 (turn 2, 3, 4)
        turns = sorted([json.loads(f.read_text())["turn"] for f in files])
        assert turns == [2, 3, 4]

    def test_japanese_text_no_mojibake(self, instance_dir):
        payload = {"user_input": "こんにちは、世界 🌏", "assistant_response": "おはよう"}
        _save_debug_log(instance_dir, payload)
        latest = json.loads((instance_dir / "debug_logs" / "latest.json").read_text(encoding="utf-8"))
        assert latest["user_input"] == "こんにちは、世界 🌏"

    def test_save_failure_does_not_raise(self, instance_dir, monkeypatch):
        """ファイル保存失敗 (権限エラー等) でも例外を上げない"""
        def fail_mkdir(*args, **kwargs):
            raise PermissionError("denied")
        monkeypatch.setattr(Path, "mkdir", fail_mkdir)

        # Should not raise
        _save_debug_log(instance_dir, {"x": 1})

    def test_non_serializable_falls_back_to_str(self, instance_dir):
        """JSON 化不可な値があっても default=str で文字列化される"""
        class Custom:
            def __repr__(self):
                return "CustomObj"

        _save_debug_log(instance_dir, {"obj": Custom()})
        latest = json.loads((instance_dir / "debug_logs" / "latest.json").read_text())
        assert latest["obj"] == "CustomObj"
