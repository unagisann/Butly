"""tests/test_knowledge_maturation.py
-----------------------------------------
Stage 3: Knowledge Maturation のテスト。

カバー範囲:
  - DB migration で Stage 3 テーブルと index が追加される
  - MemoryNodeRepository の主要操作 (run / node / source)
  - enum validation (kind / status / relation)
  - collect_review_cards の優先度 (usage_count / access_logs)
  - parse_llm_output の堅牢性
  - apply_link_existing / apply_new_nodes / supersede 経路
  - collect_promotion_proposals (§11 条件)
  - stage_3_mature_knowledge end-to-end (LLM はモック)
  - MemoryBlockBuilder の active_nodes opt-in 動作
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ===================================================================
# フィクスチャ
# ===================================================================

@pytest.fixture
def db_path(tmp_path):
    """Stage 3 テーブルが作成済みの DB を返す。"""
    from butly_core.core.database import ButlyDatabase

    p = tmp_path / "butly_memory.db"
    ButlyDatabase(db_path=str(p))
    return str(p)


@pytest.fixture
def repo(db_path):
    from butly_core.core.memory_nodes import MemoryNodeRepository

    return MemoryNodeRepository(db_path)


def _insert_card(db_path: str, card_id: str, **overrides):
    conn = sqlite3.connect(db_path)
    base = {
        "id": card_id,
        "category": "Tech",
        "title": f"title-{card_id}",
        "summary": f"summary-{card_id}",
        "episode": "ep",
        "tags": "",
        "usage_count": 0,
        "last_counted_at": None,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    base.update(overrides)
    conn.execute(
        """
        INSERT INTO knowledge_cards
            (id, category, title, summary, episode, tags, usage_count, last_counted_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            base["id"], base["category"], base["title"], base["summary"], base["episode"],
            base["tags"], base["usage_count"], base["last_counted_at"],
            base["created_at"], base["updated_at"],
        ),
    )
    conn.commit()
    conn.close()


# ===================================================================
# 0. Canonical content hash (Stage 3 計画 §5.1/§5.2)
# ===================================================================

class TestCardContentHash:
    def _card(self, **overrides):
        base = {
            "title": "Title",
            "summary": "Summary",
            "episode": "Episode",
            "tags": "a,b",
            "category": "Tech",
            "source_date": "2026-01-01",
        }
        base.update(overrides)
        return base

    def test_stable_for_same_content(self):
        from butly_core.core.card_content import compute_content_hash

        assert compute_content_hash(self._card()) == compute_content_hash(self._card())

    def test_none_and_missing_fields_equal_empty(self):
        from butly_core.core.card_content import compute_content_hash

        a = compute_content_hash(self._card(episode=None))
        b = compute_content_hash(self._card(episode=""))
        c = self._card()
        del c["episode"]
        assert a == b == compute_content_hash(c)

    def test_whitespace_and_newline_normalization(self):
        from butly_core.core.card_content import compute_content_hash

        a = compute_content_hash(self._card(summary="line1\r\nline2"))
        b = compute_content_hash(self._card(summary="  line1\nline2  "))
        assert a == b

    def test_content_fields_change_hash(self):
        from butly_core.core.card_content import compute_content_hash

        base = compute_content_hash(self._card())
        assert compute_content_hash(self._card(title="Other")) != base
        assert compute_content_hash(self._card(source_date="2026-02-02")) != base

    def test_non_content_fields_ignored(self):
        from butly_core.core.card_content import compute_content_hash

        base = compute_content_hash(self._card())
        noisy = self._card()
        noisy.update({"usage_count": 99, "is_pinned": 1, "type": "X", "ai_importance": 9})
        assert compute_content_hash(noisy) == base

    def test_normalize_maturation_time(self):
        from butly_core.core.card_content import normalize_maturation_time

        fb = "2026-07-21T00:00:00Z"
        # SQLite CURRENT_TIMESTAMP 形式（UTC 扱い）
        assert (
            normalize_maturation_time("2026-01-02 03:04:05", fallback=fb)
            == "2026-01-02T03:04:05Z"
        )
        # offset 付き → UTC へ変換
        assert (
            normalize_maturation_time("2026-01-02T12:04:05+09:00", fallback=fb)
            == "2026-01-02T03:04:05Z"
        )
        # 小数秒付き Z
        assert (
            normalize_maturation_time("2026-01-02T03:04:05.123Z", fallback=fb)
            == "2026-01-02T03:04:05Z"
        )
        # 日付のみ
        assert (
            normalize_maturation_time("2026-01-02", fallback=fb)
            == "2026-01-02T00:00:00Z"
        )
        # parse 不能 → fallback
        assert normalize_maturation_time("garbage", fallback=fb) == fb
        assert normalize_maturation_time(None, fallback=fb) == fb


# ===================================================================
# 1. Migration
# ===================================================================

class TestMigration:
    def test_stage3_tables_exist(self, db_path):
        conn = sqlite3.connect(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        assert "memory_maturation_runs" in tables
        assert "memory_nodes" in tables
        assert "memory_node_sources" in tables

    def test_indexes_exist(self, db_path):
        conn = sqlite3.connect(db_path)
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        conn.close()
        expected = {
            "idx_memory_nodes_status_kind_topic",
            "idx_memory_node_sources_card",
            "idx_memory_node_sources_node",
            "idx_memory_maturation_runs_instance_started",
            "idx_knowledge_cards_last_counted_at",
        }
        assert expected.issubset(indexes)

    def test_migration_idempotent(self, db_path):
        from butly_core.core.database import ButlyDatabase

        # 2 回目の init でも壊れない
        ButlyDatabase(db_path=db_path)
        ButlyDatabase(db_path=db_path)

    def test_queue_columns_exist(self, db_path):
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_cards)")}
        run_cards_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(memory_maturation_run_cards)")
        }
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        conn.close()
        assert {
            "content_hash",
            "last_matured_content_hash",
            "maturation_queued_at",
            "last_matured_at",
            "last_matured_run_id",
        }.issubset(cols)
        assert {
            "run_id", "card_id", "content_hash", "status", "error", "diagnostic",
        }.issubset(run_cards_cols)
        assert "idx_knowledge_cards_maturation_queue" in indexes
        assert "idx_memory_maturation_run_cards_run_status" in indexes


# ===================================================================
# 1b. content_hash backfill と書き手統合 (§5.1/§5.2)
# ===================================================================

