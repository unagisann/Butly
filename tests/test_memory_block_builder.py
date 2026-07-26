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

    def test_reflex_has_short_term_and_session_digest(self, memory_manager, mock_brain):
        """reflex: short_term + session_digest のみ"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="reflex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="おはよう",
        )

        assert blocks["tier"] == "reflex"
        assert "short_term" in blocks
        assert "session_digest" in blocks

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
        """reflex: need が無ければ RAG コンテキストは空"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="reflex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="了解",
        )

        assert blocks["rag_context"] == ""

    def test_reflex_with_need_gets_rag(self, memory_manager, mock_brain):
        """reflex でも need + candidates があれば RAG は注入される（tier 非依存）"""
        builder = MemoryBlockBuilder()

        gk_output = {
            "tier": "reflex",
            "need": "past_fact",
            "search_targets": ["キャンプ"],
            "memory_probe": {
                "status": "hit",
                "candidates": [
                    {
                        "id": "test_001",
                        "title": "キャンプの予定",
                        "summary": "- 2023年6月にキャンプ予定",
                        "score": 0.8,
                        "source": "vector",
                    }
                ],
                "glossary_hits": [],
            },
        }

        blocks = builder.build(
            tier="reflex",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="キャンプいつ行く予定だっけ",
            gatekeeper_output=gk_output,
        )

        assert "キャンプの予定" in blocks["rag_context"]
        assert blocks["mid_term"] == ""  # mid_term は引き続き tier 依存
        # 描画側も reflex で RAG を落とさない
        from butly_core.core.gatekeeper.memory_builder import _build_rag
        rendered = _build_rag(blocks, "high", "reflex", lambda k: f"[{k}]")
        assert rendered is not None and "キャンプの予定" in rendered


class TestMidTier:
    """mid tier のブロック構築テスト"""

    def test_mid_has_mid_term(self, memory_manager, mock_brain, test_instance_dir):
        """mid: mid_term が含まれる"""
        # raw_memory_cache.txt にテストデータを配置
        (test_instance_dir / "raw_memory_cache.txt").write_text(
            "[2026-04-10 21:00:00] User: Pythonのテストについて話しましょう\n"
            "[2026-04-10 21:00:00] Agent: 承知しました。pytestについてですね。",
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
        # summary モードの場合は mid_term_digest / mid_term_recent_snapshot
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
        """mid: 要約モード時に digest + recent_snapshot が含まれる"""
        (test_instance_dir / "mid_term_digest.txt").write_text(
            "[2026-03-21] テスト実装\n- pytest基盤の整備を開始",
            encoding="utf-8",
        )
        (test_instance_dir / "recent_snapshot.txt").write_text(
            "# トーン\n- 開発に集中している",
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
            assert "トーン" in blocks.get("mid_term_recent_snapshot", "")


class TestRAGWithNeed:
    """need 有時の RAG ブロック構築テスト"""

    def test_need_has_rag_context(self, memory_manager, mock_brain):
        """need 有り: probe candidates から RAG コンテキストが構築される"""
        builder = MemoryBlockBuilder()

        gk_output = {
            "tier": "mid",
            "need": "memory_probe_hit",
            "search_targets": ["テストプロジェクト"],
            "memory_probe": {
                "status": "hit",
                "candidates": [
                    {
                        "id": "test_001",
                        "title": "テストプロジェクト",
                        "summary": "テスト用のナレッジカード",
                        "episode": "テスト中に生成されたカード",
                        "score": 0.85,
                        "source": "vector",
                    }
                ],
                "glossary_hits": [],
            },
        }

        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="前に話したプロジェクトの件を教えて",
            gatekeeper_output=gk_output,
        )

        assert blocks["tier"] == "mid"
        assert blocks["rag_context"] != ""
        assert "テストプロジェクト" in blocks["rag_context"]
        # source_date が無いカードには日付プレフィックスも凡例も付かない
        assert "[YYYY-MM-DD]" not in blocks["rag_context"]

    def test_rag_context_shows_source_date(self, memory_manager, mock_brain):
        """source_date 付きカードは会話日付プレフィックスと凡例が付く"""
        builder = MemoryBlockBuilder()

        gk_output = {
            "tier": "mid",
            "need": "memory_probe_hit",
            "search_targets": ["陶芸"],
            "memory_probe": {
                "status": "hit",
                "candidates": [
                    {
                        "id": "test_001",
                        "title": "陶芸教室",
                        "summary": "- 2024-04-08に陶芸クラブへ参加\n- 青いマグを計画",
                        "episode": "誇らしげだった",
                        "score": 0.85,
                        "source": "vector",
                        "source_date": "2024-04-08",
                    },
                    {
                        "id": "test_002",
                        "title": "旧カード",
                        "summary": "source_date の無い旧形式カード",
                        "score": 0.7,
                        "source": "vector",
                    },
                ],
                "glossary_hits": [],
            },
        }

        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="陶芸の件を教えて",
            gatekeeper_output=gk_output,
        )

        rag = blocks["rag_context"]
        assert "・[2024-04-08] 陶芸教室: " in rag
        assert "[YYYY-MM-DD] は、説明された出来事の日付とは限らず" in rag
        assert "根拠が示す日付粒度を保ち" in rag
        # 旧形式カードはプレフィックスなしのまま
        assert "・旧カード: " in rag
        # 複数行 summary の継続行はインデントされる
        assert "\n  - 青いマグを計画" in rag

    def test_need_includes_need_and_targets(self, memory_manager, mock_brain):
        """が伝播される"""
        builder = MemoryBlockBuilder()

        gk_output = {
            "tier": "mid",
            "need": "バグの詳細",
            "search_targets": ["バグ", "修正"],
        }

        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="あのバグの件",
            gatekeeper_output=gk_output,
        )

        assert blocks["need"] == "バグの詳細"
        assert blocks["search_targets"] == ["バグ", "修正"]

    def test_no_need_skips_rag(self, memory_manager):
        """need=None の場合は RAG がスキップされる"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=None,
            user_input="深い相談",
        )

        assert blocks["rag_context"] == ""

    def test_no_input_skips_rag(self, memory_manager, mock_brain):
        """user_input が空の場合は RAG がスキップされる"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="",
        )

        assert blocks["rag_context"] == ""


