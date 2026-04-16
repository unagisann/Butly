"""
test_key_memory.py
------------------
key_memory.py のユニットテスト。
YAML 読み書き、ID 採番、テキスト変換、提案パース・解決・適用。
"""

import json
import pytest
from pathlib import Path

from butly_core.core.key_memory import (
    load_yaml, save_yaml, next_id,
    yaml_to_text, yaml_to_llm_format,
    parse_proposals, resolve_entry_id, apply_proposal,
    load_proposals, save_proposals,
    PROPOSALS_FILENAME,
)


@pytest.fixture
def sample_entries():
    return [
        {"id": "km_001", "target": "human", "content": "テンプレ感を好まない"},
        {"id": "km_002", "target": "human", "content": "Butlyの開発が趣味"},
        {"id": "km_003", "target": "persona", "content": "悠希と一緒にButlyの名前を考えた"},
    ]


@pytest.fixture
def yaml_path(tmp_path):
    return tmp_path / "Key_Memory.yaml"


class TestLoadSaveYaml:
    def test_load_yaml(self, yaml_path, sample_entries):
        """YAML 読み込みで entries が正しく返る"""
        save_yaml(sample_entries, yaml_path)
        loaded = load_yaml(yaml_path)
        assert len(loaded) == 3
        assert loaded[0]["id"] == "km_001"
        assert loaded[0]["content"] == "テンプレ感を好まない"

    def test_save_yaml(self, yaml_path, sample_entries):
        """entries を YAML に書き出し、再読み込みで一致"""
        save_yaml(sample_entries, yaml_path)
        loaded = load_yaml(yaml_path)
        for orig, loaded_e in zip(sample_entries, loaded):
            assert orig["id"] == loaded_e["id"]
            assert orig["target"] == loaded_e["target"]
            assert orig["content"] == loaded_e["content"]

    def test_load_nonexistent(self, tmp_path):
        """存在しないファイルは空リスト"""
        result = load_yaml(tmp_path / "nonexistent.yaml")
        assert result == []

    def test_duplicate_id_check(self, yaml_path):
        """重複 ID で保存時にエラー"""
        entries = [
            {"id": "km_001", "target": "human", "content": "a"},
            {"id": "km_001", "target": "human", "content": "b"},
        ]
        with pytest.raises(ValueError, match="Duplicate"):
            save_yaml(entries, yaml_path)


class TestNextId:
    def test_next_id(self, sample_entries):
        """自動採番が既存最大 + 1"""
        assert next_id(sample_entries) == "km_004"

    def test_next_id_empty(self):
        """entries 空 → km_001"""
        assert next_id([]) == "km_001"

    def test_next_id_with_gaps(self):
        """欠番があっても最大 + 1"""
        entries = [
            {"id": "km_001", "target": "human", "content": "a"},
            {"id": "km_010", "target": "human", "content": "b"},
        ]
        assert next_id(entries) == "km_011"


class TestYamlToText:
    def test_yaml_to_text_high(self, sample_entries):
        """high モードで [About User] / [About Me] セクション分け"""
        result = yaml_to_text(sample_entries, mode="high")
        assert "[About User]" in result
        assert "[About Me]" in result
        assert "テンプレ感を好まない" in result
        assert "悠希と一緒にButlyの名前を考えた" in result

    def test_yaml_to_text_low(self, sample_entries):
        """low モードで簡略出力"""
        result = yaml_to_text(sample_entries, mode="low")
        assert result.startswith("[会話核]")

    def test_yaml_to_text_empty(self):
        """空の entries は空文字"""
        assert yaml_to_text([]) == ""


class TestYamlToLlmFormat:
    def test_yaml_to_llm_format(self, sample_entries):
        """[target] content 形式"""
        result = yaml_to_llm_format(sample_entries)
        lines = result.split("\n")
        assert lines[0] == "[human] テンプレ感を好まない"
        assert lines[2] == "[persona] 悠希と一緒にButlyの名前を考えた"


