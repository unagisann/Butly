"""Local Web Console endpoints for LoCoMo evaluation jobs and run history."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import dependencies as deps
from evals.locomo.web_jobs import (
    EvaluationJobConflict,
    EvaluationJobError,
    EvaluationJobManager,
)


router = APIRouter(prefix="/evaluations", tags=["evaluations"])


class RoleModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: Optional[str] = None
    model_name: str
    generation_config: dict[str, Any] = Field(default_factory=dict)


class EvaluationStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_path: str
    run_id: str
    run_mode: Literal[
        "standard",
        "stage3-full",
        "stage3-source",
        "stage3-off",
        "stage3-on",
    ] = "standard"
    source_memory_run_id: Optional[str] = None
    qa_mode: Literal["independent", "sequential"] = "independent"
    locale: Literal["en", "ja"] = "en"
    sample_limit: Optional[int] = Field(default=1, ge=1)
    session_limit: Optional[int] = Field(default=3, ge=1)
    question_limit: Optional[int] = Field(default=10, ge=1)
    time_decay_rate: float = Field(default=0.0, ge=0.0)
    context_current_time: bool = True
    context_mid_term: bool = True
    context_session_digest: bool = True
    context_rag: bool = True
    rag_source_mode: Literal["cards", "raw", "both"] = "both"
    rag_raw_top_k: int = Field(default=1, ge=0)
    rag_raw_max_chars: int = Field(default=2500, ge=0)
    stage3_batch_size: int = Field(default=10, ge=1)
    stage3_bootstrap_max_cards: int = Field(default=2000, ge=1)
    role_models: dict[str, RoleModelRequest] = Field(default_factory=dict)


class RunCompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=2, max_length=8)


_manager: Optional[EvaluationJobManager] = None
_manager_data_dir: Optional[Path] = None


def _get_manager() -> EvaluationJobManager:
    global _manager, _manager_data_dir
    data_dir = Path(deps.DATA_DIR or Path(__file__).resolve().parents[1]).resolve()
    if _manager is None or _manager_data_dir != data_dir:
        _manager = EvaluationJobManager(data_dir)
        _manager_data_dir = data_dir
    return _manager


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="評価ジョブが見つかりません")
    if isinstance(exc, EvaluationJobConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, EvaluationJobError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="評価ジョブの処理に失敗しました")


@router.get("/config")
def get_evaluation_config() -> dict[str, Any]:
    return _get_manager().config()


@router.post("/jobs", status_code=202)
def start_evaluation_job(request: EvaluationStartRequest) -> dict[str, Any]:
    try:
        return _get_manager().start(request.model_dump(mode="json"))
    except (KeyError, EvaluationJobConflict, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.get("/jobs")
def list_evaluation_jobs() -> dict[str, Any]:
    return {"jobs": _get_manager().list_jobs()}


@router.get("/jobs/{job_id}")
def get_evaluation_job(job_id: str) -> dict[str, Any]:
    try:
        return _get_manager().get(job_id)
    except KeyError as exc:
        raise _translate_error(exc) from exc


@router.post("/jobs/{job_id}/stop", status_code=202)
def stop_evaluation_job(job_id: str) -> dict[str, Any]:
    try:
        return _get_manager().stop(job_id)
    except (KeyError, EvaluationJobConflict) as exc:
        raise _translate_error(exc) from exc


@router.post("/jobs/{job_id}/resume", status_code=202)
def resume_evaluation_job(job_id: str) -> dict[str, Any]:
    try:
        return _get_manager().resume(job_id)
    except (KeyError, EvaluationJobConflict, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.get("/jobs/{job_id}/log")
def get_evaluation_job_log(
    job_id: str,
    tail_lines: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    try:
        return {
            "job_id": job_id,
            "text": _get_manager().read_log(
                job_id,
                tail_lines=tail_lines,
            ),
        }
    except KeyError as exc:
        raise _translate_error(exc) from exc


@router.get("/runs")
def list_evaluation_runs() -> dict[str, Any]:
    return {
        "output_dir": str(_get_manager().output_dir),
        "runs": _get_manager().list_runs(),
    }


@router.post("/runs/compare")
def compare_evaluation_runs(request: RunCompareRequest) -> dict[str, Any]:
    try:
        return _get_manager().compare_runs(request.run_ids)
    except EvaluationJobError as exc:
        raise _translate_error(exc) from exc
