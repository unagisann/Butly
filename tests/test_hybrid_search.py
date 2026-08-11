"""
test_hybrid_search.py
─────────────────────
ハイブリッド検索（FTS5/trigram の BM25 + ベクトル / RRF 融合）の単体テスト。

trigram tokenizer の素の挙動（語境界を見ない・2文字語を引けない・高DF語が
全カードに当たる）に対する3段補正が効いているかを重点的に見る。
"""

import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from butly_core.core import hybrid_search as hs
from butly_core.core import brain as brain_module
from butly_core.core.brain import ButlyBrain
from butly_core.core.database import ButlyDatabase


# ===================================================================
# ヘルパー
# ===================================================================

def _make_db(db_path: Path, cards: list[dict]) -> None:
    """ButlyDatabase の初期化（FTS 索引・トリガ込み）でカードを入れる。"""
    ButlyDatabase(db_path=str(db_path))
    conn = sqlite3.connect(db_path)
    for card in cards:
        emb = card.get("embedding")
        blob = np.array(emb, dtype=np.float32).tobytes() if emb else None
        conn.execute(
            "INSERT INTO knowledge_cards "
            "(id, category, title, tags, summary, episode, embedding_blob, "
            " is_archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card["id"],
                card.get("category", "Life"),
                card["title"],
                card.get("tags", ""),
                card.get("summary", ""),
                card.get("episode", ""),
                blob,
                int(card.get("is_archived", 0)),
            ),
        )
    conn.commit()
    conn.close()


def _fts_rows(db_path: Path) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT card_id, title FROM {hs.FTS_TABLE} ORDER BY card_id"
        ).fetchall()
    finally:
        conn.close()


def _bm25(db_path: Path, query: str, **kwargs) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        spec = hs.build_fts_query(query)
        params = {
            "columns": ["id", "title", "summary", "episode", "is_archived"],
            "limit": 10,
        }
        params.update(kwargs)
        return hs.bm25_candidates(conn, spec, **params)
    finally:
        conn.close()


# ===================================================================
# クエリビルダ
# ===================================================================

class TestBuildFtsQuery:
    def test_drops_short_and_stopwords(self):
        spec = hs.build_fts_query("What pets does Melanie have?")
        assert "pets" in spec.terms
        assert "melanie" in spec.terms
        assert "what" not in spec.terms  # stopword
        assert "do" not in spec.terms  # 3文字未満

    def test_normalizes_fullwidth_and_case(self):
        spec = hs.build_fts_query("ＰＯＴＴＥＲＹ")
        assert spec.terms == ("pottery",)

    def test_cjk_three_char_shingles(self):
        spec = hs.build_fts_query("陶芸教室の話")
        assert "陶芸教" in spec.terms
        assert "芸教室" in spec.terms

    def test_two_char_cjk_goes_to_short_terms(self):
        """trigram では引けない2文字語は LIKE 補助側へ回す"""
        spec = hs.build_fts_query("陶芸 について")
        assert "陶芸" in spec.short_terms
        assert "陶芸" not in spec.terms

    def test_quotes_are_doubled(self):
        """MATCH 構文をユーザー入力で壊されない"""
        spec = hs.build_fts_query('he said "necklace" loudly')
        assert '"necklace"' in spec.match_expr
        assert spec.match_expr.count('"') % 2 == 0

    def test_max_terms_cap(self):
        spec = hs.build_fts_query("あ" * 200, max_terms=5)
        assert len(spec.terms) <= 5

    def test_empty_input(self):
        assert hs.build_fts_query("").is_empty()


class TestTermMatches:
    def test_ascii_requires_word_start(self):
        assert hs.term_matches("cat", "catalogs and communication", is_ascii=True) is False
        assert hs.term_matches("pet", "the carpet is red", is_ascii=True) is False

    def test_ascii_allows_short_suffix(self):
        assert hs.term_matches("pet", "she has two pets", is_ascii=True) is True
        assert hs.term_matches("pottery", "pottery", is_ascii=True) is True

    def test_cjk_is_substring(self):
        assert hs.term_matches("陶芸", "陶芸教室に行った", is_ascii=False) is True


