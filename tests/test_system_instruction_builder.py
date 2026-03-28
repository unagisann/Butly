"""
test_system_instruction_builder.py
-----------------------------------
build_system_instruction_from_blocks() および build_context_prefix() のユニットテスト。

変更後の設計:
  - build_system_instruction_from_blocks(): 不変セクションのみ（SYSTEM INSTRUCTION, KEY MEMORY）
  - build_context_prefix(): 可変セクション（ラベル, 注意文, CURRENT TIME, MID-TERM, RAG, FLOATING SUMMARY, TIER INFO）

API キー不要。
"""

import pytest

from butly_core.core.gatekeeper import (
    build_system_instruction_from_blocks,
    build_context_prefix,
)


# ==================================================================
# build_system_instruction_from_blocks() テスト（不変セクションのみ）
# ==================================================================


class TestReflexInstruction:
    """reflex tier の system_instruction テスト"""

    def test_contains_system_instruction(self, memory_manager):
        """SYSTEM INSTRUCTION セクションが含まれる"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== SYSTEM INSTRUCTION ===" in result

    def test_contains_key_memory(self, memory_manager):
        """KEY MEMORY セクションが含まれる"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== KEY MEMORY" in result

    def test_no_tier_info(self, memory_manager):
        """system_instruction に TIER INFO が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== TIER INFO" not in result

    def test_no_current_time(self, memory_manager):
        """system_instruction に CURRENT TIME が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== CURRENT TIME" not in result

    def test_no_mid_term_section(self, memory_manager):
        """system_instruction に MID-TERM セクションがない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== MID-TERM MEMORY" not in result
        assert "=== MID-TERM DIGEST" not in result

    def test_no_rag_section(self, memory_manager):
        """system_instruction に RAG セクションがない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== LONG-TERM MEMORY" not in result

    def test_no_floating_section(self, memory_manager):
        """system_instruction に FLOATING SUMMARY が含まれない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "テスト要約",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== FLOATING SUMMARY" not in result


class TestMidInstruction:
    """mid tier の system_instruction テスト"""

    def test_no_mid_term_in_system_instruction(self, memory_manager):
        """mid tier でも system_instruction に MID-TERM が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "floating": "",
            "mid_term": "昨日の会話ログ",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== MID-TERM MEMORY" not in result
        assert "昨日の会話ログ" not in result

    def test_no_rag_section(self, memory_manager):
        """mid では RAG セクションがない"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "floating": "",
            "mid_term": "テスト",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== LONG-TERM MEMORY" not in result


class TestCortexInstruction:
    """cortex tier の system_instruction テスト"""

    def test_no_rag_in_system_instruction(self, memory_manager):
        """cortex でも system_instruction に RAG が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "中期記憶のテキスト",
            "mid_term_mode": "raw",
            "rag_context": "【過去の記憶（RAG）】\n・テスト: テストデータ",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== LONG-TERM MEMORY (RAG) ===" not in result
        assert "テストデータ" not in result

    def test_no_tier_info(self, memory_manager):
        """cortex でも system_instruction に TIER INFO が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== TIER INFO" not in result


class TestSystemInstructionOrder:
    """system_instruction のセクション順序テスト"""

    def test_instruction_order(self, memory_manager):
        """system_instruction の各セクションが正しい順序で並ぶ"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "浮動要約テスト",
            "mid_term": "中期記憶テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAGテスト",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        idx_sys = result.index("=== SYSTEM INSTRUCTION ===")
        idx_key = result.index("=== KEY MEMORY")

        assert idx_sys < idx_key
        # TIER INFO は context_prefix に移動済み
        assert "=== TIER INFO" not in result


# ==================================================================
# build_context_prefix() テスト（可変セクション）
# ==================================================================


