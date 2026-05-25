"""
test_memory.py
--------------
ButlyMemory のユニットテスト。
ファイル I/O、短期記憶の保存・読み込み、会話圧縮ログのテスト。
API キー不要。
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from butly_core.core.memory import ButlyMemory


class TestMemoryInit:
    """初期化テスト"""

    def test_directories_created(self, base_dir, test_instance_dir):
        """初期化時に必要なディレクトリが作成される"""
        mem = ButlyMemory(base_dir, instance_name="99_test_instance")

        assert mem.short_term_json_dir.exists()
        assert mem.session_digest_dir.exists()
        assert mem.archive_integrated.exists()

    def test_default_files_created(self, base_dir, test_instance_dir):
        """初期化時にデフォルトファイルが存在する"""
        mem = ButlyMemory(base_dir, instance_name="99_test_instance")

        assert mem.instruction_file.exists()


class TestSystemInstruction:
    """system_instruction の読み込みテスト"""

    def test_read_instruction(self, memory_manager):
        """system_instruction.txt が正しく読み込まれる"""
        text = memory_manager.get_system_instruction()

        assert "テスト用の執事AI" in text

    def test_fallback_when_missing(self, base_dir):
        """ファイルが存在しない場合のフォールバック"""
        # 空のインスタンスディレクトリを作成
        empty_inst = base_dir / "butly_core" / "instances" / "99_empty"
        empty_inst.mkdir(parents=True, exist_ok=True)

        mem = ButlyMemory(base_dir, instance_name="99_empty")
        text = mem.get_system_instruction()

        assert text  # 何らかのデフォルト値が返る


class TestKeyMemory:
    """Key Memory の読み込みテスト"""

    def test_read_key_memory(self, memory_manager):
        """Key_Memory.txt が正しく読み込まれる"""
        text = memory_manager.get_key_memory()

        assert "プログラマー" in text

    def test_empty_key_memory(self, base_dir, test_instance_dir):
        """Key Memory が空の場合、空文字列が返る"""
        (test_instance_dir / "Key_Memory.txt").write_text("", encoding="utf-8")
        mem = ButlyMemory(base_dir, instance_name="99_test_instance")

        text = mem.get_key_memory()

        assert text == ""


class TestMidTermMemory:
    """mid_term の読み込みテスト"""

    def test_read_digest(self, memory_manager, test_instance_dir):
        """mid_term_digest.txt が読み込まれる"""
        (test_instance_dir / "mid_term_digest.txt").write_text(
            "[2026-03-21] テスト\n- ダイジェストテスト",
            encoding="utf-8",
        )

        text = memory_manager.get_mid_term_digest()

        assert "ダイジェストテスト" in text

    def test_read_recent_snapshot(self, memory_manager, test_instance_dir):
        """recent_snapshot.txt が読み込まれる"""
        (test_instance_dir / "recent_snapshot.txt").write_text(
            "# トーン\n- テスト中",
            encoding="utf-8",
        )

        text = memory_manager.get_recent_snapshot()

        assert "テスト中" in text

    def test_recent_snapshot_fallback_to_legacy(self, memory_manager, test_instance_dir):
        """recent_snapshot.txt がなければ mid_term_relationship.txt にフォールバック"""
        (test_instance_dir / "mid_term_relationship.txt").write_text(
            "# 空気感\n- レガシーテスト",
            encoding="utf-8",
        )

        text = memory_manager.get_recent_snapshot()

        assert "レガシーテスト" in text


class TestShortTermSaveLoad:
    """短期記憶の保存と読み込みテスト"""

    def test_save_single_turn(self, memory_manager):
        """1ターンの会話が保存される"""
        filename = memory_manager.save_single_turn(
            "テスト質問", "テスト回答"
        )

        assert filename is not None
        assert filename.endswith(".json")

    def test_saved_turn_loadable(self, memory_manager):
        """保存したターンが load_recent_sessions で読み込める"""
        memory_manager.save_single_turn("質問1", "回答1")

        messages, ts = memory_manager.load_recent_sessions(limit=10)

        assert len(messages) >= 2  # user + model
        texts = [m["parts"][0] for m in messages]
        assert "質問1" in texts
        assert "回答1" in texts

    def test_multiple_turns(self, memory_manager):
        """複数ターンが時系列順で読み込まれる"""
        import time
        memory_manager.save_single_turn("最初の質問", "最初の回答")
        time.sleep(1.1)  # ファイル名の秒単位が異なることを保証
        memory_manager.save_single_turn("次の質問", "次の回答")

        messages, _ = memory_manager.load_recent_sessions(limit=10)

        # user parts だけを抽出
        user_texts = [m["parts"][0] for m in messages if m["role"] == "user"]
        assert "最初の質問" in user_texts
        assert "次の質問" in user_texts
        idx_first = user_texts.index("最初の質問")
        idx_next = user_texts.index("次の質問")
        assert idx_first < idx_next

    def test_empty_input_returns_none(self, memory_manager):
        """空の入力では保存しない"""
        result = memory_manager.save_single_turn("", "")

        assert result is None

    def test_limit_respected(self, memory_manager):
        """limit パラメータが尊重される"""
        for i in range(5):
            memory_manager.save_single_turn(f"質問{i}", f"回答{i}")

        messages, _ = memory_manager.load_recent_sessions(limit=4)

        assert len(messages) <= 4


class TestSessionDigest:
    """会話圧縮ログのテスト"""

    def test_empty_session_digest(self, memory_manager):
        """会話圧縮ログが空の場合、空文字列が返る"""
        text = memory_manager.get_session_digest()

        assert text == ""

    def test_legacy_session_digest(self, memory_manager, test_instance_dir):
        """旧形式の session_digest.txt が読み込まれる"""
        (test_instance_dir / "session_digest.txt").write_text(
            "レガシー要約テスト", encoding="utf-8"
        )

        text = memory_manager.get_session_digest()

        assert "レガシー要約テスト" in text

    def test_new_session_digest_dir(self, memory_manager, test_instance_dir):
        """新形式の session_digests/ 内ファイルが読み込まれる"""
        summary_dir = test_instance_dir / "session_digests"
        (summary_dir / "summary_001.txt").write_text(
            "新形式要約テスト", encoding="utf-8"
        )

        text = memory_manager.get_session_digest()

        assert "新形式要約テスト" in text