# ===================================================================
# 索引の作成・同期
# ===================================================================

class TestFtsIndex:
    def test_created_on_db_init(self, tmp_path):
        db = tmp_path / "a.db"
        _make_db(db, [{"id": "1", "title": "pottery", "summary": "made a mug"}])
        assert _fts_rows(db) == [("1", "pottery")]

    def test_trigger_syncs_update_and_delete(self, tmp_path):
        db = tmp_path / "a.db"
        _make_db(db, [{"id": "1", "title": "pottery", "summary": "s"}])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE knowledge_cards SET title = 'necklace' WHERE id = '1'")
        conn.commit()
        assert _fts_rows(db) == [("1", "necklace")]
        conn.execute("DELETE FROM knowledge_cards WHERE id = '1'")
        conn.commit()
        conn.close()
        assert _fts_rows(db) == []

    def test_embedding_only_update_does_not_reindex(self, tmp_path):
        """embedding だけの migration で再索引を走らせない"""
        db = tmp_path / "a.db"
        _make_db(db, [{"id": "1", "title": "pottery", "summary": "s"}])
        conn = sqlite3.connect(db)
        before = conn.execute(
            f"SELECT rowid FROM {hs.FTS_TABLE} WHERE card_id = '1'"
        ).fetchone()
        conn.execute("UPDATE knowledge_cards SET embedding_blob = X'00' WHERE id = '1'")
        conn.commit()
        after = conn.execute(
            f"SELECT rowid FROM {hs.FTS_TABLE} WHERE card_id = '1'"
        ).fetchone()
        conn.close()
        assert before == after

    def test_backfill_on_count_mismatch(self, tmp_path):
        """トリガを経由しない書き込みで欠けた索引を件数不一致で検出する"""
        db = tmp_path / "a.db"
        _make_db(db, [{"id": "1", "title": "pottery", "summary": "s"}])
        conn = sqlite3.connect(db)
        conn.execute(f"DELETE FROM {hs.FTS_TABLE}")
        conn.commit()
        status = hs.ensure_fts_index(conn)
        conn.commit()
        conn.close()
        assert status["rebuilt"] is True
        assert _fts_rows(db) == [("1", "pottery")]

    def test_backfill_on_schema_version_change(self, tmp_path):
        db = tmp_path / "a.db"
        _make_db(db, [{"id": "1", "title": "pottery", "summary": "s"}])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE fts_meta SET schema_version = 0 WHERE id = 1")
        conn.commit()
        assert hs.fts_index_ready(conn) is False
        status = hs.ensure_fts_index(conn)
        conn.commit()
        conn.close()
        assert status["rebuilt"] is True

    def test_noop_when_in_sync(self, tmp_path):
        db = tmp_path / "a.db"
        _make_db(db, [{"id": "1", "title": "pottery", "summary": "s"}])
        conn = sqlite3.connect(db)
        status = hs.ensure_fts_index(conn)
        conn.close()
        assert status["available"] is True
        assert status["rebuilt"] is False

    def test_missing_fts5_does_not_raise(self, tmp_path, monkeypatch):
        db = tmp_path / "a.db"
        _make_db(db, [{"id": "1", "title": "pottery", "summary": "s"}])
        monkeypatch.setattr(hs, "fts5_trigram_available", lambda conn: False)
        conn = sqlite3.connect(db)
        status = hs.ensure_fts_index(conn)
        conn.close()
        assert status["available"] is False
        assert status["reason"] == "fts5_trigram_unavailable"

    def test_clone_init_does_not_touch_source(self, tmp_path):
        """rerun-qa の複製先を初期化しても元 run の DB は変わらない（R7）"""
        source = tmp_path / "source.db"
        _make_db(source, [{"id": "1", "title": "pottery", "summary": "s"}])
        original = source.read_bytes()
        clone = tmp_path / "clone.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(source) + suffix)
            if src.exists():
                shutil.copyfile(src, Path(str(clone) + suffix))
        conn = sqlite3.connect(clone)
        conn.execute(f"DELETE FROM {hs.FTS_TABLE}")
        conn.commit()
        conn.close()
        ButlyDatabase(db_path=str(clone))
        assert source.read_bytes() == original
        assert _fts_rows(clone) == [("1", "pottery")]