class TestMemoryIntegration:
    """ButlyMemory との統合テスト"""

    def test_get_key_memory_yaml(self, base_dir, test_instance_dir):
        """YAML があれば YAML から読む"""
        from butly_core.core.memory import ButlyMemory

        entries = [
            {"id": "km_001", "target": "human", "content": "YAMLベースの記憶"},
        ]
        save_yaml(entries, test_instance_dir / "Key_Memory.yaml")

        mem = ButlyMemory(base_dir, instance_name="test_instance")
        text = mem.get_key_memory()
        assert "YAMLベースの記憶" in text

    def test_get_key_memory_txt_fallback(self, base_dir, test_instance_dir):
        """YAML なし → Key_Memory.txt フォールバック"""
        from butly_core.core.memory import ButlyMemory

        # YAML が存在しないことを確認
        yaml_path = test_instance_dir / "Key_Memory.yaml"
        if yaml_path.exists():
            yaml_path.unlink()

        mem = ButlyMemory(base_dir, instance_name="test_instance")
        text = mem.get_key_memory()
        assert "プログラマー" in text  # conftest の Key_Memory.txt の内容

    def test_get_key_memory_entries(self, base_dir, test_instance_dir):
        """get_key_memory_entries() で entries を取得"""
        from butly_core.core.memory import ButlyMemory

        entries = [
            {"id": "km_001", "target": "human", "content": "テスト記憶"},
        ]
        save_yaml(entries, test_instance_dir / "Key_Memory.yaml")

        mem = ButlyMemory(base_dir, instance_name="test_instance")
        result = mem.get_key_memory_entries()
        assert len(result) == 1
        assert result[0]["content"] == "テスト記憶"

    def test_save_key_memory_entries(self, base_dir, test_instance_dir):
        """save_key_memory_entries() で保存→再読み込みで一致"""
        from butly_core.core.memory import ButlyMemory

        mem = ButlyMemory(base_dir, instance_name="test_instance")
        entries = [
            {"id": "km_001", "target": "human", "content": "保存テスト"},
        ]
        mem.save_key_memory_entries(entries)

        loaded = mem.get_key_memory_entries()
        assert len(loaded) == 1
        assert loaded[0]["content"] == "保存テスト"


class TestMigration:
    """マイグレーションスクリプトのテスト"""

    def test_migration_script(self, tmp_path):
        """txt → yaml 変換の正確性"""
        from migrate_key_memory import parse_txt

        txt_path = tmp_path / "Key_Memory.txt"
        txt_path.write_text(
            "- テンプレ感を好まない\n"
            "- Butlyの開発が趣味\n"
            "- BPO企業でITヘルプデスク\n",
            encoding="utf-8",
        )

        entries = parse_txt(txt_path)
        assert len(entries) == 3
        assert entries[0]["id"] == "km_001"
        assert entries[0]["content"] == "テンプレ感を好まない"
        assert entries[0]["target"] == "human"

    def test_migration_skips_profile(self, tmp_path):
        """プロファイル行がスキップされる"""
        from migrate_key_memory import parse_txt

        txt_path = tmp_path / "Key_Memory.txt"
        txt_path.write_text(
            "AI名：Jarvis\n"
            "ユーザー名：悠希\n"
            "呼称：悠希\n"
            "- テンプレ感を好まない\n"
            "# コメント行\n"
            "\n"
            "- 開発が趣味\n",
            encoding="utf-8",
        )

        entries = parse_txt(txt_path)
        assert len(entries) == 2
        assert entries[0]["content"] == "テンプレ感を好まない"
        assert entries[1]["content"] == "開発が趣味"


# ==========================================
# 提案パース・解決・適用テスト
# ==========================================