class TestContextPrefixCurrentTime:
    """context_prefix: CURRENT TIME テスト"""

    @pytest.mark.parametrize("tier", ["reflex", "mid", "cortex"])
    def test_contains_current_time(self, tier, memory_manager):
        """全 tier で CURRENT TIME が含まれる"""
        blocks = {
            "tier": tier,
            "short_term": [],
            "floating": "",
            "mid_term": "" if tier == "reflex" else "mid",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== CURRENT TIME" in result


class TestContextPrefixMidTerm:
    """context_prefix: MID-TERM テスト"""

    def test_mid_tier_includes_mid_term_raw(self, memory_manager):
        """mid（RAWモード）: MID-TERM MEMORY が context_prefix に含まれる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "floating": "",
            "mid_term": "昨日の会話ログがここに入ります。",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== MID-TERM MEMORY" in result
        assert "昨日の会話ログ" in result

    def test_mid_tier_includes_digest_and_relationship(self, memory_manager):
        """mid（要約モード）: DIGEST + RELATIONSHIP が context_prefix に含まれる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "mid_term_mode": "summary",
            "mid_term_digest": "[2026-03-21] テスト\n- pytest導入",
            "mid_term_relationship": "# 空気感\n- 集中モード",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== MID-TERM DIGEST" in result
        assert "pytest導入" in result
        assert "=== RELATIONSHIP SNAPSHOT" in result
        assert "集中モード" in result

    def test_reflex_no_mid_term(self, memory_manager):
        """reflex では MID-TERM が context_prefix に含まれない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "このテキストは見えないはず",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== MID-TERM MEMORY" not in result


class TestContextPrefixRAG:
    """context_prefix: RAG テスト"""

    def test_cortex_includes_rag(self, memory_manager):
        """cortex: LONG-TERM MEMORY (RAG) が context_prefix に含まれる"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "中期記憶のテキスト",
            "mid_term_mode": "raw",
            "rag_context": "【過去の記憶（RAG）】\n・テスト: テストデータ",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== LONG-TERM MEMORY (RAG) ===" in result
        assert "テストデータ" in result

    def test_rag_has_reference_annotation(self, memory_manager):
        """cortex: RAG セクションに「直近の会話を優先」の注釈がある"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "mid_term_mode": "raw",
            "rag_context": "テスト記憶",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert ("直近の会話を優先" in result
                or "prioritize recent conversation" in result)

    def test_mid_no_rag(self, memory_manager):
        """mid では RAG が context_prefix に含まれない"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "floating": "",
            "mid_term": "テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAGデータ",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== LONG-TERM MEMORY" not in result


class TestContextPrefixFloating:
    """context_prefix: FLOATING SUMMARY テスト"""

    @pytest.mark.parametrize("tier", ["reflex", "mid", "cortex"])
    def test_floating_included_when_present(self, tier, memory_manager):
        """floating がある場合、全 tier で context_prefix に注入される"""
        blocks = {
            "tier": tier,
            "short_term": [],
            "floating": "直前の会話で天気の話をしました。",
            "mid_term": "mid" if tier != "reflex" else "",
            "mid_term_mode": "raw",
            "rag_context": "rag" if tier == "cortex" else "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== FLOATING SUMMARY" in result
        assert "天気の話" in result

    @pytest.mark.parametrize("tier", ["reflex", "mid", "cortex"])
    def test_floating_omitted_when_empty(self, tier, memory_manager):
        """floating が空の場合、セクション自体が省略される"""
        blocks = {
            "tier": tier,
            "short_term": [],
            "floating": "",
            "mid_term": "" if tier == "reflex" else "mid",
            "mid_term_mode": "raw",
            "rag_context": "rag" if tier == "cortex" else "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== FLOATING SUMMARY" not in result


class TestContextPrefixSectionOrder:
    """context_prefix のセクション注入順序テスト"""

    def test_section_order(self, memory_manager):
        """context_prefix の各セクションが正しい順序で並ぶ"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "浮動要約テスト",
            "mid_term": "中期記憶テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAGテスト",
        }

        result = build_context_prefix(blocks, memory_manager)

        idx_time = result.index("=== CURRENT TIME")
        idx_mid = result.index("=== MID-TERM MEMORY")
        idx_rag = result.index("=== LONG-TERM MEMORY")
        idx_float = result.index("=== FLOATING SUMMARY")
        idx_tier = result.index("=== TIER INFO")

        assert idx_time < idx_mid < idx_rag < idx_float < idx_tier

    def test_starts_with_label(self, memory_manager):
        """context_prefix はラベル行で始まる"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)
        first_line = result.strip().split("\n")[0]

        # ja=[背景情報], en=[Background Info]
        assert first_line.startswith("[")

    def test_contains_priority_note(self, memory_manager):
        """context_prefix に優先順位の注意文が含まれる"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        # ja=優先順位, en=Priority
        assert "優先順位" in result or "Priority" in result

    def test_contains_tier_info(self, memory_manager):
        """context_prefix に TIER INFO が含まれる"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== TIER INFO" in result


class TestContextPrefixReflex:
    """reflex tier は最小限のコンテキストのみ"""

    def test_reflex_has_current_time(self, memory_manager):
        """reflex でも CURRENT TIME は含まれる"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== CURRENT TIME" in result

    def test_reflex_no_mid_term_or_rag(self, memory_manager):
        """reflex では MID-TERM, RAG が含まれない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAG",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== MID-TERM MEMORY" not in result
        assert "=== LONG-TERM MEMORY" not in result
