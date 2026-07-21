"""
test_context_levels.py
-----------------------
context_levels（プリセット・レベル制御）のユニットテスト。

テスト対象:
  - migrate_context_order_to_levels(): 旧 context_order → 新 context_levels マイグレーション
  - _resolve_levels / _resolve_order: ヘルパー関数
  - build_system_instruction_from_blocks(): context_levels パラメータ対応
  - build_context_prefix(): context_levels パラメータ対応
  - LOW レベルの出力（ヘッダなし・行単位圧縮）
  - ButlyMemory: get_system_instruction_low / get_key_memory_low
"""

import pytest

from butly_core.core.gatekeeper import (
    build_system_instruction_from_blocks,
    build_context_prefix,
    CONTEXT_LEVEL_PRESETS,
)
from butly_core.core.gatekeeper.memory_builder import (
    migrate_context_order_to_levels,
    _resolve_levels,
    _resolve_order,
    DEFAULT_CONTEXT_ORDER,
)


# ==================================================================
# migrate_context_order_to_levels テスト
# ==================================================================


class TestMigrateContextOrder:
    """旧 context_order → 新 context_levels マイグレーション"""

    def test_migrate_basic(self):
        """基本的な context_order が context_levels に変換される"""
        config = {
            "context_order": {
                "system_instruction": ["system_instruction", "key_memory"],
                "context_prefix": ["label_notes", "current_time"],
                "system_instruction_position": "top",
            }
        }
        result = migrate_context_order_to_levels(config)
        ctx = result["context_levels"]
        assert ctx["preset"] == "custom"
        assert ctx["levels"]["system_instruction"] == "high"
        assert ctx["levels"]["key_memory"] == "high"
        assert ctx["levels"]["label_notes"] == "high"
        assert ctx["levels"]["current_time"] == "high"
        assert ctx["levels"]["glossary"] == "off"  # not in context_prefix
        assert ctx["system_instruction_position"] == "top"

    def test_migrate_preserves_order(self):
        """マイグレーション後に順序が保持される"""
        config = {
            "context_order": {
                "system_instruction": ["key_memory", "system_instruction"],
                "context_prefix": ["current_time", "label_notes"],
            }
        }
        result = migrate_context_order_to_levels(config)
        assert result["context_levels"]["order"]["system_instruction"] == ["key_memory", "system_instruction"]
        assert result["context_levels"]["order"]["context_prefix"] == ["current_time", "label_notes"]

    def test_no_migration_when_context_levels_exists(self):
        """context_levels が既にある場合はスキップ"""
        config = {
            "context_order": {"system_instruction": ["system_instruction"]},
            "context_levels": {"preset": "normal", "levels": {}},
        }
        result = migrate_context_order_to_levels(config)
        assert result["context_levels"]["preset"] == "normal"

    def test_no_context_order_returns_unchanged(self):
        """context_order がない場合は何もしない"""
        config = {"some_key": "value"}
        result = migrate_context_order_to_levels(config)
        assert "context_levels" not in result


# ==================================================================
# _resolve_levels / _resolve_order テスト
# ==================================================================


class TestResolveLevels:
    """レベル解決ヘルパーのテスト"""

    def test_context_levels_preferred(self):
        """context_levels が context_order より優先される"""
        levels = _resolve_levels(
            context_levels={"levels": {"system_instruction": "low"}},
            context_order={"system_instruction": ["system_instruction"]},
        )
        assert levels["system_instruction"] == "low"

    def test_fallback_to_context_order(self):
        """context_levels がない場合は context_order からレベルを導出"""
        levels = _resolve_levels(
            context_levels=None,
            context_order={
                "system_instruction": ["system_instruction"],
                "context_prefix": ["current_time"],
            },
        )
        assert levels["system_instruction"] == "high"
        assert levels["key_memory"] == "off"
        assert levels["current_time"] == "high"
        assert levels["glossary"] == "off"

    def test_default_all_high(self):
        """両方Noneの場合は全 high"""
        levels = _resolve_levels(None, None)
        assert all(v == "high" for v in levels.values())

    def test_resolve_order_from_context_levels(self):
        """context_levels から順序を解決"""
        order = _resolve_order(
            context_levels={"order": {"system_instruction": ["key_memory", "system_instruction"]}},
            context_order=None,
            key="system_instruction",
        )
        assert order == ["key_memory", "system_instruction"]

    def test_resolve_order_fallback(self):
        """両方Noneの場合はデフォルト順"""
        order = _resolve_order(None, None, "system_instruction")
        assert order == DEFAULT_CONTEXT_ORDER["system_instruction"]


