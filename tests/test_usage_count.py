"""
tests/test_usage_count.py
-------------------------
usage_count / last_counted_at フィールドの実装テスト。

テスト範囲:
  - DBマイグレーション（列追加・冪等性・既存データ保護）
  - ButlyDatabase.record_card_usage() 単体
  - MemoryBlockBuilder が rag_card_ids を正しく設定するか
  - ChatService.execute / execute_stream の成功・失敗・rag=off・multi-instance 経路
"""

import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ===================================================================
# フィクスチャ
# ===================================================================

@pytest.fixture
def pre_migration_db(tmp_path):
    """マイグレーション前スキーマのDBを作成（usage_count / last_counted_at なし）。"""
    db_path = tmp_path / "butly_memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE knowledge_cards (
            id TEXT PRIMARY KEY,
            type TEXT DEFAULT 'Master',
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            tags TEXT,
            ai_importance INTEGER,
            humanity_importance INTEGER,
            summary TEXT,
            episode TEXT,
            count INTEGER DEFAULT 1,
            raw_reference TEXT,
            embedding_blob BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_pinned INTEGER DEFAULT 0,
            is_archived INTEGER DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO knowledge_cards (id, category, title, count) VALUES ('card_001', 'Tech', 'Test Card 1', 3)"
    )
    conn.execute(
        "INSERT INTO knowledge_cards (id, category, title, count) VALUES ('card_002', 'Tech', 'Test Card 2', 1)"
    )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def db(pre_migration_db):
    """マイグレーション適用済みの ButlyDatabase を返す。"""
    from butly_core.core.database import ButlyDatabase
    return ButlyDatabase(db_path=pre_migration_db)


