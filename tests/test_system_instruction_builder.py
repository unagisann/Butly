"""
test_system_instruction_builder.py
-----------------------------------
build_system_instruction_from_blocks() および build_context_prefix() のユニットテスト。

変更後の設計:
  - build_system_instruction_from_blocks(): 不変セクションのみ（SYSTEM INSTRUCTION, KEY MEMORY）
  - build_context_prefix(): 可変セクション（ラベル, CURRENT TIME, MID-TERM, RAG, SESSION DIGEST, TIER INFO）

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


class TestAgentProfileInjection:
    """Agent Profile が SI 先頭に注入されるかを検証"""

    def _write_config(self, test_instance_dir, cfg):
        import json
        (test_instance_dir / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )

    def test_si_contains_agent_name(self, memory_manager, test_instance_dir):
        """config に agent_profile.ai_name があると SI に注入される"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "アドラス", "locale": "ja"},
            "user_profile": {"user_name": "悠希"},
        })
        blocks = {"tier": "reflex", "short_term": [], "session_digest": "", "mid_term": "", "rag_context": ""}
        result = build_system_instruction_from_blocks(blocks, memory_manager)
        assert "アドラス" in result
        assert "# Agent Profile" in result

    def test_si_agent_profile_before_body(self, memory_manager, test_instance_dir):
        """Agent Profile は本文（Identity 等）より前に配置される"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "locale": "en"},
            "user_profile": {},
        })
        blocks = {"tier": "reflex", "short_term": [], "session_digest": "", "mid_term": "", "rag_context": ""}
        result = build_system_instruction_from_blocks(blocks, memory_manager)
        idx_ap = result.index("# Agent Profile")
        idx_si = result.index("=== SYSTEM INSTRUCTION ===")
        # SI header is above body, Agent Profile sits between header and body
        assert idx_si < idx_ap

    def test_si_omits_empty_agent_profile(self, memory_manager, test_instance_dir):
        """ai_name が空なら Agent Profile ブロック全体が出ない"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "", "locale": "ja"},
            "user_profile": {},
        })
        blocks = {"tier": "reflex", "short_term": [], "session_digest": "", "mid_term": "", "rag_context": ""}
        result = build_system_instruction_from_blocks(blocks, memory_manager)
        assert "# Agent Profile" not in result

    def test_si_excludes_user_profile(self, memory_manager, test_instance_dir):
        """User Profile ブロック（セクションヘッダと構造化ラベル）が SI セクションに含まれない

        ※ build_system_instruction_from_blocks() の戻り値は SI + KM 連結。
        User Profile は KM 側に属するブロックなので、SI セクション部分のみで不在を判定する。
        """
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "アドラス", "locale": "ja"},
            "user_profile": {
                "user_name": "悠希",
                "preferred_call": "マスター",
                "gender": "male",
            },
        })
        blocks = {"tier": "reflex", "short_term": [], "session_digest": "", "mid_term": "", "rag_context": ""}
        result = build_system_instruction_from_blocks(blocks, memory_manager)

        # SI セクションだけを切り出す（KEY MEMORY 以降は KM ブロック）
        si_end = result.find("=== CORE MEMORY")
        si_section = result if si_end == -1 else result[:si_end]

        # ブロック単位で判定: セクションヘッダ + 構造化ラベルの不在を確認
        assert "=== User Profile ===" not in si_section
        assert "- Preferred call:" not in si_section
        assert "- Name:" not in si_section

    def test_si_ai_gender_when_set(self, memory_manager, test_instance_dir):
        """ai_gender が設定されていれば Gender 行が出る"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "ai_gender": "neutral", "locale": "en"},
            "user_profile": {},
        })
        blocks = {"tier": "reflex", "short_term": [], "session_digest": "", "mid_term": "", "rag_context": ""}
        result = build_system_instruction_from_blocks(blocks, memory_manager)
        assert "Gender: neutral" in result

    def test_si_omits_empty_ai_gender(self, memory_manager, test_instance_dir):
        """ai_gender が空なら Gender 行は出ない"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "ai_gender": "", "locale": "en"},
            "user_profile": {},
        })
        blocks = {"tier": "reflex", "short_term": [], "session_digest": "", "mid_term": "", "rag_context": ""}
        result = build_system_instruction_from_blocks(blocks, memory_manager)
        assert "Gender:" not in result


