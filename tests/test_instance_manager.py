"""
test_instance_manager.py
------------------------
InstanceManager のユニットテスト。
インスタンスの作成・リネーム・削除のロジックを検証する。
API キー不要。
"""

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