def _fetch(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


# ===================================================================
# マイグレーションテスト
# ===================================================================

class TestMigration:
    def test_migration_adds_columns(self, pre_migration_db):
        """usage_count / last_counted_at が追加される。"""
        from butly_core.core.database import ButlyDatabase
        ButlyDatabase(db_path=pre_migration_db)

        conn = sqlite3.connect(pre_migration_db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_cards)")}
        conn.close()

        assert "usage_count" in cols
        assert "last_counted_at" in cols

    def test_migration_is_idempotent(self, pre_migration_db):
        """2回実行しても壊れない。"""
        from butly_core.core.database import ButlyDatabase
        ButlyDatabase(db_path=pre_migration_db)
        ButlyDatabase(db_path=pre_migration_db)  # 2回目はエラーにならない

        conn = sqlite3.connect(pre_migration_db)
        rows = conn.execute("SELECT id FROM knowledge_cards").fetchall()
        conn.close()
        assert len(rows) == 2

    def test_migration_preserves_existing_count(self, pre_migration_db):
        """マイグレーションで既存の count 値が変わらない。"""
        from butly_core.core.database import ButlyDatabase
        ButlyDatabase(db_path=pre_migration_db)

        row = _fetch(pre_migration_db, "SELECT count FROM knowledge_cards WHERE id = 'card_001'")
        assert row[0] == 3


# ===================================================================
# record_card_usage 単体テスト
# ===================================================================

class TestRecordCardUsage:
    def test_increment_increases_usage_count_and_sets_timestamp(self, db, pre_migration_db):
        """usage_count が 0→1 になり、last_counted_at が UTC ISO8601 で設定される。"""
        db.record_card_usage(["card_001"])

        row = _fetch(
            pre_migration_db,
            "SELECT usage_count, last_counted_at FROM knowledge_cards WHERE id = 'card_001'",
        )
        assert row[0] == 1
        assert row[1] is not None
        dt = datetime.fromisoformat(row[1])
        assert dt.tzinfo is not None  # timezone-aware

    def test_increment_skipped_within_dedup_window(self, db, pre_migration_db):
        """dedup_hours 以内に同じカードを渡しても usage_count / last_counted_at が変化しない。"""
        db.record_card_usage(["card_001"], dedup_hours=6)
        row_first = _fetch(
            pre_migration_db,
            "SELECT usage_count, last_counted_at FROM knowledge_cards WHERE id = 'card_001'",
        )

        db.record_card_usage(["card_001"], dedup_hours=6)
        row_second = _fetch(
            pre_migration_db,
            "SELECT usage_count, last_counted_at FROM knowledge_cards WHERE id = 'card_001'",
        )

        assert row_first[0] == 1
        assert row_second[0] == 1           # 変化なし
        assert row_second[1] == row_first[1]  # タイムスタンプも変化なし

    def test_increment_allowed_after_dedup_window(self, db, pre_migration_db):
        """dedup_hours 経過後なら再インクリメントされる。"""
        past = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds")
        conn = sqlite3.connect(pre_migration_db)
        conn.execute(
            "UPDATE knowledge_cards SET usage_count = 1, last_counted_at = ? WHERE id = 'card_001'",
            (past,),
        )
        conn.commit()
        conn.close()

        updated = db.record_card_usage(["card_001"], dedup_hours=6)

        row = _fetch(
            pre_migration_db,
            "SELECT usage_count FROM knowledge_cards WHERE id = 'card_001'",
        )
        assert row[0] == 2
        assert "card_001" in updated

    def test_increment_handles_empty_list(self, db):
        """空リストでもエラーにならず [] を返す。"""
        result = db.record_card_usage([])
        assert result == []

    def test_increment_handles_nonexistent_card(self, db):
        """存在しない card_id を渡してもエラーにならず、戻り値に含まれない。"""
        result = db.record_card_usage(["nonexistent_id_xyz"])
        assert "nonexistent_id_xyz" not in result

    def test_increment_mixed_dedup(self, db, pre_migration_db):
        """dedup 対象・非対象の混在: 対象外の card のみインクリメント。"""
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds")

        conn = sqlite3.connect(pre_migration_db)
        conn.execute(
            "UPDATE knowledge_cards SET usage_count = 1, last_counted_at = ? WHERE id = 'card_001'",
            (recent,),
        )
        conn.execute(
            "UPDATE knowledge_cards SET usage_count = 1, last_counted_at = ? WHERE id = 'card_002'",
            (old,),
        )
        conn.commit()
        conn.close()

        updated = db.record_card_usage(["card_001", "card_002"], dedup_hours=6)

        row1 = _fetch(pre_migration_db, "SELECT usage_count FROM knowledge_cards WHERE id = 'card_001'")
        row2 = _fetch(pre_migration_db, "SELECT usage_count FROM knowledge_cards WHERE id = 'card_002'")

        assert row1[0] == 1          # スキップ
        assert row2[0] == 2          # インクリメント
        assert "card_001" not in updated
        assert "card_002" in updated

    def test_increment_deduplicates_duplicate_ids(self, db, pre_migration_db):
        """同じ card_id が複数あっても usage_count は 1 回だけ増える。"""
        updated = db.record_card_usage(["card_001", "card_001", "card_001"])

        row = _fetch(pre_migration_db, "SELECT usage_count FROM knowledge_cards WHERE id = 'card_001'")
        assert row[0] == 1
        assert updated.count("card_001") == 1

    def test_increment_returns_only_updated_ids(self, db, pre_migration_db):
        """dedup でスキップされた ID は戻り値に含まれない。"""
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        conn = sqlite3.connect(pre_migration_db)
        conn.execute(
            "UPDATE knowledge_cards SET usage_count = 1, last_counted_at = ? WHERE id = 'card_001'",
            (recent,),
        )
        conn.commit()
        conn.close()

        updated = db.record_card_usage(["card_001", "card_002"])

        assert "card_001" not in updated  # dedup スキップ
        assert "card_002" in updated      # インクリメント済み

    def test_increment_uses_utc_iso_timestamp(self, db, pre_migration_db):
        """last_counted_at が timezone-aware な UTC ISO8601 で保存される。"""
        db.record_card_usage(["card_001"])

        ts = _fetch(
            pre_migration_db,
            "SELECT last_counted_at FROM knowledge_cards WHERE id = 'card_001'",
        )[0]
        assert ts is not None
        assert "+00:00" in ts or ts.endswith("Z")
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_old_count_column_untouched(self, db, pre_migration_db):
        """既存の count カラムは record_card_usage で変化しない。"""
        db.record_card_usage(["card_001"])

        row = _fetch(pre_migration_db, "SELECT count FROM knowledge_cards WHERE id = 'card_001'")
        assert row[0] == 3  # マイグレーション前の値を維持


# ===================================================================
# MemoryBlockBuilder レベルの統合テスト
# ===================================================================

class TestMemoryBlockBuilderRagCardIds:
    """rag_card_ids / rag_results_raw が blocks に正しく設定されるかを確認する。"""

    def _make_memory_manager(self):
        mm = MagicMock()
        mm.load_recent_sessions.return_value = ([], None)
        mm.get_session_digest.return_value = ""
        mm.get_mid_term_digest.return_value = ""
        mm.get_recent_snapshot.return_value = ""
        mm.get_raw_memory.return_value = ""
        return mm

    def test_no_rag_card_ids_for_reflex_tier(self):
        """Reflex tier では rag_card_ids が設定されない。"""
        from butly_core.core.gatekeeper.memory_builder import MemoryBlockBuilder

        blocks = MemoryBlockBuilder().build(
            tier="reflex",
            memory_manager=self._make_memory_manager(),
            brain=None,
            user_input="hello",
        )
        assert blocks.get("rag_card_ids", []) == []

    def test_no_rag_card_ids_when_candidates_empty(self):
        """candidates が空の場合は rag_card_ids が設定されない。"""
        from butly_core.core.gatekeeper.memory_builder import MemoryBlockBuilder

        gk_out = {
            "need": "rag_search",
            "need_intent": "past_fact",
            "search_targets": None,
            "topic": "",
            "memory_probe": {"status": "no_hit", "candidates": [], "glossary_hits": []},
        }
        blocks = MemoryBlockBuilder().build(
            tier="mid",
            memory_manager=self._make_memory_manager(),
            brain=None,
            user_input="test",
            gatekeeper_output=gk_out,
        )
        assert blocks.get("rag_card_ids", []) == []

    def test_rag_card_ids_set_when_candidates_present(self):
        """candidates がある場合、rag_card_ids / rag_results_raw が blocks にセットされる。"""
        from butly_core.core.gatekeeper.memory_builder import MemoryBlockBuilder

        candidates = [
            {"id": "card_001", "title": "Card 1", "summary": "Summary 1", "episode": ""},
            {"id": "card_002", "title": "Card 2", "summary": "Summary 2", "episode": "Episode"},
        ]
        gk_out = {
            "need": "rag_search",
            "need_intent": "past_fact",
            "search_targets": None,
            "topic": "",
            "memory_probe": {"status": "hit", "candidates": candidates, "glossary_hits": []},
        }
        blocks = MemoryBlockBuilder().build(
            tier="mid",
            memory_manager=self._make_memory_manager(),
            brain=None,
            user_input="test",
            gatekeeper_output=gk_out,
        )
        assert set(blocks["rag_card_ids"]) == {"card_001", "card_002"}
        assert blocks["rag_results_raw"] == candidates

    def test_no_rag_card_ids_when_need_is_none(self):
        """need=None（RAG スキップ）の場合は rag_card_ids が設定されない。"""
        from butly_core.core.gatekeeper.memory_builder import MemoryBlockBuilder

        gk_out = {
            "need": None,
            "need_intent": None,
            "search_targets": None,
            "topic": "",
            "memory_probe": {},
        }
        blocks = MemoryBlockBuilder().build(
            tier="mid",
            memory_manager=self._make_memory_manager(),
            brain=None,
            user_input="hi",
            gatekeeper_output=gk_out,
        )
        assert blocks.get("rag_card_ids", []) == []


# ===================================================================
# ChatService.execute / execute_stream 経由の統合テスト
# ===================================================================

def _run_async(coro):
    return asyncio.run(coro)


async def _collect(gen):
    out = []
    async for ev in gen:
        out.append(ev)
    return out


def _make_db_with_cards(db_path: Path, card_ids: list):
    """指定 path に migration 済みの DB を作成し、card_ids を投入する。"""
    from butly_core.core.database import ButlyDatabase
    ButlyDatabase(db_path=str(db_path))  # migration

    conn = sqlite3.connect(str(db_path))
    for cid in card_ids:
        conn.execute(
            "INSERT INTO knowledge_cards (id, category, title, count) VALUES (?, 'Tech', ?, 1)",
            (cid, f"Title {cid}"),
        )
    conn.commit()
    conn.close()


def _read_usage(db_path: Path, card_id: str):
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT usage_count, last_counted_at FROM knowledge_cards WHERE id = ?",
        (card_id,),
    ).fetchone()
    conn.close()
    return row