class TestKeyMemoryUserProfileInjection:
    """User Profile が KM に注入されるかを検証"""

    def _write_config(self, test_instance_dir, cfg):
        import json
        (test_instance_dir / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )

    def test_km_contains_user_profile(self, memory_manager, test_instance_dir):
        """get_key_memory() の戻り値に User Profile ブロックが含まれる"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "locale": "ja"},
            "user_profile": {"user_name": "悠希", "preferred_call": "マスター"},
        })
        result = memory_manager.get_key_memory()
        assert "=== User Profile ===" in result
        assert "- Name: 悠希" in result
        assert "- Preferred call: マスター" in result

    def test_km_omits_empty_location(self, memory_manager, test_instance_dir):
        """location が空なら Location 行は出ない"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "locale": "en"},
            "user_profile": {"user_name": "Taro", "location": ""},
        })
        result = memory_manager.get_key_memory()
        assert "Location:" not in result

    def test_km_core_memories_subheader(self, memory_manager, test_instance_dir):
        """User Profile と本文の両方がある場合、Core Memories サブヘッダが付く"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "locale": "en"},
            "user_profile": {"user_name": "Taro"},
        })
        result = memory_manager.get_key_memory()
        assert "=== Core Memories ===" in result


class TestCustomProfileFields:
    """カスタムフィールドが SI / KM に反映されるかを検証"""

    def _write_config(self, test_instance_dir, cfg):
        import json
        (test_instance_dir / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )

    def test_agent_custom_field_in_header(self, memory_manager, test_instance_dir):
        """agent_profile のカスタムフィールドが SI に反映される"""
        self._write_config(test_instance_dir, {
            "agent_profile": {
                "ai_name": "ジャービス",
                "ai_gender": "男性",
                "locale": "ja",
                "ai_hobby": "意味のない会話",
            },
            "user_profile": {},
        })
        header = memory_manager.get_agent_profile_header()
        assert "Ai Hobby: 意味のない会話" in header

    def test_agent_custom_field_in_dict(self, memory_manager, test_instance_dir):
        """get_agent_profile() がカスタムフィールドを含む"""
        self._write_config(test_instance_dir, {
            "agent_profile": {
                "ai_name": "ジャービス",
                "locale": "ja",
                "favorite_topic": "宇宙",
            },
            "user_profile": {},
        })
        profile = memory_manager.get_agent_profile()
        assert profile["favorite_topic"] == "宇宙"

    def test_user_custom_field_in_header(self, memory_manager, test_instance_dir):
        """user_profile のカスタムフィールドが KM に反映される"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "locale": "en"},
            "user_profile": {
                "user_name": "悠希",
                "occupation": "エンジニア",
            },
        })
        header = memory_manager.get_user_profile_header()
        assert "- Occupation: エンジニア" in header

    def test_user_custom_field_in_dict(self, memory_manager, test_instance_dir):
        """get_user_profile() がカスタムフィールドを含む"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "locale": "en"},
            "user_profile": {
                "user_name": "悠希",
                "hobby": "ゲーム",
            },
        })
        profile = memory_manager.get_user_profile()
        assert profile["hobby"] == "ゲーム"

    def test_empty_custom_field_omitted(self, memory_manager, test_instance_dir):
        """空のカスタムフィールドはヘッダに出力されない"""
        self._write_config(test_instance_dir, {
            "agent_profile": {"ai_name": "Atlas", "locale": "en", "motto": ""},
            "user_profile": {"user_name": "悠希", "hobby": ""},
        })
        agent_header = memory_manager.get_agent_profile_header()
        user_header = memory_manager.get_user_profile_header()
        assert "Motto:" not in agent_header
        assert "Hobby:" not in user_header


class TestLegacyAgentFallback:
    """旧 agent セクションからのフォールバック"""

    def test_legacy_agent_migrates_in_memory(self, memory_manager, test_instance_dir):
        """config.json に旧 agent のみの場合でも agent_profile/user_profile が取得できる"""
        import json
        (test_instance_dir / "config.json").write_text(
            json.dumps({
                "agent": {
                    "ai_name": "レガシー",
                    "user_name": "旧ユーザー",
                    "nickname": "旧呼称",
                    "gender": "男性",
                    "birthday": "1990/01/01",
                    "locale": "ja",
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        ap = memory_manager.get_agent_profile()
        up = memory_manager.get_user_profile()
        assert ap["ai_name"] == "レガシー"
        assert up["user_name"] == "旧ユーザー"
        assert up["preferred_call"] == "旧呼称"
        assert up["gender"] == "男性"
        assert up["birthday"] == "1990/01/01"


class TestReflexInstruction:
    """reflex tier の system_instruction テスト"""

    def test_contains_system_instruction(self, memory_manager):
        """SYSTEM INSTRUCTION セクションが含まれる"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
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
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== CORE MEMORY" in result

    def test_no_tier_info(self, memory_manager):
        """system_instruction に TIER INFO が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "[実行モード]" not in result

    def test_no_current_time(self, memory_manager):
        """system_instruction に CURRENT TIME が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "[現在時刻]" not in result

    def test_no_mid_term_section(self, memory_manager):
        """system_instruction に MID-TERM セクションがない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "[中期記憶]" not in result
        assert "[中期要約]" not in result

    def test_no_rag_section(self, memory_manager):
        """system_instruction に RAG セクションがない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== RETRIEVED MEMORY" not in result

    def test_no_session_digest_section(self, memory_manager):
        """system_instruction に SESSION DIGEST が含まれない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "テスト要約",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "[以前のセッション要約]" not in result


class TestMidInstruction:
    """mid tier の system_instruction テスト"""

    def test_no_mid_term_in_system_instruction(self, memory_manager):
        """mid tier でも system_instruction に MID-TERM が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "昨日の会話ログ",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "[中期記憶]" not in result
        assert "昨日の会話ログ" not in result

    def test_no_rag_section(self, memory_manager):
        """mid では RAG セクションがない"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "テスト",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== RETRIEVED MEMORY" not in result


class TestRAGInstruction:
    """RAG 付き tier の system_instruction テスト"""

    def test_no_rag_in_system_instruction(self, memory_manager):
        """<<system_instruction に RAG が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "中期記憶のテキスト",
            "mid_term_mode": "raw",
            "rag_context": "【過去の記憶（RAG）】\n・テスト: テストデータ",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== RETRIEVED MEMORY (検索された記憶) ===" not in result
        assert "テストデータ" not in result

    def test_no_tier_info(self, memory_manager):
        """system_instruction に TIER INFO が含まれない（context_prefix に移動済み）"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "[実行モード]" not in result


class TestSystemInstructionOrder:
    """system_instruction のセクション順序テスト"""

    def test_instruction_order(self, memory_manager):
        """system_instruction の各セクションが正しい順序で並ぶ"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "会話圧縮ログテスト",
            "mid_term": "中期記憶テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAGテスト",
        }

        result = build_system_instruction_from_blocks(blocks, memory_manager)

        idx_sys = result.index("=== SYSTEM INSTRUCTION ===")
        idx_key = result.index("=== CORE MEMORY")

        assert idx_sys < idx_key
        # TIER INFO は context_prefix に移動済み
        assert "[実行モード]" not in result