class TestContentHashWriters:
    def test_backfill_on_migration(self, tmp_path):
        from butly_core.core.database import ButlyDatabase
        from butly_core.core.card_content import compute_content_hash

        p = str(tmp_path / "m.db")
        ButlyDatabase(db_path=p)
        # hash 列を NULL に戻した既存カードを模す
        _insert_card(p, "old_card", created_at="2026-01-02 03:04:05")
        conn = sqlite3.connect(p)
        row = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='old_card'"
        ).fetchone()
        conn.close()
        assert row == (None, None)

        # 再 migration で backfill される
        ButlyDatabase(db_path=p)
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM knowledge_cards WHERE id='old_card'"
        ).fetchone()
        conn.close()
        assert row["content_hash"] == compute_content_hash(dict(row))
        # created_at (UTC 扱い) から正規化された固定長 UTC
        assert row["maturation_queued_at"] == "2026-01-02T03:04:05Z"
        assert row["last_matured_content_hash"] is None

    def test_register_knowledge_sets_hash(self, tmp_path):
        from butly_core.core.database import ButlyDatabase

        p = str(tmp_path / "r.db")
        db = ButlyDatabase(db_path=p)
        db.register_knowledge(
            {
                "id": "reg_1",
                "category": "Tech",
                "title": "t",
                "summary": "s",
                "episode": "e",
                "ai_importance": 5,
                "humanity_importance": 5,
                "raw_reference": "raw",
            }
        )
        conn = sqlite3.connect(p)
        row = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='reg_1'"
        ).fetchone()
        conn.close()
        assert row[0] is not None
        assert row[1] is not None

    def test_update_card_requeues_only_on_content_change(self, tmp_path):
        from butly_core.core.database import ButlyDatabase

        p = str(tmp_path / "u.db")
        db = ButlyDatabase(db_path=p)
        _insert_card(p, "c1")
        ButlyDatabase(db_path=p)  # backfill

        conn = sqlite3.connect(p)
        before = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='c1'"
        ).fetchone()
        conn.close()

        # 本文以外 (ai_importance) の更新では hash / queue 時刻とも不変
        assert db.update_card("c1", {"ai_importance": 9}) is True
        conn = sqlite3.connect(p)
        after_meta = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='c1'"
        ).fetchone()
        conn.close()
        assert after_meta == before

        # 本文更新では hash が変わり再キューされる
        assert db.update_card("c1", {"summary": "totally new"}) is True
        conn = sqlite3.connect(p)
        after_content = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='c1'"
        ).fetchone()
        conn.close()
        assert after_content[0] != before[0]

    def test_pin_archive_do_not_touch_hash(self, tmp_path):
        from butly_core.core.database import ButlyDatabase

        p = str(tmp_path / "pin.db")
        db = ButlyDatabase(db_path=p)
        _insert_card(p, "c1")
        ButlyDatabase(db_path=p)  # backfill
        conn = sqlite3.connect(p)
        before = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='c1'"
        ).fetchone()
        conn.close()

        db.toggle_pin("c1", True)
        db.toggle_archive("c1", True)
        conn = sqlite3.connect(p)
        after = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='c1'"
        ).fetchone()
        conn.close()
        assert after == before

    def test_insert_knowledge_sets_hash(self, tmp_path):
        import sleeptime as _sl
        from butly_core.core.database import ButlyDatabase
        from butly_core.core.card_content import compute_content_hash

        p = str(tmp_path / "ins.db")
        ButlyDatabase(db_path=p)

        hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
        hk.generate_embedding = lambda *a, **k: None
        card = {
            "category": "Tech",
            "title": "t",
            "tags": "x",
            "ai_importance": 5,
            "humanity_importance": 5,
            "summary": "s",
            "episode": "e",
        }
        assert hk.insert_knowledge(
            card, "tech_20260101_001", "inst", "raw_ref", p,
            source_date="2026-01-01",
        ) is True

        conn = sqlite3.connect(p)
        row = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards "
            "WHERE id='tech_20260101_001'"
        ).fetchone()
        conn.close()
        assert row[0] == compute_content_hash(
            {
                "title": "t",
                "summary": "s",
                "episode": "e",
                "tags": "x",
                "category": "Tech",
                "source_date": "2026-01-01",
            }
        )
        assert row[1] is not None


# ===================================================================
# 2. Repository ops
# ===================================================================

class TestRunLifecycle:
    def test_start_and_complete(self, repo):
        rid = repo.start_run("test", metadata={"source": "unit"})
        run = repo.get_run(rid)
        assert run["status"] == "running"
        assert run["started_at"]

        repo.update_run_counters(rid, reviewed_card_count=3, created_node_count=1)
        repo.complete_run(rid)
        run = repo.get_run(rid)
        assert run["status"] == "completed"
        assert run["completed_at"]
        assert run["reviewed_card_count"] == 3
        assert run["created_node_count"] == 1

    def test_fail_run(self, repo):
        rid = repo.start_run("test")
        repo.fail_run(rid, "boom")
        run = repo.get_run(rid)
        assert run["status"] == "failed"
        assert run["error"] == "boom"

    def test_invalid_complete_status_rejected(self, repo):
        rid = repo.start_run("test")
        with pytest.raises(ValueError):
            repo.complete_run(rid, status="weird")


class TestNodeOps:
    def test_create_and_get(self, repo):
        nid = repo.create_node(
            kind="preference",
            subject="user",
            topic="food",
            statement="fruits ok",
            confidence=0.5,
        )
        node = repo.get_node(nid)
        assert node["status"] == "candidate"
        assert node["statement"] == "fruits ok"
        assert node["kind"] == "preference"

    def test_unknown_kind_falls_back_to_other(self, repo):
        nid = repo.create_node(kind="weird_kind", statement="s")
        assert repo.get_node(nid)["kind"] == "other"

    def test_invalid_status_rejected(self, repo):
        with pytest.raises(ValueError):
            repo.create_node(kind="fact", statement="x", status="bogus")

    def test_supersede_marks_old_node(self, repo):
        old = repo.create_node(kind="fact", statement="old", status="active")
        new = repo.create_node(kind="fact", statement="new", status="active")
        ok = repo.supersede_node(old_node_id=old, new_node_id=new)
        assert ok is True
        old_row = repo.get_node(old)
        assert old_row["status"] == "superseded"
        assert old_row["superseded_by_node_id"] == new

    def test_update_node_reinforce(self, repo):
        nid = repo.create_node(kind="fact", statement="s", confidence=0.5)
        repo.update_node(nid, confidence=0.7, reinforce=True)
        n = repo.get_node(nid)
        assert n["confidence"] == 0.7
        assert n["last_reinforced_at"] is not None