# ===================================================================
# BM25 候補（3段補正）
# ===================================================================

class TestBm25Candidates:
    @pytest.fixture
    def db(self, tmp_path):
        path = tmp_path / "cards.db"
        _make_db(path, [
            {"id": "c1", "title": "Melanie's pets", "tags": "pets",
             "summary": "Two cats and a dog"},
            {"id": "c2", "title": "Melanie's catalogs",
             "summary": "communication and catalogs"},
            {"id": "c3", "title": "Melanie's pottery",
             "summary": "made pottery with her kids"},
            {"id": "c4", "title": "陶芸教室", "summary": "陶芸を習っている"},
            {"id": "c5", "title": "Melanie's garden", "summary": "she grows herbs"},
            {"id": "c6", "title": "Melanie's commute", "summary": "train every day"},
        ])
        return path

    def test_word_boundary_filters_substring_hits(self, db):
        """trigram の部分文字列一致（cat→catalogs/communication）を落とす。

        cats は cat の語尾変化なので残る（落としたいのは語境界を跨ぐ誤爆）。
        """
        ids = [r["id"] for r in _bm25(db, "cat")["results"]]
        assert "c2" not in ids
        assert ids == ["c1"]

    def test_real_hit_survives(self, db):
        out = _bm25(db, "pottery")
        assert [r["id"] for r in out["results"]] == ["c3"]

    def test_plural_still_matches(self, db):
        out = _bm25(db, "pet")
        assert "c1" in [r["id"] for r in out["results"]]

    def test_high_df_term_alone_is_dropped(self, db):
        """ほぼ全カードに出る Melanie だけでは候補にしない"""
        out = _bm25(db, "Melanie")
        assert out["results"] == []
        assert "melanie" in out["diagnostics"]["weak_terms"]

    def test_min_weak_df_protects_small_db(self, tmp_path):
        """カードが少ない DB では比率が高くても弱い語にしない"""
        path = tmp_path / "small.db"
        _make_db(path, [
            {"id": "s1", "title": "pottery class", "summary": "clay"},
            {"id": "s2", "title": "pottery studio", "summary": "wheel"},
        ])
        out = _bm25(path, "pottery")
        assert sorted(r["id"] for r in out["results"]) == ["s1", "s2"]
        assert out["diagnostics"]["weak_terms"] == []

    def test_high_df_term_with_strong_term(self, db):
        out = _bm25(db, "What pets does Melanie have?")
        assert [r["id"] for r in out["results"]] == ["c1"]

    def test_short_cjk_term_via_like_fallback(self, db):
        """2文字語（trigram では 0 件）を LIKE 補助候補で拾う"""
        out = _bm25(db, "陶芸")
        assert [r["id"] for r in out["results"]] == ["c4"]
        assert out["diagnostics"]["short_term_hits"] == 1

    def test_archived_only_used_when_no_active(self, tmp_path):
        path = tmp_path / "arch.db"
        _make_db(path, [
            {"id": "a1", "title": "pottery class", "summary": "active"},
            {"id": "a2", "title": "pottery old", "summary": "archived",
             "is_archived": 1},
        ])
        assert [r["id"] for r in _bm25(path, "pottery")["results"]] == ["a1"]

        path2 = tmp_path / "arch2.db"
        _make_db(path2, [
            {"id": "b1", "title": "pottery old", "summary": "archived",
             "is_archived": 1},
        ])
        out = _bm25(path2, "pottery")
        assert [r["id"] for r in out["results"]] == ["b1"]
        assert out["diagnostics"]["archived_fallback"] is True

    def test_no_terms_returns_empty(self, db):
        out = _bm25(db, "?? !!")
        assert out["results"] == []


# ===================================================================
# RRF
# ===================================================================

