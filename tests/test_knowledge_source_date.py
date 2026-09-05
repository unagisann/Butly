"""knowledge_cards の source_date / source_files と time decay の回帰テスト。

- insert_knowledge が元会話の日付と RAW ファイル名リストを保存する
- time decay が created_at（カード作成日時）ではなく source_date
  （出来事の古さ）を基準に計算される
- decay の「現在」が Chronos の固定時刻（BUTLY_CHRONOS_NOW）を尊重する
"""

import json
import math
import sqlite3

import numpy as np
import pytest

from butly_core.core.brain import ButlyBrain, _decay_basis_datetime
from butly_core.core.database import ButlyDatabase
from butly_core.llm.errors import EmbeddingUnavailable
from sleeptime import ButlySleeptime


def _make_instance_db(tmp_path, instance_name="test_inst"):
    instance_dir = tmp_path / "butly_core" / "instances" / instance_name
    instance_dir.mkdir(parents=True)
    db_path = instance_dir / "butly_memory.db"
    ButlyDatabase(db_path=str(db_path))
    return db_path


class TestDecayBasis:
    def test_source_date_takes_precedence(self):
        basis = _decay_basis_datetime(
            {"source_date": "2023-05-08", "created_at": "2026-07-11 06:58:32"}
        )
        assert basis.year == 2023 and basis.month == 5 and basis.day == 8

    def test_falls_back_to_created_at(self):
        basis = _decay_basis_datetime(
            {"source_date": None, "created_at": "2026-07-11 06:58:32"}
        )
        assert basis.year == 2026

    def test_none_when_unparseable(self):
        assert _decay_basis_datetime({"source_date": "??", "created_at": ""}) is None


class TestKnowledgeSelectCols:
    def test_includes_source_columns_when_present(self, tmp_path):
        """マイグレーション済み DB では source_date / source_files も SELECT する"""
        db_path = _make_instance_db(tmp_path)
        conn = sqlite3.connect(db_path)
        cols = ButlyBrain._knowledge_select_cols(conn.cursor())
        conn.close()
        assert "source_date" in cols
        assert "source_files" in cols

    def test_base_columns_only_for_legacy_schema(self, tmp_path):
        """未マイグレーションの旧スキーマでは base カラムのみ"""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE knowledge_cards ("
            "id TEXT, title TEXT, summary TEXT, episode TEXT, type TEXT, "
            "embedding_blob BLOB, created_at TEXT, is_archived INTEGER)"
        )
        cols = ButlyBrain._knowledge_select_cols(conn.cursor())
        conn.close()
        assert "source_date" not in cols
        assert "source_files" not in cols


class TestQuickVectorSearchErrorPath:
    def test_returns_diag_dict_on_db_error(self, tmp_path, monkeypatch):
        """DB 破損等の例外時も diag dict 契約を守る（list を返すと呼び出し側で TypeError）"""
        inst = "broken_inst"
        inst_dir = tmp_path / "butly_core" / "instances" / inst
        inst_dir.mkdir(parents=True)
        (inst_dir / "butly_memory.db").write_text("not a sqlite database")

        brain = ButlyBrain(base_dir=tmp_path)
        monkeypatch.setattr(brain, "get_embedding", lambda *a, **k: [0.1, 0.2])

        diag = brain.quick_vector_search_diag("query", inst)
        assert diag["results"] == []
        assert diag["diagnostics"]["fetched_count"] == 0


