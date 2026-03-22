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

        with patch("butly_core.core.gatekeeper.SYSTEM_CONFIG", {
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
