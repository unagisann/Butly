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
    # embedding ロールのみ。クエリ/文書 prefix の規約。
    # 既定 (auto) はモデル名から推定する。
    profile: Optional[str] = None
    query_prefix: Optional[str] = None
    document_prefix: Optional[str] = None
    # reranker ロールのみ。cross_encoder は生成LLMを呼ばずローカルで
    # query/card ペアを一括採点する。
    engine: Optional[Literal["llm", "cross_encoder"]] = None
    batch_size: Optional[int] = Field(default=None, ge=1, le=100)
    score_threshold: Optional[float] = None
    device: Optional[str] = None


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
    # --- 検索設定（検索改修計画 §3.5）。hybrid は eval で先行検証する ---
    search_mode: Literal["vector", "hybrid", "dual_query"] = "vector"
    retrieval_execution: Literal["always", "intent_gated"] = "always"
    injection_policy: Literal[
        "intent_gated", "retrieval_assisted", "candidates"
    ] = "intent_gated"
    bm25_candidates: int = Field(default=20, ge=1)
    vector_candidates: int = Field(default=20, ge=1)
    dual_query_candidates: int = Field(default=15, ge=1)
    dual_query_pool_limit: int = Field(default=25, ge=1)
    reranker_candidate_limit: int = Field(default=20, ge=1, le=100)
    reranker_max_candidate_chars: int = Field(default=1600, ge=100, le=10000)
    rrf_k: int = Field(default=60, ge=1)
    bm25_max_df_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    role_models: dict[str, RoleModelRequest] = Field(default_factory=dict)
    # 記憶を再利用する run で、保存済みベクトルと embedding 設定が
    # 食い違っていても実行する（既定は事前に弾く）。
    allow_embedding_mismatch: bool = False


class RunCompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=2, max_length=8)