class TestQuickVectorSearchCandidateScope:
    def test_accepts_precomputed_query_embedding(self, tmp_path, monkeypatch):
        db_path = _make_instance_db(tmp_path)
        stored = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_cards (
                    id, category, title, summary, embedding_blob
                ) VALUES ('match', 'Life', 'match', 's', ?)
                """,
                (stored,),
            )

        brain = ButlyBrain(tmp_path)
        monkeypatch.setattr(
            brain,
            "get_embedding",
            lambda *_args, **_kwargs: pytest.fail(
                "precomputed query must avoid another embedding call"
            ),
        )

        result = brain.quick_vector_search_diag(
            "query",
            "test_inst",
            limit=1,
            threshold=0.9,
            override_config={"brain": {"time_decay_rate": 0.0}},
            query_embedding=np.array([1.0, 0.0], dtype=np.float32),
        )

        assert [item["id"] for item in result["results"]] == ["match"]

    def test_hybrid_accepts_precomputed_query_embedding(
        self,
        tmp_path,
        monkeypatch,
    ):
        db_path = _make_instance_db(tmp_path)
        stored = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_cards (
                    id, category, title, summary, embedding_blob
                ) VALUES ('match', 'Life', 'unrelated', 's', ?)
                """,
                (stored,),
            )

        brain = ButlyBrain(tmp_path)
        monkeypatch.setattr(
            brain,
            "get_embedding",
            lambda *_args, **_kwargs: pytest.fail(
                "hybrid must reuse the precomputed query embedding"
            ),
        )

        result = brain.quick_vector_search_diag(
            "query",
            "test_inst",
            limit=1,
            threshold=0.9,
            override_config={
                "brain": {
                    "search_mode": "hybrid",
                    "time_decay_rate": 0.0,
                    "vector_candidates": 1,
                    "bm25_candidates": 1,
                }
            },
            query_embedding=np.array([1.0, 0.0], dtype=np.float32),
        )

        assert [item["id"] for item in result["results"]] == ["match"]

    def test_scores_older_card_outside_keyword_fallback_limit(
        self,
        tmp_path,
        monkeypatch,
    ):
        db_path = _make_instance_db(tmp_path)
        old_match = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        recent_miss = np.array([0.0, 1.0], dtype=np.float32).tobytes()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_cards (
                    id, category, title, summary, embedding_blob,
                    created_at, updated_at, source_date
                ) VALUES (
                    'old_match', 'Life', 'old match', 's', ?,
                    '2023-05-01 00:00:00', '2023-05-01 00:00:00',
                    '2023-05-01'
                )
                """,
                (old_match,),
            )
            conn.executemany(
                """
                INSERT INTO knowledge_cards (
                    id, category, title, summary, embedding_blob,
                    created_at, updated_at, source_date
                ) VALUES (?, 'Life', ?, 's', ?, ?, ?, ?)
                """,
                [
                    (
                        f"recent_{index:03d}",
                        f"recent {index:03d}",
                        recent_miss,
                        f"2024-02-{(index % 28) + 1:02d} 00:00:00",
                        f"2024-02-{(index % 28) + 1:02d} 00:00:00",
                        f"2024-02-{(index % 28) + 1:02d}",
                    )
                    for index in range(50)
                ],
            )

        brain = ButlyBrain(tmp_path)
        monkeypatch.setattr(
            brain,
            "get_embedding",
            lambda text, conf=None: [1.0, 0.0],
        )

        diag = brain.quick_vector_search_diag(
            "old fact",
            "test_inst",
            limit=1,
            threshold=0.9,
            override_config={
                "brain": {
                    "fallback_fetch_limit": 50,
                    "time_decay_rate": 0.0,
                }
            },
        )

        assert [result["id"] for result in diag["results"]] == ["old_match"]
        assert diag["diagnostics"]["fetch_limit"] is None
        assert diag["diagnostics"]["fetched_count"] == 51


class TestInsertKnowledgeSourceColumns:
    def test_insert_stores_source_date_and_files(self, tmp_path, monkeypatch):
        db_path = _make_instance_db(tmp_path)
        sleeptime = ButlySleeptime(
            base_dir=tmp_path,
            instances_dir=tmp_path / "butly_core" / "instances",
        )
        monkeypatch.setattr(
            sleeptime, "generate_embedding", lambda text, instance_name=None: [0.1, 0.2]
        )

        card = {
            "category": "Life",
            "title": "Pottery class",
            "tags": "pottery",
            "ai_importance": 5,
            "humanity_importance": 5,
            "summary": "- Maya joined the club on 2024-04-08\n- planned a blue mug",
            "episode": "She sounded proud.",
        }
        ok = sleeptime.insert_knowledge(
            card,
            "test_inst_20240408_001",
            "test_inst",
            "2024-04-08_raw_combined",
            str(db_path),
            source_date="2024-04-08",
            source_files=["session_20240408_103000_000000.json"],
        )
        assert ok is True

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute("SELECT * FROM knowledge_cards").fetchone())
        assert row["source_date"] == "2024-04-08"
        assert json.loads(row["source_files"]) == [
            "session_20240408_103000_000000.json"
        ]
        # summary の改行はそのまま保存される
        assert "\n" in row["summary"]

    def test_insert_without_source_info_keeps_nulls(self, tmp_path, monkeypatch):
        db_path = _make_instance_db(tmp_path)
        sleeptime = ButlySleeptime(
            base_dir=tmp_path,
            instances_dir=tmp_path / "butly_core" / "instances",
        )
        monkeypatch.setattr(
            sleeptime, "generate_embedding", lambda text, instance_name=None: [0.1, 0.2]
        )
        card = {
            "category": "Life",
            "title": "t",
            "tags": "",
            "ai_importance": 1,
            "humanity_importance": 1,
            "summary": "s",
            "episode": "e",
        }
        assert sleeptime.insert_knowledge(
            card, "id_001", "test_inst", "ref", str(db_path)
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT source_date, source_files FROM knowledge_cards"
            ).fetchone()
        assert row == (None, None)

    def test_insert_skipped_when_embedding_unavailable(self, tmp_path, monkeypatch):
        """ベクトルが取れないカードは保存しない。

        以前は embedding_blob=NULL で INSERT していたため、RAG から永久に
        見えないカードが残り、RAW は処理済みへ移動されて気づけなかった。
        """
        db_path = _make_instance_db(tmp_path)
        sleeptime = ButlySleeptime(
            base_dir=tmp_path,
            instances_dir=tmp_path / "butly_core" / "instances",
        )

        def _boom(text, instance_name=None):
            raise EmbeddingUnavailable("429")

        monkeypatch.setattr(sleeptime, "generate_embedding", _boom)
        card = {
            "category": "Life",
            "title": "t",
            "tags": "",
            "ai_importance": 1,
            "humanity_importance": 1,
            "summary": "s",
            "episode": "e",
        }
        with pytest.raises(EmbeddingUnavailable):
            sleeptime.insert_knowledge(
                card, "id_001", "test_inst", "ref", str(db_path)
            )

        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_cards"
            ).fetchone()[0] == 0


class TestVectorSearchDecayUsesSourceDate:
    def _insert_card(self, db_path, card_id, source_date):
        blob = np.array([1.0, 0.0, 0.0], dtype=np.float32).tobytes()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_cards (
                    id, category, title, summary, embedding_blob,
                    created_at, updated_at, source_date
                ) VALUES (?, 'Life', ?, 's', ?, ?, ?, ?)
                """,
                (
                    card_id,
                    card_id,
                    blob,
                    # created_at は両カードとも同じ（source_date 優先の証明用）
                    "2024-05-20 12:00:00",
                    "2024-05-20 12:00:00",
                    source_date,
                ),
            )

    def test_older_event_decays_more(self, tmp_path, monkeypatch):
        db_path = _make_instance_db(tmp_path)
        self._insert_card(db_path, "recent_card", "2024-05-01")
        self._insert_card(db_path, "old_card", "2023-05-01")

        brain = ButlyBrain(tmp_path)
        monkeypatch.setattr(
            brain, "get_embedding", lambda text, conf=None: [1.0, 0.0, 0.0]
        )
        monkeypatch.setenv("BUTLY_CHRONOS_NOW", "2024-05-21T00:00:00")

        diag = brain.quick_vector_search_diag(
            "pottery", "test_inst", limit=5, threshold=0.0
        )
        results = diag["results"]

        assert [r["id"] for r in results] == ["recent_card", "old_card"]
        # cosine は両方 1.0。差は source_date 由来の decay のみ。
        from butly_core.config import SYSTEM_CONFIG

        decay_rate = SYSTEM_CONFIG["brain"].get("time_decay_rate", 0.005)
        assert results[0]["score"] == pytest.approx(
            math.exp(-decay_rate * 20), rel=1e-3
        )
        assert results[1]["score"] == pytest.approx(
            math.exp(-decay_rate * 386), rel=1e-3
        )