class TestParseProposals:
    def test_parse_add(self, sample_entries):
        """ADD 提案が正しくパースされる"""
        llm_output = "ADD [human] 新しい趣味はランニング\nREASON: 最近走り始めた"
        proposals = parse_proposals(llm_output, sample_entries)
        assert len(proposals) == 1
        p = proposals[0]
        assert p["action"] == "add"
        assert p["target"] == "human"
        assert p["content"] == "新しい趣味はランニング"
        assert p["reason"] == "最近走り始めた"
        assert p["entry_id"] is None

    def test_parse_remove(self, sample_entries):
        """REMOVE 提案が正しくパースされ、entry_id が解決される"""
        llm_output = "REMOVE [human] テンプレ感を好まない\nREASON: もう古い情報"
        proposals = parse_proposals(llm_output, sample_entries)
        assert len(proposals) == 1
        p = proposals[0]
        assert p["action"] == "remove"
        assert p["entry_id"] == "km_001"
        assert p["reason"] == "もう古い情報"

    def test_parse_update(self, sample_entries):
        """UPDATE 提案が正しくパースされる"""
        llm_output = "UPDATE [human] Butlyの開発が趣味 → Butlyの開発と運用が趣味\nREASON: 運用も開始した"
        proposals = parse_proposals(llm_output, sample_entries)
        assert len(proposals) == 1
        p = proposals[0]
        assert p["action"] == "update"
        assert p["old_content"] == "Butlyの開発が趣味"
        assert p["new_content"] == "Butlyの開発と運用が趣味"
        assert p["entry_id"] == "km_002"

    def test_parse_no_proposals(self, sample_entries):
        """NO_PROPOSALS → 空リスト"""
        assert parse_proposals("NO_PROPOSALS", sample_entries) == []

    def test_parse_empty(self, sample_entries):
        """空文字列 → 空リスト"""
        assert parse_proposals("", sample_entries) == []

    def test_parse_multiple(self, sample_entries):
        """複数提案を一度にパース"""
        llm_output = (
            "ADD [persona] ユーザーの健康を気遣う性格\n"
            "REASON: 会話から判明\n"
            "\n"
            "REMOVE [human] テンプレ感を好まない\n"
            "REASON: 古い情報\n"
        )
        proposals = parse_proposals(llm_output, sample_entries)
        assert len(proposals) == 2
        assert proposals[0]["action"] == "add"
        assert proposals[1]["action"] == "remove"


class TestResolveEntryId:
    def test_exact_match(self, sample_entries):
        """完全一致で ID 解決"""
        eid = resolve_entry_id(sample_entries, "human", "テンプレ感を好まない")
        assert eid == "km_001"

    def test_partial_match(self, sample_entries):
        """前方一致で ID 解決"""
        eid = resolve_entry_id(sample_entries, "human", "テンプレ感を好ま")
        assert eid == "km_001"

    def test_no_match(self, sample_entries):
        """一致なしで None"""
        eid = resolve_entry_id(sample_entries, "human", "存在しない内容")
        assert eid is None

    def test_wrong_target(self, sample_entries):
        """target 不一致で None"""
        eid = resolve_entry_id(sample_entries, "persona", "テンプレ感を好まない")
        assert eid is None


