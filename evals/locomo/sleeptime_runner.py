"""Synchronous Sleeptime execution with structured Phase 2 logging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Optional

from sleeptime import ButlySleeptime

from .artifacts import append_jsonl, count_knowledge_cards


class SleeptimeRunError(RuntimeError):
    """Raised after a failed Sleeptime stage has been written to JSONL."""


@dataclass(frozen=True)
class SleeptimeResult:
    run_id: str
    sample_id: str
    session_id: str
    instance_name: str
    started_at: str
    finished_at: str
    duration_ms: int
    stage_1_success: bool
    stage_2_success: bool
    stage_2_status: str
    knowledge_cards_created: int
    knowledge_chunks: int
    knowledge_chunk_failures: int
    knowledge_chunk_failure_details: list
    digest_updated: bool
    recent_snapshot_updated: bool
    retry_count: int
    error: Optional[str]
    llm_prompt_tokens: Optional[int] = None
    llm_completion_tokens: Optional[int] = None
    llm_calls: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "sample_id": self.sample_id,
            "session_id": self.session_id,
            "instance_name": self.instance_name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "stage_1_success": self.stage_1_success,
            "stage_2_success": self.stage_2_success,
            "stage_2_status": self.stage_2_status,
            "knowledge_cards_created": self.knowledge_cards_created,
            "knowledge_chunks": self.knowledge_chunks,
            "knowledge_chunk_failures": self.knowledge_chunk_failures,
            "knowledge_chunk_failure_details": self.knowledge_chunk_failure_details,
            "digest_updated": self.digest_updated,
            "recent_snapshot_updated": self.recent_snapshot_updated,
            "retry_count": self.retry_count,
            "error": self.error,
            "llm_prompt_tokens": self.llm_prompt_tokens,
            "llm_completion_tokens": self.llm_completion_tokens,
            "llm_calls": self.llm_calls,
        }


class SleeptimeRunner:
    def __init__(
        self,
        sleeptime: ButlySleeptime,
        *,
        run_id: str,
        log_path: Path,
    ):
        self.sleeptime = sleeptime
        self.run_id = run_id
        self.log_path = Path(log_path)

    def run(
        self,
        *,
        sample_id: str,
        session_id: str,
        instance_name: str,
    ) -> SleeptimeResult:
        """Run Stage 1 then Stage 2 directly and wait for completion."""
        instance_dir = self.sleeptime.instances_dir / instance_name
        database_path = instance_dir / "butly_memory.db"
        digest_path = instance_dir / "mid_term_digest.txt"
        snapshot_path = instance_dir / "recent_snapshot.txt"
        cards_before = count_knowledge_cards(database_path)
        digest_before = _read_optional(digest_path)
        snapshot_before = _read_optional(snapshot_path)
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        stage_1_success = False
        stage_2_success = False
        stage_2_status = "not_started"
        stage_2_stats: dict = {}
        error = None
        # 前回 run の取り残しを捨ててこのセッション分だけを累積する
        self.sleeptime.pop_llm_usage()

        try:
            self.sleeptime.stage_1_cleanup(instance_name)
            stage_1_success = True
            instance_config = self.sleeptime.get_instance_config(instance_name)
            if self.sleeptime.should_update(instance_config, "knowledge_cards"):
                stage_2_stats = (
                    self.sleeptime.stage_2_knowledgeize(instance_name, instance_name)
                    or {}
                )
                stage_2_success = True
                # チャンク失敗は致命エラーではないが「成功」とも言わせない
                stage_2_status = (
                    "partial"
                    if stage_2_stats.get("failed_chunks")
                    else "succeeded"
                )
            else:
                stage_2_success = True
                stage_2_status = "skipped"
            self.sleeptime.backup_database(instance_name)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if stage_1_success:
                stage_2_status = "failed"

        finished_at = datetime.now(timezone.utc)
        cards_after = count_knowledge_cards(database_path)
        llm_usage = self.sleeptime.pop_llm_usage()
        if not isinstance(llm_usage, dict):
            # モック sleeptime や旧実装では usage が取れない → フィールドは None
            llm_usage = {}
        result = SleeptimeResult(
            run_id=self.run_id,
            sample_id=sample_id,
            session_id=session_id,
            instance_name=instance_name,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_ms=int((time.perf_counter() - started_clock) * 1000),
            stage_1_success=stage_1_success,
            stage_2_success=stage_2_success,
            stage_2_status=stage_2_status,
            knowledge_cards_created=max(0, cards_after - cards_before),
            knowledge_chunks=stage_2_stats.get("chunks", 0),
            knowledge_chunk_failures=stage_2_stats.get("failed_chunks", 0),
            knowledge_chunk_failure_details=stage_2_stats.get("failures", []),
            digest_updated=_read_optional(digest_path) != digest_before,
            recent_snapshot_updated=(
                _read_optional(snapshot_path) != snapshot_before
            ),
            retry_count=0,
            error=error,
            llm_prompt_tokens=llm_usage.get("prompt_tokens"),
            llm_completion_tokens=llm_usage.get("completion_tokens"),
            llm_calls=llm_usage.get("calls"),
        )
        append_jsonl(self.log_path, result.to_dict())
        if error is not None:
            raise SleeptimeRunError(
                f"Sleeptime failed for {sample_id} {session_id}: {error}"
            )
        return result


def _read_optional(path: Path) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path.is_file() else None
