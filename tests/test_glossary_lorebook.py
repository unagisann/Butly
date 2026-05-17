"""
test_glossary_lorebook.py
-------------------------
Glossary + Lorebook 統合のテスト。

検証範囲:
  - is_long_definition の境界
  - _match_glossary の履歴スキャン (scan_depth / scan_target)
  - 重複 dedupe (user_input 優先)
  - _build_glossary の短文/長文分割
  - (priority, _yaml_index) での安定ソート
  - max_entries の打ち切り
  - max_chars の greedy skip
"""

import pytest
from unittest.mock import MagicMock

from butly_core.core.gatekeeper.memory_probe import MemoryProbe
from butly_core.core.gatekeeper.memory_builder import (
    is_long_definition,
    _build_glossary,
    _split_short_long,
    _sort_hits,
    _apply_caps,
)


# ===================================================================
# is_long_definition
# ===================================================================

class TestIsLongDefinition:
    @pytest.mark.parametrize("text", [
        "",
        "   ",
        "\n",
        "\n\n",
        "単一行の定義",
        "Single line definition",
        # YAML "|" 末尾の改行のみ → 単一行扱い
        "単一行ですが末尾改行あり\n",
        "   trailing whitespace and newline \n   ",
    ])
    def test_short(self, text):
        assert is_long_definition(text) is False

    @pytest.mark.parametrize("text", [
        "1行目\n2行目",
        "  1行目\n2行目\n3行目  ",
        "preface\nbody",
        # 中間に改行を含む YAML "|" 形式
        "魔法学院\n王都北区にある国立教育機関。\n",
    ])
    def test_long(self, text):
        assert is_long_definition(text) is True


# ===================================================================
# _match_glossary: 履歴スキャン & dedupe
# ===================================================================

def _build_memory_with_entries(entries):
    mm = MagicMock()
    mm.get_glossary_raw.return_value = {"version": 2, "entries": entries}
    return mm


@pytest.fixture
def probe():
    return MemoryProbe()


@pytest.fixture
def memory_basic():
    entries = [
        {
            "term": "Gatekeeper",
            "definition": "入力分類システム",
            "aliases": ["GK"],
            "status": "active",
            "priority": 100,
        },
        {
            "term": "魔法学院",
            "definition": "王都北区にある国立教育機関。\n資格認定を行う。",
            "aliases": ["学院"],
            "status": "active",
            "priority": 50,
        },
        {
            "term": "RAG",
            "definition": "Retrieval-Augmented Generation",
            "aliases": [],
            "status": "active",
            "priority": 100,
        },
    ]
    return _build_memory_with_entries(entries)