# ==================================================================
# プリセット定義テスト
# ==================================================================


class TestPresets:
    """プリセット定義の妥当性"""

    def test_normal_all_high(self):
        """normal プリセットは全セクション high"""
        assert all(v == "high" for v in CONTEXT_LEVEL_PRESETS["normal"].values())

    def test_low_preset_has_off_sections(self):
        """low プリセットに off セクションがある"""
        low = CONTEXT_LEVEL_PRESETS["low"]
        assert low["label_notes"] == "off"
        assert low["glossary"] == "off"
        assert low["session_digest"] == "off"
        assert low["tier_info"] == "off"

    def test_low_preset_web_search_high(self):
        """low プリセットでも web_search は high"""
        assert CONTEXT_LEVEL_PRESETS["low"]["web_search"] == "high"


# ==================================================================
# build_system_instruction_from_blocks with context_levels
# ==================================================================


class TestSystemInstructionLevels:
    """context_levels を使ったシステム命令生成テスト"""

    def _make_levels(self, si="high", km="high"):
        return {
            "levels": {
                "system_instruction": si,
                "key_memory": km,
            },
            "order": {"system_instruction": ["system_instruction", "key_memory"]},
        }

    def test_high_has_headers(self, memory_manager):
        """high レベルでヘッダ付き"""
        blocks = {"tier": "mid"}
        ctx = self._make_levels("high", "high")
        result = build_system_instruction_from_blocks(blocks, memory_manager, context_levels=ctx)
        assert "=== SYSTEM INSTRUCTION ===" in result
        assert "=== KEY MEMORY" in result

    def test_low_si_no_header(self, memory_manager):
        """low レベルでヘッダなし"""
        blocks = {"tier": "mid"}
        ctx = self._make_levels("low", "low")
        result = build_system_instruction_from_blocks(blocks, memory_manager, context_levels=ctx)
        assert "=== SYSTEM INSTRUCTION ===" not in result
        assert "[会話核]" in result

    def test_km_off(self, memory_manager):
        """key_memory off で KM セクションが消える"""
        blocks = {"tier": "mid"}
        ctx = self._make_levels("high", "off")
        result = build_system_instruction_from_blocks(blocks, memory_manager, context_levels=ctx)
        assert "=== SYSTEM INSTRUCTION ===" in result
        assert "=== KEY MEMORY" not in result

    def test_low_si_uses_low_file(self, test_instance_dir, memory_manager):
        """LOW版ファイルがある場合はそちらを使用"""
        (test_instance_dir / "system_instruction_low.txt").write_text(
            "簡略版の性格設定です。", encoding="utf-8"
        )
        blocks = {"tier": "mid"}
        ctx = self._make_levels("low", "high")
        result = build_system_instruction_from_blocks(blocks, memory_manager, context_levels=ctx)
        assert "簡略版の性格設定" in result
        assert "=== SYSTEM INSTRUCTION ===" not in result

    def test_low_si_fallback_to_normal(self, test_instance_dir, memory_manager):
        """LOW版ファイルが空（コメントのみ）の場合は通常版にフォールバック"""
        (test_instance_dir / "system_instruction_low.txt").write_text(
            "# コメントのみ\n# もう一行\n", encoding="utf-8"
        )
        blocks = {"tier": "mid"}
        ctx = self._make_levels("low", "high")
        result = build_system_instruction_from_blocks(blocks, memory_manager, context_levels=ctx)
        assert "テスト用の執事AI" in result  # conftest の通常版

    def test_low_km_uses_low_file(self, test_instance_dir, memory_manager):
        """LOW版 Key_Memory ファイルがある場合はそちらを使用"""
        (test_instance_dir / "Key_Memory_low.txt").write_text(
            "簡略版根幹記憶", encoding="utf-8"
        )
        blocks = {"tier": "mid"}
        ctx = self._make_levels("high", "low")
        result = build_system_instruction_from_blocks(blocks, memory_manager, context_levels=ctx)
        assert "[会話核] 簡略版根幹記憶" in result


