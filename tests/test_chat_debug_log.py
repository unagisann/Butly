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

from butly_core.chat.service import _save_debug_log


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