@pytest.fixture
def chatservice_env(tmp_path):
    """
    ChatService 統合テスト用の最小環境を構築する。
    - 実 DB を 2 instance 分作成 (multi-instance テスト用)
    - brain._get_db_path はその実 DB を指す
    - その他のコンポーネントは MagicMock
    """
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True)

    self_inst = "SelfInst"
    other_inst = "OtherInst"
    (instances_dir / self_inst).mkdir()
    (instances_dir / other_inst).mkdir()

    self_db = instances_dir / self_inst / "butly_memory.db"
    other_db = instances_dir / other_inst / "butly_memory.db"

    _make_db_with_cards(self_db, ["self_c1", "self_c2"])
    _make_db_with_cards(other_db, ["other_c1"])

    # mocks
    memory = MagicMock()
    memory.get_last_interaction_time.return_value = None
    memory.load_recent_sessions.return_value = ([], None)
    memory.save_single_turn = MagicMock()
    memory.maintain_memory = MagicMock()

    brain = MagicMock()
    brain._get_db_path = lambda inst: instances_dir / inst / "butly_memory.db"

    chronos = MagicMock()
    chronos.get_system_note.return_value = "TIME"

    components = {"memory": memory, "brain": brain, "chronos": chronos}

    instance_manager = MagicMock()
    instance_manager.get_instance_config.return_value = {
        "chat": {"model_name": "gemini-flash-test"},
        "brain": {"use_rag": True},
        "gatekeeper": {"enabled": True},
    }

    gatekeeper = MagicMock()
    gatekeeper.classify.return_value = {
        "tier": "mid",
        "topic": "",
        "need": "past_fact",
        "need_intent": "past_fact",
        "search_targets": None,
        "state_delta": {},
        "llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.2, "continuity_need": 0.3},
        "memory_probe": {"status": "hit", "candidates": [], "glossary_hits": []},
    }
    gatekeeper.update_state.return_value = {}

    return {
        "instances_dir": instances_dir,
        "self_inst": self_inst,
        "other_inst": other_inst,
        "self_db": self_db,
        "other_db": other_db,
        "components": components,
        "instance_manager": instance_manager,
        "gatekeeper": gatekeeper,
        "memory": memory,
        "brain": brain,
    }