class TestBlockStructure:
    """全 tier 共通の構造テスト"""

    @pytest.mark.parametrize("tier", ["reflex", "mid"])
    def test_all_tiers_have_required_keys(self, tier, memory_manager, mock_brain):
        """全 tier で必須キーが存在する"""
        builder = MemoryBlockBuilder()

        blocks = builder.build(
            tier=tier,
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        required_keys = {"tier", "short_term", "session_digest", "mid_term", "rag_context"}
        assert required_keys.issubset(blocks.keys())

    @pytest.mark.parametrize("tier", ["reflex", "mid"])
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
# RAG 注入ソース (rag_source_mode) テスト
# ===================================================================

class TestRAGSourceMode:
    """rag_source_mode（cards / raw / both）による RAG ブロック構築テスト"""

    RAW_HEADER_BOTH = "[根拠となる会話抜粋]"
    RAW_HEADER_ONLY = "[関連する会話抜粋]"
    CARDS_HEADER = "[関連する記憶カード]"

    def _write_raw_file(self, base_dir, date, name, text):
        from tests.conftest import TEST_INSTANCE_FOLDER
        dest = (
            base_dir / "butly_core" / "instances" / TEST_INSTANCE_FOLDER
            / "memory_archive" / "2_knowledgeized" / date
        )
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(
            json.dumps(
                {
                    "timestamp": f"{date}T10:00:00",
                    "messages": [
                        {"role": "user", "parts": [text]},
                        {"role": "model", "parts": ["覚えています"]},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _gk_output(self, with_source_files=True):
        card = {
            "id": "test_001",
            "title": "引っ越し",
            "summary": "- 2023-05-08 に引っ越した",
            "score": 0.85,
            "source": "vector",
            "source_date": "2023-05-08",
        }
        if with_source_files:
            card["source_files"] = json.dumps(["s1.json"])
        return {
            "tier": "mid",
            "need": "memory_probe_hit",
            "search_targets": ["引っ越し"],
            "memory_probe": {
                "status": "hit",
                "candidates": [card],
                "glossary_hits": [],
            },
        }

    def _two_card_gk_output(self):
        """distinct な source_files を持つ2枚（スコア順）。"""
        return {
            "tier": "mid",
            "need": "memory_probe_hit",
            "search_targets": ["引っ越し"],
            "memory_probe": {
                "status": "hit",
                "candidates": [
                    {
                        "id": "top", "title": "引っ越し", "score": 0.9,
                        "source": "vector", "source_date": "2023-05-08",
                        "summary": "- 2023-05-08 に引っ越した",
                        "source_files": json.dumps(["s1.json"]),
                    },
                    {
                        "id": "second", "title": "旅行", "score": 0.6,
                        "source": "vector", "source_date": "2023-06-09",
                        "summary": "- 2023-06-09 に旅行した",
                        "source_files": json.dumps(["s2.json"]),
                    },
                ],
                "glossary_hits": [],
            },
        }

    def _build(self, memory_manager, mock_brain, base_dir, mode=None,
               mem_extra=None, **kwargs):
        from tests.conftest import TEST_INSTANCE_FOLDER
        mock_brain.instances_dir = base_dir / "butly_core" / "instances"
        mem = {}
        if mode:
            mem["rag_source_mode"] = mode
        if mem_extra:
            mem.update(mem_extra)
        override = {"memory": mem} if mem else None
        builder = MemoryBlockBuilder()
        return builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="引っ越しっていつだっけ",
            instance_name=TEST_INSTANCE_FOLDER,
            override_config=override,
            gatekeeper_output=kwargs.get("gk_output", self._gk_output()),
        )

    def test_default_mode_is_cards(self, memory_manager, mock_brain, base_dir):
        """既定はカードのみ（従来挙動）— RAW ファイルがあっても読まない"""
        self._write_raw_file(base_dir, "2023-05-08", "s1.json", "引っ越したよ")
        blocks = self._build(memory_manager, mock_brain, base_dir)

        assert blocks["rag_source_mode"] == "cards"
        assert self.CARDS_HEADER in blocks["rag_context"]
        assert self.RAW_HEADER_BOTH not in blocks["rag_context"]
        assert "rag_raw_reference" not in blocks

    def test_both_appends_raw_block(self, memory_manager, mock_brain, base_dir):
        """both: カードブロックの後に原文抜粋が付く"""
        self._write_raw_file(base_dir, "2023-05-08", "s1.json", "先週の日曜に引っ越したよ")
        blocks = self._build(memory_manager, mock_brain, base_dir, mode="both")

        rag = blocks["rag_context"]
        assert self.CARDS_HEADER in rag
        assert self.RAW_HEADER_BOTH in rag
        assert "先週の日曜に引っ越したよ" in rag
        assert rag.find(self.CARDS_HEADER) < rag.find(self.RAW_HEADER_BOTH)
        assert blocks["rag_source_mode"] == "both"
        assert blocks["rag_raw_reference"]["status"] == "ok"
        assert blocks["rag_raw_reference"]["files"] == ["s1.json"]
        assert blocks["rag_raw_reference"]["truncated"] is False
        # usage_count / debug 用のキーは mode に関わらず維持される
        assert blocks["rag_card_ids"] == ["test_001"]

    def test_raw_mode_replaces_cards(self, memory_manager, mock_brain, base_dir):
        """raw: 原文のみ注入され、カードブロックは含まれない"""
        self._write_raw_file(base_dir, "2023-05-08", "s1.json", "先週の日曜に引っ越したよ")
        blocks = self._build(memory_manager, mock_brain, base_dir, mode="raw")

        rag = blocks["rag_context"]
        assert self.RAW_HEADER_ONLY in rag
        assert self.CARDS_HEADER not in rag
        assert "先週の日曜に引っ越したよ" in rag
        assert blocks["rag_card_ids"] == ["test_001"]

    def test_fallback_to_cards_when_no_source_files(
        self, memory_manager, mock_brain, base_dir
    ):
        """source_files の無い旧カードのみ → カード注入にフォールバック"""
        blocks = self._build(
            memory_manager, mock_brain, base_dir, mode="both",
            gk_output=self._gk_output(with_source_files=False),
        )

        assert self.CARDS_HEADER in blocks["rag_context"]
        assert self.RAW_HEADER_BOTH not in blocks["rag_context"]
        # 無音フォールバックにしない（観測用ステータスを残す）
        assert blocks["rag_raw_reference"]["status"] == "fallback_cards"

    def test_fallback_to_cards_when_files_missing(
        self, memory_manager, mock_brain, base_dir
    ):
        """RAW ファイルが実在しない → カード注入にフォールバック"""
        blocks = self._build(memory_manager, mock_brain, base_dir, mode="both")

        assert self.CARDS_HEADER in blocks["rag_context"]
        assert self.RAW_HEADER_BOTH not in blocks["rag_context"]
        assert blocks["rag_raw_reference"]["status"] == "fallback_cards"

    def test_unknown_mode_falls_back_to_cards(
        self, memory_manager, mock_brain, base_dir
    ):
        """不明な mode 値は cards として扱う"""
        self._write_raw_file(base_dir, "2023-05-08", "s1.json", "引っ越したよ")
        blocks = self._build(memory_manager, mock_brain, base_dir, mode="everything")

        assert blocks["rag_source_mode"] == "cards"
        assert self.CARDS_HEADER in blocks["rag_context"]
        assert self.RAW_HEADER_BOTH not in blocks["rag_context"]

    def test_default_top_k_1_injects_only_top_card_raw(
        self, memory_manager, mock_brain, base_dir
    ):
        """既定 rag_raw_top_k=1: 全カードのサマリ + 最上位カードの原文のみ"""
        self._write_raw_file(base_dir, "2023-05-08", "s1.json", "引っ越したのは最上位")
        self._write_raw_file(base_dir, "2023-06-09", "s2.json", "旅行したのは二番目")
        blocks = self._build(
            memory_manager, mock_brain, base_dir, mode="both",
            gk_output=self._two_card_gk_output(),
        )

        rag = blocks["rag_context"]
        # 両カードのサマリは入る
        assert "引っ越し" in rag and "旅行" in rag
        # 原文は最上位カードのみ
        assert "引っ越したのは最上位" in rag
        assert "旅行したのは二番目" not in rag
        assert blocks["rag_raw_reference"]["files"] == ["s1.json"]
        assert blocks["rag_raw_reference"]["top_k"] == 1

    def test_top_k_0_injects_all_card_raw(
        self, memory_manager, mock_brain, base_dir
    ):
        """rag_raw_top_k=0（従来 both 挙動）: 全カードの原文を注入"""
        self._write_raw_file(base_dir, "2023-05-08", "s1.json", "引っ越したのは最上位")
        self._write_raw_file(base_dir, "2023-06-09", "s2.json", "旅行したのは二番目")
        blocks = self._build(
            memory_manager, mock_brain, base_dir, mode="both",
            mem_extra={"rag_raw_top_k": 0},
            gk_output=self._two_card_gk_output(),
        )

        rag = blocks["rag_context"]
        assert "引っ越したのは最上位" in rag
        assert "旅行したのは二番目" in rag
        assert blocks["rag_raw_reference"]["files"] == ["s1.json", "s2.json"]


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

        (test_instance_dir / "session_digest.txt").write_text(
            "前回はPythonの話をしました。", encoding="utf-8",
        )

        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier="mid",
            memory_manager=memory_manager,
            brain=mock_brain,
            user_input="テスト",
        )

        # session_digest を current_time より前に配置
        result = build_context_prefix(
            blocks, memory_manager,
            context_order={"context_prefix": ["session_digest", "current_time", "tier_info"]},
        )

        floating_pos = result.find("Pythonの話")
        # session_digest が存在する
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