class TestSourceOps:
    def test_upsert_supports(self, repo, db_path):
        nid = repo.create_node(kind="fact", statement="s")
        _insert_card(db_path, "card_001")
        repo.upsert_source(node_id=nid, card_id="card_001", relation="supports", confidence=0.8)
        assert repo.count_sources(nid) == 1
        assert repo.count_sources(nid, relation="supports") == 1

    def test_invalid_relation_rejected(self, repo, db_path):
        nid = repo.create_node(kind="fact", statement="s")
        _insert_card(db_path, "card_001")
        with pytest.raises(ValueError):
            repo.upsert_source(node_id=nid, card_id="card_001", relation="bogus")

    def test_distinct_support_days(self, repo, db_path):
        nid = repo.create_node(kind="fact", statement="s")
        _insert_card(
            db_path, "card_a",
            created_at=(datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        _insert_card(
            db_path, "card_b",
            created_at=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        _insert_card(
            db_path, "card_c",
            created_at=(datetime.now() - timedelta(days=1, hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        repo.upsert_source(node_id=nid, card_id="card_a", relation="supports")
        repo.upsert_source(node_id=nid, card_id="card_b", relation="supports")
        repo.upsert_source(node_id=nid, card_id="card_c", relation="supports")
        # b と c は同日なので 2 日
        assert repo.distinct_support_days(nid) == 2


# ===================================================================
# 3. レビューキュー: preflight / FIFO 選択 / backlog (§5.3)
# ===================================================================

def _backfill(db_path: str):
    """_insert_card 直挿入分の NULL hash を preflight で自己修復する。"""
    from butly_core.core.knowledge_maturation import preflight_backfill_hashes

    return preflight_backfill_hashes(db_path)


class TestQueueSelection:
    def test_preflight_self_heals_null_hashes(self, db_path):
        from butly_core.core.knowledge_maturation import preflight_backfill_hashes

        _insert_card(db_path, "c1")
        _insert_card(db_path, "c_archived")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE knowledge_cards SET is_archived=1 WHERE id='c_archived'")
        conn.commit()
        conn.close()

        n = preflight_backfill_hashes(db_path, now_stamp="2026-07-21T00:00:00Z")
        assert n == 1  # 非アーカイブのみ
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT content_hash, maturation_queued_at FROM knowledge_cards WHERE id='c1'"
        ).fetchone()
        conn.close()
        assert row[0] is not None
        assert row[1] == "2026-07-21T00:00:00Z"

    def test_fifo_order_oldest_first(self, db_path):
        from butly_core.core.knowledge_maturation import select_queue_cards

        _insert_card(db_path, "new_hot", usage_count=99, created_at="2026-07-20 00:00:00")
        _insert_card(db_path, "old_cold", usage_count=0, created_at="2026-01-01 00:00:00")
        _insert_card(db_path, "mid", usage_count=1, created_at="2026-03-01 00:00:00")
        from butly_core.core.database import ButlyDatabase

        ButlyDatabase(db_path=db_path)  # backfill queued_at from created_at

        cards = select_queue_cards(db_path, batch_size=2)
        ids = [c["id"] for c in cards]
        # 高 usage の新規カードが低 usage の既存カードを追い越さない（被覆保証）
        assert ids == ["old_cold", "mid"]

    def test_usage_breaks_ties_within_same_queue_time(self, db_path):
        from butly_core.core.knowledge_maturation import select_queue_cards

        same = "2026-05-01 00:00:00"
        _insert_card(db_path, "a_low", usage_count=0, created_at=same)
        _insert_card(db_path, "b_high", usage_count=5, created_at=same)
        from butly_core.core.database import ButlyDatabase

        ButlyDatabase(db_path=db_path)

        cards = select_queue_cards(db_path, batch_size=2)
        assert [c["id"] for c in cards] == ["b_high", "a_low"]

    def test_excludes_archived_and_matured(self, db_path):
        from butly_core.core.knowledge_maturation import select_queue_cards

        _insert_card(db_path, "live")
        _insert_card(db_path, "archived")
        _insert_card(db_path, "done")
        from butly_core.core.database import ButlyDatabase

        ButlyDatabase(db_path=db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE knowledge_cards SET is_archived=1 WHERE id='archived'")
        conn.execute(
            "UPDATE knowledge_cards SET last_matured_content_hash=content_hash WHERE id='done'"
        )
        conn.commit()
        conn.close()

        ids = {c["id"] for c in select_queue_cards(db_path, batch_size=10)}
        assert ids == {"live"}

    def test_content_change_requeues_card(self, db_path):
        from butly_core.core.database import ButlyDatabase
        from butly_core.core.knowledge_maturation import select_queue_cards

        _insert_card(db_path, "c1")
        db = ButlyDatabase(db_path=db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE knowledge_cards SET last_matured_content_hash=content_hash WHERE id='c1'"
        )
        conn.commit()
        conn.close()
        assert select_queue_cards(db_path, batch_size=10) == []

        # 本文変更 → 新 hash がキューへ戻る
        db.update_card("c1", {"summary": "new version"})
        ids = [c["id"] for c in select_queue_cards(db_path, batch_size=10)]
        assert ids == ["c1"]

    def test_exclude_ids(self, db_path):
        from butly_core.core.knowledge_maturation import select_queue_cards

        _insert_card(db_path, "keep")
        _insert_card(db_path, "skip")
        from butly_core.core.database import ButlyDatabase

        ButlyDatabase(db_path=db_path)
        ids = {c["id"] for c in select_queue_cards(db_path, batch_size=10, exclude_ids=["skip"])}
        assert ids == {"keep"}

    def test_batch_size_zero(self, db_path):
        from butly_core.core.knowledge_maturation import select_queue_cards

        _insert_card(db_path, "x")
        assert select_queue_cards(db_path, batch_size=0) == []

    def test_count_queue_backlog(self, db_path):
        from butly_core.core.knowledge_maturation import count_queue_backlog

        _insert_card(db_path, "a", created_at="2026-01-01 00:00:00")
        _insert_card(db_path, "b", created_at="2026-02-01 00:00:00")
        from butly_core.core.database import ButlyDatabase

        ButlyDatabase(db_path=db_path)
        backlog = count_queue_backlog(db_path)
        assert backlog["backlog"] == 2
        assert backlog["oldest_queued_at"] == "2026-01-01T00:00:00Z"


# ===================================================================
# 4. 厳密 parse と LLM 結果分類 (§5.4)
# ===================================================================

class TestParseReviewOutput:
    def test_extracts_fenced_json(self):
        from butly_core.core.knowledge_maturation import parse_review_output

        out = parse_review_output(
            '```json\n{"link_existing": [{"node_id": "n", "card_id": "c"}], "new_nodes": []}\n```'
        )
        assert out["link_existing"][0]["node_id"] == "n"
        assert out["link_existing"][0]["relation"] == "supports"
        assert out["new_nodes"] == []
        assert out["reviewed_card_ids"] is None

    def test_reviewed_card_ids_normalized(self):
        from butly_core.core.knowledge_maturation import parse_review_output

        out = parse_review_output(
            '{"reviewed_card_ids": ["c1", 42], "link_existing": [], "new_nodes": []}'
        )
        assert out["reviewed_card_ids"] == ["c1"]

    def test_confidence_string_accepted(self):
        from butly_core.core.knowledge_maturation import parse_review_output

        out = parse_review_output(
            '{"link_existing": [{"node_id": "n", "card_id": "c", "confidence": "0.8"}],'
            ' "new_nodes": []}'
        )
        assert out["link_existing"][0]["confidence"] == 0.8

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            "{}",  # 必須キー欠落
            '{"link_existing": {}, "new_nodes": []}',  # 型違反
            '{"link_existing": [{"card_id": "c"}], "new_nodes": []}',  # node_id 欠落
            '{"link_existing": [{"node_id": "n", "card_id": "c", "relation": "bogus"}], "new_nodes": []}',
            '{"link_existing": [], "new_nodes": [{"confidence": 0.9}]}',  # statement 欠落
            '{"link_existing": [], "new_nodes": [{"statement": "x", "confidence": "high"}]}',
            '{"link_existing": [], "new_nodes": [{"statement": "x", "source_card_ids": "c1"}]}',
            '{"link_existing": [], "new_nodes": [], "reviewed_card_ids": "c1"}',
        ],
    )
    def test_schema_violations_raise(self, raw):
        from butly_core.core.knowledge_maturation import (
            ReviewParseError,
            parse_review_output,
        )

        with pytest.raises(ReviewParseError):
            parse_review_output(raw)


class TestClassifyReviewResponse:
    def test_ok(self):
        from butly_core.core.knowledge_maturation import classify_review_response

        outcome, parsed, err = classify_review_response(
            '{"link_existing": [], "new_nodes": [{"statement": "x", "confidence": 0.7}]}'
        )
        assert outcome == "ok"
        assert parsed["new_nodes"]
        assert err is None

    def test_no_changes(self):
        from butly_core.core.knowledge_maturation import classify_review_response

        outcome, parsed, err = classify_review_response(
            '{"link_existing": [], "new_nodes": []}'
        )
        assert outcome == "no_changes"
        assert parsed is not None

    def test_empty_response(self):
        from butly_core.core.knowledge_maturation import classify_review_response

        assert classify_review_response("")[0] == "empty_response"
        assert classify_review_response(None)[0] == "empty_response"
        assert classify_review_response("   ")[0] == "empty_response"

    def test_parse_error(self):
        from butly_core.core.knowledge_maturation import classify_review_response

        outcome, parsed, err = classify_review_response("garbage")
        assert outcome == "parse_error"
        assert parsed is None
        assert err

    def test_truncation_overrides_valid_json(self):
        from butly_core.core.knowledge_maturation import classify_review_response

        # provider が truncation を報告したら、本文が parse 可能でも失敗させる
        for reason in ("MAX_TOKENS", "FinishReason.MAX_TOKENS", "length"):
            outcome, parsed, err = classify_review_response(
                '{"link_existing": [], "new_nodes": []}', reason
            )
            assert outcome == "truncated_response", reason
            assert parsed is None

    def test_normal_finish_reasons_accepted(self):
        from butly_core.core.knowledge_maturation import classify_review_response

        for reason in ("STOP", "stop", None):
            outcome, _, _ = classify_review_response(
                '{"link_existing": [], "new_nodes": []}', reason
            )
            assert outcome == "no_changes", reason

    def test_check_reviewed_card_ids(self):
        from butly_core.core.knowledge_maturation import check_reviewed_card_ids

        # 一致 / 未申告は診断なし
        assert check_reviewed_card_ids({"reviewed_card_ids": ["a", "b"]}, {"a", "b"}) is None
        assert check_reviewed_card_ids({"reviewed_card_ids": None}, {"a"}) is None
        # 不足・余分は診断文字列（成功条件にはしない）
        diag = check_reviewed_card_ids({"reviewed_card_ids": ["a", "x"]}, {"a", "b"})
        assert "missing=['b']" in diag
        assert "unknown=['x']" in diag


# ===================================================================
# 5. apply_link_existing / apply_new_nodes
# ===================================================================

def _link_entry(node_id, card_id, relation="supports", confidence=0.5, note=None):
    """parse_review_output 正規化後の link_existing entry 形状。"""
    return {
        "node_id": node_id,
        "card_id": card_id,
        "relation": relation,
        "confidence": confidence,
        "note": note,
    }


def _new_node_entry(statement, confidence, **overrides):
    """parse_review_output 正規化後の new_nodes entry 形状。"""
    base = {
        "kind": "preference",
        "statement": statement,
        "subject": None,
        "topic": None,
        "confidence": confidence,
        "source_card_ids": [],
        "supersedes_node_id": None,
    }
    base.update(overrides)
    return base


class TestApply:
    def test_link_existing_rejects_unknown_ids_with_diagnostics(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_link_existing

        existing = repo.create_node(kind="fact", statement="x")
        _insert_card(db_path, "card_1")

        rid = repo.start_run("test")
        linked, uncertain, diags = apply_link_existing(
            repo=repo,
            entries=[
                _link_entry(existing, "card_1", confidence=0.9),
                _link_entry("nope", "card_1"),  # 入力外 node
                _link_entry(existing, "missing"),  # 入力外 card
            ],
            valid_node_ids={existing},
            valid_card_ids={"card_1"},
            run_id=rid,
        )
        assert linked == 1
        assert uncertain == []
        assert repo.count_sources(existing) == 1
        assert len(diags) == 2
        assert any("nope" in d for d in diags)
        assert any("missing" in d for d in diags)

    def test_link_existing_contradicts_marks_uncertain(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_link_existing

        existing = repo.create_node(kind="fact", statement="x")
        _insert_card(db_path, "c1")
        _insert_card(db_path, "c2")

        rid = repo.start_run("test")
        _, uncertain, _ = apply_link_existing(
            repo=repo,
            entries=[
                _link_entry(existing, "c1", relation="contradicts"),
                _link_entry(existing, "c2", relation="contradicts"),
            ],
            valid_node_ids={existing},
            valid_card_ids={"c1", "c2"},
            run_id=rid,
        )
        assert existing in uncertain

    def test_new_nodes_below_threshold_skipped(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_new_nodes

        _insert_card(db_path, "c1")
        rid = repo.start_run("test")
        created, sup, _ = apply_new_nodes(
            repo=repo,
            entries=[_new_node_entry("low conf", 0.3, source_card_ids=["c1"])],
            valid_node_ids=set(),
            valid_card_ids={"c1"},
            run_id=rid,
            candidate_threshold=0.65,
            active_threshold=0.75,
        )
        assert created == 0
        assert sup == 0

    def test_new_nodes_active_status_at_high_confidence(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_new_nodes

        _insert_card(db_path, "c1")
        _insert_card(db_path, "c2")
        rid = repo.start_run("test")
        created, sup, _ = apply_new_nodes(
            repo=repo,
            entries=[
                _new_node_entry("stable preference", 0.8, source_card_ids=["c1", "c2"]),
            ],
            valid_node_ids=set(),
            valid_card_ids={"c1", "c2"},
            run_id=rid,
            candidate_threshold=0.65,
            active_threshold=0.75,
        )
        assert created == 1
        # 新規 active node が作られた
        actives = repo.find_nodes(statuses=["active"])
        assert len(actives) == 1
        # supports source が 2 件
        assert repo.count_sources(actives[0]["id"], relation="supports") == 2

    def test_new_nodes_unknown_source_card_rejected(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_new_nodes

        _insert_card(db_path, "c1")
        rid = repo.start_run("test")
        created, _, diags = apply_new_nodes(
            repo=repo,
            entries=[
                _new_node_entry("s", 0.8, source_card_ids=["c1", "outside"]),
            ],
            valid_node_ids=set(),
            valid_card_ids={"c1"},
            run_id=rid,
            candidate_threshold=0.65,
            active_threshold=0.75,
        )
        assert created == 1
        node = repo.find_nodes(statuses=["active"])[0]
        assert repo.count_sources(node["id"]) == 1  # outside は link されない
        assert any("outside" in d for d in diags)

    def test_supersede_applied(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_new_nodes

        _insert_card(db_path, "c1")
        old = repo.create_node(kind="preference", statement="old", status="active")

        rid = repo.start_run("test")
        created, sup, _ = apply_new_nodes(
            repo=repo,
            entries=[
                _new_node_entry(
                    "new", 0.9, source_card_ids=["c1"], supersedes_node_id=old
                ),
            ],
            valid_node_ids={old},
            valid_card_ids={"c1"},
            run_id=rid,
            candidate_threshold=0.65,
            active_threshold=0.75,
        )
        assert created == 1
        assert sup == 1
        assert repo.get_node(old)["status"] == "superseded"

    def test_supersede_outside_input_rejected(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_new_nodes

        _insert_card(db_path, "c1")
        outside = repo.create_node(kind="preference", statement="old", status="active")

        rid = repo.start_run("test")
        created, sup, diags = apply_new_nodes(
            repo=repo,
            entries=[
                _new_node_entry(
                    "new", 0.9, source_card_ids=["c1"], supersedes_node_id=outside
                ),
            ],
            valid_node_ids=set(),  # prompt に載せていない node は入力外
            valid_card_ids={"c1"},
            run_id=rid,
            candidate_threshold=0.65,
            active_threshold=0.75,
        )
        assert created == 1
        assert sup == 0
        assert repo.get_node(outside)["status"] == "active"
        assert any("supersede rejected" in d for d in diags)


# ===================================================================
# 6. Promotion proposals (§11)
# ===================================================================

class TestPromotionProposals:
    def test_meets_all_conditions(self, repo, db_path):
        from butly_core.core.knowledge_maturation import collect_promotion_proposals

        nid = repo.create_node(kind="preference", statement="s", status="active", confidence=0.9)
        _insert_card(
            db_path, "c1",
            created_at=(datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        _insert_card(
            db_path, "c2",
            created_at=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        repo.upsert_source(node_id=nid, card_id="c1", relation="supports")
        repo.upsert_source(node_id=nid, card_id="c2", relation="supports")

        proposals = collect_promotion_proposals(
            repo=repo, confidence_threshold=0.85, min_sources=2
        )
        assert len(proposals) == 1
        assert proposals[0]["node_id"] == nid

    def test_fails_when_single_day(self, repo, db_path):
        from butly_core.core.knowledge_maturation import collect_promotion_proposals

        nid = repo.create_node(kind="preference", statement="s", status="active", confidence=0.9)
        same = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _insert_card(db_path, "c1", created_at=same)
        _insert_card(db_path, "c2", created_at=same)
        repo.upsert_source(node_id=nid, card_id="c1", relation="supports")
        repo.upsert_source(node_id=nid, card_id="c2", relation="supports")

        proposals = collect_promotion_proposals(
            repo=repo, confidence_threshold=0.85, min_sources=2
        )
        # 同じ日なので 2 日条件未達 → 除外
        assert proposals == []

    def test_candidate_node_not_proposed(self, repo, db_path):
        from butly_core.core.knowledge_maturation import collect_promotion_proposals

        nid = repo.create_node(kind="preference", statement="s", status="candidate", confidence=0.95)
        _insert_card(db_path, "c1")
        _insert_card(db_path, "c2")
        repo.upsert_source(node_id=nid, card_id="c1", relation="supports")
        repo.upsert_source(node_id=nid, card_id="c2", relation="supports")

        proposals = collect_promotion_proposals(
            repo=repo, confidence_threshold=0.85, min_sources=2
        )
        assert proposals == []

    def test_source_date_preferred_over_created_at(self, repo, db_path):
        """同日 bootstrap（created_at 同日）でも source_date が複数日なら昇格候補（§7）。"""
        from butly_core.core.knowledge_maturation import collect_promotion_proposals

        nid = repo.create_node(kind="preference", statement="s", status="active", confidence=0.9)
        same = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _insert_card(db_path, "c1", created_at=same)
        _insert_card(db_path, "c2", created_at=same)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE knowledge_cards SET source_date='2026-01-01' WHERE id='c1'")
        conn.execute("UPDATE knowledge_cards SET source_date='2026-01-05' WHERE id='c2'")
        conn.commit()
        conn.close()
        repo.upsert_source(node_id=nid, card_id="c1", relation="supports")
        repo.upsert_source(node_id=nid, card_id="c2", relation="supports")

        assert repo.distinct_support_days(nid) == 2
        proposals = collect_promotion_proposals(
            repo=repo, confidence_threshold=0.85, min_sources=2
        )
        assert len(proposals) == 1

    def test_pagination_covers_beyond_200(self, repo, db_path):
        """LIMIT 200 撤去: eligible node が 200 を超えても全件評価される（§8）。"""
        from butly_core.core.knowledge_maturation import collect_promotion_proposals

        _insert_card(db_path, "c1", created_at="2026-01-01 00:00:00")
        _insert_card(db_path, "c2", created_at="2026-01-05 00:00:00")
        node_ids = []
        for i in range(205):
            nid = repo.create_node(
                kind="fact", statement=f"s{i}", status="active", confidence=0.9
            )
            node_ids.append(nid)
        for nid in node_ids:
            repo.upsert_source(node_id=nid, card_id="c1", relation="supports")
            repo.upsert_source(node_id=nid, card_id="c2", relation="supports")

        proposals = collect_promotion_proposals(
            repo=repo, confidence_threshold=0.85, min_sources=2
        )
        assert len(proposals) == 205


# ===================================================================
# 6b. 既存 node 文脈のスコープ化 (§5.5)
# ===================================================================

class TestSelectContextNodes:
    def test_linked_nodes_first_then_vocab_then_recent(self, repo, db_path):
        from butly_core.core.knowledge_maturation import select_context_nodes

        _insert_card(db_path, "c1", title="coffee brewing", tags="coffee,hobby")
        linked = repo.create_node(kind="fact", statement="linked node")
        repo.upsert_source(node_id=linked, card_id="c1", relation="supports")
        vocab = repo.create_node(
            kind="preference", statement="user enjoys coffee in the morning"
        )
        recent = repo.create_node(kind="other", statement="unrelated stuff")
        superseded = repo.create_node(kind="fact", statement="dead", status="superseded")

        cards = [{"id": "c1", "title": "coffee brewing", "tags": "coffee,hobby", "category": "Hobby"}]
        nodes = select_context_nodes(db_path, cards, limit=10)
        ids = [n["id"] for n in nodes]
        assert ids[0] == linked
        assert ids.index(linked) < ids.index(vocab) < ids.index(recent)
        assert superseded not in ids

    def test_limit_respected(self, repo, db_path):
        from butly_core.core.knowledge_maturation import select_context_nodes

        for i in range(6):
            repo.create_node(kind="fact", statement=f"s{i}")
        nodes = select_context_nodes(db_path, [], limit=3)
        assert len(nodes) == 3


# ===================================================================
# 7. End-to-end stage_3_mature_knowledge (LLM モック)
# ===================================================================

from datetime import timezone as _tz

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=_tz.utc)

_GOOD_RESPONSE = json.dumps(
    {
        "reviewed_card_ids": ["c_001", "c_002"],
        "link_existing": [],
        "new_nodes": [
            {
                "kind": "preference",
                "subject": "user",
                "topic": "food",
                "statement": "ユーザーはフルーツが好き",
                "confidence": 0.8,
                "source_card_ids": ["c_001", "c_002"],
            }
        ],
    },
    ensure_ascii=False,
)


def _build_instance(tmp_path: Path, instance_name: str = "test_instance") -> Path:
    instances_dir = tmp_path / "butly_core" / "instances"
    inst = instances_dir / instance_name
    inst.mkdir(parents=True)
    (inst / "system_instruction.txt").write_text("テスト用 AI", encoding="utf-8")
    (inst / "Key_Memory.txt").write_text("テストユーザー", encoding="utf-8")
    return inst


def _make_hk(inst_path: Path):
    import sleeptime as _sl

    hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
    hk.instances_dir = inst_path.parent
    hk.instruction = "fallback instruction"
    hk.key_memory = "fallback memory"
    return hk


def _fake_provider(response=_GOOD_RESPONSE):
    provider = MagicMock()
    provider.classify.return_value = response
    provider.pop_last_completion_metadata.return_value = None
    provider.pop_last_token_usage.return_value = None
    return provider


class TestStage3EndToEnd:
    def _setup(self, tmp_path, cards=("c_001", "c_002")):
        from butly_core.core.database import ButlyDatabase

        inst_path = _build_instance(tmp_path)
        db_path = str(inst_path / "butly_memory.db")
        ButlyDatabase(db_path=db_path)
        for cid in cards:
            _insert_card(db_path, cid)
        return inst_path, db_path

    def test_run_creates_nodes_completes_and_stamps(self, tmp_path):
        import sleeptime as _sl
        from butly_core.core.memory_nodes import MemoryNodeRepository

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)
        provider = _fake_provider()

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert totals["status"] == "completed"
        assert totals["applied_cards"] == 2
        assert totals["created"] == 1
        assert totals["backlog"] == 0

        repo = MemoryNodeRepository(db_path)
        nodes = repo.find_nodes(statuses=["active", "candidate"])
        assert len(nodes) == 1
        assert nodes[0]["status"] == "active"
        assert repo.count_sources(nodes[0]["id"], relation="supports") == 2

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM memory_maturation_runs WHERE status='completed'"
        ).fetchone()
        assert run["reviewed_card_count"] == 2
        assert run["created_node_count"] == 1
        run_cards = conn.execute(
            "SELECT status FROM memory_maturation_run_cards WHERE run_id=?",
            (run["id"],),
        ).fetchall()
        assert {r["status"] for r in run_cards} == {"applied"}
        stamped = conn.execute(
            """
            SELECT COUNT(*) FROM knowledge_cards
            WHERE last_matured_content_hash = content_hash
              AND last_matured_run_id = ?
              AND last_matured_at = '2026-07-21T12:00:00Z'
            """,
            (run["id"],),
        ).fetchone()[0]
        conn.close()
        assert stamped == 2

    def test_second_run_reviews_zero_cards(self, tmp_path):
        """完了条件: 本文無変更の 2 回目 run は reviewed_card_count == 0。"""
        import sleeptime as _sl

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)
        provider = _fake_provider()

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            hk.stage_3_mature_knowledge(inst_path, now=_NOW)
            totals2 = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert provider.classify.call_count == 1  # 2 回目は LLM を呼ばない
        assert totals2["applied_cards"] == 0
        assert totals2["reviewed_cards"] == 0
        conn = sqlite3.connect(db_path)
        statuses = [
            r[0]
            for r in conn.execute("SELECT status FROM memory_maturation_runs")
        ]
        conn.close()
        assert statuses.count("skipped") == 1

    def test_content_change_triggers_rereview(self, tmp_path):
        import sleeptime as _sl
        from butly_core.core.database import ButlyDatabase

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)
        provider = _fake_provider()

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            hk.stage_3_mature_knowledge(inst_path, now=_NOW)
            ButlyDatabase(db_path=db_path).update_card(
                "c_001", {"summary": "changed content"}
            )
            totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert provider.classify.call_count == 2
        assert totals["applied_cards"] == 1  # 変更された 1 枚だけ再レビュー

    def test_run_skipped_when_queue_empty(self, tmp_path):
        inst_path, db_path = self._setup(tmp_path, cards=())
        hk = _make_hk(inst_path)

        totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)
        assert totals["batches"] == 0

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT status FROM memory_maturation_runs").fetchone()
        conn.close()
        assert row[0] == "skipped"

    def test_parse_error_keeps_cards_in_queue(self, tmp_path):
        import sleeptime as _sl
        from butly_core.core.knowledge_maturation import select_queue_cards

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)
        provider = _fake_provider(response="not json at all")

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert totals["status"] == "partial"
        assert set(totals["failed_cards"]) == {"c_001", "c_002"}
        # retry → 分割 → 1 件でも失敗、を経て複数回呼ばれる
        assert provider.classify.call_count > 1
        # カード版は stamp されずキューに残る
        assert len(select_queue_cards(db_path, batch_size=10)) == 2
        conn = sqlite3.connect(db_path)
        failed_runs = conn.execute(
            "SELECT COUNT(*) FROM memory_maturation_runs WHERE status='failed'"
        ).fetchone()[0]
        conn.close()
        assert failed_runs > 0

    def test_truncated_response_not_stamped(self, tmp_path):
        """provider が truncation を報告したら、本文が parse 可能でも失敗（§5.4）。"""
        import sleeptime as _sl
        from butly_core.core.knowledge_maturation import select_queue_cards

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)
        provider = _fake_provider()
        provider.pop_last_completion_metadata.return_value = {
            "finish_reason": "MAX_TOKENS"
        }

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert totals["applied_cards"] == 0
        assert "truncated_response" in totals["outcomes"]
        assert len(select_queue_cards(db_path, batch_size=10)) == 2

    def test_provider_error_recorded_and_aborts(self, tmp_path):
        import sleeptime as _sl

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)
        provider = _fake_provider()
        provider.classify.side_effect = RuntimeError("LLM exploded")

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert totals["status"] == "partial"
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, error FROM memory_maturation_runs"
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
        assert "LLM exploded" in (row[1] or "")

    def test_lock_skips_concurrent_run(self, tmp_path):
        from butly_core.core import knowledge_maturation as km

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)

        with km.stage3_process_lock(inst_path) as acquired:
            assert acquired is True
            totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)
        assert totals["status"] == "locked"
        assert totals["batches"] == 0

    def test_orphan_running_run_recovered_as_abandoned(self, tmp_path):
        import sleeptime as _sl
        from butly_core.core.memory_nodes import MemoryNodeRepository

        inst_path, db_path = self._setup(tmp_path)
        repo = MemoryNodeRepository(db_path)
        orphan_id = repo.start_run("test_instance", metadata={"crashed": True})
        repo.record_run_cards(orphan_id, [("c_001", "deadbeef")])

        hk = _make_hk(inst_path)
        provider = _fake_provider()
        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        run = repo.get_run(orphan_id)
        assert run["status"] == "abandoned"
        cards = repo.list_run_cards(orphan_id)
        assert all(c["status"] == "abandoned" for c in cards)

    def test_changed_during_run_leaves_new_version_queued(self, tmp_path):
        """LLM 呼び出し中に本文が編集されたら batch を適用しない（§5.4-5）。"""
        import sleeptime as _sl
        from butly_core.core.database import ButlyDatabase
        from butly_core.core.knowledge_maturation import select_queue_cards
        from butly_core.core.memory_nodes import MemoryNodeRepository

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)

        def _mutate_then_respond(prompt, conf):
            ButlyDatabase(db_path=db_path).update_card(
                "c_001", {"summary": "edited during llm call"}
            )
            return _GOOD_RESPONSE

        provider = _fake_provider()
        provider.classify.side_effect = _mutate_then_respond

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert "changed_during_run" in totals["outcomes"]
        assert totals["applied_cards"] == 0
        # node は一切作られていない
        repo = MemoryNodeRepository(db_path)
        assert repo.find_nodes(statuses=["active", "candidate"]) == []
        # 新版はキューに残る（c_002 も未処理のまま）
        assert len(select_queue_cards(db_path, batch_size=10)) == 2
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT card_id, status FROM memory_maturation_run_cards"
        ).fetchall()
        conn.close()
        by_card = {r["card_id"]: r["status"] for r in rows}
        assert by_card["c_001"] == "changed_during_run"
        assert by_card["c_002"] == "abandoned"

    def test_crash_during_apply_rolls_back_and_retry_has_no_duplicates(self, tmp_path):
        """完了条件: 適用途中の中断を再試行しても node/source/counter が重複しない。"""
        import sleeptime as _sl
        from butly_core.core.memory_nodes import (
            MaturationUnitOfWork,
            MemoryNodeRepository,
        )

        inst_path, db_path = self._setup(tmp_path)
        hk = _make_hk(inst_path)
        provider = _fake_provider()

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            with patch.object(
                MaturationUnitOfWork,
                "stamp_card_version",
                side_effect=RuntimeError("simulated crash"),
            ):
                totals1 = hk.stage_3_mature_knowledge(inst_path, now=_NOW)
            totals2 = hk.stage_3_mature_knowledge(inst_path, now=_NOW)

        assert "db_error" in totals1["outcomes"]
        repo = MemoryNodeRepository(db_path)
        # rollback により 1 回分だけ適用されている
        nodes = repo.find_nodes(statuses=["active", "candidate"])
        assert len(nodes) == 1
        assert repo.count_sources(nodes[0]["id"], relation="supports") == 2
        assert totals2["applied_cards"] == 2
        conn = sqlite3.connect(db_path)
        completed = conn.execute(
            "SELECT COUNT(*) FROM memory_maturation_runs WHERE status='completed'"
        ).fetchone()[0]
        conn.close()
        assert completed == 1


# ===================================================================
# 7b. stage3-bootstrap (§6)
# ===================================================================

_NO_CHANGES_RESPONSE = '{"link_existing": [], "new_nodes": []}'


class TestStage3Bootstrap:
    def _setup(self, tmp_path, n_cards=5, batch_size=2):
        from butly_core.core.database import ButlyDatabase

        inst_path = _build_instance(tmp_path)
        (inst_path / "config.json").write_text(
            json.dumps(
                {"memory": {"knowledge_maturation_batch_size": batch_size}}
            ),
            encoding="utf-8",
        )
        db_path = str(inst_path / "butly_memory.db")
        ButlyDatabase(db_path=db_path)
        for i in range(n_cards):
            _insert_card(
                db_path, f"bc_{i:03d}", created_at=f"2026-01-{i + 1:02d} 00:00:00"
            )
        return inst_path, db_path

    def test_bootstrap_drains_whole_queue(self, tmp_path):
        import sleeptime as _sl

        inst_path, db_path = self._setup(tmp_path, n_cards=5, batch_size=2)
        hk = _make_hk(inst_path)
        provider = _fake_provider(response=_NO_CHANGES_RESPONSE)

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage3_bootstrap("test_instance", now=_NOW)

        assert totals["status"] == "completed"
        assert totals["applied_cards"] == 5
        assert totals["batches"] == 3  # 2 + 2 + 1
        assert totals["backlog"] == 0
        conn = sqlite3.connect(db_path)
        stamped = conn.execute(
            "SELECT COUNT(*) FROM knowledge_cards WHERE last_matured_content_hash = content_hash"
        ).fetchone()[0]
        conn.close()
        assert stamped == 5

    def test_bootstrap_safety_limit(self, tmp_path):
        import sleeptime as _sl

        inst_path, db_path = self._setup(tmp_path, n_cards=5, batch_size=2)
        hk = _make_hk(inst_path)
        provider = _fake_provider(response=_NO_CHANGES_RESPONSE)

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage3_bootstrap(
                "test_instance", now=_NOW, max_cards_override=2
            )

        assert totals["status"] == "partial"
        assert totals.get("safety_limit_reached") is True
        assert totals["applied_cards"] == 2
        assert totals["backlog"] == 3

    def test_bootstrap_isolates_failing_card_and_continues(self, tmp_path):
        """1 件の整形失敗でキュー全体を停止させない（§6 単独失敗の隔離）。"""
        import sleeptime as _sl
        from butly_core.core.knowledge_maturation import select_queue_cards

        inst_path, db_path = self._setup(tmp_path, n_cards=0, batch_size=3)
        _insert_card(db_path, "poison_card", created_at="2026-01-01 00:00:00")
        _insert_card(db_path, "good_a", created_at="2026-01-02 00:00:00")
        _insert_card(db_path, "good_b", created_at="2026-01-03 00:00:00")

        def _classify(prompt, conf):
            if "poison_card" in prompt:
                return "garbage output"
            return _NO_CHANGES_RESPONSE

        hk = _make_hk(inst_path)
        provider = _fake_provider()
        provider.classify.side_effect = _classify

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            totals = hk.stage3_bootstrap("test_instance", now=_NOW)

        assert totals["status"] == "partial"
        assert totals["failed_cards"] == ["poison_card"]
        assert totals["applied_cards"] == 2
        # poison はstampされず、次の invocation で再選択できる
        remaining = [c["id"] for c in select_queue_cards(db_path, batch_size=10)]
        assert remaining == ["poison_card"]

    def test_bootstrap_resumes_from_queue(self, tmp_path):
        """安全上限で止めた続きを再実行で drain できる（冪等・再開）。"""
        import sleeptime as _sl

        inst_path, db_path = self._setup(tmp_path, n_cards=5, batch_size=2)
        hk = _make_hk(inst_path)
        provider = _fake_provider(response=_NO_CHANGES_RESPONSE)

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=provider):
            first = hk.stage3_bootstrap(
                "test_instance", now=_NOW, max_cards_override=2
            )
            second = hk.stage3_bootstrap("test_instance", now=_NOW)

        assert first["applied_cards"] == 2
        assert second["applied_cards"] == 3
        assert second["status"] == "completed"
        assert second["backlog"] == 0

    def test_bootstrap_locked(self, tmp_path):
        from butly_core.core import knowledge_maturation as km

        inst_path, db_path = self._setup(tmp_path, n_cards=1)
        hk = _make_hk(inst_path)
        with km.stage3_process_lock(inst_path) as acquired:
            assert acquired
            totals = hk.stage3_bootstrap("test_instance", now=_NOW)
        assert totals["status"] == "locked"