class TestMatchGlossaryWithHistory:
    """履歴スキャン (scan_depth / scan_target) のテスト"""

    def test_user_input_only_default_when_no_history(self, probe, memory_basic):
        """履歴なし → user_input のみスキャン"""
        hits = probe._match_glossary("Gatekeeperの話", memory_basic, history_msgs=None)
        assert len(hits) == 1
        assert hits[0]["term"] == "Gatekeeper"
        assert hits[0]["match_source"] == "user"
        assert hits[0]["_yaml_index"] == 0
        assert hits[0]["priority"] == 100

    def test_history_user_message_hits(self, probe, memory_basic):
        """直近 user メッセージでヒット (scan_target=both)"""
        history = [
            {"role": "user", "parts": ["昨日RAGについて話したよね"]},
            {"role": "model", "parts": ["はい、Retrieval-Augmented Generationですね"]},
        ]
        hits = probe._match_glossary("で、続きだけど", memory_basic, history_msgs=history)
        terms = {h["term"] for h in hits}
        assert "RAG" in terms

    def test_history_assistant_message_hits_when_both(self, probe, memory_basic):
        """assistant 発言だけにあるキーワードもヒット (scan_target=both)"""
        history = [
            {"role": "user", "parts": ["説明して"]},
            {"role": "model", "parts": ["魔法学院は国立の教育機関です"]},
        ]
        hits = probe._match_glossary("続けて", memory_basic, history_msgs=history)
        terms = {h["term"] for h in hits}
        assert "魔法学院" in terms

    def test_scan_target_user_only(self, probe, memory_basic):
        """scan_target='user' → assistant 発言は無視"""
        history = [
            {"role": "user", "parts": ["普通の話"]},
            {"role": "model", "parts": ["魔法学院について"]},
        ]
        override = {"glossary": {"scan_target": "user"}}
        hits = probe._match_glossary(
            "続けて", memory_basic, history_msgs=history, override_config=override
        )
        terms = {h["term"] for h in hits}
        assert "魔法学院" not in terms

    def test_scan_target_assistant_only(self, probe, memory_basic):
        """scan_target='assistant' → user 発言は無視（user_input は別ソース扱いなので残る）"""
        history = [
            {"role": "user", "parts": ["Gatekeeperを変更"]},
            {"role": "model", "parts": ["魔法学院を変更"]},
        ]
        override = {"glossary": {"scan_target": "assistant"}}
        hits = probe._match_glossary(
            "普通の話", memory_basic, history_msgs=history, override_config=override
        )
        terms = {h["term"] for h in hits}
        # 履歴の user 側 (Gatekeeper) は無視。assistant 側 (魔法学院) のみ
        assert "Gatekeeper" not in terms
        assert "魔法学院" in terms

    def test_scan_depth_zero(self, probe, memory_basic):
        """scan_depth=0 → 履歴を一切見ない"""
        history = [
            {"role": "user", "parts": ["Gatekeeperの話"]},
            {"role": "model", "parts": ["RAGの話"]},
        ]
        override = {"glossary": {"scan_depth": 0}}
        hits = probe._match_glossary(
            "普通の話", memory_basic, history_msgs=history, override_config=override
        )
        assert hits == []

    def test_scan_depth_one_includes_only_last_pair(self, probe, memory_basic):
        """scan_depth=1 → 直近 1 ペア (= 最大 2 メッセージ) のみ"""
        history = [
            {"role": "user", "parts": ["Gatekeeperの話"]},     # 古い (範囲外)
            {"role": "model", "parts": ["はい"]},               # 古い (範囲外)
            {"role": "user", "parts": ["別の話"]},              # 範囲内
            {"role": "model", "parts": ["RAGの話"]},            # 範囲内
        ]
        override = {"glossary": {"scan_depth": 1, "scan_target": "both"}}
        hits = probe._match_glossary(
            "続き", memory_basic, history_msgs=history, override_config=override
        )
        terms = {h["term"] for h in hits}
        assert "RAG" in terms
        assert "Gatekeeper" not in terms

    def test_dedupe_user_priority_over_history(self, probe, memory_basic):
        """同じ term が user_input と履歴の両方にある場合 → 1 件、match_source は user"""
        history = [
            {"role": "user", "parts": ["前にGatekeeperの話"]},
        ]
        hits = probe._match_glossary("Gatekeeperの続き", memory_basic, history_msgs=history)
        gk_hits = [h for h in hits if h["term"] == "Gatekeeper"]
        assert len(gk_hits) == 1
        assert gk_hits[0]["match_source"] == "user"


# ===================================================================
# _match_glossary: priority / _yaml_index 付与
# ===================================================================

class TestMatchGlossaryMetadata:
    def test_priority_default_100(self, probe):
        mm = _build_memory_with_entries([
            {"term": "Foo", "definition": "ふー", "aliases": [], "status": "active"},
        ])
        hits = probe._match_glossary("Foo", mm)
        assert hits[0]["priority"] == 100

    def test_priority_custom(self, probe):
        mm = _build_memory_with_entries([
            {"term": "Foo", "definition": "ふー", "aliases": [], "status": "active", "priority": 10},
        ])
        hits = probe._match_glossary("Foo", mm)
        assert hits[0]["priority"] == 10

    def test_yaml_index_preserved(self, probe):
        mm = _build_memory_with_entries([
            {"term": "A", "definition": "a", "aliases": [], "status": "active"},
            {"term": "B", "definition": "b", "aliases": [], "status": "active"},
            {"term": "C", "definition": "c", "aliases": [], "status": "active"},
        ])
        hits = probe._match_glossary("A B C", mm)
        by_term = {h["term"]: h["_yaml_index"] for h in hits}
        assert by_term["A"] == 0
        assert by_term["B"] == 1
        assert by_term["C"] == 2


# ===================================================================
# _build_glossary 関連ユーティリティ
# ===================================================================