class TestResolveCardSourceFiles:
    """カード自己申告 source_files の検証（幻覚耐性とフォールバック）。

    LLM が「そのカードの根拠ファイル」を出すが、名前を幻覚しうる。実在する
    チャンク内ファイルだけを採用し、絞れないときはチャンク全体に戻す。
    """

    CHUNK = ["session_a.json", "session_b.json", "session_c.json"]

    def _resolve(self, card):
        return ButlySleeptime.resolve_card_source_files(card, self.CHUNK)

    def test_valid_subset_becomes_card_granularity(self):
        files, gran = self._resolve({"source_files": ["session_b.json"]})
        assert files == ["session_b.json"]
        assert gran == "card"

    def test_hallucinated_names_dropped(self):
        files, gran = self._resolve(
            {"source_files": ["session_b.json", "does_not_exist.json"]}
        )
        assert files == ["session_b.json"]
        assert gran == "card"

    def test_all_hallucinated_falls_back_to_chunk(self):
        files, gran = self._resolve({"source_files": ["ghost.json"]})
        assert files == self.CHUNK
        assert gran == "chunk"

    def test_missing_or_empty_falls_back_to_chunk(self):
        for card in ({}, {"source_files": []}, {"source_files": None}):
            files, gran = self._resolve(card)
            assert files == self.CHUNK
            assert gran == "chunk"

    def test_full_set_counts_as_chunk(self):
        """全ファイルを挙げた＝絞れていないので chunk 扱い"""
        files, gran = self._resolve({"source_files": list(self.CHUNK)})
        assert files == self.CHUNK
        assert gran == "chunk"

    def test_path_prefixed_name_matched_by_basename(self):
        files, gran = self._resolve(
            {"source_files": ["2_knowledgeized/2023-05-08/session_c.json"]}
        )
        assert files == ["session_c.json"]
        assert gran == "card"

    def test_string_instead_of_list_accepted(self):
        files, gran = self._resolve({"source_files": "session_a.json"})
        assert files == ["session_a.json"]
        assert gran == "card"

    def test_duplicates_and_noise_normalized(self):
        files, gran = self._resolve(
            {"source_files": ["`session_a.json`", "session_a.json", 42, "  "]}
        )
        assert files == ["session_a.json"]
        assert gran == "card"

    def test_non_dict_card_falls_back(self):
        files, gran = ButlySleeptime.resolve_card_source_files("nope", self.CHUNK)
        assert files == self.CHUNK
        assert gran == "chunk"