@pytest.fixture
def chat_request(chatservice_env):
    from butly_core.chat.types import ChatRequest
    req = MagicMock(spec=ChatRequest)
    req.text = "hello"
    req.attachments = []
    req.instance_name = chatservice_env["self_inst"]
    req.model_name = "gemini-flash-test"
    req.use_rag = True
    req.use_google_search = False
    req.use_web_search = False
    return req


def _mem_builder_returning(blocks: dict):
    mb = MagicMock()
    mb.build.return_value = blocks
    return mb


def _success_provider(text: str = "ok"):
    """generate() が正常にレスポンスを返す Provider mock。"""
    from butly_core.chat.types import ChatResponse
    provider = MagicMock()
    provider.supports_vision = MagicMock(return_value=True)
    provider.generate.return_value = ChatResponse(text=text, debug_info={})
    return provider


def _failing_provider():
    """generate() が例外を投げる Provider mock。"""
    provider = MagicMock()
    provider.supports_vision = MagicMock(return_value=True)
    provider.generate.side_effect = RuntimeError("provider blew up")
    return provider


def _execute(req, env):
    from butly_core.chat.service import ChatService
    session_state_mock = MagicMock()
    session_state_mock.to_dict.return_value = {
        "topic": "", "mood": "neutral", "turn_count": 0,
    }
    with patch("butly_core.core.gatekeeper.SessionState", return_value=session_state_mock):
        return _run_async(ChatService.execute(
            request=req,
            get_instance_components=lambda _n: env["components"],
            instance_manager=env["instance_manager"],
            instances_dir=env["instances_dir"],
            gatekeeper=env["gatekeeper"],
            mem_block_builder=env["mem_builder"],
        ))