class TestApplyProposal:
    def test_apply_add(self, yaml_path, sample_entries):
        """ADD 提案の適用"""
        save_yaml(sample_entries, yaml_path)
        proposal = {"action": "add", "target": "human", "content": "新しい事実"}
        assert apply_proposal(proposal, yaml_path) is True
        entries = load_yaml(yaml_path)
        assert len(entries) == 4
        assert entries[3]["id"] == "km_004"
        assert entries[3]["content"] == "新しい事実"

    def test_apply_remove(self, yaml_path, sample_entries):
        """REMOVE 提案の適用"""
        save_yaml(sample_entries, yaml_path)
        proposal = {"action": "remove", "entry_id": "km_001"}
        assert apply_proposal(proposal, yaml_path) is True
        entries = load_yaml(yaml_path)
        assert len(entries) == 2
        assert all(e["id"] != "km_001" for e in entries)

    def test_apply_remove_no_id(self, yaml_path, sample_entries):
        """entry_id なしの REMOVE は失敗"""
        save_yaml(sample_entries, yaml_path)
        proposal = {"action": "remove", "entry_id": None}
        assert apply_proposal(proposal, yaml_path) is False

    def test_apply_update(self, yaml_path, sample_entries):
        """UPDATE 提案の適用"""
        save_yaml(sample_entries, yaml_path)
        proposal = {
            "action": "update",
            "entry_id": "km_002",
            "target": "human",
            "new_content": "更新後の内容",
        }
        assert apply_proposal(proposal, yaml_path) is True
        entries = load_yaml(yaml_path)
        km002 = [e for e in entries if e["id"] == "km_002"][0]
        assert km002["content"] == "更新後の内容"

    def test_apply_update_missing_id(self, yaml_path, sample_entries):
        """存在しない ID の UPDATE は失敗"""
        save_yaml(sample_entries, yaml_path)
        proposal = {"action": "update", "entry_id": "km_999", "new_content": "x"}
        assert apply_proposal(proposal, yaml_path) is False

    def test_yaml_not_modified_on_proposal_parse(self, yaml_path, sample_entries):
        """parse_proposals は YAML を変更しない"""
        save_yaml(sample_entries, yaml_path)
        original = load_yaml(yaml_path)

        llm_output = "ADD [human] 追加提案\nREASON: テスト"
        parse_proposals(llm_output, sample_entries)

        after = load_yaml(yaml_path)
        assert len(after) == len(original)


class TestProposalIO:
    def test_save_and_load_proposals(self, tmp_path):
        """提案の保存・読み込み"""
        proposals = [
            {"action": "add", "target": "human", "content": "新情報", "status": "pending"},
        ]
        save_proposals(proposals, tmp_path)
        loaded = load_proposals(tmp_path)
        assert len(loaded) == 1
        assert loaded[0]["status"] == "pending"

    def test_load_empty(self, tmp_path):
        """ファイルなし → 空リスト"""
        assert load_proposals(tmp_path) == []

    def test_approve_with_edit(self, tmp_path, sample_entries):
        """承認時に content 上書き可能"""
        yaml_path = tmp_path / "Key_Memory.yaml"
        save_yaml(sample_entries, yaml_path)

        proposal = {"action": "add", "target": "human", "content": "元の内容"}
        # content を上書きしてから適用
        proposal["content"] = "編集後の内容"
        assert apply_proposal(proposal, yaml_path) is True

        entries = load_yaml(yaml_path)
        assert entries[-1]["content"] == "編集後の内容"

    def test_reject_does_not_modify_yaml(self, tmp_path, sample_entries):
        """reject は YAML を変更しない"""
        yaml_path = tmp_path / "Key_Memory.yaml"
        save_yaml(sample_entries, yaml_path)

        proposals = [
            {"action": "add", "target": "human", "content": "却下予定", "status": "pending"},
        ]
        save_proposals(proposals, tmp_path)

        # reject = status 変更のみ
        loaded = load_proposals(tmp_path)
        loaded[0]["status"] = "rejected"
        save_proposals(loaded, tmp_path)

        entries = load_yaml(yaml_path)
        assert len(entries) == 3  # 元の3件のまま


class TestProposalDefaultOff:
    """Key Memory 提案はデフォルトで無効（update_targets.key_memory = false）"""

    def test_default_off(self):
        """_should_update のデフォルト値が False"""
        from sleeptime import ButlySleeptime
        hk = ButlySleeptime()
        # 空の inst_cfg → defaults を使用
        assert hk._should_update({}, "key_memory") is False

    def test_enabled_via_config(self):
        """sleeptime.update_targets.key_memory = true で有効化"""
        from sleeptime import ButlySleeptime
        hk = ButlySleeptime()
        cfg = {"sleeptime": {"update_targets": {"key_memory": True}}}
        assert hk._should_update(cfg, "key_memory") is True