# ==================================================================
# build_context_prefix() テスト（可変セクション）
# ==================================================================


class TestContextPrefixCurrentTime:
    """context_prefix: CURRENT TIME テスト"""

    @pytest.mark.parametrize("tier", ["reflex", "mid"])
    def test_contains_current_time(self, tier, memory_manager):
        """全 tier で CURRENT TIME が含まれる"""
        blocks = {
            "tier": tier,
            "short_term": [],
            "session_digest": "",
            "mid_term": "" if tier == "reflex" else "mid",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[現在時刻]" in result


class TestContextPrefixMidTerm:
    """context_prefix: MID-TERM テスト"""

    def test_mid_tier_includes_mid_term_raw(self, memory_manager):
        """mid（RAWモード）: MID-TERM MEMORY が context_prefix に含まれる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "昨日の会話ログがここに入ります。",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[中期記憶]" in result
        assert "昨日の会話ログ" in result

    def test_mid_tier_includes_digest_and_recent_snapshot(self, memory_manager):
        """mid（要約モード）: DIGEST + RECENT SNAPSHOT が context_prefix に含まれる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "mid_term_mode": "summary",
            "mid_term_digest": "[2026-03-21] テスト\n- pytest導入",
            "mid_term_recent_snapshot": "# トーン\n- 集中モード",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[中期要約]" in result
        assert "pytest導入" in result
        assert "[近況スナップショット]" in result
        assert "集中モード" in result

    def test_reflex_no_mid_term(self, memory_manager):
        """reflex では MID-TERM が context_prefix に含まれない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "このテキストは見えないはず",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[中期記憶]" not in result


class TestContextPrefixRAG:
    """context_prefix: RAG テスト"""

    def test_need_includes_rag(self, memory_manager):
        """need 有り + rag_context がある場合: LONG-TERM MEMORY (RAG) が context_prefix に含まれる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "中期記憶のテキスト",
            "mid_term_mode": "raw",
            "rag_context": "【過去の記憶（RAG）】\n・テスト: テストデータ",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== RETRIEVED MEMORY (検索された記憶) ===" in result
        assert "テストデータ" in result

    def test_rag_has_reference_annotation(self, memory_manager):
        """RAG セクションに「直近の会話を優先」の注釈がある"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "mid_term_mode": "raw",
            "rag_context": "テスト記憶",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert ("直近の会話を優先" in result
                or "prioritize recent conversation" in result)

    def test_no_rag_without_rag_context(self, memory_manager):
        """では RAG が context_prefix に含まれない"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "テスト",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "=== RETRIEVED MEMORY" not in result


class TestContextPrefixSessionDigest:
    """context_prefix: SESSION DIGEST テスト"""

    @pytest.mark.parametrize("tier", ["reflex", "mid"])
    def test_session_digest_included_when_present(self, tier, memory_manager):
        """session_digest がある場合、全 tier で context_prefix に注入される"""
        blocks = {
            "tier": tier,
            "short_term": [],
            "session_digest": "直前の会話で天気の話をしました。",
            "mid_term": "mid" if tier != "reflex" else "",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[以前のセッション要約]" in result
        assert "天気の話" in result

    @pytest.mark.parametrize("tier", ["reflex", "mid"])
    def test_session_digest_omitted_when_empty(self, tier, memory_manager):
        """session_digest が空の場合、セクション自体が省略される"""
        blocks = {
            "tier": tier,
            "short_term": [],
            "session_digest": "",
            "mid_term": "" if tier == "reflex" else "mid",
            "mid_term_mode": "raw",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[以前のセッション要約]" not in result


class TestContextPrefixSectionOrder:
    """context_prefix のセクション注入順序テスト"""

    def test_section_order(self, memory_manager):
        """context_prefix の各セクションが正しい順序で並ぶ"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "会話圧縮ログテスト",
            "mid_term": "中期記憶テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAGテスト",
        }

        result = build_context_prefix(blocks, memory_manager)

        idx_time = result.index("[現在時刻]")
        idx_mid = result.index("[中期記憶]")
        idx_rag = result.index("=== RETRIEVED MEMORY")
        idx_session_digest = result.index("[以前のセッション要約]")
        idx_tier = result.index("[実行モード]")

        assert idx_time < idx_mid < idx_rag < idx_session_digest < idx_tier

    def test_starts_with_label(self, memory_manager):
        """context_prefix はラベル行で始まる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)
        first_line = result.strip().split("\n")[0]

        assert first_line == "=== CONTEXT (文脈) ==="

    def test_omits_global_priority_note(self, memory_manager):
        """context_prefix は記憶レイヤー間の固定優先順位を指示しない"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "優先順位" not in result
        assert "Priority order" not in result

    def test_contains_tier_info(self, memory_manager):
        """context_prefix に TIER INFO が含まれる"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[実行モード]" in result


class TestContextPrefixReflex:
    """reflex tier は最小限のコンテキストのみ"""

    def test_reflex_has_current_time(self, memory_manager):
        """reflex でも CURRENT TIME は含まれる"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[現在時刻]" in result

    def test_reflex_no_mid_term_but_rag_allowed(self, memory_manager):
        """reflex では MID-TERM は含まれないが、RAG は need 連動で tier 非依存

        rag_context は need が立ったときだけ MemoryBlockBuilder が構築する。
        reflex でも構築済みなら描画する（docs の tier×ブロック行列どおり）。
        """
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "テスト",
            "mid_term_mode": "raw",
            "rag_context": "RAG",
        }

        result = build_context_prefix(blocks, memory_manager)

        assert "[中期記憶]" not in result
        assert "=== RETRIEVED MEMORY" in result


# ==================================================================
# Glossary in context_prefix テスト
# ==================================================================


class TestContextPrefixGlossary:
    """Glossary が context_prefix に正しく注入されることを検証"""

    @pytest.fixture(autouse=True)
    def setup_glossary(self, test_instance_dir):
        """テスト用 glossary.yaml を配置"""
        import yaml
        glossary_data = {
            "version": 1,
            "entries": [
                {"term": "TestTerm", "definition": "テスト用の用語", "aliases": [], "category": "system", "status": "active"},
                {"term": "ArchivedTerm", "definition": "アーカイブ済み", "aliases": [], "category": "system", "status": "archived"},
            ],
        }
        glossary_file = test_instance_dir / "glossary.yaml"
        with open(glossary_file, "w", encoding="utf-8") as f:
            yaml.dump(glossary_data, f, allow_unicode=True, default_flow_style=False)

    def test_glossary_in_reflex(self, memory_manager):
        """reflex でも SHARED TERMS が注入される"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }
        result = build_context_prefix(blocks, memory_manager)
        assert "SHARED TERMS" in result
        assert "TestTerm" in result

    def test_glossary_excludes_archived(self, memory_manager):
        """archived ステータスのエントリは注入されない"""
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }
        result = build_context_prefix(blocks, memory_manager)
        assert "ArchivedTerm" not in result

    def test_glossary_before_mid_term(self, memory_manager):
        """SHARED TERMS が MID-TERM より前に配置される"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "中期記憶テスト",
            "mid_term_mode": "raw",
            "rag_context": "",
        }
        result = build_context_prefix(blocks, memory_manager)

        idx_glossary = result.index("SHARED TERMS")
        idx_mid = result.index("[中期記憶]")
        assert idx_glossary < idx_mid

    def test_glossary_after_current_time(self, memory_manager):
        """SHARED TERMS が CURRENT TIME より後に配置される"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "中期記憶テスト",
            "mid_term_mode": "raw",
            "rag_context": "",
        }
        result = build_context_prefix(blocks, memory_manager)

        idx_time = result.index("[現在時刻]")
        idx_glossary = result.index("SHARED TERMS")
        assert idx_time < idx_glossary

    def test_no_glossary_without_file(self, base_dir, test_instance_dir):
        """glossary.yaml がない場合は SHARED TERMS セクションが出ない"""
        import os
        glossary_file = test_instance_dir / "glossary.yaml"
        if glossary_file.exists():
            os.remove(glossary_file)

        from butly_core.core.memory import ButlyMemory
        mem = ButlyMemory(base_dir, instance_name="test_instance")
        blocks = {
            "tier": "reflex",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }
        result = build_context_prefix(blocks, mem)
        assert "SHARED TERMS" not in result

    def test_glossary_not_in_system_instruction(self, memory_manager):
        """system_instruction には SHARED TERMS が含まれない"""
        blocks = {
            "tier": "mid",
            "short_term": [],
            "session_digest": "",
            "mid_term": "",
            "rag_context": "",
        }
        result = build_system_instruction_from_blocks(blocks, memory_manager)
        assert "SHARED TERMS" not in result