def _execute_stream(req, env):
    from butly_core.chat.service import ChatService
    session_state_mock = MagicMock()
    session_state_mock.to_dict.return_value = {
        "topic": "", "mood": "neutral", "turn_count": 0,
    }
    with patch("butly_core.core.gatekeeper.SessionState", return_value=session_state_mock):
        return _run_async(_collect(ChatService.execute_stream(
            request=req,
            get_instance_components=lambda _n: env["components"],
            instance_manager=env["instance_manager"],
            instances_dir=env["instances_dir"],
            gatekeeper=env["gatekeeper"],
            mem_block_builder=env["mem_builder"],
        )))


class TestChatServiceExecuteUsageCount:
    """ChatService.execute() 経由の usage_count 動作検証。"""

    def test_increment_for_brain_delivered_cards(self, chat_request, chatservice_env):
        """mid tier + RAG 候補あり → 該当カードの usage_count が +1。"""
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1", "self_c2"],
            "rag_results_raw": [
                {"id": "self_c1", "title": "T1", "summary": "S1"},
                {"id": "self_c2", "title": "T2", "summary": "S2"},
            ],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = _success_provider("hi there")
            _execute(chat_request, chatservice_env)

        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 1
        assert _read_usage(chatservice_env["self_db"], "self_c2")[0] == 1

    def test_no_increment_for_reflex_tier(self, chat_request, chatservice_env):
        """Reflex tier (rag_card_ids 未設定) では DB は変化しない。"""
        chatservice_env["gatekeeper"].classify.return_value["tier"] = "reflex"
        blocks = {"tier": "reflex", "topic": "", "short_term": [], "session_digest": ""}
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = _success_provider()
            _execute(chat_request, chatservice_env)

        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 0

    def test_no_increment_for_probe_only_candidates(self, chat_request, chatservice_env):
        """candidates が MemoryProbe にあっても rag_results_raw が空ならカウントしない。"""
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            # rag_results_raw を入れない (RAG block には入らなかった想定)
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = _success_provider()
            _execute(chat_request, chatservice_env)

        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 0

    def test_no_increment_when_context_levels_rag_off(self, chat_request, chatservice_env):
        """context_levels.rag=off の場合、カードは Brain に渡らないのでカウントしない。"""
        chatservice_env["instance_manager"].get_instance_config.return_value = {
            "chat": {"model_name": "gemini-flash-test"},
            "brain": {"use_rag": True},
            "gatekeeper": {"enabled": True},
            "context_levels": {
                "preset": "custom",
                "levels": {"rag": "off", "system_instruction": "high"},
                "order": {
                    "system_instruction": ["system_instruction"],
                    "context_prefix": ["current_time", "rag"],
                },
                "system_instruction_position": "top",
            },
        }
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1"],
            "rag_results_raw": [{"id": "self_c1", "title": "T1", "summary": "S1"}],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = _success_provider()
            _execute(chat_request, chatservice_env)

        # rag=off なのでカウントされない
        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 0

    def test_no_increment_on_brain_failure(self, chat_request, chatservice_env):
        """Brain (provider.generate) が失敗した場合は usage_count が増えない。"""
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1"],
            "rag_results_raw": [{"id": "self_c1", "title": "T1", "summary": "S1"}],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = _failing_provider()
            with pytest.raises(RuntimeError):
                _execute(chat_request, chatservice_env)

        # 例外で離脱したのでカウントされない
        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 0

    def test_routes_to_source_instance_db_in_multi_instance(self, chat_request, chatservice_env):
        """readable_instances 横断時、source_instance の DB に振り分けて記録される。"""
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1", "other_c1"],
            "rag_results_raw": [
                {"id": "self_c1", "title": "T1", "summary": "S1", "source_instance": "SelfInst"},
                {"id": "other_c1", "title": "T2", "summary": "S2", "source_instance": "OtherInst"},
            ],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = _success_provider()
            _execute(chat_request, chatservice_env)

        # 各 DB に正しく振り分けられているか
        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 1
        assert _read_usage(chatservice_env["other_db"], "other_c1")[0] == 1