class TestSplitShortLong:
    def test_split(self):
        hits = [
            {"term": "Short1", "definition": "短い定義"},
            {"term": "Long1", "definition": "長い\n定義"},
            {"term": "Short2", "definition": "もう一つ短い"},
            {"term": "Long2", "definition": "もう一つ\n長い"},
        ]
        short, long_ = _split_short_long(hits)
        assert [h["term"] for h in short] == ["Short1", "Short2"]
        assert [h["term"] for h in long_] == ["Long1", "Long2"]


class TestSortHits:
    def test_priority_asc(self):
        hits = [
            {"term": "C", "priority": 100, "_yaml_index": 2},
            {"term": "A", "priority": 10,  "_yaml_index": 0},
            {"term": "B", "priority": 50,  "_yaml_index": 1},
        ]
        out = _sort_hits(hits)
        assert [h["term"] for h in out] == ["A", "B", "C"]

    def test_same_priority_falls_back_to_yaml_index(self):
        hits = [
            {"term": "C", "priority": 100, "_yaml_index": 2},
            {"term": "A", "priority": 100, "_yaml_index": 0},
            {"term": "B", "priority": 100, "_yaml_index": 1},
        ]
        out = _sort_hits(hits)
        assert [h["term"] for h in out] == ["A", "B", "C"]

    def test_missing_keys_default(self):
        hits = [
            {"term": "X"},                              # priority=100, idx=0
            {"term": "Y", "priority": 50},              # priority=50,  idx=0
        ]
        out = _sort_hits(hits)
        assert [h["term"] for h in out] == ["Y", "X"]


class TestApplyCaps:
    def test_max_entries_cuts_tail(self):
        hits = [
            {"term": f"T{i}", "definition": "x"} for i in range(5)
        ]
        out = _apply_caps(hits, max_entries=3, max_chars=1000)
        assert len(out) == 3
        assert [h["term"] for h in out] == ["T0", "T1", "T2"]

    def test_max_chars_greedy_skip_lets_smaller_through(self):
        """合計 max_chars を超えるエントリは個別スキップ。後続の小さいエントリは通過する"""
        hits = [
            {"term": "small1", "definition": "abc"},        # 3
            {"term": "huge",   "definition": "x" * 50},     # 50 → スキップされる
            {"term": "small2", "definition": "de"},         # 2
        ]
        out = _apply_caps(hits, max_entries=10, max_chars=10)
        terms = [h["term"] for h in out]
        assert "small1" in terms
        assert "small2" in terms
        assert "huge" not in terms

    def test_strip_used_for_cost(self):
        """定義の前後空白は cost から除外（strip 後の長さで計上）"""
        hits = [
            {"term": "spaced", "definition": "   ab   "},  # strip → 2
        ]
        out = _apply_caps(hits, max_entries=10, max_chars=2)
        assert len(out) == 1


# ===================================================================
# _build_glossary 結合テスト
# ===================================================================

def _fake_h(headers: dict):
    """get_section_header 互換のフェイク関数を作る"""
    def _h(key):
        return headers.get(key, key)
    return _h