class TestRrfFuse:
    def test_both_sources_rank_first(self):
        vector = [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.8}]
        bm25 = [{"id": "b", "bm25_score": -3.0}, {"id": "c", "bm25_score": -1.0}]
        fused = hs.rrf_fuse(vector, bm25, k=60, limit=3)
        assert [r["id"] for r in fused] == ["b", "a", "c"]
        assert fused[0]["retrieval_source"] == "both"
        assert fused[1]["retrieval_source"] == "vector"

    def test_score_is_rrf_score(self):
        """下流は score 降順で並べ直すので、score は RRF スコアでなければならない"""
        vector = [{"id": "a", "score": 0.9}]
        bm25 = [{"id": "b", "bm25_score": -3.0}]
        fused = hs.rrf_fuse(vector, bm25, k=60, limit=2)
        for row in fused:
            assert row["score"] == pytest.approx(row["rrf_score"])
            assert row["score"] == pytest.approx(1 / 61)
        assert sorted(fused, key=lambda r: -r["score"]) == fused

    def test_ranks_are_recorded(self):
        vector = [{"id": "a", "score": 0.9}]
        bm25 = [{"id": "a", "bm25_score": -3.0}]
        fused = hs.rrf_fuse(vector, bm25, k=60, limit=1)
        assert fused[0]["vector_rank"] == 1
        assert fused[0]["bm25_rank"] == 1
        assert fused[0]["rrf_score"] == pytest.approx(2 / 61)


# ===================================================================
# Brain 経由（Layer 1 / Layer 2）
# ===================================================================

def _unit(*values) -> list:
    vec = np.array(values, dtype=np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


@pytest.fixture
def hybrid_brain(tmp_path, monkeypatch):
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "00_master").mkdir(parents=True)
    _make_db(instances_dir / "00_master" / "butly_memory.db", [
        # 質問ベクトルと近いが字面は無関係なカード
        {"id": "v1", "title": "Melanie's weekend", "summary": "she relaxed",
         "embedding": _unit(1.0, 0.0, 0.0)},
        # 字面が一致するがベクトルは遠いカード（BM25 でしか拾えない）
        {"id": "k1", "title": "Pottery class", "summary": "made pottery with kids",
         "embedding": _unit(0.0, 0.0, 1.0)},
    ])
    brain = ButlyBrain(tmp_path)
    monkeypatch.setattr(brain, "get_embedding", lambda *a, **kw: _unit(1.0, 0.0, 0.0))
    return brain


