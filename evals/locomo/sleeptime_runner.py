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
    digest_updated: bool
    recent_snapshot_updated: bool
    retry_count: int
    error: Optional[str]

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
            "digest_updated": self.digest_updated,
            "recent_snapshot_updated": self.recent_snapshot_updated,
            "retry_count": self.retry_count,
            "error": self.error,
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
        error = None

        try:
            self.sleeptime.stage_1_cleanup(instance_name)
            stage_1_success = True
            instance_config = self.sleeptime._get_instance_config(instance_name)
            if self.sleeptime._should_update(instance_config, "knowledge_cards"):
                self.sleeptime.stage_2_knowledgeize(instance_name, instance_name)
                stage_2_success = True
                stage_2_status = "succeeded"
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
            digest_updated=_read_optional(digest_path) != digest_before,
            recent_snapshot_updated=(
                _read_optional(snapshot_path) != snapshot_before
            ),
            retry_count=0,
            error=error,
        )
        append_jsonl(self.log_path, result.to_dict())
        if error is not None:
            raise SleeptimeRunError(
                f"Sleeptime failed for {sample_id} {session_id}: {error}"
            )
        return result


def _read_optional(path: Path) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path.is_file() else None