# ==================================================================
# build_context_prefix with context_levels
# ==================================================================


class TestContextPrefixLevels:
    """context_levels を使ったコンテキストプレフィックス生成テスト"""

    def _make_levels(self, **overrides):
        levels = dict(CONTEXT_LEVEL_PRESETS["normal"])
        levels.update(overrides)
        return {
            "levels": levels,
            "order": {
                "context_prefix": DEFAULT_CONTEXT_ORDER["context_prefix"],
            },
        }

    def test_normal_preset_same_as_default(self, memory_manager):
        """normal プリセットはデフォルトと同じ出力"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "会話圧縮ログテスト",
            "mid_term": "中期記憶テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAGテスト",
        }
        result_default = build_context_prefix(blocks, memory_manager)
        result_normal = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(),
        )
        assert result_default == result_normal

    def test_label_notes_off(self, memory_manager):
        """label_notes off で背景ラベルが消える"""
        blocks = {"tier": "mid", "mid_term": "", "rag_context": "", "session_digest": ""}
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(label_notes="off"),
        )
        assert "[背景情報]" not in result
        assert "[Background Info]" not in result

    def test_current_time_low(self, memory_manager):
        """current_time low は時刻のみ1行"""
        blocks = {"tier": "mid", "mid_term": "", "rag_context": "", "session_digest": ""}
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(current_time="low"),
        )
        assert "=== CURRENT TIME" not in result

    def test_current_time_off(self, memory_manager):
        """current_time off で時刻セクションが消える"""
        blocks = {"tier": "mid", "mid_term": "", "rag_context": "", "session_digest": ""}
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(current_time="off"),
        )
        # 時刻系の文字が出現しないことを確認（CURRENT TIMEヘッダもtime文字列もなし）
        assert "CURRENT TIME" not in result

    def test_glossary_low_excluded(self, memory_manager):
        """glossary low は off と同じ（将来拡張予定）"""
        blocks = {"tier": "mid", "mid_term": "", "rag_context": "", "session_digest": ""}
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(glossary="low"),
        )
        assert "GLOSSARY" not in result

    def test_mid_term_low_truncated(self, memory_manager):
        """mid_term low で末尾4行に圧縮"""
        blocks = {
            "tier": "mid",
            "mid_term": "行1\n行2\n行3\n行4\n行5\n行6",
            "mid_term_mode": "raw",
            "rag_context": "",
            "session_digest": "",
        }
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(mid_term="low"),
        )
        assert "[直近の背景]" in result
        assert "行3" in result
        assert "行6" in result
        assert "行1" not in result
        assert "=== MID-TERM" not in result

    def test_rag_low_truncated(self, memory_manager):
        """rag low で先頭3行に圧縮"""
        blocks = {
            "tier": "mid",
            "mid_term": "",
            "mid_term_mode": "raw",
            "rag_context": "記憶1\n記憶2\n記憶3\n記憶4\n記憶5",
            "session_digest": "",
        }
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(rag="low"),
        )
        assert "[関連記憶]" in result
        assert "記憶1" in result
        assert "記憶3" in result
        assert "記憶4" not in result
        assert "=== LONG-TERM MEMORY" not in result

    def test_session_digest_low_excluded(self, memory_manager):
        """session_digest low は off と同じ"""
        blocks = {
            "tier": "mid",
            "mid_term": "",
            "rag_context": "",
            "session_digest": "会話圧縮ログテスト",
        }
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(session_digest="low"),
        )
        assert "SESSION DIGEST" not in result
        assert "会話圧縮ログテスト" not in result

    def test_tier_info_low_excluded(self, memory_manager):
        """tier_info low は off と同じ"""
        blocks = {"tier": "mid", "mid_term": "", "rag_context": "", "session_digest": ""}
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(tier_info="low"),
        )
        assert "=== TIER INFO" not in result

    def test_web_search_low_truncated(self, memory_manager):
        """web_search low は300文字以内"""
        long_text = "検索結果" * 100  # 400文字
        blocks = {
            "tier": "mid",
            "mid_term": "",
            "rag_context": "",
            "session_digest": "",
            "web_search_context": long_text,
        }
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._make_levels(web_search="low"),
        )
        # web_search部分だけを見ると300文字以内（ヘッダなし）
        assert "=== WEB SEARCH" not in result
        assert len(long_text[:300]) == 300


# ==================================================================
# LOWプリセット統合テスト
# ==================================================================


class TestLowPresetIntegration:
    """low プリセットの統合テスト"""

    def _low_levels(self):
        return {
            "levels": CONTEXT_LEVEL_PRESETS["low"],
            "order": {
                "system_instruction": DEFAULT_CONTEXT_ORDER["system_instruction"],
                "context_prefix": DEFAULT_CONTEXT_ORDER["context_prefix"],
            },
        }

    def test_low_system_instruction(self, memory_manager):
        """low プリセットでの system_instruction 生成"""
        blocks = {"tier": "mid"}
        result = build_system_instruction_from_blocks(
            blocks, memory_manager,
            context_levels=self._low_levels(),
        )
        # ヘッダなし
        assert "=== SYSTEM INSTRUCTION ===" not in result
        assert "=== KEY MEMORY" not in result
        # [会話核] ラベルあり
        assert "[会話核]" in result

    def test_low_context_prefix_minimal(self, memory_manager):
        """low プリセットでの context_prefix は最小限"""
        blocks = {
            "tier": "mid",
            "mid_term": "行1\n行2\n行3\n行4\n行5",
            "mid_term_mode": "raw",
            "rag_context": "",
            "session_digest": "会話圧縮ログ",
        }
        result = build_context_prefix(
            blocks, memory_manager,
            context_levels=self._low_levels(),
        )
        # label_notes, glossary, session_digest, tier_info は off
        assert "[背景情報]" not in result
        assert "[Background Info]" not in result
        assert "GLOSSARY" not in result
        assert "SESSION DIGEST" not in result
        assert "=== TIER INFO" not in result
        # mid_term low: 末尾4行
        assert "[直近の背景]" in result
        assert "行2" in result


# ==================================================================
# ButlyMemory LOW版メソッドテスト
# ==================================================================


class TestMemoryLowMethods:
    """ButlyMemory の LOW版読み取りメソッドテスト"""

    def test_low_si_file_exists(self, test_instance_dir, memory_manager):
        """_low.txt に内容がある場合はそちらを使用"""
        (test_instance_dir / "system_instruction_low.txt").write_text(
            "軽量版設定", encoding="utf-8"
        )
        result = memory_manager.get_system_instruction_low()
        assert result == "軽量版設定"

    def test_low_si_file_comment_only(self, test_instance_dir, memory_manager):
        """_low.txt がコメントのみの場合はフォールバック"""
        (test_instance_dir / "system_instruction_low.txt").write_text(
            "# コメント\n# もう一行\n", encoding="utf-8"
        )
        result = memory_manager.get_system_instruction_low()
        assert result == memory_manager.get_system_instruction()

    def test_low_si_file_not_exists(self, memory_manager):
        """_low.txt が存在しない場合はフォールバック"""
        result = memory_manager.get_system_instruction_low()
        assert result == memory_manager.get_system_instruction()

    def test_low_km_file_exists(self, test_instance_dir, memory_manager):
        """Key_Memory_low.txt に内容がある場合はそちらを使用"""
        (test_instance_dir / "Key_Memory_low.txt").write_text(
            "軽量版記憶", encoding="utf-8"
        )
        result = memory_manager.get_key_memory_low()
        assert result == "軽量版記憶"

    def test_low_km_file_not_exists(self, memory_manager):
        """Key_Memory_low.txt が存在しない場合はフォールバック"""
        result = memory_manager.get_key_memory_low()
        assert result == memory_manager.get_key_memory()
