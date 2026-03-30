"""
test_usage_tracker.py
---------------------
UsageTracker のユニットテスト。
ファイル I/O をモックまたは tmp_path で代替。
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from butly_core.search.usage_tracker import UsageTracker


@pytest.fixture(autouse=True)
def use_tmp_file(tmp_path):
    """テストごとに一時ファイルを使用する"""
    tmp_file = tmp_path / "search_usage.json"
    with patch.object(UsageTracker, "_file_path", tmp_file):
        yield tmp_file


class TestUsageTracker:
    """UsageTracker のテスト"""

    def test_initial_count_is_zero(self):
        """初期状態でカウントは 0"""
        assert UsageTracker.get_current_month_count() == 0

    def test_increment_once(self):
        """1回 increment すると 1 になる"""
        UsageTracker.increment()
        assert UsageTracker.get_current_month_count() == 1

    def test_increment_multiple(self):
        """複数回 increment で正しくカウントされる"""
        for _ in range(5):
            UsageTracker.increment()
        assert UsageTracker.get_current_month_count() == 5

    def test_get_all_returns_dict(self):
        """get_all() は辞書を返す"""
        UsageTracker.increment()
        data = UsageTracker.get_all()
        key = datetime.now().strftime("%Y-%m")
        assert isinstance(data, dict)
        assert data[key] == 1

    def test_corrupted_file_returns_zero(self, use_tmp_file):
        """破損ファイルの場合は 0 を返す"""
        use_tmp_file.write_text("not valid json")
        assert UsageTracker.get_current_month_count() == 0

    def test_month_key_format(self):
        """キーの形式が YYYY-MM であることを確認"""
        UsageTracker.increment()
        data = UsageTracker.get_all()
        keys = list(data.keys())
        assert len(keys) == 1
        # YYYY-MM 形式の検証
        key = keys[0]
        parts = key.split("-")
        assert len(parts) == 2
        assert len(parts[0]) == 4  # YYYY
        assert len(parts[1]) == 2  # MM
