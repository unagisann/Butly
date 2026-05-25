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
# 3. collect_review_cards
# ===================================================================

class TestCollectReviewCards:
    def test_prioritises_usage_count(self, db_path):
        from butly_core.core.knowledge_maturation import collect_review_cards

        _insert_card(db_path, "card_hot", usage_count=5)
        _insert_card(db_path, "card_cold", usage_count=0)
        _insert_card(db_path, "card_warm", usage_count=2)

        cards = collect_review_cards(
            db_path, window_days=0, max_cards=10, min_usage_count=1
        )
        ids = [c["id"] for c in cards]
        assert "card_hot" in ids
        assert "card_warm" in ids
        # min_usage_count=1 のため card_cold は含まれない
        assert "card_cold" not in ids
        # 使用回数降順
        assert ids.index("card_hot") < ids.index("card_warm")

    def test_falls_back_to_access_logs(self, db_path):
        from butly_core.core.knowledge_maturation import collect_review_cards

        _insert_card(db_path, "card_recent", usage_count=0)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO access_logs (card_id, accessed_at) VALUES (?, ?)",
            ("card_recent", datetime.now().date().isoformat()),
        )
        conn.commit()
        conn.close()

        cards = collect_review_cards(
            db_path, window_days=7, max_cards=5, min_usage_count=99
        )
        assert any(c["id"] == "card_recent" for c in cards)

    def test_max_cards_zero(self, db_path):
        from butly_core.core.knowledge_maturation import collect_review_cards

        _insert_card(db_path, "x", usage_count=10)
        cards = collect_review_cards(db_path, window_days=0, max_cards=0, min_usage_count=0)
        assert cards == []


# ===================================================================
# 4. parse_llm_output
# ===================================================================

class TestParseLlmOutput:
    def test_extracts_fenced_json(self):
        from butly_core.core.knowledge_maturation import parse_llm_output

        out = parse_llm_output('```json\n{"link_existing": [{"node_id": "n", "card_id": "c"}], "new_nodes": []}\n```')
        assert out["link_existing"][0]["node_id"] == "n"
        assert out["new_nodes"] == []

    def test_handles_bare_json(self):
        from butly_core.core.knowledge_maturation import parse_llm_output

        out = parse_llm_output('{"link_existing": [], "new_nodes": [{"statement": "x"}]}')
        assert out["new_nodes"][0]["statement"] == "x"

    def test_invalid_json_returns_empty(self):
        from butly_core.core.knowledge_maturation import parse_llm_output

        out = parse_llm_output("not json at all")
        assert out == {"link_existing": [], "new_nodes": []}

    def test_normalises_missing_keys(self):
        from butly_core.core.knowledge_maturation import parse_llm_output

        out = parse_llm_output("{}")
        assert out == {"link_existing": [], "new_nodes": []}


# ===================================================================
# 5. apply_link_existing / apply_new_nodes
# ===================================================================

