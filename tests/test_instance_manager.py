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
        # ディレクトリが作成されている
        created_dirs = list(manager.instances_dir.glob("*_my_agent"))
        assert len(created_dirs) == 1

    def test_create_with_subdirectories(self, manager: InstanceManager):
        """サブディレクトリが正しく作成される"""
        manager.create_instance("sub_test", "テンプレート")

        created_dir = list(manager.instances_dir.glob("*_sub_test"))[0]
        assert (created_dir / "short_term_json").exists()
        assert (created_dir / "memory_archive" / "1_integrated").exists()
        assert (created_dir / "memory_archive" / "2_knowledgeized").exists()

    def test_create_with_system_instruction(self, manager: InstanceManager):
        """system_instruction.txt が作成される"""
        manager.create_instance("inst_test", "テンプレート文")

        created_dir = list(manager.instances_dir.glob("*_inst_test"))[0]
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

    def test_sequential_ids(self, manager: InstanceManager):
        """連番IDが正しく振られる"""
        manager.create_instance("first", "t")
        manager.create_instance("second", "t")

        dirs = sorted([
            d.name for d in manager.instances_dir.iterdir()
            if d.is_dir() and d.name != "00_master"
        ])

        # 01_first, 02_second の順
        assert len(dirs) == 2
        assert dirs[0].startswith("01")
        assert dirs[1].startswith("02")


class TestDeleteInstance:
    """インスタンス削除のテスト"""

    def test_delete_success(self, manager: InstanceManager):
        """正常に削除される"""
        manager.create_instance("to_delete", "t")
        folder_name = list(manager.instances_dir.glob("*_to_delete"))[0].name

        success, msg = manager.delete_instance(folder_name)

        assert success is True
        assert not (manager.instances_dir / folder_name).exists()

    def test_delete_master_fails(self, manager: InstanceManager):
        """00_master は削除できない"""
        success, msg = manager.delete_instance("00_master")

        assert success is False

    def test_delete_nonexistent(self, manager: InstanceManager):
        """存在しないインスタンスの削除はエラー"""
        success, msg = manager.delete_instance("99_nonexistent")

        assert success is False


class TestRenameInstance:
    """インスタンスリネームのテスト"""

    def test_rename_success(self, manager: InstanceManager):
        """正常にリネームされる"""
        manager.create_instance("old_name", "t")
        old_folder = list(manager.instances_dir.glob("*_old_name"))[0].name

        success, new_name = manager.rename_instance(old_folder, "new_name")

        assert success is True
        assert "new_name" in new_name
        assert not (manager.instances_dir / old_folder).exists()

    def test_rename_invalid_name(self, manager: InstanceManager):
        """不正な名前へのリネームはエラー"""
        manager.create_instance("valid", "t")
        folder = list(manager.instances_dir.glob("*_valid"))[0].name

        success, msg = manager.rename_instance(folder, "日本語名")

        assert success is False


class TestInstanceConfig:
    """インスタンス設定の読み書きテスト"""

    def test_empty_config_returns_dict(self, manager: InstanceManager):
        """config.json がない場合、空 dict が返る"""
        config = manager.get_instance_config("00_master")

        assert isinstance(config, dict)

    def test_save_and_load_config(self, manager: InstanceManager):
        """設定の保存と読み込み"""
        manager.create_instance("cfg_test", "t")
        folder = list(manager.instances_dir.glob("*_cfg_test"))[0].name

        test_config = {"brain": {"search_limit": 5}, "chat": {"model_name": "test-model"}}
        success, _ = manager.update_instance_config(folder, test_config)

        assert success is True

        loaded = manager.get_instance_config(folder)
        assert loaded["brain"]["search_limit"] == 5


class TestInstancePrompts:
    """インスタンスプロンプトの読み書きテスト"""

    def test_get_prompts(self, manager: InstanceManager):
        """プロンプト取得"""
        result = manager.get_instance_prompts("00_master")

        assert result is not None
        assert "system_instruction" in result
        assert "key_memory" in result

    def test_update_prompts(self, manager: InstanceManager):
        """プロンプト更新"""
        success, _ = manager.update_instance_prompts("00_master", {
            "system_instruction": "更新されたシステム指示",
            "key_memory": "更新された根幹記憶",
        })

        assert success is True

        result = manager.get_instance_prompts("00_master")
        assert "更新されたシステム指示" in result["system_instruction"]