class TestBrainHybrid:
    def test_vector_mode_misses_lexical_card(self, hybrid_brain):
        out = hybrid_brain.quick_vector_search_diag(
            "What pottery did they make?", "00_master", limit=3, threshold=0.4,
        )
        assert [r["id"] for r in out["results"]] == ["v1"]
        assert out["diagnostics"]["mode"] == "vector"

    def test_hybrid_mode_rescues_lexical_card(self, hybrid_brain):
        out = hybrid_brain.quick_vector_search_diag(
            "What pottery did they make?", "00_master", limit=3, threshold=0.4,
            override_config={"brain": {"search_mode": "hybrid"}},
        )
        ids = [r["id"] for r in out["results"]]
        assert "k1" in ids
        assert out["diagnostics"]["mode"] == "hybrid"
        assert out["diagnostics"]["bm25_candidate_ids"] == ["k1"]

    def test_hybrid_results_carry_provenance(self, hybrid_brain):
        out = hybrid_brain.quick_vector_search_diag(
            "What pottery did they make?", "00_master", limit=3, threshold=0.4,
            override_config={"brain": {"search_mode": "hybrid"}},
        )
        by_id = {r["id"]: r for r in out["results"]}
        assert by_id["k1"]["retrieval_source"] == "bm25"
        assert by_id["v1"]["retrieval_source"] == "vector"
        assert by_id["v1"]["vector_score"] == pytest.approx(1.0)
        # score は RRF スコア。cosine を上書きしていない
        assert by_id["v1"]["score"] != by_id["v1"]["vector_score"]

    def test_hybrid_evidence_fusion_reorders_top_n(
        self, hybrid_brain, monkeypatch
    ):
        class FakeCache:
            @staticmethod
            def diagnostics():
                return {
                    "hits": 1,
                    "misses": 2,
                    "writes": 2,
                    "errors": 0,
                    "by_kind": {},
                }

        class FakeFusion:
            def __init__(self, *_args, **_kwargs):
                self.cache = FakeCache()

            @staticmethod
            def embed_query(_question):
                return _unit(1.0, 0.0, 0.0)

            @staticmethod
            def rerank(_question, candidates, **_kwargs):
                original = [str(row["id"]) for row in candidates]
                selected = list(reversed(original))
                return {
                    "status": "completed",
                    "fallback": False,
                    "error": None,
                    "candidate_ids": selected,
                    "selected_candidate_ids": selected,
                    "evidence_candidate_ids": selected,
                    "scores": [],
                    "fusion_scores": [
                        {
                            "card_id": card_id,
                            "fusion_score": 1.0 / rank,
                            "evidence_score": 0.9 / rank,
                        }
                        for rank, card_id in enumerate(selected, start=1)
                    ],
                    "candidate_count": len(candidates),
                    "scored_count": len(candidates),
                    "latency_ms": 3,
                }

            @staticmethod
            def close():
                return None

        monkeypatch.setattr(brain_module, "RuntimeEvidenceFusion", FakeFusion)
        out = hybrid_brain.quick_vector_search_diag(
            "What pottery did they make?",
            "00_master",
            limit=2,
            threshold=0.4,
            override_config={
                "brain": {"search_mode": "hybrid_evidence_fusion"}
            },
        )

        assert [row["id"] for row in out["results"]] == ["k1", "v1"]
        assert out["diagnostics"]["mode"] == "hybrid_evidence_fusion"
        assert out["diagnostics"]["hybrid_candidate_ids"] == ["v1", "k1"]
        assert out["diagnostics"]["effective_candidate_ids"] == ["k1", "v1"]
        assert out["diagnostics"]["evidence_fusion"]["status"] == "completed"

    def test_hybrid_evidence_fusion_failure_preserves_hybrid(
        self, hybrid_brain, monkeypatch
    ):
        class BrokenFusion:
            def __init__(self, *_args, **_kwargs):
                raise RuntimeError("cache unavailable")

        monkeypatch.setattr(brain_module, "RuntimeEvidenceFusion", BrokenFusion)
        out = hybrid_brain.quick_vector_search_diag(
            "What pottery did they make?",
            "00_master",
            limit=2,
            threshold=0.4,
            override_config={
                "brain": {"search_mode": "hybrid_evidence_fusion"}
            },
        )

        assert [row["id"] for row in out["results"]] == ["v1", "k1"]
        diag = out["diagnostics"]
        assert diag["mode"] == "hybrid_evidence_fusion"
        assert diag["effective_candidate_ids"] == ["v1", "k1"]
        assert diag["evidence_fusion"]["fallback"] is True
        assert "cache unavailable" in diag["evidence_fusion"]["error"]

    def test_hybrid_survives_embedding_failure(self, hybrid_brain, monkeypatch):
        """embedding が落ちても BM25 側だけで候補を返せる"""
        monkeypatch.setattr(hybrid_brain, "get_embedding", lambda *a, **kw: None)
        out = hybrid_brain.quick_vector_search_diag(
            "pottery", "00_master", limit=3, threshold=0.4,
            override_config={"brain": {"search_mode": "hybrid"}},
        )
        assert [r["id"] for r in out["results"]] == ["k1"]

    def test_hybrid_falls_back_when_fts_missing(self, hybrid_brain, monkeypatch):
        monkeypatch.setattr(hs, "fts_index_ready", lambda conn: False)
        monkeypatch.setattr(
            hs, "ensure_fts_index",
            lambda conn: {"available": False, "reason": "fts5_trigram_unavailable"},
        )
        out = hybrid_brain.quick_vector_search_diag(
            "What pottery did they make?", "00_master", limit=3, threshold=0.4,
            override_config={"brain": {"search_mode": "hybrid"}},
        )
        assert [r["id"] for r in out["results"]] == ["v1"]

    def test_deep_search_drops_vector_threshold(self, hybrid_brain):
        """Layer 2（hybrid）は閾値ゲートを外して救済に回る。

        Layer 1 の閾値 0.4 では落ちる cosine 0 のカードも候補に入る。
        """
        results = hybrid_brain.search_knowledge(
            None, "unrelated question text", instance_name="00_master",
            override_config={"brain": {"search_mode": "hybrid"}},
        )
        assert [r["id"] for r in results] == ["v1", "k1"]

        gated = hybrid_brain.quick_vector_search_diag(
            "unrelated question text", "00_master", limit=3, threshold=0.4,
            override_config={"brain": {"search_mode": "hybrid"}},
        )
        assert [r["id"] for r in gated["results"]] == ["v1"]

    def test_layer2_keeps_positional_signature(self, hybrid_brain):
        """既存呼び出し（keywords を positional）が壊れていない"""
        results = hybrid_brain.search_knowledge(
            ["pottery"], "pottery", instance_name="00_master",
        )
        assert [r["id"] for r in results][:1] == ["v1"]


