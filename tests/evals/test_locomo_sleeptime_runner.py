"""SleeptimeRunner のチャンク統計と Stage 3 分離ログのテスト。"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from evals.locomo.sleeptime_runner import SleeptimeRunner


def _mock_sleeptime(tmp_path, stage2_stats):
    sleeptime = MagicMock()
    sleeptime.instances_dir = tmp_path / "instances"
    (sleeptime.instances_dir / "inst").mkdir(parents=True)
    sleeptime.should_update.return_value = True
    sleeptime.stage_2_knowledgeize.return_value = stage2_stats
    sleeptime._should_run_stage_3.return_value = False
    sleeptime.pop_llm_usage.return_value = None
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


def test_runner_stage3_disabled_records_status(tmp_path):
    sleeptime = _mock_sleeptime(tmp_path, {"chunks": 1, "failed_chunks": 0, "failures": []})
    runner = SleeptimeRunner(
        sleeptime, run_id="r1", log_path=tmp_path / "sleeptime_log.jsonl"
    )

    result = runner.run(sample_id="s", session_id="session_1", instance_name="inst")

    assert result.stage_3_status == "disabled"
    sleeptime.stage_3_mature_knowledge.assert_not_called()
    row = json.loads(
        (tmp_path / "sleeptime_log.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert row["stage_3_status"] == "disabled"


def test_runner_stage3_runs_with_session_clock_and_split_usage(tmp_path):
    sleeptime = _mock_sleeptime(tmp_path, {"chunks": 1, "failed_chunks": 0, "failures": []})
    sleeptime._should_run_stage_3.return_value = True
    sleeptime.stage_3_mature_knowledge.return_value = {
        "status": "completed",
        "batches": 2,
        "llm_calls": 2,
        "applied_cards": 5,
        "reviewed_cards": 5,
        "created": 3,
        "linked": 4,
        "superseded": 1,
        "failed_cards": ["c_bad"],
        "outcomes": ["ok", "ok"],
    }
    # pop の呼び出し順: run 冒頭の捨て → Stage 1/2 分 → Stage 3 分
    sleeptime.pop_llm_usage.side_effect = [
        None,
        {"prompt_tokens": 10, "completion_tokens": 5, "calls": 2},
        {"prompt_tokens": 7, "completion_tokens": 3, "calls": 1},
    ]
    session_now = datetime(2024, 4, 8, 10, 0, tzinfo=timezone.utc)
    runner = SleeptimeRunner(
        sleeptime, run_id="r1", log_path=tmp_path / "sleeptime_log.jsonl"
    )

    result = runner.run(
        sample_id="s",
        session_id="session_1",
        instance_name="inst",
        session_now=session_now,
    )

    sleeptime.stage_3_mature_knowledge.assert_called_once_with(
        sleeptime.instances_dir / "inst", now=session_now
    )
    assert result.stage_3_status == "completed"
    assert result.stage_3_batches == 2
    assert result.stage_3_created_nodes == 3
    assert result.stage_3_linked_sources == 4
    assert result.stage_3_failed_cards == 1
    assert result.stage_3_llm_calls == 2
    # Stage 3 のコストは分離しつつ、合計にも含める
    assert result.stage_3_prompt_tokens == 7
    assert result.llm_prompt_tokens == 17
    assert result.llm_completion_tokens == 8
    assert result.llm_calls == 3


def test_runner_stage3_failure_not_conflated_with_stage2(tmp_path):
    sleeptime = _mock_sleeptime(tmp_path, {"chunks": 1, "failed_chunks": 0, "failures": []})
    sleeptime._should_run_stage_3.return_value = True
    sleeptime.stage_3_mature_knowledge.side_effect = RuntimeError("stage3 boom")
    runner = SleeptimeRunner(
        sleeptime, run_id="r1", log_path=tmp_path / "sleeptime_log.jsonl"
    )

    # Stage 3 失敗は SleeptimeRunError にせず、分離フィールドに記録する
    result = runner.run(sample_id="s", session_id="session_1", instance_name="inst")

    assert result.stage_2_success is True
    assert result.stage_2_status == "succeeded"
    assert result.error is None
    assert result.stage_3_status == "failed"
    assert "stage3 boom" in result.stage_3_error
    row = json.loads(
        (tmp_path / "sleeptime_log.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert row["stage_3_status"] == "failed"
