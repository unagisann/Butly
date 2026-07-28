"""
test_card_field_coercion.py
───────────────────────────
LLM がカードの summary / tags を配列で返したときに、カードを落とさず保存する。

実害の記録: ja_dialogue_ab_v1 の記憶生成で、10 チャンクすべてがカードを1枚ずつ
生成したのに 9 枚が `Error binding parameter 8: type 'list' is not supported` で
消えた（保存できたのは summary を文字列で返した1枚だけ）。RAW は処理済みへ
移動されるため、その回のカードは復元されない。
"""

import json
import sqlite3

import pytest

from butly_core.core.card_content import (
    coerce_card_fields,
    coerce_card_text,
    compute_content_hash,
)
from butly_core.core.database import ButlyDatabase


class TestCoerceCardText:
    def test_list_becomes_bulleted_lines(self):
        text = coerce_card_text(["妹のあおいはミステリー好き", "誕生日は9月14日"])
        assert text == "- 妹のあおいはミステリー好き\n- 誕生日は9月14日"

    def test_existing_bullets_are_kept(self):
        assert coerce_card_text(["- すでに箇条書き", "・中黒も"]) == (
            "- すでに箇条書き\n・中黒も"
        )

    def test_tags_join_with_comma(self):
        assert coerce_card_text(["雑談", "人物情報"], separator=", ") == "雑談, 人物情報"

    def test_empty_items_are_dropped(self):
        assert coerce_card_text(["a", "", "  ", "b"]) == "- a\n- b"

    def test_str_passthrough_and_none(self):
        assert coerce_card_text("そのまま") == "そのまま"
        assert coerce_card_text(None) == ""

    def test_mapping_becomes_json(self):
        assert coerce_card_text({"k": "値"}) == '{"k": "値"}'

    def test_coerce_card_fields_normalizes_only_text_fields(self):
        card = {
            "title": "陶芸教室",
            "summary": ["藍色のマグカップを作った"],
            "tags": ["趣味", "制作"],
            "ai_importance": 5,
        }

        normalized = coerce_card_fields(card)

        assert normalized["summary"] == "- 藍色のマグカップを作った"
        assert normalized["tags"] == "趣味, 制作"
        assert normalized["ai_importance"] == 5
        assert card["summary"] == ["藍色のマグカップを作った"]  # 元 dict は不変

    def test_content_hash_is_computable_after_coercion(self):
        """hash 計算は str 化するので、配列のままでも例外にはならないが、
        保存値と hash 対象を一致させるため coerce 後の値で計算する。"""
        card = coerce_card_fields({"title": "t", "summary": ["a", "b"], "tags": []})
        assert compute_content_hash(card) == compute_content_hash(
            {"title": "t", "summary": "- a\n- b", "tags": ""}
        )


class TestSleeptimeInsertWithListFields:
    @pytest.fixture
    def sleeptime(self, tmp_path, monkeypatch):
        from sleeptime import ButlySleeptime

        instances_dir = tmp_path / "butly_core" / "instances"
        (instances_dir / "test_inst").mkdir(parents=True)
        runner = ButlySleeptime(base_dir=tmp_path, instances_dir=instances_dir)
        monkeypatch.setattr(
            runner, "generate_embedding", lambda text, instance_name=None: [0.1, 0.2]
        )
        return runner

    def test_list_summary_is_stored_instead_of_dropped(self, tmp_path, sleeptime):
        db_path = tmp_path / "butly_core" / "instances" / "test_inst" / "test.db"
        ButlyDatabase(db_path=str(db_path))
        card = {
            "category": "Life",
            "title": "保護猫を迎えた",
            "tags": ["雑談", "家族"],
            "ai_importance": 6,
            "humanity_importance": 7,
            "summary": ["三毛猫の女の子を迎えた", "名前はこむぎ"],
            "episode": "うれしそうだった。",
        }

        assert sleeptime.insert_knowledge(
            card, "test_inst_20260512_001", "test_inst",
            "2026-05-12_raw_combined", str(db_path),
            source_date="2026-05-12",
            source_files=["session_20260512_120000_000000.json"],
        ) is True

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute("SELECT * FROM knowledge_cards").fetchone())
        assert row["summary"] == "- 三毛猫の女の子を迎えた\n- 名前はこむぎ"
        assert row["tags"] == "雑談, 家族"
        assert json.loads(row["source_files"]) == [
            "session_20260512_120000_000000.json"
        ]

    def test_unclassified_reason_still_prefixed(self, tmp_path, sleeptime):
        db_path = tmp_path / "butly_core" / "instances" / "test_inst" / "u.db"
        ButlyDatabase(db_path=str(db_path))
        card = {
            "category": "Unclassified",
            "title": "t",
            "tags": "",
            "ai_importance": 1,
            "humanity_importance": 1,
            "summary": ["事実A"],
            "episode": "e",
            "reason": ["判断材料が足りない"],
        }

        assert sleeptime.insert_knowledge(
            card, "id_001", "test_inst", "ref", str(db_path)
        )

        with sqlite3.connect(db_path) as conn:
            summary = conn.execute(
                "SELECT summary FROM knowledge_cards"
            ).fetchone()[0]
        assert summary.startswith("【分類不能理由: - 判断材料が足りない】")
        assert "- 事実A" in summary


class TestRegisterKnowledgeWithListFields:
    def test_api_path_also_coerces(self, tmp_path):
        db = ButlyDatabase(db_path=str(tmp_path / "api.db"))

        assert db.register_knowledge(
            {
                "id": "api_001",
                "category": "Life",
                "title": "陶芸教室",
                "tags": ["趣味"],
                "ai_importance": 5,
                "humanity_importance": 5,
                "summary": ["藍色のマグカップ"],
                "episode": ["楽しそうだった"],
                "raw_reference": "ref.json",
            }
        )

        with sqlite3.connect(tmp_path / "api.db") as conn:
            row = conn.execute(
                "SELECT tags, summary, episode FROM knowledge_cards"
            ).fetchone()
        assert row == ("趣味", "- 藍色のマグカップ", "- 楽しそうだった")