class TestBrainDualQuery:
    @staticmethod
    def _ranking(ids):
        return {
            "results": [
                {
                    "id": card_id,
                    "source_instance": "Jarvis",
                    "score": 0.9 - index * 0.05,
                    "retrieval_source": "vector",
                }
                for index, card_id in enumerate(ids)
            ],
            "raw_scores": [0.9],
            "final_scores": [0.9],
            "fetched_count": 10,
        }

    def test_fuses_original_and_gatekeeper_query(self, tmp_path, monkeypatch):
        brain = ButlyBrain(tmp_path)
        calls = []

        def fake_candidates(query, *_args, **_kwargs):
            calls.append(query)
            if query == "あれっていつだっけ？":
                return self._ranking(["original-only", "shared"])
            return self._ranking(["shared", "rewrite-only"])

        monkeypatch.setattr(brain, "_vector_query_candidates", fake_candidates)
        output = brain.quick_vector_search_diag(
            "あれっていつだっけ？",
            "Jarvis",
            limit=3,
            threshold=0.4,
            override_config={
                "brain": {
                    "search_mode": "dual_query",
                    "dual_query_candidates": 15,
                    "dual_query_pool_limit": 25,
                }
            },
            retrieval_query="由紀が陶芸教室へ行った日付",
        )
        diagnostics = output["diagnostics"]
        assert calls == ["あれっていつだっけ？", "由紀が陶芸教室へ行った日付"]
        assert [row["id"] for row in output["results"]][0] == "shared"
        assert diagnostics["mode"] == "dual_query"
        assert diagnostics["original_candidate_ids"] == [
            "original-only",
            "shared",
        ]
        assert diagnostics["retrieval_query_candidate_ids"] == [
            "shared",
            "rewrite-only",
        ]
        assert diagnostics["query_fusion"]["overlap_count"] == 1
        assert diagnostics["query_fusion"]["executed"] is True
        shared = output["results"][0]
        assert shared["query_source"] == "both"
        # Query overlap is not BM25 evidence and must not activate the
        # retrieval_assisted injection policy.
        assert shared["retrieval_source"] == "vector"

    def test_missing_rewrite_preserves_original_ranking(
        self, tmp_path, monkeypatch
    ):
        brain = ButlyBrain(tmp_path)
        calls = []

        def fake_candidates(query, *_args, **_kwargs):
            calls.append(query)
            return self._ranking(["first", "second"])

        monkeypatch.setattr(brain, "_vector_query_candidates", fake_candidates)
        output = brain.quick_vector_search_diag(
            "standalone question",
            "Jarvis",
            limit=3,
            threshold=0.4,
            override_config={"brain": {"search_mode": "dual_query"}},
            retrieval_query=None,
        )
        assert calls == ["standalone question"]
        assert [row["id"] for row in output["results"]] == ["first", "second"]
        assert output["results"][0]["score"] == pytest.approx(0.9)
        assert output["diagnostics"]["query_fusion"] == {
            "status": "fallback",
            "reason": "missing_or_same_query",
            "executed": False,
            "candidate_limit_per_query": 15,
            "pool_limit": 25,
            "original_count": 2,
            "retrieval_query_count": 0,
            "overlap_count": 0,
            "unique_count": 2,
        }
