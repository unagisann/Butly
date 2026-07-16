"""SleeptimeRunner のチャンク統計ログのテスト。"""

import json
from unittest.mock import MagicMock

from evals.locomo.sleeptime_runner import SleeptimeRunner


def _mock_sleeptime(tmp_path, stage2_stats):
    sleeptime = MagicMock()
    sleeptime.instances_dir = tmp_path / "instances"
    (sleeptime.instances_dir / "inst").mkdir(parents=True)
    sleeptime.should_update.return_value = True
    sleeptime.stage_2_knowledgeize.return_value = stage2_stats
    return sleeptime


def test_runner_records_chunk_failures_as_partial(tmp_path):
    stats = {
        "chunks": 2,
        "failed_chunks": 1,
        "cards_created": 3,
        "failures": [{"date": "2023-06-09", "chunk": 2, "reason": "parse_error"}],
    }
    runner = SleeptimeRunner(
        _mock_sleeptime(tmp_path, stats),
        run_id="r1",
        log_path=tmp_path / "sleeptime_log.jsonl",
    )

    result = runner.run(sample_id="s", session_id="session_3", instance_name="inst")

    assert result.stage_2_status == "partial"
    assert result.knowledge_chunks == 2
    assert result.knowledge_chunk_failures == 1
    assert result.error is None

    row = json.loads(
        (tmp_path / "sleeptime_log.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert row["knowledge_chunk_failures"] == 1
    assert row["knowledge_chunk_failure_details"][0]["reason"] == "parse_error"


def test_runner_all_chunks_ok_stays_succeeded(tmp_path):
    stats = {"chunks": 2, "failed_chunks": 0, "cards_created": 5, "failures": []}
    runner = SleeptimeRunner(
        _mock_sleeptime(tmp_path, stats),
        run_id="r1",
        log_path=tmp_path / "sleeptime_log.jsonl",
    )

    result = runner.run(sample_id="s", session_id="session_1", instance_name="inst")

    assert result.stage_2_status == "succeeded"
    assert result.knowledge_chunk_failures == 0


def test_runner_tolerates_stats_none(tmp_path):
    """stage_2 が None を返す旧実装互換でも落ちない"""
    runner = SleeptimeRunner(
        _mock_sleeptime(tmp_path, None),
        run_id="r1",
        log_path=tmp_path / "sleeptime_log.jsonl",
    )

    result = runner.run(sample_id="s", session_id="session_1", instance_name="inst")

    assert result.stage_2_status == "succeeded"
    assert result.knowledge_chunks == 0
