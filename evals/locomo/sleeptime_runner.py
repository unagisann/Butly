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
    # カード単位の根拠に絞れた枚数 / チャンク全体へフォールバックした枚数
    knowledge_source_files_card: int = 0
    knowledge_source_files_chunk: int = 0
    # Stage 3 (knowledge maturation)。Stage 3 の失敗を Stage 2 成功へ
    # 紛れ込ませないため、status / counters / tokens を分離して記録する。
    stage_3_status: str = "disabled"
    stage_3_error: Optional[str] = None
    stage_3_batches: int = 0
    stage_3_reviewed_cards: int = 0
    stage_3_created_nodes: int = 0
    stage_3_linked_sources: int = 0
    stage_3_superseded_nodes: int = 0
    stage_3_failed_cards: int = 0
    stage_3_llm_calls: int = 0
    stage_3_prompt_tokens: Optional[int] = None
    stage_3_completion_tokens: Optional[int] = None

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
            "knowledge_source_files_card": self.knowledge_source_files_card,
            "knowledge_source_files_chunk": self.knowledge_source_files_chunk,
            "digest_updated": self.digest_updated,
            "recent_snapshot_updated": self.recent_snapshot_updated,
            "retry_count": self.retry_count,
            "error": self.error,
            "llm_prompt_tokens": self.llm_prompt_tokens,
            "llm_completion_tokens": self.llm_completion_tokens,
            "llm_calls": self.llm_calls,
            "stage_3_status": self.stage_3_status,
            "stage_3_error": self.stage_3_error,
            "stage_3_batches": self.stage_3_batches,
            "stage_3_reviewed_cards": self.stage_3_reviewed_cards,
            "stage_3_created_nodes": self.stage_3_created_nodes,
            "stage_3_linked_sources": self.stage_3_linked_sources,
            "stage_3_superseded_nodes": self.stage_3_superseded_nodes,
            "stage_3_failed_cards": self.stage_3_failed_cards,
            "stage_3_llm_calls": self.stage_3_llm_calls,
            "stage_3_prompt_tokens": self.stage_3_prompt_tokens,
            "stage_3_completion_tokens": self.stage_3_completion_tokens,
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
        session_now: Optional[datetime] = None,
    ) -> SleeptimeResult:
        """Run Stage 1, Stage 2, then Stage 3 (opt-in) and wait for completion.

        session_now: Stage 3 へ注入する clock。session 直後の実行では session の
        元日時を渡す（QA 時だけ設定される CHRONOS_NOW_ENV に依存しない）。
        """
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
        stage_3_status = "disabled"
        stage_3_error: Optional[str] = None
        stage_3_stats: dict = {}
        stage_3_usage: dict = {}
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

        # Stage 1/2 の LLM usage を確定してから Stage 3 を走らせ、
        # Stage 3 のコストを分離して観測できるようにする。
        llm_usage = self.sleeptime.pop_llm_usage()
        if not isinstance(llm_usage, dict):
            # モック sleeptime や旧実装では usage が取れない → フィールドは None
            llm_usage = {}

        if error is None and stage_2_success:
            stage_3_status, stage_3_error, stage_3_stats, stage_3_usage = (
                self._run_stage_3(instance_name, session_now)
            )

        finished_at = datetime.now(timezone.utc)
        cards_after = count_knowledge_cards(database_path)
        combined_prompt = _sum_optional(
            llm_usage.get("prompt_tokens"), stage_3_usage.get("prompt_tokens")
        )
        combined_completion = _sum_optional(
            llm_usage.get("completion_tokens"),
            stage_3_usage.get("completion_tokens"),
        )
        combined_calls = _sum_optional(
            llm_usage.get("calls"), stage_3_usage.get("calls")
        )
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
            knowledge_source_files_card=stage_2_stats.get("source_files_card", 0),
            knowledge_source_files_chunk=stage_2_stats.get("source_files_chunk", 0),
            knowledge_chunk_failures=stage_2_stats.get("failed_chunks", 0),
            knowledge_chunk_failure_details=stage_2_stats.get("failures", []),
            digest_updated=_read_optional(digest_path) != digest_before,
            recent_snapshot_updated=(
                _read_optional(snapshot_path) != snapshot_before
            ),
            retry_count=0,
            error=error,
            llm_prompt_tokens=combined_prompt,
            llm_completion_tokens=combined_completion,
            llm_calls=combined_calls,
            stage_3_status=stage_3_status,
            stage_3_error=stage_3_error,
            stage_3_batches=int(stage_3_stats.get("batches", 0) or 0),
            stage_3_reviewed_cards=int(
                stage_3_stats.get("reviewed_cards", 0) or 0
            ),
            stage_3_created_nodes=int(stage_3_stats.get("created", 0) or 0),
            stage_3_linked_sources=int(stage_3_stats.get("linked", 0) or 0),
            stage_3_superseded_nodes=int(
                stage_3_stats.get("superseded", 0) or 0
            ),
            stage_3_failed_cards=len(stage_3_stats.get("failed_cards", []) or []),
            stage_3_llm_calls=int(stage_3_stats.get("llm_calls", 0) or 0),
            stage_3_prompt_tokens=stage_3_usage.get("prompt_tokens"),
            stage_3_completion_tokens=stage_3_usage.get("completion_tokens"),
        )
        append_jsonl(self.log_path, result.to_dict())
        if error is not None:
            raise SleeptimeRunError(
                f"Sleeptime failed for {sample_id} {session_id}: {error}"
            )
        return result

    def _run_stage_3(
        self,
        instance_name: str,
        session_now: Optional[datetime],
    ) -> tuple[str, Optional[str], dict, dict]:
        """Stage 3 を評価経路で実行する。失敗は Stage 2 成功へ紛れ込ませず分離。"""
        try:
            enabled = bool(
                self.sleeptime._should_run_stage_3(
                    self.sleeptime.get_instance_config(instance_name)
                )
            )
        except Exception as exc:
            return "failed", f"{type(exc).__name__}: {exc}", {}, {}
        if not enabled:
            return "disabled", None, {}, {}

        instance_dir = self.sleeptime.instances_dir / instance_name
        try:
            stats = self.sleeptime.stage_3_mature_knowledge(
                instance_dir, now=session_now
            )
        except Exception as exc:
            usage = self.sleeptime.pop_llm_usage()
            return (
                "failed",
                f"{type(exc).__name__}: {exc}",
                {},
                usage if isinstance(usage, dict) else {},
            )
        usage = self.sleeptime.pop_llm_usage()
        if not isinstance(usage, dict):
            usage = {}
        if not isinstance(stats, dict):
            return "unknown", None, {}, usage
        return str(stats.get("status", "completed")), None, stats, usage


def _sum_optional(*values: Optional[int]) -> Optional[int]:
    present = [v for v in values if isinstance(v, int)]
    return sum(present) if present else None


def _read_optional(path: Path) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path.is_file() else None
