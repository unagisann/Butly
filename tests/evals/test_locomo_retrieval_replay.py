"""
test_locomo_retrieval_replay.py
───────────────────────────────
検索だけを差し替える offline replay のテスト。BM25 モードは embedding を
呼ばないので、LLM/API 無しで最後まで回せる。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from butly_core.core.database import ButlyDatabase
from evals.locomo.retrieval_replay import evaluate, main, mirror_workspace


def _write_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "results").mkdir(parents=True)

    instance_dir = (
        run_dir / "workspace" / "butly_core" / "instances" / "conv_1"
    )
    (instance_dir / "short_term_json").mkdir(parents=True)

    db_path = instance_dir / "butly_memory.db"
    ButlyDatabase(db_path=str(db_path))
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO knowledge_cards (id, category, title, summary, source_files) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("k1", "Life", "Pottery workshop", "made mugs with the kids",
             json.dumps(["session_0001.json"])),
            ("k2", "Life", "Garden notes", "herbs on the balcony",
             json.dumps(["session_0002.json"])),
        ],
    )
    conn.commit()
    conn.close()

    (instance_dir / "short_term_json" / "session_0001.json").write_text(
        json.dumps({
            "messages": [{
                "role": "user",
                "parts": ["We made mugs."],
                "meta": {"locomo_dialog_ids": ["D1:1"]},
            }]
        }),
        encoding="utf-8",
    )

    rows = [
        {
            "question_id": "qa-1",
            "question": "What pottery did they make?",
            "category": 2,
            "instance_name": "conv_1",
            "evidence": ["D1:1"],
            "retrieved_card_ids": [],
        },
        {
            # cat5 は対象外
            "question_id": "qa-2",
            "question": "Where do they live?",
            "category": 5,
            "instance_name": "conv_1",
            "evidence": ["D1:1"],
            "retrieved_card_ids": [],
        },
        {
            # oracle カードが無い問（evidence のファイルがどのカードにも無い）
            "question_id": "qa-3",
            "question": "Anything else?",
            "category": 2,
            "instance_name": "conv_1",
            "evidence": ["D9:9"],
            "retrieved_card_ids": [],
        },
    ]
    (run_dir / "results" / "qa_results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    (run_dir / "run_config.json").write_text(
        json.dumps({"run_id": "replay-test"}), encoding="utf-8"
    )
    return run_dir


class TestRetrievalReplay:
    def test_bm25_mode_measures_recall(self, tmp_path):
        run_dir = _write_run(tmp_path)

        result = evaluate(run_dir, ["bm25"])

        # cat5 と oracle 無しの問は分母から外れる
        assert result["oracle_questions"] == 1
        assert result["bm25"]["recall_at_3"] == pytest.approx(1.0)
        assert result["bm25"]["hit_at_1"] == 1

    def test_source_run_is_not_modified(self, tmp_path):
        """replay は複製した DB に索引を作る。元 run は不変（R7）"""
        run_dir = _write_run(tmp_path)
        db_path = (
            run_dir / "workspace" / "butly_core" / "instances" / "conv_1"
            / "butly_memory.db"
        )
        before = db_path.read_bytes()

        evaluate(run_dir, ["bm25"])

        assert db_path.read_bytes() == before

    def test_mirror_copies_only_databases(self, tmp_path):
        run_dir = _write_run(tmp_path)
        dest = mirror_workspace(run_dir, tmp_path / "mirror")

        instance = dest / "butly_core" / "instances" / "conv_1"
        assert (instance / "butly_memory.db").is_file()
        assert not (instance / "short_term_json").exists()

    def test_missing_workspace_raises(self, tmp_path):
        run_dir = tmp_path / "empty"
        (run_dir / "results").mkdir(parents=True)
        (run_dir / "results" / "qa_results.jsonl").write_text(
            json.dumps({"question_id": "q", "question": "x", "category": 1,
                        "instance_name": "conv_1", "evidence": ["D1:1"]}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            evaluate(run_dir, ["bm25"])

    def test_cli_prints_summary(self, tmp_path, capsys):
        run_dir = _write_run(tmp_path)
        out = tmp_path / "replay.json"

        assert main(["--run", str(run_dir), "--modes", "bm25", "--out", str(out)]) == 0

        printed = capsys.readouterr().out
        assert "oracle" in printed
        assert "bm25" in printed
        assert json.loads(out.read_text(encoding="utf-8"))["bm25"]["questions"] == 1
