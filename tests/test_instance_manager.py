"""
test_instance_manager.py
------------------------
InstanceManager のユニットテスト。
インスタンスの作成・リネーム・削除のロジックを検証する。
API キー不要。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from butly_core.core.instance_manager import InstanceManager


@pytest.fixture
def manager(base_dir: Path) -> InstanceManager:
    """テスト用の InstanceManager"""
    return InstanceManager(base_dir)


class TestCreateInstance:
    """インスタンス作成のテスト"""

    def test_create_success(self, manager: InstanceManager):
        """正常にインスタンスが作成される"""
        success, msg = manager.create_instance("my_agent", "テスト用テンプレート")

        assert success is True
        assert (manager.instances_dir / "my_agent").exists()

    def test_create_with_subdirectories(self, manager: InstanceManager):
        """サブディレクトリが正しく作成される"""
        manager.create_instance("sub_test", "テンプレート")

        created_dir = manager.instances_dir / "sub_test"
        assert (created_dir / "short_term_json").exists()
        assert (created_dir / "memory_archive" / "1_integrated").exists()
        assert (created_dir / "memory_archive" / "2_knowledgeized").exists()

    def test_create_with_system_instruction(self, manager: InstanceManager):
        """system_instruction.txt が作成される"""
        manager.create_instance("inst_test", "テンプレート文")

        created_dir = manager.instances_dir / "inst_test"
        si_file = created_dir / "system_instruction.txt"
        assert si_file.exists()

    def test_create_invalid_name(self, manager: InstanceManager):
        """不正な名前（日本語等）はエラーになる"""
        success, msg = manager.create_instance("日本語テスト", "テンプレート")

        assert success is False
        assert "半角英数字" in msg

    def test_create_invalid_name_space(self, manager: InstanceManager):
        """スペースを含む名前はエラーになる"""
        success, msg = manager.create_instance("my agent", "テンプレート")

        assert success is False

    def test_duplicate_name_rejected(self, manager: InstanceManager):
        """同名インスタンスは作成できない"""
        manager.create_instance("dup_test", "t")
        success, msg = manager.create_instance("dup_test", "t")

        assert success is False
        assert "既に存在" in msg


class TestDeleteInstance:
    """インスタンス削除のテスト"""

    def test_delete_success(self, manager: InstanceManager):
        """正常に削除される"""
        manager.create_instance("to_delete", "t")

        success, msg = manager.delete_instance("to_delete")

        assert success is True
        assert not (manager.instances_dir / "to_delete").exists()

    def test_delete_nonexistent(self, manager: InstanceManager):
        """存在しないインスタンスの削除はエラー"""
        success, msg = manager.delete_instance("nonexistent")

        assert success is False


class TestRenameInstance:
    """インスタンスリネームのテスト"""

    def test_rename_success(self, manager: InstanceManager):
        """正常にリネームされる"""
        manager.create_instance("old_name", "t")

        success, new_name = manager.rename_instance("old_name", "new_name")

        assert success is True
        assert new_name == "new_name"
        assert not (manager.instances_dir / "old_name").exists()
        assert (manager.instances_dir / "new_name").exists()

    def test_rename_invalid_name(self, manager: InstanceManager):
        """不正な名前へのリネームはエラー"""
        manager.create_instance("valid", "t")

        success, msg = manager.rename_instance("valid", "日本語名")

        assert success is False


class TestInstanceConfig:
    """インスタンス設定の読み書きテスト"""

    def test_empty_config_returns_dict(self, manager: InstanceManager):
        """config.json がない場合、空 dict が返る"""
        manager.create_instance("cfg_empty", "t")
        config = manager.get_instance_config("cfg_empty")

        assert isinstance(config, dict)

    def test_save_and_load_config(self, manager: InstanceManager):
        """設定の保存と読み込み"""
        manager.create_instance("cfg_test", "t")

        test_config = {"brain": {"search_limit": 5}, "chat": {"model_name": "test-model"}}
        success, _ = manager.update_instance_config("cfg_test", test_config)

        assert success is True

        loaded = manager.get_instance_config("cfg_test")
        assert loaded["brain"]["search_limit"] == 5


class TestInstancePrompts:
    """インスタンスプロンプトの読み書きテスト"""

    def test_get_prompts(self, manager: InstanceManager):
        """プロンプト取得"""
        manager.create_instance("prompt_test", "テスト用テンプレート")
        result = manager.get_instance_prompts("prompt_test")

        assert result is not None
        assert "system_instruction" in result
        assert "key_memory" in result

    def test_update_prompts(self, manager: InstanceManager):
        """プロンプト更新"""
        manager.create_instance("prompt_upd", "テンプレート")
        success, _ = manager.update_instance_prompts("prompt_upd", {
            "system_instruction": "更新されたシステム指示",
            "key_memory": "更新された根幹記憶",
        })

        assert success is True

        result = manager.get_instance_prompts("prompt_upd")
        assert "更新されたシステム指示" in result["system_instruction"]


class TestRenameDbSync:
    """リネーム時のDB・config連動テスト"""

    def test_rename_updates_db_type(self, manager: InstanceManager):
        """リネーム後にDB内のtypeが新名に変わる"""
        manager.create_instance("old_bot", "t")

        db_path = manager.instances_dir / "old_bot" / "butly_memory.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cards (
                id TEXT PRIMARY KEY, type TEXT, title TEXT,
                category TEXT, summary TEXT, tags TEXT,
                ai_importance INTEGER, humanity_importance INTEGER,
                episode TEXT, count INTEGER, raw_reference TEXT,
                embedding_blob BLOB, created_at TEXT, updated_at TEXT,
                is_pinned INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO knowledge_cards (id, type, title, category, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            ("card_001", "old_bot", "Test Card", "Tech", "Test summary"),
        )
        conn.commit()
        conn.close()

        success, new_name = manager.rename_instance("old_bot", "new_bot")
        assert success is True

        new_db_path = manager.instances_dir / "new_bot" / "butly_memory.db"
        conn = sqlite3.connect(new_db_path)
        row = conn.execute(
            "SELECT type FROM knowledge_cards WHERE id = ?", ("card_001",)
        ).fetchone()
        conn.close()

        assert row[0] == "new_bot"

    def test_rename_updates_agent_type_prefix(self, manager: InstanceManager):
        """エージェントカード type='old/coding' が 'new/coding' に更新される"""
        manager.create_instance("old_bot2", "t")

        db_path = manager.instances_dir / "old_bot2" / "butly_memory.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cards (
                id TEXT PRIMARY KEY, type TEXT, title TEXT,
                category TEXT, summary TEXT, tags TEXT,
                ai_importance INTEGER, humanity_importance INTEGER,
                episode TEXT, count INTEGER, raw_reference TEXT,
                embedding_blob BLOB, created_at TEXT, updated_at TEXT,
                is_pinned INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO knowledge_cards (id, type, title, category, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            ("agent_001", "old_bot2/coding", "Agent Card", "Tech", "Summary"),
        )
        conn.commit()
        conn.close()

        manager.rename_instance("old_bot2", "new_bot2")

        new_db_path = manager.instances_dir / "new_bot2" / "butly_memory.db"
        conn = sqlite3.connect(new_db_path)
        row = conn.execute(
            "SELECT type FROM knowledge_cards WHERE id = ?", ("agent_001",)
        ).fetchone()
        conn.close()

        assert row[0] == "new_bot2/coding"

    def test_rename_updates_readable_instances(self, manager: InstanceManager):
        """リネーム後に他インスタンスのreadable_instancesが追従する"""
        manager.create_instance("bot_a", "t")
        manager.create_instance("bot_b", "t")

        config = {"brain": {"readable_instances": ["self", "bot_a"]}}
        manager.update_instance_config("bot_b", config)

        manager.rename_instance("bot_a", "bot_a_renamed")

        updated_config = manager.get_instance_config("bot_b")
        readable = updated_config["brain"]["readable_instances"]
        assert "bot_a_renamed" in readable
        assert "bot_a" not in readable

    def test_rename_updates_agent_parent(self, manager: InstanceManager):
        """リネーム後にエージェントのparent設定が追従する"""
        manager.create_instance("parent_bot", "t")

        agents_dir = manager.instances_dir / "parent_bot" / "agents" / "coding"
        agents_dir.mkdir(parents=True)
        agent_config = {"instance_type": "agent", "parent": "parent_bot"}
        (agents_dir / "config.json").write_text(
            json.dumps(agent_config), encoding="utf-8"
        )

        manager.rename_instance("parent_bot", "jarvis")

        new_agent_config = json.loads(
            (manager.instances_dir / "jarvis" / "agents" / "coding" / "config.json")
            .read_text(encoding="utf-8")
        )
        assert new_agent_config["parent"] == "jarvis"

    def test_rename_db_failure_does_not_block(self, manager: InstanceManager):
        """DB更新失敗してもフォルダリネーム自体は成功する"""
        manager.create_instance("fragile", "t")

        # DBなしでもリネームは成功する
        success, new_name = manager.rename_instance("fragile", "robust")
        assert success is True
        assert (manager.instances_dir / "robust").exists()


class TestKeyMemoryCreation:
    """Key Memory の初期設定テスト"""

    def test_create_with_key_memory(self, manager: InstanceManager):
        """key_memory パラメータで Key_Memory.txt が書き込まれる"""
        key_mem = "AI Name: TestBot\n\nUser Name: Taro\nPreferred Name: taro-kun"
        manager.create_instance("km_test", "template text", key_memory=key_mem)

        km_path = manager.instances_dir / "km_test" / "Key_Memory.txt"
        assert km_path.exists()
        content = km_path.read_text(encoding="utf-8")
        assert "TestBot" in content
        assert "taro-kun" in content

    def test_create_without_key_memory(self, manager: InstanceManager):
        """key_memory 省略時は空の Key_Memory.txt が作成される"""
        manager.create_instance("km_empty", "template text")

        km_path = manager.instances_dir / "km_empty" / "Key_Memory.txt"
        assert km_path.exists()
        assert km_path.read_text(encoding="utf-8") == ""

    def test_create_with_partial_key_memory(self, manager: InstanceManager):
        """AI名のみの Key Memory でも正常に作成される"""
        manager.create_instance("km_partial", "t", key_memory="AI Name: Luna")

        content = (manager.instances_dir / "km_partial" / "Key_Memory.txt").read_text(encoding="utf-8")
        assert "Luna" in content
        assert "User Name" not in content