class TestApply:
    def test_link_existing_skips_invalid_ids(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_link_existing

        existing = repo.create_node(kind="fact", statement="x")
        _insert_card(db_path, "card_1")

        rid = repo.start_run("test")
        linked, uncertain = apply_link_existing(
            repo=repo,
            entries=[
                {"node_id": existing, "card_id": "card_1", "relation": "supports", "confidence": 0.9},
                {"node_id": "nope", "card_id": "card_1", "relation": "supports"},  # 無効 node
                {"node_id": existing, "card_id": "missing", "relation": "supports"},  # 無効 card
                {"node_id": existing, "card_id": "card_1", "relation": "bogus"},  # 無効 relation
            ],
            valid_node_ids={existing},
            valid_card_ids={"card_1"},
            run_id=rid,
        )
        assert linked == 1
        assert uncertain == []
        assert repo.count_sources(existing) == 1

    def test_link_existing_contradicts_marks_uncertain(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_link_existing

        existing = repo.create_node(kind="fact", statement="x")
        _insert_card(db_path, "c1")
        _insert_card(db_path, "c2")

        rid = repo.start_run("test")
        _, uncertain = apply_link_existing(
            repo=repo,
            entries=[
                {"node_id": existing, "card_id": "c1", "relation": "contradicts"},
                {"node_id": existing, "card_id": "c2", "relation": "contradicts"},
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
        created, sup = apply_new_nodes(
            repo=repo,
            entries=[
                {"kind": "preference", "statement": "low conf", "confidence": 0.3, "source_card_ids": ["c1"]},
            ],
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
        created, sup = apply_new_nodes(
            repo=repo,
            entries=[
                {
                    "kind": "preference",
                    "statement": "stable preference",
                    "confidence": 0.8,
                    "source_card_ids": ["c1", "c2"],
                },
            ],
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

    def test_supersede_applied(self, repo, db_path):
        from butly_core.core.knowledge_maturation import apply_new_nodes

        _insert_card(db_path, "c1")
        old = repo.create_node(kind="preference", statement="old", status="active")

        rid = repo.start_run("test")
        created, sup = apply_new_nodes(
            repo=repo,
            entries=[
                {
                    "kind": "preference",
                    "statement": "new",
                    "confidence": 0.9,
                    "source_card_ids": ["c1"],
                    "supersedes_node_id": old,
                },
            ],
            valid_card_ids={"c1"},
            run_id=rid,
            candidate_threshold=0.65,
            active_threshold=0.75,
        )
        assert created == 1
        assert sup == 1
        assert repo.get_node(old)["status"] == "superseded"


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


# ===================================================================
# 7. End-to-end stage_3_mature_knowledge (LLM モック)
# ===================================================================

class TestStage3EndToEnd:
    def _build_instance(self, tmp_path: Path, instance_name: str = "test_instance") -> Path:
        instances_dir = tmp_path / "butly_core" / "instances"
        inst = instances_dir / instance_name
        inst.mkdir(parents=True)
        (inst / "system_instruction.txt").write_text("テスト用 AI", encoding="utf-8")
        (inst / "Key_Memory.txt").write_text("テストユーザー", encoding="utf-8")
        return inst

    def test_run_creates_nodes_and_completes(self, tmp_path, monkeypatch):
        from butly_core.core.database import ButlyDatabase
        import sleeptime as _sl

        inst_path = self._build_instance(tmp_path)
        db_path = str(inst_path / "butly_memory.db")
        ButlyDatabase(db_path=db_path)
        _insert_card(db_path, "c_001", usage_count=3)
        _insert_card(db_path, "c_002", usage_count=2)

        # INSTANCES_DIR を tmp に差し替え
        monkeypatch.setattr(_sl, "INSTANCES_DIR", inst_path.parent)

        hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
        hk.instances_dir = inst_path.parent
        hk.instruction = "fallback instruction"
        hk.key_memory = "fallback memory"

        # provider をモック化: link + new_node を返す
        fake_response = json.dumps(
            {
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

        fake_provider = MagicMock()
        fake_provider.classify.return_value = fake_response

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=fake_provider):
            hk.stage_3_mature_knowledge(inst_path)

        # node が 1 つ作られている
        from butly_core.core.memory_nodes import MemoryNodeRepository

        repo = MemoryNodeRepository(db_path)
        nodes = repo.find_nodes(statuses=["active", "candidate"])
        assert len(nodes) == 1
        n = nodes[0]
        assert n["status"] == "active"
        assert n["statement"] == "ユーザーはフルーツが好き"
        assert repo.count_sources(n["id"], relation="supports") == 2

        # run table に completed として記録されている
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, created_node_count, linked_source_count FROM memory_maturation_runs"
        ).fetchone()
        conn.close()
        assert row[0] == "completed"
        assert row[1] == 1

    def test_run_skipped_when_no_review_cards(self, tmp_path, monkeypatch):
        from butly_core.core.database import ButlyDatabase
        import sleeptime as _sl

        inst_path = self._build_instance(tmp_path)
        db_path = str(inst_path / "butly_memory.db")
        ButlyDatabase(db_path=db_path)
        # カードを入れない

        monkeypatch.setattr(_sl, "INSTANCES_DIR", inst_path.parent)
        hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
        hk.instances_dir = inst_path.parent
        hk.instruction = "x"
        hk.key_memory = "y"

        hk.stage_3_mature_knowledge(inst_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status FROM memory_maturation_runs"
        ).fetchone()
        conn.close()
        assert row[0] == "skipped"

    def test_run_failure_recorded(self, tmp_path, monkeypatch):
        from butly_core.core.database import ButlyDatabase
        import sleeptime as _sl

        inst_path = self._build_instance(tmp_path)
        db_path = str(inst_path / "butly_memory.db")
        ButlyDatabase(db_path=db_path)
        _insert_card(db_path, "c1", usage_count=2)

        monkeypatch.setattr(_sl, "INSTANCES_DIR", inst_path.parent)
        hk = _sl.ButlySleeptime.__new__(_sl.ButlySleeptime)
        hk.instances_dir = inst_path.parent
        hk.instruction = "x"
        hk.key_memory = "y"

        # provider が例外を投げる
        fake_provider = MagicMock()
        fake_provider.classify.side_effect = RuntimeError("LLM exploded")

        with patch.object(_sl.ButlySleeptime, "_get_provider", return_value=fake_provider):
            hk.stage_3_mature_knowledge(inst_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, error FROM memory_maturation_runs"
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
        assert "LLM exploded" in (row[1] or "")


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