class TestBuildGlossaryRendering:
    def test_off_returns_none(self):
        blocks = {"glossary_hits": [
            {"term": "X", "definition": "x", "priority": 100, "_yaml_index": 0}
        ]}
        h = _fake_h({"glossary": "=== G ===", "note_glossary": "note"})
        assert _build_glossary(blocks, memory_manager=None, level="off", h=h) is None

    def test_low_returns_none(self):
        """既存挙動を維持: low は注入しない"""
        blocks = {"glossary_hits": [
            {"term": "X", "definition": "x", "priority": 100, "_yaml_index": 0}
        ]}
        h = _fake_h({"glossary": "=== G ===", "note_glossary": "note"})
        assert _build_glossary(blocks, memory_manager=None, level="low", h=h) is None

    def test_short_only(self):
        blocks = {"glossary_hits": [
            {"term": "Gatekeeper", "definition": "入力分類", "priority": 100, "_yaml_index": 0},
            {"term": "RAG", "definition": "検索拡張", "priority": 100, "_yaml_index": 1},
        ]}
        h = _fake_h({
            "glossary": "=== GLOSSARY ===",
            "note_glossary": "ノート",
            "glossary_short_label": "用語説明:",
            "glossary_long_label": "関連設定:",
        })
        out = _build_glossary(blocks, memory_manager=None, level="high", h=h)
        assert "=== GLOSSARY ===" in out
        assert "用語説明:" in out
        assert "- Gatekeeper: 入力分類" in out
        assert "- RAG: 検索拡張" in out
        assert "関連設定:" not in out  # 長文なし

    def test_long_only(self):
        blocks = {"glossary_hits": [
            {"term": "魔法学院", "definition": "王都北区にある。\n資格認定を行う。",
             "priority": 50, "_yaml_index": 0},
        ]}
        h = _fake_h({
            "glossary": "=== GLOSSARY ===",
            "note_glossary": "ノート",
            "glossary_short_label": "用語説明:",
            "glossary_long_label": "関連設定:",
        })
        out = _build_glossary(blocks, memory_manager=None, level="high", h=h)
        assert "関連設定:" in out
        assert "### 魔法学院" in out
        assert "王都北区にある。" in out
        assert "用語説明:" not in out  # 短文なし

    def test_short_before_long_ordering(self):
        """短文セクションが長文セクションより前に出る"""
        blocks = {"glossary_hits": [
            {"term": "魔法学院", "definition": "長い\n定義", "priority": 50, "_yaml_index": 0},
            {"term": "GK", "definition": "短い定義", "priority": 100, "_yaml_index": 1},
        ]}
        h = _fake_h({
            "glossary": "=== GLOSSARY ===",
            "note_glossary": "ノート",
            "glossary_short_label": "用語説明:",
            "glossary_long_label": "関連設定:",
        })
        out = _build_glossary(blocks, memory_manager=None, level="high", h=h)
        idx_short = out.index("用語説明:")
        idx_long = out.index("関連設定:")
        assert idx_short < idx_long

    def test_priority_orders_within_section(self):
        blocks = {"glossary_hits": [
            {"term": "Z", "definition": "z", "priority": 200, "_yaml_index": 0},
            {"term": "A", "definition": "a", "priority": 10,  "_yaml_index": 1},
            {"term": "M", "definition": "m", "priority": 100, "_yaml_index": 2},
        ]}
        h = _fake_h({
            "glossary": "=== GLOSSARY ===",
            "note_glossary": "ノート",
            "glossary_short_label": "用語説明:",
            "glossary_long_label": "関連設定:",
        })
        out = _build_glossary(blocks, memory_manager=None, level="high", h=h)
        idx_a = out.index("- A:")
        idx_m = out.index("- M:")
        idx_z = out.index("- Z:")
        assert idx_a < idx_m < idx_z

    def test_max_entries_via_glossary_config(self):
        blocks = {
            "glossary_hits": [
                {"term": f"T{i}", "definition": str(i), "priority": 100, "_yaml_index": i}
                for i in range(5)
            ],
            "glossary_config": {"max_entries": 2, "max_chars": 1000},
        }
        h = _fake_h({
            "glossary": "=== GLOSSARY ===",
            "note_glossary": "ノート",
            "glossary_short_label": "用語説明:",
            "glossary_long_label": "関連設定:",
        })
        out = _build_glossary(blocks, memory_manager=None, level="high", h=h)
        assert "T0" in out
        assert "T1" in out
        assert "T2" not in out

    def test_empty_hits_falls_back_to_memory_manager_when_probe_not_ran(self):
        """probe が走らなかった場合 (例: Gatekeeper 無効) は全件 fallback"""
        blocks = {"glossary_hits": []}  # _probe_ran フラグなし
        mm = MagicMock()
        mm.get_glossary.return_value = "- foo: bar"
        h = _fake_h({"glossary": "=== G ===", "note_glossary": "note"})
        out = _build_glossary(blocks, memory_manager=mm, level="high", h=h)
        assert "- foo: bar" in out

    def test_empty_hits_with_probe_ran_returns_none(self):
        """probe が走った上で hits 0 → 全件 fallback せず None を返す"""
        blocks = {"glossary_hits": [], "_probe_ran": True}
        mm = MagicMock()
        mm.get_glossary.return_value = "- foo: bar"
        h = _fake_h({"glossary": "=== G ===", "note_glossary": "note"})
        out = _build_glossary(blocks, memory_manager=mm, level="high", h=h)
        assert out is None
        # memory_manager.get_glossary は呼ばれない
        mm.get_glossary.assert_not_called()

    def test_empty_hits_no_memory_returns_none(self):
        blocks = {"glossary_hits": []}
        h = _fake_h({})
        assert _build_glossary(blocks, memory_manager=None, level="high", h=h) is None