class RetrievalReplayRequest(BaseModel):
    """検索だけを回して Recall@k を比べる（QA トークンを使わない足切り）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    modes: list[
        Literal[
            "bm25",
            "vector",
            "hybrid",
            "dual_query",
            "reranked",
            "evidence_rerank",
        ]
    ] = Field(
        default_factory=lambda: ["bm25"], min_length=1, max_length=6
    )
    limit: int = Field(default=20, ge=1, le=100)
    reranker: Optional[RoleModelRequest] = None
    reranker_max_candidate_chars: int = Field(default=1600, ge=100, le=10000)
    evidence_raw_chunk_chars: int = Field(default=1800, ge=200, le=10000)


class DialogueABStartRequest(BaseModel):
    """Japanese natural-dialogue A/B with one shared memory seed."""

    model_config = ConfigDict(extra="forbid")

    dataset_path: str
    run_id: str
    time_decay_rate: float = Field(default=0.003, ge=0.0)
    context_current_time: bool = True
    context_mid_term: bool = True
    context_session_digest: bool = True
    context_rag: bool = True
    rag_source_mode: Literal["cards", "raw", "both"] = "both"
    rag_raw_top_k: int = Field(default=1, ge=0)
    rag_raw_max_chars: int = Field(default=2500, ge=0)
    stage3_enabled: bool = True
    stage3_batch_size: int = Field(default=10, ge=1)
    stage3_bootstrap_max_cards: int = Field(default=2000, ge=1)
    search_mode: Literal["vector", "hybrid", "dual_query"] = "vector"
    retrieval_execution: Literal["always", "intent_gated"] = "always"
    vector_search_limit: int = Field(default=3, ge=1)
    intent_gated_vector_search_limit: int = Field(default=3, ge=1)
    candidates_vector_search_limit: int = Field(default=3, ge=1)
    vector_search_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    deep_search_enabled: bool = True
    bm25_candidates: int = Field(default=20, ge=1)
    vector_candidates: int = Field(default=20, ge=1)
    dual_query_candidates: int = Field(default=15, ge=1)
    dual_query_pool_limit: int = Field(default=25, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    bm25_max_df_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    role_models: dict[str, RoleModelRequest] = Field(default_factory=dict)
    # 既存インスタンスを種にする（本番の記憶量・System Instruction をそのまま使う）。
    # 未指定なら dataset の memory_seed から Sleeptime で作る従来経路。
    seed_instance: Optional[str] = None
    # 複製側のカードを profile の embedding で貼り直す。既定は保存済みベクトルを使う。
    reembed: bool = False


class SemanticJudgeRequest(BaseModel):
    """Evaluation-only model selection for judging an existing run."""

    model_config = ConfigDict(extra="forbid")

    connection: Optional[str] = None
    model_name: str = Field(min_length=1)
    max_output_tokens: int = Field(default=2048, ge=1)


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


@router.post("/dialogue-ab/jobs", status_code=202)
def start_dialogue_ab_job(
    request: DialogueABStartRequest,
) -> dict[str, Any]:
    try:
        return _get_manager().start_dialogue_ab(
            request.model_dump(mode="json")
        )
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


@router.get("/dialogue-ab/runs")
def list_dialogue_ab_runs() -> dict[str, Any]:
    manager = _get_manager()
    return {
        "output_dir": str(manager.dialogue_output_dir),
        "runs": manager.list_dialogue_ab_runs(),
    }


@router.get("/dialogue-ab/runs/{run_id}")
def get_dialogue_ab_result(run_id: str) -> dict[str, Any]:
    try:
        return _get_manager().get_dialogue_ab_result(run_id)
    except (KeyError, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.get("/runs/{run_id}")
def get_evaluation_run_result(run_id: str) -> dict[str, Any]:
    """Return official and current semantic results for each LoCoMo problem."""
    try:
        return _get_manager().get_run_result(run_id)
    except (KeyError, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.post("/runs/{run_id}/judge", status_code=202)
def judge_evaluation_run(
    run_id: str,
    request: SemanticJudgeRequest,
) -> dict[str, Any]:
    try:
        return _get_manager().start_judge(
            run_id,
            request.model_dump(mode="json"),
            run_type="locomo",
        )
    except (KeyError, EvaluationJobConflict, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.post("/dialogue-ab/runs/{run_id}/judge", status_code=202)
def judge_dialogue_ab_run(
    run_id: str,
    request: SemanticJudgeRequest,
) -> dict[str, Any]:
    try:
        return _get_manager().start_judge(
            run_id,
            request.model_dump(mode="json"),
            run_type="dialogue_ab",
        )
    except (KeyError, EvaluationJobConflict, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.post("/runs/retrieval-replay")
def replay_run_retrieval(request: RetrievalReplayRequest) -> dict[str, Any]:
    """既存 run の記憶に対して検索だけ再実行する。

    ``bm25`` は embedding を呼ばない。``vector`` / ``hybrid`` は質問1件につき
    embedding 1回、``dual_query`` は必要に応じてGatekeeper 1回とembedding 2回を
    呼ぶ。``evidence_rerank``は初回にEpisode/RAW文書もembeddingするため、応答まで
    分単位でかかりうる。長いrunでは永続job endpointを推奨する。
    """
    try:
        replay_options: dict[str, Any] = {
            "limit": request.limit,
            "evidence_raw_chunk_chars": request.evidence_raw_chunk_chars,
        }
        if request.reranker is not None:
            replay_options["reranker"] = request.reranker.model_dump(
                mode="json"
            )
            replay_options["reranker_max_candidate_chars"] = (
                request.reranker_max_candidate_chars
            )
        return _get_manager().retrieval_replay(
            request.run_id,
            list(request.modes),
            **replay_options,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"評価runが見つかりません: {request.run_id}"
        ) from exc
    except EvaluationJobError as exc:
        raise _translate_error(exc) from exc


@router.post("/runs/retrieval-replay/jobs", status_code=202)
def start_retrieval_replay_job(
    request: RetrievalReplayRequest,
) -> dict[str, Any]:
    """Start an offline retrieval comparison with persistent progress."""
    try:
        return _get_manager().start_retrieval_replay(
            request.model_dump(mode="json")
        )
    except (KeyError, EvaluationJobConflict, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.get("/runs/{run_id}/retrieval-replay")
def get_retrieval_replay_result(run_id: str) -> dict[str, Any]:
    try:
        return _get_manager().get_retrieval_replay_result(run_id)
    except (KeyError, EvaluationJobError) as exc:
        raise _translate_error(exc) from exc


@router.post("/runs/compare")
def compare_evaluation_runs(request: RunCompareRequest) -> dict[str, Any]:
    try:
        return _get_manager().compare_runs(request.run_ids)
    except EvaluationJobError as exc:
        raise _translate_error(exc) from exc
