"""
test_brain_multi_db.py
──────────────────────
Phase B: readable_instances による複数DB横断検索のテスト。
- 単一インスタンス検索
- 複数インスタンス横断検索
- 存在しないインスタンスのグレースフル処理
"""

import json
import sqlite3
import struct
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from butly_core.core.brain import ButlyBrain


# ===================================================================
# ヘルパー
# ===================================================================

def _create_test_db(db_path: Path, cards: list[dict]):
    """テスト用のknowledge_cardsテーブルを作成しカードを挿入する。

    cards: list of dict with keys: id, title, summary, episode, type, embedding (list[float])
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_cards (
            id TEXT PRIMARY KEY,
            title TEXT,
            summary TEXT,
            episode TEXT,
            type TEXT,
            embedding_blob BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_archived INTEGER DEFAULT 0
        )
    """)
    for card in cards:
        emb = card.get("embedding")
        blob = np.array(emb, dtype=np.float32).tobytes() if emb else None
        conn.execute(
            "INSERT INTO knowledge_cards (id, title, summary, episode, type, embedding_blob) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (card["id"], card["title"], card["summary"],
             card.get("episode", ""), card.get("type", "test"), blob),
        )
    conn.commit()
    conn.close()


def _fake_embedding(seed: float = 0.5, dim: int = 8):
    """テスト用の固定ベクトルを返す"""
    rng = np.random.RandomState(int(seed * 1000))
    vec = rng.randn(dim).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


# ===================================================================
# フィクスチャ
# ===================================================================

@pytest.fixture
def brain_base(tmp_path: Path):
    """テスト用の base_dir を構築し ButlyBrain を返す"""
    instances_dir = tmp_path / "butly_core" / "instances"
    instances_dir.mkdir(parents=True)
    return tmp_path, instances_dir


@pytest.fixture
def brain_with_two_dbs(brain_base):
    """2つのインスタンスDB (99_test, 00_master) を持つ Brain"""
    base_dir, instances_dir = brain_base

    # 99_test のDB
    test_dir = instances_dir / "99_test"
    test_dir.mkdir()
    _create_test_db(test_dir / "butly_memory.db", [
        {"id": "t1", "title": "Python基礎", "summary": "Pythonの基礎文法について",
         "type": "99_test", "embedding": _fake_embedding(1.0)},
        {"id": "t2", "title": "TypeScript入門", "summary": "TSの型システムについて",
         "type": "99_test", "embedding": _fake_embedding(2.0)},
    ])

    # 00_master のDB
    master_dir = instances_dir / "00_master"
    master_dir.mkdir()
    _create_test_db(master_dir / "butly_memory.db", [
        {"id": "m1", "title": "Python応用", "summary": "Pythonのデコレータについて",
         "type": "00_master", "embedding": _fake_embedding(1.5)},
        {"id": "m2", "title": "AI基礎", "summary": "機械学習の基礎知識",
         "type": "00_master", "embedding": _fake_embedding(3.0)},
    ])

    brain = ButlyBrain(base_dir)
    return brain


# ===================================================================
# テスト
# ===================================================================

class TestSingleInstanceSearch:
    """自インスタンスのDBのみ検索する (readable_instances: ["self"])"""

    def test_search_own_db_only(self, brain_with_two_dbs):
        """デフォルト設定(self)では自分のDBのみ検索する"""
        brain = brain_with_two_dbs
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="99_test",
                override_config={"brain": {"readable_instances": ["self"]}},
            )
        ids = {r["id"] for r in results}
        # 99_test のカードのみ返ること
        assert ids <= {"t1", "t2"}
        assert "m1" not in ids
        assert "m2" not in ids

    def test_search_master_db_only(self, brain_with_two_dbs):
        """00_master で検索すると 00_master のカードのみ返る"""
        brain = brain_with_two_dbs
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.5)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="00_master",
                override_config={"brain": {"readable_instances": ["self"]}},
            )
        ids = {r["id"] for r in results}
        assert ids <= {"m1", "m2"}
        assert "t1" not in ids


class TestMultiInstanceSearch:
    """readable_instances で複数DBを横断検索する"""

    def test_search_multiple_dbs(self, brain_with_two_dbs):
        """readable_instances: ["self", "00_master"] で両方のDBを横断検索する"""
        brain = brain_with_two_dbs
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="99_test",
                limit=10,
                override_config={"brain": {"readable_instances": ["self", "00_master"]}},
            )
        ids = {r["id"] for r in results}
        # 両方のDBのカードが返ること
        assert "t1" in ids or "t2" in ids  # 99_test のカードが含まれる
        assert "m1" in ids or "m2" in ids  # 00_master のカードが含まれる

    def test_results_sorted_by_score(self, brain_with_two_dbs):
        """横断検索結果がスコア降順でソートされている"""
        brain = brain_with_two_dbs
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="99_test",
                limit=10,
                override_config={"brain": {"readable_instances": ["self", "00_master"]}},
            )
        scores = [r.get("score", 0) for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_source_instance_annotated(self, brain_with_two_dbs):
        """横断検索結果に source_instance が付与されている"""
        brain = brain_with_two_dbs
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="99_test",
                limit=10,
                override_config={"brain": {"readable_instances": ["self", "00_master"]}},
            )
        for r in results:
            assert "source_instance" in r
            assert r["source_instance"] in ("99_test", "00_master")

    def test_self_dedup_with_explicit(self, brain_with_two_dbs):
        """readable_instances: ["self", "99_test"] は重複除去されて1回だけ検索"""
        brain = brain_with_two_dbs
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="99_test",
                limit=10,
                override_config={"brain": {"readable_instances": ["self", "99_test"]}},
            )
        # 重複IDがないこと
        ids = [r["id"] for r in results]
        assert len(ids) == len(set(ids))


class TestGracefulHandling:
    """存在しないインスタンスを指定してもエラーにならない"""

    def test_nonexistent_db_graceful(self, brain_with_two_dbs):
        """存在しないインスタンスを含んでもエラーなく正常な結果が返る"""
        brain = brain_with_two_dbs
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="99_test",
                override_config={"brain": {"readable_instances": ["self", "nonexistent_instance"]}},
            )
        # エラーなく 99_test の結果だけ返ること
        assert isinstance(results, list)
        ids = {r["id"] for r in results}
        assert ids <= {"t1", "t2"}

    def test_all_nonexistent_returns_empty(self, brain_base):
        """全て存在しないインスタンスの場合は空リストを返す"""
        base_dir, _ = brain_base
        brain = ButlyBrain(base_dir)
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="nonexistent_a",
                override_config={"brain": {"readable_instances": ["self", "nonexistent_b"]}},
            )
        assert results == []

    def test_empty_db_returns_empty(self, brain_base):
        """DBが存在するがカードが0件の場合は空リストを返す"""
        base_dir, instances_dir = brain_base
        empty_dir = instances_dir / "empty_inst"
        empty_dir.mkdir()
        _create_test_db(empty_dir / "butly_memory.db", [])

        brain = ButlyBrain(base_dir)
        with patch.object(brain, "get_embedding", return_value=_fake_embedding(1.0)):
            results = brain.search_knowledge(
                ["Python"], "Python",
                instance_name="empty_inst",
            )
        assert results == []