class TestChatServiceExecuteStreamUsageCount:
    """ChatService.execute_stream() 経由の usage_count 動作検証。"""

    def _success_stream(self, text="hi"):
        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": text}
            yield {
                "type": "done",
                "full_text": text,
                "sources": [],
                "debug": {"system_instruction": "SYS", "system_instruction_full": "SYS"},
            }
        return fake_stream

    def _error_stream(self):
        async def fake_stream(*args, **kwargs):
            yield {"type": "chunk", "text": "x"}
            yield {"type": "error", "message": "stream boom"}
        return fake_stream

    def test_increment_on_stream_success(self, chat_request, chatservice_env):
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1"],
            "rag_results_raw": [{"id": "self_c1", "title": "T1", "summary": "S1"}],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        provider = MagicMock()
        provider.supports_vision = MagicMock(return_value=True)
        provider.async_generate_stream = self._success_stream("こんにちは")

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = provider
            events = _execute_stream(chat_request, chatservice_env)

        assert events[-1]["type"] == "done"
        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 1

    def test_no_increment_on_stream_error(self, chat_request, chatservice_env):
        """stream 途中で error イベントが出た場合は usage_count を更新しない。"""
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1"],
            "rag_results_raw": [{"id": "self_c1", "title": "T1", "summary": "S1"}],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        provider = MagicMock()
        provider.supports_vision = MagicMock(return_value=True)
        provider.async_generate_stream = self._error_stream()

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = provider
            events = _execute_stream(chat_request, chatservice_env)

        assert events[-1]["type"] == "error"
        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 0

    def test_no_increment_when_context_levels_rag_off_stream(self, chat_request, chatservice_env):
        chatservice_env["instance_manager"].get_instance_config.return_value = {
            "chat": {"model_name": "gemini-flash-test"},
            "brain": {"use_rag": True},
            "gatekeeper": {"enabled": True},
            "context_levels": {
                "preset": "custom",
                "levels": {"rag": "off", "system_instruction": "high"},
                "order": {
                    "system_instruction": ["system_instruction"],
                    "context_prefix": ["current_time", "rag"],
                },
                "system_instruction_position": "top",
            },
        }
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1"],
            "rag_results_raw": [{"id": "self_c1", "title": "T1", "summary": "S1"}],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        provider = MagicMock()
        provider.supports_vision = MagicMock(return_value=True)
        provider.async_generate_stream = self._success_stream("hi")

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = provider
            events = _execute_stream(chat_request, chatservice_env)

        assert events[-1]["type"] == "done"
        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 0

    def test_routes_to_source_instance_db_stream(self, chat_request, chatservice_env):
        """stream 経由でも source_instance ごとに DB が振り分けられる。"""
        blocks = {
            "tier": "mid", "topic": "", "short_term": [], "session_digest": "",
            "rag_context": "...",
            "rag_card_ids": ["self_c1", "other_c1"],
            "rag_results_raw": [
                {"id": "self_c1", "title": "T1", "summary": "S1", "source_instance": "SelfInst"},
                {"id": "other_c1", "title": "T2", "summary": "S2", "source_instance": "OtherInst"},
            ],
        }
        chatservice_env["mem_builder"] = _mem_builder_returning(blocks)

        provider = MagicMock()
        provider.supports_vision = MagicMock(return_value=True)
        provider.async_generate_stream = self._success_stream("hi")

        with patch("butly_core.chat.service.ProviderFactory") as PF:
            PF.create.return_value = provider
            events = _execute_stream(chat_request, chatservice_env)

        assert events[-1]["type"] == "done"
        assert _read_usage(chatservice_env["self_db"], "self_c1")[0] == 1
        assert _read_usage(chatservice_env["other_db"], "other_c1")[0] == 1
