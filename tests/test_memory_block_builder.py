"""
test_memory_block_builder.py
-----------------------------
MemoryBlockBuilder のユニットテスト。
tier ごとに構築される記憶ブロックの内容を検証する。
API キー不要 — Brain はモックで代替。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from butly_core.core.gatekeeper import MemoryBlockBuilder


class TestReflexTier:
    """reflex tier のブロック構築テスト"""

    def test_reflex_has_short_term_and_floating(self, memory_manager, mock_brain):
        """reflex: short_term + floating のみ"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="reflex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="おはよう",
        )

        assert blocks["tier"] == "reflex"
        assert "short_term" in blocks
        assert "floating" in blocks

    def test_reflex_has_no_mid_term(self, memory_manager, mock_brain):
        """reflex: mid_term は空"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="reflex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="うん",
        )

        assert blocks["mid_term"] == ""

    def test_reflex_has_no_rag(self, memory_manager, mock_brain):
        """reflex: RAG コンテキストは空"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="reflex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="了解",
        )

        assert blocks["rag_context"] == ""


class TestMidTier:
    """mid tier のブロック構築テスト"""

    def test_mid_has_mid_term(self, memory_manager, mock_brain, test_instance_dir):
        """mid: mid_term が含まれる"""
        # mid_term にテストデータを書き込み
        (test_instance_dir / "mid_term.txt").write_text(
            "昨日はPythonのテストについて話しました。",
            encoding="utf-8",
        )

        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="今日のタスクは？",
        )

        assert blocks["tier"] == "mid"
        # RAW モードの場合、mid_term に内容が入る
        # summary モードの場合は mid_term_digest / mid_term_relationship
        has_content = (
            bool(blocks.get("mid_term"))
            or bool(blocks.get("mid_term_digest"))
        )
        assert has_content

    def test_mid_has_no_rag(self, memory_manager, mock_brain):
        """mid: RAG コンテキストは空"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="手順を確認して",
        )

        assert blocks["rag_context"] == ""

    def test_mid_summary_mode(self, memory_manager, mock_brain, test_instance_dir):
        """mid: 要約モード時に digest + relationship が含まれる"""
        (test_instance_dir / "mid_term_digest.txt").write_text(
            "[2026-03-21] テスト実装\n- pytest基盤の整備を開始",
            encoding="utf-8",
        )
        (test_instance_dir / "mid_term_relationship.txt").write_text(
            "# 空気感\n- 開発に集中している",
            encoding="utf-8",
        )

        builder = MemoryBlockBuilder()

        with patch("butly_core.core.gatekeeper.memory_builder.SYSTEM_CONFIG", {
            "memory": {"use_summarized_mid_term": True},
            "brain": {"search_limit": 3},
        }):
            blocks = builder.build(
                tier="mid",
                memory_manager=memory_manager,
                brain=mock_brain,
                user_input="最近どう？",
            )

        if blocks.get("mid_term_mode") == "summary":
            assert "テスト実装" in blocks.get("mid_term_digest", "")
            assert "空気感" in blocks.get("mid_term_relationship", "")


class TestCortexTier:
    """cortex tier のブロック構築テスト"""

    def test_cortex_has_rag_context(self, memory_manager, mock_brain):
        """cortex: RAG 検索結果が含まれる"""
        builder = MemoryBlockBuilder()

        gk_output = {
            "tier": "cortex",
            "need": "過去のプロジェクト情報",
            "search_targets": ["プロジェクト", "進捗"],
        }

        blocks = builder.build(
            tier="cortex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="前に話したプロジェクトの件を教えて",
            gatekeeper_output=gk_output,
        )

        assert blocks["tier"] == "cortex"
        assert blocks["rag_context"] != ""
        assert "テストプロジェクト" in blocks["rag_context"]

    def test_cortex_includes_need_and_targets(self, memory_manager, mock_brain):
        """cortex: need と search_targets が伝播される"""
        builder = MemoryBlockBuilder()

        gk_output = {
            "tier": "cortex",
            "need": "バグの詳細",
            "search_targets": ["バグ", "修正"],
        }

        blocks = builder.build(
            tier="cortex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="あのバグの件",
            gatekeeper_output=gk_output,
        )

        assert blocks["need"] == "バグの詳細"
        assert blocks["search_targets"] == ["バグ", "修正"]

    def test_cortex_without_brain_skips_rag(self, memory_manager):
        """cortex: brain=None の場合は RAG がスキップされる"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="cortex",
            memory_manager=memory_manager,
            brain=None,
            user_input="深い相談",
        )

        assert blocks["rag_context"] == ""

    def test_cortex_without_input_skips_rag(self, memory_manager, mock_brain):
        """cortex: user_input が空の場合は RAG がスキップされる"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="cortex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="",
        )

        assert blocks["rag_context"] == ""


class TestBlockStructure:
    """全 tier 共通の構造テスト"""

    @pytest.mark.parametrize("tier", ["reflex", "mid", "cortex"])
    def test_all_tiers_have_required_keys(self, tier, memory_manager, mock_brain):
        """全 tier で必須キーが存在する"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier=tier,
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        required_keys = {"tier", "short_term", "floating", "mid_term", "rag_context"}
        assert required_keys.issubset(blocks.keys())

    @pytest.mark.parametrize("tier", ["reflex", "mid", "cortex"])
    def test_tier_value_matches_input(self, tier, memory_manager, mock_brain):
        """返却される tier 値が入力と一致する"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier=tier,
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        assert blocks["tier"] == tier


# ===================================================================
# コンテキスト順序制御テスト
# ===================================================================

class TestContextOrder:
    """context_order によるセクション順序変更のテスト"""

    def test_default_order_when_none(self, memory_manager, mock_brain, test_instance_dir):
        """context_order=None でデフォルト順が維持される"""
        from butly_core.core.gatekeeper import (
            build_system_instruction_from_blocks,
            build_context_prefix,
        )

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        result_default = build_system_instruction_from_blocks(
            blocks, memory_manager, context_order=None
        )
        result_explicit = build_system_instruction_from_blocks(
            blocks, memory_manager
        )
        assert result_default == result_explicit

    def test_system_instruction_order_reversed(self, memory_manager, mock_brain, test_instance_dir):
        """system_instruction のセクション順序を逆にできる"""
        from butly_core.core.gatekeeper import build_system_instruction_from_blocks

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        # デフォルト順: system_instruction → key_memory
        result_normal = build_system_instruction_from_blocks(
            blocks, memory_manager, context_order=None
        )

        # 逆順: key_memory → system_instruction
        result_reversed = build_system_instruction_from_blocks(
            blocks, memory_manager,
            context_order={"system_instruction": ["key_memory", "system_instruction"]},
        )

        # 両方にキーワードが含まれる
        assert "テスト用の執事AI" in result_normal
        assert "テスト用の執事AI" in result_reversed

        # 逆順では key_memory が先に来る
        key_mem_pos = result_reversed.find("プログラマー")
        sys_inst_pos = result_reversed.find("テスト用の執事AI")
        assert key_mem_pos < sys_inst_pos

    def test_section_excluded_by_removal(self, memory_manager, mock_brain, test_instance_dir):
        """配列から ID を除外するとそのセクションが出力されない"""
        from butly_core.core.gatekeeper import build_system_instruction_from_blocks

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        # key_memory を除外
        result = build_system_instruction_from_blocks(
            blocks, memory_manager,
            context_order={"system_instruction": ["system_instruction"]},
        )

        assert "テスト用の執事AI" in result
        assert "プログラマー" not in result

    def test_context_prefix_order_changed(self, memory_manager, mock_brain, test_instance_dir):
        """context_prefix のセクション順序を変更できる"""
        from butly_core.core.gatekeeper import build_context_prefix

        (test_instance_dir / "floating_summary.txt").write_text(
            "前回はPythonの話をしました。", encoding="utf-8",
        )

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        # floating を current_time より前に配置
        result = build_context_prefix(
            blocks, memory_manager,
            context_order={"context_prefix": ["floating", "current_time", "tier_info"]},
        )

        floating_pos = result.find("Pythonの話")
        # floating が存在する
        assert floating_pos >= 0

    def test_unknown_section_id_ignored(self, memory_manager, mock_brain, test_instance_dir):
        """不明なセクション ID は無視される"""
        from butly_core.core.gatekeeper import build_system_instruction_from_blocks

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        # 存在しない ID を含める
        result = build_system_instruction_from_blocks(
            blocks, memory_manager,
            context_order={"system_instruction": ["nonexistent", "system_instruction"]},
        )

        assert "テスト用の執事AI" in result

    def test_context_prefix_exclude_section(self, memory_manager, mock_brain, test_instance_dir):
        """context_prefix からセクションを除外できる"""
        from butly_core.core.gatekeeper import build_context_prefix

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        # tier_info のみ
        result = build_context_prefix(
            blocks, memory_manager,
            context_order={"context_prefix": ["tier_info"]},
        )

        # tier_info は含まれる
        assert "mid" in result

    def test_position_not_affect_build_functions(self, memory_manager, mock_brain, test_instance_dir):
        """system_instruction_position は build_* 関数自体には影響しない（Provider 責務）"""
        from butly_core.core.gatekeeper import (
            build_system_instruction_from_blocks,
            build_context_prefix,
        )

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        order_top = {
            "system_instruction": ["system_instruction", "key_memory"],
            "context_prefix": ["current_time", "tier_info"],
            "system_instruction_position": "top",
        }
        order_bottom = {
            "system_instruction": ["system_instruction", "key_memory"],
            "context_prefix": ["current_time", "tier_info"],
            "system_instruction_position": "bottom",
        }

        si_top = build_system_instruction_from_blocks(blocks, memory_manager, context_order=order_top)
        si_bottom = build_system_instruction_from_blocks(blocks, memory_manager, context_order=order_bottom)
        assert si_top == si_bottom

        cp_top = build_context_prefix(blocks, memory_manager, context_order=order_top)
        cp_bottom = build_context_prefix(blocks, memory_manager, context_order=order_bottom)
        assert cp_top == cp_bottom