# ===================================================================
# 8. should_run_stage_3 gating
# ===================================================================

class TestShouldRunStage3:
    def test_disabled_by_default(self):
        import sleeptime as _sl

        hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
        hk.instances_dir = Path("/tmp")
        assert hk._should_run_stage_3({}) is False

    def test_enabled_by_memory_flag(self):
        import sleeptime as _sl

        hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
        hk.instances_dir = Path("/tmp")
        cfg = {
            "memory": {"knowledge_maturation_enabled": True},
            "sleeptime": {"update_targets": {"knowledge_maturation": True}},
        }
        assert hk._should_run_stage_3(cfg) is True

    def test_blocked_by_update_targets(self):
        import sleeptime as _sl

        hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
        hk.instances_dir = Path("/tmp")
        cfg = {
            "memory": {"knowledge_maturation_enabled": True},
            "sleeptime": {"update_targets": {"knowledge_maturation": False}},
        }
        assert hk._should_run_stage_3(cfg) is False


# ===================================================================
# 9. MemoryBlockBuilder active_nodes opt-in
# ===================================================================

class TestActiveNodesInRag:
    def _mm(self):
        mm = MagicMock()
        mm.load_recent_sessions.return_value = ([], None)
        mm.get_session_digest.return_value = ""
        mm.get_mid_term_digest.return_value = ""
        mm.get_recent_snapshot.return_value = ""
        mm.get_raw_memory.return_value = ""
        return mm

    def test_active_nodes_not_added_when_disabled(self, tmp_path):
        from butly_core.core.gatekeeper.memory_builder import MemoryBlockBuilder

        gk = {
            "need": "rag_search",
            "need_intent": "past_fact",
            "topic": "",
            "memory_probe": {
                "status": "hit",
                "candidates": [{"id": "c1", "title": "t", "summary": "s"}],
                "glossary_hits": [],
            },
        }
        blocks = MemoryBlockBuilder().build(
            tier="mid",
            memory_manager=self._mm(),
            brain=MagicMock(),
            user_input="hi",
            gatekeeper_output=gk,
            override_config={"memory": {"knowledge_maturation_enabled": False}},
        )
        assert "active_nodes" not in blocks

    def test_active_nodes_added_when_enabled(self, tmp_path):
        from butly_core.core.database import ButlyDatabase
        from butly_core.core.memory_nodes import MemoryNodeRepository
        from butly_core.core.gatekeeper.memory_builder import MemoryBlockBuilder

        # 実 DB を作って node + source を投入
        db = tmp_path / "butly_memory.db"
        ButlyDatabase(db_path=str(db))
        _insert_card(str(db), "c1")
        repo = MemoryNodeRepository(str(db))
        nid = repo.create_node(
            kind="preference", statement="user likes fruit",
            confidence=0.9, status="active",
        )
        repo.upsert_source(node_id=nid, card_id="c1", relation="supports")

        brain = MagicMock()
        brain._get_db_path = lambda _inst: db

        gk = {
            "need": "rag_search",
            "need_intent": "past_fact",
            "topic": "",
            "memory_probe": {
                "status": "hit",
                "candidates": [{"id": "c1", "title": "t", "summary": "s"}],
                "glossary_hits": [],
            },
        }
        blocks = MemoryBlockBuilder().build(
            tier="mid",
            memory_manager=self._mm(),
            brain=brain,
            user_input="hi",
            gatekeeper_output=gk,
            override_config={"memory": {"knowledge_maturation_enabled": True}},
        )
        assert blocks["active_nodes"]
        assert blocks["active_nodes"][0]["statement"] == "user likes fruit"
