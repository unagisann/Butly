"""
test_system_instruction_builder.py
-----------------------------------
build_system_instruction_from_blocks() のユニットテスト。
tier ごとに生成される system_instruction の内容・構造を検証する。
API キー不要。
"""

import pytest

from butly_core.core.gatekeeper import build_system_instruction_from_blocks


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

    def test_contains_current_time(self, memory_manager):
        """CURRENT TIME セクションが含まれる"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== CURRENT TIME" in result

    def test_contains_tier_info(self, memory_manager):
        """TIER INFO が reflex と表示される"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "reflex" in result

    def test_no_mid_term_section(self, memory_manager):
        """reflex では MID-TERM セクションがない"""
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
        """reflex では RAG セクションがない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== LONG-TERM MEMORY" not in result


class TestMidInstruction:
    """mid tier の system_instruction テスト"""

    def test_contains_mid_term_raw(self, memory_manager):
        """mid（RAWモード）: MID-TERM MEMORY セクションが含まれる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "floating": "",
            "mid_term": "昨日の会話ログがここに入ります。",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== MID-TERM MEMORY" in result
        assert "昨日の会話ログ" in result

    def test_contains_digest_and_relationship(self, memory_manager):
        """mid（要約モード）: DIGEST + RELATIONSHIP が含まれる"""
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

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== MID-TERM DIGEST" in result
        assert "pytest導入" in result
        assert "=== RELATIONSHIP SNAPSHOT" in result
        assert "集中モード" in result

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

    def test_contains_rag_section(self, memory_manager):
        """cortex: LONG-TERM MEMORY (RAG) セクションが含まれる"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "中期記憶のテキスト",
            "mid_term_mode": "raw",
            "rag_context": "【過去の記憶（RAG）】\n・テスト: テストデータ",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== LONG-TERM MEMORY (RAG) ===" in result
        assert "テストデータ" in result

    def test_contains_mid_term(self, memory_manager):
        """cortex: MID-TERM も含まれる（mid の上位互換）"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "中期記憶",
            "mid_term_mode": "raw",
            "rag_context": "RAGデータ",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== MID-TERM MEMORY" in result
        assert "中期記憶" in result

    def test_rag_has_priority_annotation(self, memory_manager):
        """cortex: RAG セクションに「直近の会話を優先」の注釈がある"""
        blocks = {
            "tier": "cortex",
            "short_term": [],
            "floating": "",
            "mid_term": "",
            "mid_term_mode": "raw",
            "rag_context": "テスト記憶",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "直近の会話を優先" in result


class TestFloatingSummary:
    """floating summary の注入テスト"""

    @pytest.mark.parametrize("tier", ["reflex", "mid", "cortex"])
    def test_floating_included_when_present(self, tier, memory_manager):
        """floating がある場合、全 tier で注入される"""
        blocks = {
            "tier": tier,
            "short_term": [],
            "floating": "直前の会話で天気の話をしました。",
            "mid_term": "mid" if tier != "reflex" else "",
            "mid_term_mode": "raw",
            "rag_context": "rag" if tier == "cortex" else "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

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

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== FLOATING SUMMARY" not in result


class TestSectionOrder:
    """セクションの注入順序テスト"""

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

        # 各セクションのインデックスを取得
        idx_sys = result.index("=== SYSTEM INSTRUCTION ===")
        idx_key = result.index("=== KEY MEMORY")
        idx_mid = result.index("=== MID-TERM MEMORY")
        idx_time = result.index("=== CURRENT TIME")
        idx_rag = result.index("=== LONG-TERM MEMORY")
        idx_float = result.index("=== FLOATING SUMMARY")
        idx_tier = result.index("=== TIER INFO ===")

        # 順序チェック
        assert idx_sys < idx_key < idx_mid < idx_time < idx_rag < idx_float < idx_tier
