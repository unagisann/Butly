"""Persistent subprocess jobs for the local evaluation console."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Optional
from uuid import uuid4

import yaml

from butly_core.core.embedding_check import check_embeddings
from butly_core.io_utils import atomic_write_text

from .config import WORKFLOWS
from .workspace import PROJECT_ROOT


logger = logging.getLogger(__name__)

RUN_MODES = (
    "standard",
    "stage3-full",
    "stage3-source",
    "stage3-off",
    "stage3-on",
)
SEARCH_MODES = (
    "vector",
    "hybrid",
    "dual_query",
    "hybrid_evidence_fusion",
)
RETRIEVAL_REPLAY_MODES = (
    "bm25",
    "vector",
    "hybrid",
    "dual_query",
    "reranked",
    "evidence_rerank",
    "hybrid_evidence_rerank",
    "hybrid_evidence_fusion",
)
RETRIEVAL_EXECUTIONS = ("always", "intent_gated")
INJECTION_POLICIES = ("intent_gated", "retrieval_assisted", "candidates")
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "stopping"})
RESUMABLE_JOB_STATUSES = frozenset({"stopped", "failed", "interrupted"})
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PROGRESS_LINE = re.compile(
    r"^\[(?:LoCoMo|DialogueAB)\s+([0-9.]+)%\]"
    r"(?:\s+\[[^\]]+\])?\s+(\S+)\s+\|\s+(.*)$"
)
_INSTANCE_PROFILE_ROLES = (
    "chat",
    "gatekeeper",
    "summary",
    "knowledge",
    "embedding",
    "reranker",
)
_EVALUATION_MODEL_ROLES = (*_INSTANCE_PROFILE_ROLES, "judge")
_JUDGE_JOB_TYPES = frozenset({"locomo_judge", "dialogue_ab_judge"})
_RETRIEVAL_REPLAY_JOB_TYPE = "retrieval_replay"


class EvaluationJobError(ValueError):
    """Raised when a web evaluation job request is invalid."""


class EvaluationJobConflict(RuntimeError):
    """Raised when a job cannot transition from its current state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def describe_source_embedding_mismatch(
    source_run_dir: Path, embedding_role: Any
) -> Optional[str]:
    """再利用元 run のベクトルが今回の embedding 設定と噛み合うか調べる。

    ``rerun-qa`` は元 run の workspace（カード + embedding_blob）をそのまま
    使い、QA だけやり直す。埋め込みモデルや prefix 規約が変わっていると、
    保存済みベクトルと検索クエリが別空間になり、**エラーは出ないまま**
    検索が壊れる。1時間かけて無意味な数字を出す前に止めたい。

    Returns: 不整合の説明（問題なければ None）。
    """
    if not isinstance(embedding_role, dict):
        return None
    model_name = str(embedding_role.get("model_name") or "").strip()
    if not model_name:
        return None

    instances_dir = source_run_dir / "workspace" / "butly_core" / "instances"
    if not instances_dir.is_dir():
        return None

    conf = {k: v for k, v in embedding_role.items() if k != "generation_config"}
    try:
        summary = check_embeddings(instances_dir, embedding_conf=conf)
    except Exception as e:  # pragma: no cover — 判定不能なら通す
        logger.warning("embedding compatibility check skipped: %s", e)
        return None

    if summary["actions"]:
        return f"Source run '{source_run_dir.name}': {summary['actions'][0]}"
    return None


# Gatekeeper が thinking を出すモデルだと、分類 JSON を書く前に出力上限へ
# 到達して content が空になる。実測では Qwen3-14B が 512 トークンを thinking で
# 使い切り、classifier fallback 77.9% (empty_response=135) → need_intent が
# 立たず RAG が 33% しか発火しない、という壊れ方をした。
# ベストエフォートの名前判定。増やすときは「既定で thinking するモデル」だけ。
_REASONING_MODEL_PATTERNS = (
    "qwen3",
    "qwq",
    "deepseek-r1",
    "magistral",
    "phi-4-reasoning",
    "exaone-deep",
    "gpt-oss",
    "minimax-m1",
    "thinking",
    "-reasoning",
)
RECOMMENDED_REASONING_MAX_OUTPUT_TOKENS = 2048


def looks_like_reasoning_model(model_name: Any) -> bool:
    """既定で thinking を出すモデルらしいかを名前から判定する（best-effort）。"""
    if not isinstance(model_name, str):
        return False
    name = model_name.lower()
    if "non-reasoning" in name:
        return False
    return any(pattern in name for pattern in _REASONING_MODEL_PATTERNS)


def gatekeeper_token_warning(
    model_name: Any, max_output_tokens: Any
) -> Optional[str]:
    """Gatekeeper の出力上限が thinking に食われそうなら警告文を返す。

    Returns: 警告文（問題なければ None）。
    """
    if not looks_like_reasoning_model(model_name):
        return None
    try:
        limit = int(max_output_tokens)
    except (TypeError, ValueError):
        return None
    if limit >= RECOMMENDED_REASONING_MAX_OUTPUT_TOKENS:
        return None
    return (
        f"Gatekeeper の {model_name} は thinking を出すモデルです。"
        f"max output tokens={limit} だと分類 JSON を書く前に上限へ達し、"
        "空応答→need_intent が立たず RAG が発火しなくなります"
        f"（{RECOMMENDED_REASONING_MAX_OUTPUT_TOKENS} 以上を推奨）。"
    )


def describe_locomo_dataset(dataset_path: Path) -> dict[str, Any]:
    """Validate a dataset and return its selectable sample metadata."""
    path = Path(dataset_path).expanduser()
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise EvaluationJobError(f"dataset not found: {path}") from exc
    if not path.is_file():
        raise EvaluationJobError(f"dataset is not a file: {path}")

    from .dataset import LocomoDatasetError, load_dataset

    try:
        conversations = load_dataset(path)
    except LocomoDatasetError as exc:
        raise EvaluationJobError(str(exc)) from exc
    except (OSError, UnicodeError) as exc:
        raise EvaluationJobError(f"cannot read dataset {path}: {exc}") from exc
    return {
        "dataset_path": str(path),
        "sample_count": len(conversations),
        "samples": [
            {
                "sample_id": conversation.sample_id,
                "session_count": len(conversation.sessions),
                "question_count": len(conversation.questions),
                "speaker_a": conversation.speaker_a,
                "speaker_b": conversation.speaker_b,
            }
            for conversation in conversations
        ],
    }


def _normalize_role_models(raw_models: Any) -> dict[str, dict[str, Any]]:
    """Validate and normalize model selections persisted by evaluation jobs."""
    if raw_models is None:
        return {}
    if not isinstance(raw_models, dict):
        raise EvaluationJobError("role_models must be an object")
    unknown_roles = set(raw_models) - set(_EVALUATION_MODEL_ROLES)
    if unknown_roles:
        raise EvaluationJobError(
            f"unsupported role_models: {sorted(unknown_roles)}"
        )

    normalized: dict[str, dict[str, Any]] = {}
    for role, raw_role in raw_models.items():
        if not isinstance(raw_role, dict):
            raise EvaluationJobError(f"role_models.{role} must be an object")
        model_name = str(raw_role.get("model_name") or "").strip()
        if not model_name:
            raise EvaluationJobError(
                f"role_models.{role}.model_name must be non-empty"
            )
        role_config: dict[str, Any] = {"model_name": model_name}
        connection = str(raw_role.get("connection") or "").strip()
        if connection:
            role_config["connection"] = connection
        generation_config = raw_role.get("generation_config") or {}
        if not isinstance(generation_config, dict):
            raise EvaluationJobError(
                f"role_models.{role}.generation_config must be an object"
            )
        generation = dict(generation_config)
        if role == "judge":
            # Evaluation classifiers are fixed for reproducibility.
            generation["temperature"] = 0.0
            max_tokens = generation.get("max_output_tokens", 2048)
            if (
                isinstance(max_tokens, bool)
                or not isinstance(max_tokens, int)
                or max_tokens < 1
            ):
                raise EvaluationJobError(
                    f"role_models.{role}.generation_config.max_output_tokens "
                    "must be a positive integer"
                )
            generation["max_output_tokens"] = max_tokens
        if generation:
            role_config["generation_config"] = generation
        if role == "embedding":
            for key in ("profile", "query_prefix", "document_prefix"):
                value = raw_role.get(key)
                if isinstance(value, str) and value.strip():
                    role_config[key] = value
        if role == "judge":
            from evals.semantic_judge import JudgeConfig

            judge_config = JudgeConfig.from_mapping(role_config)
            if judge_config is None:  # pragma: no cover - model was validated
                raise EvaluationJobError("judge config is missing")
            if not judge_config.connection:
                from butly_core.llm.model_registry import normalize_model_ref

                try:
                    normalize_model_ref(judge_config.provider_config())
                except ValueError as exc:
                    raise EvaluationJobError(str(exc)) from exc
            role_config["config_signature"] = judge_config.signature()
        if role == "reranker":
            from butly_core.core.reranker import RerankerConfig, RerankerError

            reranker_mapping = dict(raw_role)
            reranker_mapping["model_name"] = model_name
            reranker_mapping["generation_config"] = generation
            if connection:
                reranker_mapping["connection"] = connection
            else:
                reranker_mapping.pop("connection", None)
            try:
                reranker_config = RerankerConfig.from_mapping(
                    reranker_mapping
                )
            except RerankerError as exc:
                raise EvaluationJobError(str(exc)) from exc
            if reranker_config is None:  # pragma: no cover - model validated
                raise EvaluationJobError("reranker config is missing")
            if (
                reranker_config.engine == "llm"
                and not reranker_config.connection
            ):
                from butly_core.llm.model_registry import normalize_model_ref

                try:
                    normalize_model_ref(reranker_config.provider_config())
                except ValueError as exc:
                    raise EvaluationJobError(str(exc)) from exc
            role_config = reranker_config.public_dict()
            for key in (
                "enabled",
                "candidate_limit",
                "max_candidate_chars",
                "prompt_version",
            ):
                role_config.pop(key, None)
        normalized[role] = role_config
    return normalized


def build_profile_payload(request: dict[str, Any]) -> dict[str, Any]:
    """Build the same profile sections used by the Colab parameter cell."""
    run_mode = str(request.get("run_mode") or "standard")
    profile: dict[str, Any] = {
        "name": f"web_console_{request['run_id']}",
        "locale": request.get("locale") or "en",
    }

    role_models = _normalize_role_models(request.get("role_models"))
    for role in _INSTANCE_PROFILE_ROLES:
        raw_role = role_models.get(role)
        if not isinstance(raw_role, dict):
            continue
        model_name = str(raw_role.get("model_name") or "").strip()
        if not model_name:
            continue
        role_config: dict[str, Any] = {"model_name": model_name}
        connection = str(raw_role.get("connection") or "").strip()
        if connection:
            role_config["connection"] = connection
        generation_config = raw_role.get("generation_config")
        if isinstance(generation_config, dict) and generation_config:
            role_config["generation_config"] = dict(generation_config)
        if role == "embedding":
            # クエリ/文書 prefix の規約。既定 (auto) はモデル名から推定するが、
            # 名前から判別できないモデル用に明示指定も通す。
            for key in ("profile", "query_prefix", "document_prefix"):
                value = raw_role.get(key)
                if isinstance(value, str) and value.strip():
                    role_config[key] = value
        if role == "reranker":
            for key in (
                "engine",
                "model_revision",
                "batch_size",
                "score_threshold",
                "device",
            ):
                if key in raw_role:
                    role_config[key] = raw_role[key]
            role_config.update(
                {
                    "enabled": True,
                    "candidate_limit": int(
                        request.get("reranker_candidate_limit", 20)
                    ),
                    "max_candidate_chars": int(
                        request.get("reranker_max_candidate_chars", 1600)
                    ),
                }
            )
        profile[role] = role_config

    # Judge is evaluation-only. EvaluationProfile keeps this top-level section
    # separate so it is never merged into an instance config.
    if "judge" in role_models:
        profile["judge"] = dict(role_models["judge"])

    use_rag = bool(request.get("context_rag", True))
    profile["memory"] = {
        "rag_source_mode": request.get("rag_source_mode") or "both",
        "rag_raw_max_chars": int(request.get("rag_raw_max_chars", 2500)),
        "rag_raw_top_k": int(request.get("rag_raw_top_k", 1)),
    }
    search_mode = str(request.get("search_mode") or "vector")
    profile["brain"] = {
        "time_decay_rate": float(request.get("time_decay_rate", 0.0)),
        "use_rag": use_rag,
        "search_mode": search_mode,
    }
    if search_mode in {"hybrid", "hybrid_evidence_fusion"}:
        # BM25 側のパラメータはhybrid系のときだけ書く。vector runのprofileに
        # 無関係なキーを残さない（run 間 diff を読みやすくするため）。
        profile["brain"].update(
            {
                "bm25_candidates": int(request.get("bm25_candidates", 20)),
                "vector_candidates": int(request.get("vector_candidates", 20)),
                "rrf_k": int(request.get("rrf_k", 60)),
                "bm25_max_df_ratio": float(
                    request.get("bm25_max_df_ratio", 0.5)
                ),
            }
        )
        if search_mode == "hybrid_evidence_fusion":
            profile["brain"].update(
                {
                    "evidence_fusion_base_weight": float(
                        request.get("evidence_fusion_base_weight", 0.7)
                    ),
                    "evidence_raw_chunk_chars": int(
                        request.get("evidence_raw_chunk_chars", 1800)
                    ),
                }
            )
    elif search_mode == "dual_query":
        profile["brain"].update(
            {
                "dual_query_candidates": int(
                    request.get("dual_query_candidates", 15)
                ),
                "dual_query_pool_limit": int(
                    request.get("dual_query_pool_limit", 25)
                ),
                "rrf_k": int(request.get("rrf_k", 60)),
            }
        )
    profile["memory_probe"] = {
        "retrieval_execution": str(
            request.get("retrieval_execution") or "always"
        ),
        "injection_policy": str(
            request.get("injection_policy") or "intent_gated"
        ),
    }
    profile["context_levels"] = {
        "preset": "custom",
        "levels": {
            "current_time": (
                "high" if request.get("context_current_time", True) else "off"
            ),
            "mid_term": (
                "high" if request.get("context_mid_term", True) else "off"
            ),
            "session_digest": (
                "high" if request.get("context_session_digest", True) else "off"
            ),
            "rag": "high" if use_rag else "off",
        },
    }

    if run_mode in {"stage3-full", "stage3-source", "stage3-off", "stage3-on"}:
        stage3_enabled = run_mode in {"stage3-full", "stage3-on"}
        profile["memory"].update(
            {
                "knowledge_maturation_enabled": stage3_enabled,
                "knowledge_maturation_batch_size": int(
                    request.get("stage3_batch_size", 10)
                ),
                "knowledge_maturation_bootstrap_max_cards": int(
                    request.get("stage3_bootstrap_max_cards", 2000)
                ),
            }
        )
        profile["sleeptime"] = {
            "update_targets": {"knowledge_maturation": stage3_enabled}
        }
    return profile


def build_job_command(
    request: dict[str, Any],
    *,
    output_dir: Path,
    profile_path: Path,
    python_executable: str = sys.executable,
) -> list[str]:
    """Translate a validated web form request into the existing CLI."""
    run_id = str(request["run_id"])
    run_mode = str(request.get("run_mode") or "standard")
    source_run_id = str(request.get("source_memory_run_id") or "").strip()
    dataset_path = str(request["dataset_path"])
    qa_mode = str(request.get("qa_mode") or "independent")
    workflow = str(request.get("workflow") or "full")
    locale = str(request.get("locale") or "en")

    if source_run_id:
        command = [
            python_executable,
            "-m",
            "evals.locomo.cli",
            "rerun-qa",
            "--source-run",
            str(output_dir / source_run_id),
            "--dataset",
            dataset_path,
            "--output-dir",
            str(output_dir),
            "--run-id",
            run_id,
            "--profile",
            str(profile_path),
            "--qa-mode",
            qa_mode,
            "--locale",
            locale,
        ]
        question_limit = request.get("question_limit")
        if question_limit is None:
            command.append("--all-questions")
        else:
            command.extend(["--question-limit", str(question_limit)])
        if run_mode == "stage3-on":
            command.append("--stage3-bootstrap")
        return command

    command = [
        python_executable,
        "-m",
        "evals.locomo.cli",
        "run",
        "--dataset",
        dataset_path,
        "--output-dir",
        str(output_dir),
        "--run-id",
        run_id,
        "--profile",
        str(profile_path),
        "--qa-mode",
        qa_mode,
        "--workflow",
        workflow,
        "--locale",
        locale,
    ]
    sample_ids = list(request.get("sample_ids") or [])
    if sample_ids:
        command.extend(["--sample-ids", *sample_ids])
    else:
        sample_limit = request.get("sample_limit")
        if sample_limit is None:
            command.append("--all-samples")
        else:
            command.extend(["--sample-limit", str(sample_limit)])
    for dimension in ("session", "question"):
        limit = request.get(f"{dimension}_limit")
        if limit is None:
            command.append(f"--all-{dimension}s")
        else:
            command.extend([f"--{dimension}-limit", str(limit)])
    return command


def validate_job_request(
    request: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    normalized = dict(request)
    run_id = str(normalized.get("run_id") or "").strip()
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise EvaluationJobError(
            "run_id must start with an alphanumeric character and contain only "
            "letters, numbers, '.', '_' or '-'"
        )
    normalized["run_id"] = run_id

    run_mode = str(normalized.get("run_mode") or "standard")
    if run_mode not in RUN_MODES:
        raise EvaluationJobError(f"unsupported run_mode: {run_mode}")
    normalized["run_mode"] = run_mode

    workflow = str(normalized.get("workflow") or "full")
    if workflow not in WORKFLOWS:
        raise EvaluationJobError(f"unsupported workflow: {workflow}")
    normalized["workflow"] = workflow

    qa_mode = str(normalized.get("qa_mode") or "independent")
    if qa_mode not in {"independent", "sequential"}:
        raise EvaluationJobError(f"unsupported qa_mode: {qa_mode}")
    if workflow == "retrieval_prep" and qa_mode != "independent":
        raise EvaluationJobError(
            "retrieval_prep requires qa_mode=independent"
        )
    if workflow == "retrieval_prep" and run_mode in {"stage3-off", "stage3-on"}:
        raise EvaluationJobError(
            "retrieval_prep cannot use Stage 3 QA-only clone modes"
        )
    normalized["qa_mode"] = qa_mode

    dataset_info = describe_locomo_dataset(
        Path(str(normalized.get("dataset_path") or ""))
    )
    normalized["dataset_path"] = dataset_info["dataset_path"]
    raw_sample_ids = normalized.get("sample_ids") or []
    if not isinstance(raw_sample_ids, list):
        raise EvaluationJobError("sample_ids must be an array")
    sample_ids = []
    for raw_sample_id in raw_sample_ids:
        if not isinstance(raw_sample_id, str) or not raw_sample_id.strip():
            raise EvaluationJobError(
                "sample_ids must contain non-empty strings"
            )
        sample_id = raw_sample_id.strip()
        if sample_id in sample_ids:
            raise EvaluationJobError("sample_ids must not contain duplicates")
        sample_ids.append(sample_id)
    available_sample_ids = {
        item["sample_id"] for item in dataset_info["samples"]
    }
    unknown_sample_ids = set(sample_ids) - available_sample_ids
    if unknown_sample_ids:
        raise EvaluationJobError(
            f"unknown sample_ids: {sorted(unknown_sample_ids)}"
        )
    normalized["sample_ids"] = sample_ids
    if sample_ids:
        normalized["sample_limit"] = None

    source_run_id = str(
        normalized.get("source_memory_run_id") or ""
    ).strip()
    if source_run_id and not _SAFE_RUN_ID.fullmatch(source_run_id):
        raise EvaluationJobError("invalid source_memory_run_id")
    if run_mode in {"stage3-full", "stage3-source"} and source_run_id:
        raise EvaluationJobError(
            f"{run_mode} requires a blank source_memory_run_id"
        )
    if run_mode in {"stage3-off", "stage3-on"} and not source_run_id:
        raise EvaluationJobError(f"{run_mode} requires source_memory_run_id")
    if run_mode in {"stage3-source", "stage3-off", "stage3-on"} and (
        normalized.get("qa_mode", "independent") != "independent"
    ):
        raise EvaluationJobError(
            "formal Stage 3 evaluation requires qa_mode=independent"
        )
    if source_run_id == run_id:
        raise EvaluationJobError(
            "run_id must differ from source_memory_run_id"
        )
    if source_run_id and not (output_dir / source_run_id / "run_config.json").is_file():
        raise EvaluationJobError(
            f"source evaluation run not found: {source_run_id}"
        )
    normalized["source_memory_run_id"] = source_run_id or None

    if source_run_id and sample_ids:
        raise EvaluationJobError(
            "sample_ids cannot be changed when source memory is reused"
        )
    if workflow == "retrieval_prep" and source_run_id:
        raise EvaluationJobError(
            "retrieval_prep builds fresh memory and does not accept "
            "source_memory_run_id"
        )

    normalized["role_models"] = _normalize_role_models(
        normalized.get("role_models")
    )
    if (
        workflow == "retrieval_prep"
        and "judge" in normalized["role_models"]
    ):
        raise EvaluationJobError(
            "retrieval_prep does not generate answers, so judge must be disabled"
        )

    allow_mismatch = bool(normalized.get("allow_embedding_mismatch"))
    normalized["allow_embedding_mismatch"] = allow_mismatch
    if source_run_id and not allow_mismatch:
        reason = describe_source_embedding_mismatch(
            output_dir / source_run_id,
            (normalized.get("role_models") or {}).get("embedding"),
        )
        if reason:
            raise EvaluationJobError(
                f"{reason} Re-embed the source workspace or pick a matching "
                "embedding model. Set allow_embedding_mismatch=true to run "
                "anyway (retrieval numbers will be meaningless)."
            )

    if (output_dir / run_id).exists():
        raise EvaluationJobConflict(
            f"evaluation run already exists: {output_dir / run_id}"
        )

    for name in (
        "sample_limit",
        "session_limit",
        "question_limit",
        "rag_raw_top_k",
        "rag_raw_max_chars",
        "stage3_batch_size",
        "stage3_bootstrap_max_cards",
    ):
        value = normalized.get(name)
        if value is None and name.endswith("_limit"):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvaluationJobError(f"{name} must be a non-negative integer")
        if name in {"stage3_batch_size", "stage3_bootstrap_max_cards"} and value < 1:
            raise EvaluationJobError(f"{name} must be at least 1")
        if name.endswith("_limit") and value < 1:
            raise EvaluationJobError(f"{name} must be at least 1 or null")

    # --- 検索設定（検索改修計画 §3.5） ---
    for name, allowed, default in (
        ("search_mode", SEARCH_MODES, "vector"),
        ("retrieval_execution", RETRIEVAL_EXECUTIONS, "always"),
        ("injection_policy", INJECTION_POLICIES, "intent_gated"),
    ):
        value = str(normalized.get(name) or default)
        if value not in allowed:
            raise EvaluationJobError(f"unsupported {name}: {value}")
        normalized[name] = value

    for name, default in (
        ("bm25_candidates", 20),
        ("vector_candidates", 20),
        ("dual_query_candidates", 15),
        ("dual_query_pool_limit", 25),
        ("rrf_k", 60),
        ("reranker_candidate_limit", 20),
    ):
        value = normalized.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EvaluationJobError(f"{name} must be a positive integer")
        normalized[name] = value

    if normalized["reranker_candidate_limit"] > 100:
        raise EvaluationJobError(
            "reranker_candidate_limit must be at most 100"
        )

    reranker_max_chars = normalized.get("reranker_max_candidate_chars", 1600)
    if (
        isinstance(reranker_max_chars, bool)
        or not isinstance(reranker_max_chars, int)
        or not 100 <= reranker_max_chars <= 10000
    ):
        raise EvaluationJobError(
            "reranker_max_candidate_chars must be between 100 and 10000"
        )
    normalized["reranker_max_candidate_chars"] = reranker_max_chars
    if (
        "reranker" in normalized["role_models"]
        and normalized["search_mode"] != "vector"
    ):
        raise EvaluationJobError("reranker currently requires search_mode=vector")

    df_ratio = normalized.get("bm25_max_df_ratio", 0.5)
    if isinstance(df_ratio, bool) or not isinstance(df_ratio, (int, float)):
        raise EvaluationJobError("bm25_max_df_ratio must be a number")
    if not 0.0 < float(df_ratio) <= 1.0:
        raise EvaluationJobError("bm25_max_df_ratio must be in (0.0, 1.0]")
    normalized["bm25_max_df_ratio"] = float(df_ratio)

    _validate_evidence_fusion_settings(normalized)

    return normalized


def _validate_evidence_fusion_settings(request: dict[str, Any]) -> None:
    """Normalize the runtime Episode/RAW fusion controls in-place."""
    weight = request.get("evidence_fusion_base_weight", 0.7)
    if (
        isinstance(weight, bool)
        or not isinstance(weight, (int, float))
        or not 0.0 <= float(weight) <= 1.0
    ):
        raise EvaluationJobError(
            "evidence_fusion_base_weight must be in [0.0, 1.0]"
        )
    request["evidence_fusion_base_weight"] = float(weight)

    raw_chunk_chars = request.get("evidence_raw_chunk_chars", 1800)
    if (
        isinstance(raw_chunk_chars, bool)
        or not isinstance(raw_chunk_chars, int)
        or not 200 <= raw_chunk_chars <= 10000
    ):
        raise EvaluationJobError(
            "evidence_raw_chunk_chars must be between 200 and 10000"
        )
    request["evidence_raw_chunk_chars"] = raw_chunk_chars


def build_dialogue_ab_profile_payload(
    request: dict[str, Any],
) -> dict[str, Any]:
    """Build one shared profile for both Japanese dialogue A/B arms."""
    payload = {
        **request,
        "run_mode": (
            "stage3-full"
            if request.get("stage3_enabled", True)
            else "standard"
        ),
        "locale": "ja",
        "search_mode": request.get("search_mode") or "vector",
        "retrieval_execution": (
            request.get("retrieval_execution") or "always"
        ),
        # The runner overwrites this per isolated arm.
        "injection_policy": "intent_gated",
    }
    profile = build_profile_payload(payload)
    profile["memory_probe"].update(
        {
            "vector_search_limit": int(
                request.get("vector_search_limit", 3)
            ),
            "vector_search_threshold": float(
                request.get("vector_search_threshold", 0.4)
            ),
            "deep_search_enabled": bool(
                request.get("deep_search_enabled", True)
            ),
        }
    )
    return profile


def build_dialogue_ab_command(
    request: dict[str, Any],
    *,
    output_dir: Path,
    profile_path: Path,
    python_executable: str = sys.executable,
) -> list[str]:
    command = [
        python_executable,
        "-m",
        "evals.dialogue_ab",
        "run",
        "--dataset",
        str(request["dataset_path"]),
        "--output-dir",
        str(output_dir),
        "--run-id",
        str(request["run_id"]),
        "--profile",
        str(profile_path),
    ]
    if request.get("seed_instance_path"):
        command.extend(["--seed-instance", str(request["seed_instance_path"])])
    if request.get("reembed"):
        command.append("--reembed")
    command.extend(
        [
            "--intent-gated-search-limit",
            str(request.get("intent_gated_vector_search_limit", 3)),
            "--candidates-search-limit",
            str(request.get("candidates_vector_search_limit", 3)),
        ]
    )
    return command


def validate_dialogue_ab_request(
    request: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate the dedicated Japanese production-dialogue A/B request."""
    from evals.dialogue_ab import load_dialogue_dataset

    normalized = dict(request)
    run_id = str(normalized.get("run_id") or "").strip()
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise EvaluationJobError(
            "run_id must start with an alphanumeric character and contain only "
            "letters, numbers, '.', '_' or '-'"
        )
    normalized["run_id"] = run_id
    if (output_dir / run_id).exists():
        raise EvaluationJobConflict(
            f"dialogue A/B run already exists: {output_dir / run_id}"
        )

    dataset_path = Path(str(normalized.get("dataset_path") or "")).expanduser()
    try:
        dataset_path = dataset_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise EvaluationJobError(f"dataset not found: {dataset_path}") from exc
    if not dataset_path.is_file():
        raise EvaluationJobError(f"dataset is not a file: {dataset_path}")
    try:
        load_dialogue_dataset(dataset_path)
    except ValueError as exc:
        raise EvaluationJobError(str(exc)) from exc
    normalized["dataset_path"] = str(dataset_path)

    normalized["stage3_enabled"] = bool(
        normalized.get("stage3_enabled", True)
    )
    normalized["locale"] = "ja"

    for name, allowed, default in (
        ("search_mode", SEARCH_MODES, "vector"),
        ("retrieval_execution", RETRIEVAL_EXECUTIONS, "always"),
    ):
        value = str(normalized.get(name) or default)
        if value not in allowed:
            raise EvaluationJobError(f"unsupported {name}: {value}")
        normalized[name] = value

    for name, default in (
        ("vector_search_limit", 3),
        ("intent_gated_vector_search_limit", 3),
        ("candidates_vector_search_limit", 3),
        ("bm25_candidates", 20),
        ("vector_candidates", 20),
        ("dual_query_candidates", 15),
        ("dual_query_pool_limit", 25),
        ("rrf_k", 60),
    ):
        value = normalized.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EvaluationJobError(f"{name} must be a positive integer")
        normalized[name] = value

    threshold = normalized.get("vector_search_threshold", 0.4)
    if isinstance(threshold, bool) or not isinstance(
        threshold, (int, float)
    ):
        raise EvaluationJobError("vector_search_threshold must be a number")
    if not 0.0 <= float(threshold) <= 1.0:
        raise EvaluationJobError(
            "vector_search_threshold must be in [0.0, 1.0]"
        )
    normalized["vector_search_threshold"] = float(threshold)
    normalized["deep_search_enabled"] = bool(
        normalized.get("deep_search_enabled", True)
    )

    df_ratio = normalized.get("bm25_max_df_ratio", 0.5)
    if isinstance(df_ratio, bool) or not isinstance(df_ratio, (int, float)):
        raise EvaluationJobError("bm25_max_df_ratio must be a number")
    if not 0.0 < float(df_ratio) <= 1.0:
        raise EvaluationJobError("bm25_max_df_ratio must be in (0.0, 1.0]")
    normalized["bm25_max_df_ratio"] = float(df_ratio)

    for name, default, minimum in (
        ("rag_raw_top_k", 1, 0),
        ("rag_raw_max_chars", 2500, 0),
        ("stage3_batch_size", 10, 1),
        ("stage3_bootstrap_max_cards", 2000, 1),
    ):
        value = normalized.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise EvaluationJobError(f"{name} must be at least {minimum}")
        normalized[name] = value

    decay = normalized.get("time_decay_rate", 0.003)
    if isinstance(decay, bool) or not isinstance(decay, (int, float)):
        raise EvaluationJobError("time_decay_rate must be a number")
    if float(decay) < 0:
        raise EvaluationJobError("time_decay_rate must be non-negative")
    normalized["time_decay_rate"] = float(decay)

    rag_mode = str(normalized.get("rag_source_mode") or "both")
    if rag_mode not in {"cards", "raw", "both"}:
        raise EvaluationJobError(f"unsupported rag_source_mode: {rag_mode}")
    normalized["rag_source_mode"] = rag_mode

    # 既存インスタンスを種にする場合。複製元は読み取りのみで、run 側の
    # コピーだけを操作する（本番インスタンスは変更しない）。
    seed_instance = str(normalized.get("seed_instance") or "").strip()
    if seed_instance:
        instance_dir = (PROJECT_ROOT / "butly_core" / "instances" / seed_instance)
        if not _SAFE_RUN_ID.fullmatch(seed_instance):
            raise EvaluationJobError(f"invalid seed_instance: {seed_instance}")
        if not (instance_dir / "butly_memory.db").is_file():
            raise EvaluationJobError(
                f"seed instance not found: {instance_dir}"
            )
        normalized["seed_instance"] = seed_instance
        normalized["seed_instance_path"] = str(instance_dir.resolve())
    else:
        normalized["seed_instance"] = None
        normalized["seed_instance_path"] = None
    normalized["reembed"] = bool(normalized.get("reembed"))

    normalized["role_models"] = _normalize_role_models(
        normalized.get("role_models")
    )
    if (
        "reranker" in normalized["role_models"]
        and normalized["search_mode"] != "vector"
    ):
        raise EvaluationJobError("reranker currently requires search_mode=vector")
    _validate_evidence_fusion_settings(normalized)
    return normalized


def validate_judge_request(
    request: dict[str, Any],
    *,
    run_id: str,
    run_type: str,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate a post-hoc semantic judge request for an existing run."""
    if run_type not in {"locomo", "dialogue_ab"}:
        raise EvaluationJobError(f"unsupported judge run_type: {run_type}")
    normalized_run_id = str(run_id or "").strip()
    if not _SAFE_RUN_ID.fullmatch(normalized_run_id):
        raise EvaluationJobError(f"invalid run_id: {run_id}")
    run_dir = output_dir / normalized_run_id
    config = None
    try:
        config = json.loads(
            (run_dir / "run_config.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise KeyError(normalized_run_id) from exc
    except json.JSONDecodeError as exc:
        raise EvaluationJobError(
            f"invalid evaluation run: {normalized_run_id}"
        ) from exc
    if not isinstance(config, dict):
        raise EvaluationJobError(
            f"invalid evaluation run: {normalized_run_id}"
        )
    actual_type = (
        "dialogue_ab" if config.get("run_type") == "dialogue_ab" else "locomo"
    )
    if actual_type != run_type:
        raise EvaluationJobError(
            f"run {normalized_run_id} is {actual_type}, not {run_type}"
        )
    if not (run_dir / "scores.json").is_file():
        raise EvaluationJobConflict(
            f"evaluation scores are not ready: {normalized_run_id}"
        )

    judge_model = {
        "connection": request.get("connection"),
        "model_name": request.get("model_name"),
        "generation_config": {
            "temperature": 0.0,
            "max_output_tokens": request.get("max_output_tokens", 2048),
        },
    }
    judge = _normalize_role_models({"judge": judge_model})["judge"]
    return {
        "run_id": normalized_run_id,
        "run_type": run_type,
        "run_dir": str(run_dir.resolve()),
        "judge": judge,
    }


def build_judge_command(
    request: dict[str, Any],
    *,
    python_executable: str = sys.executable,
) -> list[str]:
    """Build a post-hoc judge CLI command without including any secret."""
    run_type = str(request["run_type"])
    judge = request["judge"]
    generation = judge.get("generation_config") or {}
    if run_type == "dialogue_ab":
        command = [
            python_executable,
            "-m",
            "evals.dialogue_ab",
            "judge",
            "--run-dir",
            str(request["run_dir"]),
            "--judge-model-name",
            str(judge["model_name"]),
        ]
        connection_flag = "--judge-connection"
        max_tokens_flag = "--judge-max-output-tokens"
    elif run_type == "locomo":
        command = [
            python_executable,
            "-m",
            "evals.locomo.cli",
            "judge",
            "--run-dir",
            str(request["run_dir"]),
            "--judge-model-name",
            str(judge["model_name"]),
        ]
        connection_flag = "--judge-connection"
        max_tokens_flag = "--judge-max-output-tokens"
    else:  # pragma: no cover - validated before command construction
        raise EvaluationJobError(f"unsupported judge run_type: {run_type}")
    if judge.get("connection"):
        command.extend([connection_flag, str(judge["connection"])])
    command.extend(
        [max_tokens_flag, str(generation.get("max_output_tokens", 2048))]
    )
    return command


def build_retrieval_replay_command(
    request: dict[str, Any],
    *,
    python_executable: str = sys.executable,
) -> list[str]:
    """Build a restart-safe offline retrieval replay command."""
    command = [
        python_executable,
        "-m",
        "evals.locomo.retrieval_replay",
        "--run",
        str(request["run_dir"]),
        "--modes",
        *[str(mode) for mode in request["modes"]],
        "--limit",
        str(request["limit"]),
        "--out",
        str(request["result_path"]),
        "--job-id",
        str(request["job_id"]),
    ]
    profile_path = request.get("profile_path")
    if profile_path:
        command.extend(["--profile", str(profile_path)])

    if {
        "evidence_rerank",
        "hybrid_evidence_rerank",
        "hybrid_evidence_fusion",
    } & set(request["modes"]):
        command.extend(
            [
                "--evidence-raw-chunk-chars",
                str(request["evidence_raw_chunk_chars"]),
                "--evidence-cache",
                str(request["evidence_cache_path"]),
                "--evidence-fusion-base-weight",
                str(request.get("evidence_fusion_base_weight", 0.7)),
            ]
        )

    reranker = request.get("reranker")
    if not isinstance(reranker, dict):
        return command
    command.extend(
        [
            "--reranker-engine",
            str(reranker.get("engine") or "auto"),
            "--reranker-model-name",
            str(reranker["model_name"]),
            "--reranker-max-candidate-chars",
            str(request["reranker_max_candidate_chars"]),
        ]
    )
    if reranker.get("connection"):
        command.extend(
            ["--reranker-connection", str(reranker["connection"])]
        )
    if reranker.get("engine") == "cross_encoder":
        command.extend(
            [
                "--reranker-batch-size",
                str(reranker.get("batch_size", 20)),
                "--reranker-device",
                str(reranker.get("device") or "auto"),
            ]
        )
        if reranker.get("score_threshold") is not None:
            command.extend(
                [
                    "--reranker-score-threshold",
                    str(reranker["score_threshold"]),
                ]
            )
    else:
        generation = reranker.get("generation_config") or {}
        command.extend(
            [
                "--reranker-max-output-tokens",
                str(generation.get("max_output_tokens", 2048)),
            ]
        )
    return command


class EvaluationJobManager:
    """Launch, persist, stop, resume, and inspect evaluation CLI processes."""

    def __init__(
        self,
        data_dir: Path,
        *,
        output_dir: Optional[Path] = None,
        project_root: Path = PROJECT_ROOT,
        python_executable: str = sys.executable,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.project_root = Path(project_root).resolve()
        configured_output = os.environ.get("BUTLY_EVALUATION_OUTPUT_DIR")
        if output_dir is not None:
            resolved_output = Path(output_dir)
        elif configured_output:
            resolved_output = Path(configured_output).expanduser()
        else:
            resolved_output = self.data_dir / "eval_runs" / "runs"
        self.output_dir = resolved_output.resolve()
        self.state_root = self.data_dir / "eval_runs"
        configured_dialogue_output = os.environ.get(
            "BUTLY_DIALOGUE_AB_OUTPUT_DIR"
        )
        self.dialogue_output_dir = (
            Path(configured_dialogue_output).expanduser().resolve()
            if configured_dialogue_output
            else (self.state_root / "dialogue_ab").resolve()
        )
        self.jobs_dir = self.state_root / "jobs"
        self.profiles_dir = self.state_root / "profiles"
        self.python_executable = python_executable
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._monitors: dict[str, threading.Thread] = {}
        for directory in (
            self.output_dir,
            self.dialogue_output_dir,
            self.jobs_dir,
            self.profiles_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._load_records()

    def config(self) -> dict[str, Any]:
        candidates = []
        configured_dataset = os.environ.get("BUTLY_LOCOMO_DATASET")
        for candidate in (
            Path(configured_dataset).expanduser() if configured_dataset else None,
            self.project_root / "data" / "locomo10.json",
            self.data_dir / "data" / "locomo10.json",
            self.project_root / "tests" / "evals" / "fixtures" / "mini_locomo.json",
        ):
            if candidate is None:
                continue
            resolved = candidate.resolve()
            if resolved.is_file() and str(resolved) not in candidates:
                candidates.append(str(resolved))
        with self._lock:
            previous_records = [
                record
                for record in self._records.values()
                if record.get("job_type", "locomo") == "locomo"
                if isinstance(record.get("request"), dict)
            ]
            latest = (
                max(
                    previous_records,
                    key=lambda record: record.get("created_at") or "",
                )
                if previous_records
                else None
            )
            last_request = dict(latest["request"]) if latest is not None else None
            dialogue_records = [
                record
                for record in self._records.values()
                if record.get("job_type") == "dialogue_ab"
                and isinstance(record.get("request"), dict)
            ]
            latest_dialogue = (
                max(
                    dialogue_records,
                    key=lambda record: record.get("created_at") or "",
                )
                if dialogue_records
                else None
            )
            last_dialogue_request = (
                dict(latest_dialogue["request"])
                if latest_dialogue is not None
                else None
            )
        dialogue_candidates = []
        for candidate in (
            self.project_root / "data" / "ja_dialogue_ab_prompts_v1.json",
            self.data_dir / "data" / "ja_dialogue_ab_prompts_v1.json",
        ):
            resolved = candidate.resolve()
            if resolved.is_file() and str(resolved) not in dialogue_candidates:
                dialogue_candidates.append(str(resolved))
        return {
            "output_dir": str(self.output_dir),
            "dataset_candidates": candidates,
            "workflows": list(WORKFLOWS),
            "run_modes": list(RUN_MODES),
            "search_modes": list(SEARCH_MODES),
            "retrieval_executions": list(RETRIEVAL_EXECUTIONS),
            "injection_policies": list(INJECTION_POLICIES),
            # APIキー本体はrequestへ入らない。前回のフォーム設定をBackend再起動後も
            # 復元できるよう、永続job recordの正規化済みrequestを返す。
            "last_request": last_request,
            "dialogue_ab": {
                "output_dir": str(self.dialogue_output_dir),
                "dataset_candidates": dialogue_candidates,
                "last_request": last_dialogue_request,
                "policies": ["intent_gated", "candidates"],
                "rag_source_modes": ["cards", "raw", "both"],
                "search_modes": list(SEARCH_MODES),
                "retrieval_executions": list(RETRIEVAL_EXECUTIONS),
                "seed_instances": self._seed_instance_candidates(),
            },
        }

    def dataset_samples(self, dataset_path: str) -> dict[str, Any]:
        """Return sample IDs and scope counts for the Web selector."""
        return describe_locomo_dataset(Path(dataset_path))

    def _seed_instance_candidates(self) -> list[str]:
        """種にできる実インスタンス名（カードDBを持つものだけ）。"""
        instances_dir = self.project_root / "butly_core" / "instances"
        if not instances_dir.is_dir():
            return []
        return sorted(
            path.name
            for path in instances_dir.iterdir()
            if path.is_dir() and (path / "butly_memory.db").is_file()
        )

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._refresh_records()
            active = [
                record["job_id"]
                for record in self._records.values()
                if record.get("status") in ACTIVE_JOB_STATUSES
            ]
            if active:
                raise EvaluationJobConflict(
                    f"another evaluation job is active: {active[0]}"
                )
            normalized = validate_job_request(
                request,
                output_dir=self.output_dir,
            )
            job_id = uuid4().hex
            profile_path = self.profiles_dir / f"{job_id}.yaml"
            profile = build_profile_payload(normalized)
            atomic_write_text(
                profile_path,
                yaml.safe_dump(
                    profile,
                    allow_unicode=True,
                    sort_keys=False,
                ),
            )
            command = build_job_command(
                normalized,
                output_dir=self.output_dir,
                profile_path=profile_path,
                python_executable=self.python_executable,
            )
            record = {
                "schema_version": 1,
                "job_type": "locomo",
                "job_id": job_id,
                "run_id": normalized["run_id"],
                "run_mode": normalized["run_mode"],
                "workflow": normalized["workflow"],
                "status": "queued",
                "progress": 0.0,
                "phase": "queued",
                "message": "Waiting to start",
                "created_at": utc_now(),
                "started_at": None,
                "ended_at": None,
                "pid": None,
                "process_created_at": None,
                "return_code": None,
                "attempt": 0,
                "stop_requested": False,
                "output_dir": str(self.output_dir),
                "run_dir": str(self.output_dir / normalized["run_id"]),
                "profile_path": str(profile_path),
                "log_path": str(self.jobs_dir / f"{job_id}.log"),
                "request": normalized,
                "command": command,
            }
            self._records[job_id] = record
            self._save_record(record)
            return self._launch(record, command)

    def start_dialogue_ab(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._refresh_records()
            active = [
                record["job_id"]
                for record in self._records.values()
                if record.get("status") in ACTIVE_JOB_STATUSES
            ]
            if active:
                raise EvaluationJobConflict(
                    f"another evaluation job is active: {active[0]}"
                )
            normalized = validate_dialogue_ab_request(
                request,
                output_dir=self.dialogue_output_dir,
            )
            job_id = uuid4().hex
            profile_path = self.profiles_dir / f"{job_id}.yaml"
            profile = build_dialogue_ab_profile_payload(normalized)
            atomic_write_text(
                profile_path,
                yaml.safe_dump(
                    profile,
                    allow_unicode=True,
                    sort_keys=False,
                ),
            )
            command = build_dialogue_ab_command(
                normalized,
                output_dir=self.dialogue_output_dir,
                profile_path=profile_path,
                python_executable=self.python_executable,
            )
            record = {
                "schema_version": 1,
                "job_type": "dialogue_ab",
                "job_id": job_id,
                "run_id": normalized["run_id"],
                "run_mode": "dialogue-ab",
                "status": "queued",
                "progress": 0.0,
                "phase": "queued",
                "message": "Waiting to start",
                "created_at": utc_now(),
                "started_at": None,
                "ended_at": None,
                "pid": None,
                "process_created_at": None,
                "return_code": None,
                "attempt": 0,
                "stop_requested": False,
                "output_dir": str(self.dialogue_output_dir),
                "run_dir": str(
                    self.dialogue_output_dir / normalized["run_id"]
                ),
                "profile_path": str(profile_path),
                "log_path": str(self.jobs_dir / f"{job_id}.log"),
                "request": normalized,
                "command": command,
            }
            self._records[job_id] = record
            self._save_record(record)
            return self._launch(record, command)

    def start_judge(
        self,
        run_id: str,
        request: dict[str, Any],
        *,
        run_type: str,
    ) -> dict[str, Any]:
        """Launch semantic judging for an already completed evaluation run."""
        with self._lock:
            self._refresh_records()
            active = [
                record["job_id"]
                for record in self._records.values()
                if record.get("status") in ACTIVE_JOB_STATUSES
            ]
            if active:
                raise EvaluationJobConflict(
                    f"another evaluation job is active: {active[0]}"
                )
            output_dir = (
                self.dialogue_output_dir
                if run_type == "dialogue_ab"
                else self.output_dir
            )
            normalized = validate_judge_request(
                request,
                run_id=run_id,
                run_type=run_type,
                output_dir=output_dir,
            )
            job_id = uuid4().hex
            command = build_judge_command(
                normalized,
                python_executable=self.python_executable,
            )
            record = {
                "schema_version": 1,
                "job_type": f"{run_type}_judge",
                "job_id": job_id,
                "run_id": normalized["run_id"],
                "run_mode": "semantic-judge",
                "status": "queued",
                "progress": 0.0,
                "phase": "queued",
                "message": "Waiting to start semantic judge",
                "created_at": utc_now(),
                "started_at": None,
                "ended_at": None,
                "pid": None,
                "process_created_at": None,
                "return_code": None,
                "attempt": 0,
                "stop_requested": False,
                "output_dir": str(output_dir),
                "run_dir": normalized["run_dir"],
                "profile_path": None,
                "log_path": str(self.jobs_dir / f"{job_id}.log"),
                "request": normalized,
                "command": command,
            }
            self._records[job_id] = record
            self._save_record(record)
            return self._launch(record, command)

    def start_retrieval_replay(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Launch an offline retrieval comparison as a persistent job."""
        with self._lock:
            self._refresh_records()
            active = [
                record["job_id"]
                for record in self._records.values()
                if record.get("status") in ACTIVE_JOB_STATUSES
            ]
            if active:
                raise EvaluationJobConflict(
                    f"another evaluation job is active: {active[0]}"
                )
            normalized, _override_config = (
                self._prepare_retrieval_replay_request(request)
            )
            job_id = uuid4().hex
            result_path = Path(normalized["run_dir"]) / (
                "retrieval_replay.json"
            )
            normalized.update(
                {
                    "job_id": job_id,
                    "result_path": str(result_path),
                }
            )
            command = build_retrieval_replay_command(
                normalized,
                python_executable=self.python_executable,
            )
            record = {
                "schema_version": 1,
                "job_type": _RETRIEVAL_REPLAY_JOB_TYPE,
                "job_id": job_id,
                "run_id": normalized["run_id"],
                "run_mode": "retrieval-replay",
                "status": "queued",
                "progress": 0.0,
                "phase": "queued",
                "message": "Waiting to compare retrieval modes",
                "created_at": utc_now(),
                "started_at": None,
                "ended_at": None,
                "pid": None,
                "process_created_at": None,
                "return_code": None,
                "attempt": 0,
                "stop_requested": False,
                "output_dir": str(self.output_dir),
                "run_dir": normalized["run_dir"],
                "profile_path": normalized.get("profile_path"),
                "result_path": str(result_path),
                "log_path": str(self.jobs_dir / f"{job_id}.log"),
                "request": normalized,
                "command": command,
            }
            self._records[job_id] = record
            self._save_record(record)
            return self._launch(record, command)

    def stop(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require_job(job_id)
            self._refresh_record(record)
            if record.get("status") not in {"queued", "running", "stopping"}:
                raise EvaluationJobConflict(
                    f"job {job_id} is not running: {record.get('status')}"
                )
            record["status"] = "stopping"
            record["phase"] = "stopping"
            record["message"] = "Stop requested"
            record["stop_requested"] = True
            self._save_record(record)
            process = self._processes.get(job_id)
            pid = record.get("pid")
            created_at = record.get("process_created_at")

        self._terminate_process(process, pid, created_at)
        return self.get(job_id)

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._refresh_records()
            record = self._require_job(job_id)
            if record.get("status") not in RESUMABLE_JOB_STATUSES:
                raise EvaluationJobConflict(
                    f"job {job_id} cannot resume from {record.get('status')}"
                )
            active = [
                item["job_id"]
                for item in self._records.values()
                if item.get("job_id") != job_id
                and item.get("status") in ACTIVE_JOB_STATUSES
            ]
            if active:
                raise EvaluationJobConflict(
                    f"another evaluation job is active: {active[0]}"
                )
            run_dir = Path(record["run_dir"])
            if not (run_dir / "run_config.json").is_file():
                raise EvaluationJobConflict(
                    "run directory was not created; start a new run instead"
                )
            job_type = record.get("job_type")
            if job_type in _JUDGE_JOB_TYPES:
                command = build_judge_command(
                    record["request"],
                    python_executable=self.python_executable,
                )
            elif job_type == _RETRIEVAL_REPLAY_JOB_TYPE:
                command = build_retrieval_replay_command(
                    record["request"],
                    python_executable=self.python_executable,
                )
            elif job_type == "dialogue_ab":
                command = [
                    self.python_executable,
                    "-m",
                    "evals.dialogue_ab",
                    "resume",
                    "--run-dir",
                    str(run_dir),
                ]
            else:
                command = [
                    self.python_executable,
                    "-m",
                    "evals.locomo.cli",
                    "resume",
                    "--run-dir",
                    str(run_dir),
                ]
            record["command"] = command
            record["stop_requested"] = False
            record["status"] = "queued"
            record["phase"] = "resume"
            record["message"] = "Waiting to resume"
            record["ended_at"] = None
            record["return_code"] = None
            self._save_record(record)
            return self._launch(record, command)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require_job(job_id)
            self._refresh_record(record)
            return self._public_record(record)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_records()
            records = [
                self._public_record(record)
                for record in self._records.values()
            ]
        return sorted(
            records,
            key=lambda record: record.get("created_at") or "",
            reverse=True,
        )

    def read_log(self, job_id: str, *, tail_lines: int = 200) -> str:
        with self._lock:
            record = self._require_job(job_id)
            log_path = Path(record["log_path"])
        if not log_path.is_file():
            return ""
        lines = log_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        return "\n".join(lines[-tail_lines:])

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_records()
            jobs_by_run = self._latest_jobs_by_run(
                {"locomo", "locomo_judge"}
            )
        runs = []
        for config_path in self.output_dir.glob("*/run_config.json"):
            try:
                summary = self._summarize_run(config_path.parent)
            except EvaluationJobError:
                logger.exception(
                    "Failed to summarize evaluation run: %s",
                    config_path.parent,
                )
                continue
            job = jobs_by_run.get(summary["run_id"])
            if job is not None:
                if job.get("job_type", "locomo") == "locomo":
                    summary["run_mode"] = job.get("run_mode")
                summary["job_id"] = job["job_id"]
                if job.get("status") in ACTIVE_JOB_STATUSES:
                    summary["status"] = job["status"]
                    summary["progress"] = job.get("progress", 0.0)
            runs.append(summary)
        return sorted(
            runs,
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )

    def list_dialogue_ab_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_records()
            jobs_by_run = self._latest_jobs_by_run(
                {"dialogue_ab", "dialogue_ab_judge"}
            )
        runs = []
        for config_path in self.dialogue_output_dir.glob("*/run_config.json"):
            try:
                summary = self._summarize_dialogue_ab_run(
                    config_path.parent
                )
            except EvaluationJobError:
                logger.exception(
                    "Failed to summarize dialogue A/B run: %s",
                    config_path.parent,
                )
                continue
            job = jobs_by_run.get(summary["run_id"])
            if job is not None:
                summary["job_id"] = job["job_id"]
                if job.get("status") in ACTIVE_JOB_STATUSES:
                    summary["status"] = job["status"]
                    summary["progress"] = job.get("progress", 0.0)
            runs.append(summary)
        return sorted(
            runs,
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )

    def _latest_jobs_by_run(
        self,
        job_types: set[str],
    ) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in self._records.values():
            if record.get("job_type", "locomo") not in job_types:
                continue
            run_id = str(record.get("run_id") or "")
            current = latest.get(run_id)
            if current is None or (record.get("created_at") or "") > (
                current.get("created_at") or ""
            ):
                latest[run_id] = record
        return latest

    def get_dialogue_ab_result(self, run_id: str) -> dict[str, Any]:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise EvaluationJobError(f"invalid run_id: {run_id}")
        run_dir = self.dialogue_output_dir / run_id
        scores = self._read_json(run_dir / "scores.json")
        if not isinstance(scores, dict):
            raise KeyError(run_id)
        return scores

    def get_run_result(self, run_id: str) -> dict[str, Any]:
        """Return LoCoMo official answers merged with current judge details."""
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise EvaluationJobError(f"invalid run_id: {run_id}")
        run_dir = self.output_dir / run_id
        scores = self._read_json(run_dir / "scores.json")
        if not isinstance(scores, dict):
            raise KeyError(run_id)

        from .semantic_judge_runner import build_semantic_question_details

        semantic, questions = build_semantic_question_details(
            scores,
            self._read_json(run_dir / "semantic_scores.json"),
        )
        semantic_public = dict(semantic) if isinstance(semantic, dict) else None
        if semantic_public is not None:
            # The merged rows below are the canonical problem-level response.
            # Avoid returning a second, potentially bulky copy.
            semantic_public.pop("questions", None)
        review_count = sum(
            1 for item in questions if item.get("review_required")
        )
        if not semantic or semantic.get("status") == "stale":
            review_count = None
        return {
            "run_id": str(scores.get("run_id") or run_id),
            "question_count": scores.get("question_count", len(questions)),
            "official": scores.get("official") or {},
            "semantic_judge": semantic_public,
            "review_required_count": review_count,
            "questions": questions,
        }

    def get_retrieval_replay_result(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Return the latest completed offline retrieval comparison."""
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise EvaluationJobError(f"invalid run_id: {run_id}")
        run_dir = self.output_dir / run_id
        if not (run_dir / "run_config.json").is_file():
            raise KeyError(run_id)
        result = self._read_json(run_dir / "retrieval_replay.json")
        if not isinstance(result, dict):
            raise KeyError(run_id)
        return result

    def compare_retrieval_runs(
        self,
        run_ids: list[str],
    ) -> dict[str, Any]:
        """Compare saved offline retrieval results across evaluation runs."""
        if len(run_ids) < 2:
            raise EvaluationJobError("select at least two retrieval runs")
        if len(run_ids) > 8:
            raise EvaluationJobError("compare at most eight retrieval runs")
        if len(set(run_ids)) != len(run_ids):
            raise EvaluationJobError("run_ids must be unique")

        results: dict[str, dict[str, Any]] = {}
        question_maps: dict[
            str,
            dict[str, dict[tuple[str, str], dict[str, Any]]],
        ] = {}
        run_rows = []
        warnings = []
        for run_id in run_ids:
            if not _SAFE_RUN_ID.fullmatch(run_id):
                raise EvaluationJobError(f"invalid run_id: {run_id}")
            try:
                result = self.get_retrieval_replay_result(run_id)
            except KeyError as exc:
                raise EvaluationJobError(
                    f"retrieval replay not found for run: {run_id}"
                ) from exc
            if result.get("status") != "completed":
                raise EvaluationJobError(
                    f"retrieval replay is not completed for run: {run_id}"
                )
            modes = [
                mode
                for mode in RETRIEVAL_REPLAY_MODES
                if isinstance(result.get(mode), dict)
            ]
            if not modes:
                raise EvaluationJobError(
                    f"retrieval modes not found for run: {run_id}"
                )
            results[run_id] = result
            by_mode = {
                mode: self._retrieval_question_map(result[mode])
                for mode in modes
            }
            question_maps[run_id] = by_mode
            mode_sets = [set(items) for items in by_mode.values()]
            canonical = max(mode_sets, key=len, default=set())
            internally_consistent = all(
                item_set == canonical for item_set in mode_sets
            )
            if not internally_consistent:
                warnings.append(
                    f"{run_id}: modeごとの質問集合が一致しません"
                )
            run_rows.append(
                {
                    "run_id": run_id,
                    "generated_at": result.get("generated_at"),
                    "limit": result.get("limit"),
                    "oracle_questions": result.get("oracle_questions"),
                    "sample_ids": sorted(
                        {sample_id for sample_id, _ in canonical}
                    ),
                    "question_count": len(canonical),
                    "question_keys": canonical,
                    "modes": modes,
                    "mode_question_counts": {
                        mode: len(items) for mode, items in by_mode.items()
                    },
                }
            )

        baseline = run_ids[0]
        comparison = run_ids[-1]
        baseline_keys = run_rows[0]["question_keys"]
        question_set_match = all(
            row["question_keys"] == baseline_keys for row in run_rows
        )
        if not question_set_match:
            warnings.append(
                "Sample IDまたは質問集合が異なるため、run間の数値は参考比較です"
            )
        limits = {row.get("limit") for row in run_rows}
        limit_match = len(limits) == 1
        if not limit_match:
            warnings.append(
                "候補数limitが異なるため、Recall@kを直接比較できません"
            )
        common_modes = [
            mode
            for mode in RETRIEVAL_REPLAY_MODES
            if all(mode in row["modes"] for row in run_rows)
        ]
        if not common_modes:
            warnings.append("全runに共通する検索モードがありません")

        baseline_result = results[baseline]
        metric_rows = []
        for row in run_rows:
            run_id = row["run_id"]
            result = results[run_id]
            for mode in row["modes"]:
                stats = result[mode]
                diagnostics = (
                    stats.get("evidence_reranker")
                    or stats.get("reranker")
                    or stats.get("query_fusion")
                    or {}
                )
                metric = {
                    "run_id": run_id,
                    "mode": mode,
                    "recall_at_1": stats.get("recall_at_1"),
                    "recall_at_3": stats.get("recall_at_3"),
                    "recall_at_20": stats.get("recall_at_20"),
                    "hit_at_1": stats.get("hit_at_1"),
                    "hit_at_3": stats.get("hit_at_3"),
                    "hit_at_20": stats.get("hit_at_20"),
                    "base_search_mode": diagnostics.get(
                        "base_search_mode"
                    ),
                    "rescued_at_3": diagnostics.get("rescued_at_3"),
                    "harmed_at_3": diagnostics.get("harmed_at_3"),
                    "fallback_rate": diagnostics.get("fallback_rate"),
                }
                baseline_stats = baseline_result.get(mode)
                for k in (1, 3, 20):
                    current_value = stats.get(f"recall_at_{k}")
                    baseline_value = (
                        baseline_stats.get(f"recall_at_{k}")
                        if isinstance(baseline_stats, dict)
                        else None
                    )
                    metric[f"delta_vs_baseline_at_{k}"] = (
                        current_value - baseline_value
                        if self._is_number(current_value)
                        and self._is_number(baseline_value)
                        else None
                    )
                metric_rows.append(metric)

        question_rows = []
        for mode in common_modes:
            baseline_map = question_maps[baseline][mode]
            comparison_map = question_maps[comparison][mode]
            for key in sorted(set(baseline_map) & set(comparison_map)):
                base_item = baseline_map[key]
                comparison_item = comparison_map[key]
                item = {
                    "mode": mode,
                    "sample_id": key[0],
                    "question_id": key[1],
                    "question": (
                        comparison_item.get("question")
                        or base_item.get("question")
                    ),
                }
                for k in (1, 3, 20):
                    base_value = base_item.get(f"recall_at_{k}")
                    comparison_value = comparison_item.get(f"recall_at_{k}")
                    item[f"baseline_recall_at_{k}"] = base_value
                    item[f"comparison_recall_at_{k}"] = comparison_value
                    item[f"delta_at_{k}"] = (
                        comparison_value - base_value
                        if self._is_number(comparison_value)
                        and self._is_number(base_value)
                        else None
                    )
                question_rows.append(item)
        question_rows.sort(
            key=lambda item: (
                item["mode"],
                item["delta_at_3"] is None,
                item["delta_at_3"] or 0.0,
                item["sample_id"],
                item["question_id"],
            )
        )

        for row in run_rows:
            row.pop("question_keys", None)
        return {
            "baseline_run_id": baseline,
            "comparison_run_id": comparison,
            "comparable": (
                question_set_match and limit_match and bool(common_modes)
            ),
            "question_set_match": question_set_match,
            "limit_match": limit_match,
            "common_modes": common_modes,
            "warnings": warnings,
            "runs": run_rows,
            "metrics": metric_rows,
            "questions": question_rows,
        }

    @staticmethod
    def _retrieval_question_map(
        stats: dict[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        items = {}
        for detail in stats.get("details") or []:
            if not isinstance(detail, dict):
                continue
            sample_id = str(detail.get("sample_id") or "")
            question_id = str(detail.get("question_id") or "")
            if sample_id and question_id:
                items[(sample_id, question_id)] = detail
        return items

    @staticmethod
    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _prepare_retrieval_replay_request(
        self,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate replay input and resolve the source run profile."""
        if not isinstance(request, dict):
            raise EvaluationJobError("retrieval replay request must be a mapping")
        run_id = str(request.get("run_id") or "").strip()
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise EvaluationJobError(f"invalid run_id: {run_id}")
        raw_modes = request.get("modes")
        if not isinstance(raw_modes, list):
            raw_modes = []
        modes = [str(mode) for mode in raw_modes]
        unknown = [mode for mode in modes if mode not in RETRIEVAL_REPLAY_MODES]
        if not modes or unknown:
            raise EvaluationJobError(
                f"modes must be a subset of {list(RETRIEVAL_REPLAY_MODES)}"
            )
        if len(set(modes)) != len(modes):
            raise EvaluationJobError("modes must not contain duplicates")

        limit = request.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise EvaluationJobError("limit must be a positive integer")
        if limit > 100:
            raise EvaluationJobError("limit must be at most 100")
        max_chars = request.get("reranker_max_candidate_chars", 1600)
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 100 <= max_chars <= 10000
        ):
            raise EvaluationJobError(
                "reranker_max_candidate_chars must be between 100 and 10000"
            )
        evidence_raw_chunk_chars = request.get(
            "evidence_raw_chunk_chars", 1800
        )
        if (
            isinstance(evidence_raw_chunk_chars, bool)
            or not isinstance(evidence_raw_chunk_chars, int)
            or not 200 <= evidence_raw_chunk_chars <= 10000
        ):
            raise EvaluationJobError(
                "evidence_raw_chunk_chars must be between 200 and 10000"
            )
        evidence_fusion_base_weight = request.get(
            "evidence_fusion_base_weight", 0.7
        )
        if (
            isinstance(evidence_fusion_base_weight, bool)
            or not isinstance(evidence_fusion_base_weight, (int, float))
            or not 0.0 <= float(evidence_fusion_base_weight) <= 1.0
        ):
            raise EvaluationJobError(
                "evidence_fusion_base_weight must be between 0 and 1"
            )

        run_dir = self.output_dir / run_id
        config = self._read_json(run_dir / "run_config.json")
        if not isinstance(config, dict):
            raise KeyError(run_id)
        override_config = self._run_profile_sections(run_dir)
        normalized_reranker = None
        reranker = request.get("reranker")
        if reranker is not None:
            normalized_reranker = _normalize_role_models(
                {"reranker": reranker}
            )["reranker"]
            override_config["reranker"] = {
                **normalized_reranker,
                "enabled": True,
                "candidate_limit": limit,
                "max_candidate_chars": max_chars,
            }
        if "reranked" in modes and not (
            isinstance(override_config.get("reranker"), dict)
            and override_config["reranker"].get("model_name")
        ):
            raise EvaluationJobError(
                "reranked mode requires a reranker model selection"
            )

        profile_path = None
        raw_profile_path = config.get("profile_path")
        if raw_profile_path:
            candidate = Path(str(raw_profile_path))
            if candidate.is_file():
                profile_path = str(candidate)
        normalized = {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "modes": modes,
            "limit": limit,
            "reranker": normalized_reranker,
            "reranker_max_candidate_chars": max_chars,
            "evidence_raw_chunk_chars": evidence_raw_chunk_chars,
            "evidence_fusion_base_weight": float(
                evidence_fusion_base_weight
            ),
            "evidence_cache_path": str(
                run_dir
                / "retrieval_cache"
                / "evidence_embeddings.sqlite3"
            ),
            "profile_path": profile_path,
        }
        return normalized, override_config

    def retrieval_replay(
        self,
        run_id: str,
        modes: list[str],
        *,
        limit: int = 20,
        reranker: Optional[dict[str, Any]] = None,
        reranker_max_candidate_chars: int = 1600,
        evidence_raw_chunk_chars: int = 1800,
        evidence_fusion_base_weight: float = 0.7,
    ) -> dict[str, Any]:
        """既存 run の記憶に対して検索だけを回し、Recall@k を比較する。

        QA を回す前の足切り（検索改修計画 §8）。``bm25`` は embedding を
        呼ばない。``vector`` / ``hybrid`` は質問1件につきembeddingを1回、
        ``dual_query``は2回（旧runは加えてGatekeeperを1回）呼ぶため、
        Evidence rerank系は初回にEpisode/RAW文書もembeddingするため、runの
        規模に比例して時間がかかる。``hybrid_evidence_rerank``はhybrid候補を
        同じevidence indexでtop 3へ並べ替える。
        結果は run 直下の ``retrieval_replay.json`` に残す。
        """
        from evals.locomo.retrieval_replay import evaluate

        normalized, override_config = self._prepare_retrieval_replay_request(
            {
                "run_id": run_id,
                "modes": list(modes),
                "limit": limit,
                "reranker": reranker,
                "reranker_max_candidate_chars": (
                    reranker_max_candidate_chars
                ),
                "evidence_raw_chunk_chars": evidence_raw_chunk_chars,
                "evidence_fusion_base_weight": (
                    evidence_fusion_base_weight
                ),
            }
        )
        run_dir = Path(normalized["run_dir"])
        try:
            result = evaluate(
                run_dir,
                normalized["modes"],
                limit=normalized["limit"],
                override_config=override_config,
                evidence_cache_path=Path(
                    normalized["evidence_cache_path"]
                ),
                evidence_raw_chunk_chars=normalized[
                    "evidence_raw_chunk_chars"
                ],
                evidence_fusion_base_weight=normalized[
                    "evidence_fusion_base_weight"
                ],
            )
        except (FileNotFoundError, ValueError) as exc:
            raise EvaluationJobError(str(exc)) from exc

        result["status"] = "completed"
        result["generated_at"] = utc_now()
        result["limit"] = normalized["limit"]
        result["modes"] = normalized["modes"]
        atomic_write_text(
            run_dir / "retrieval_replay.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result

    def _run_profile_sections(self, run_dir: Path) -> dict[str, Any]:
        """run が使った profile の section を読む（embedding 接続の再現用）。

        profile が消えていても replay 自体は続ける（その場合はグローバル設定の
        embedding が使われる）。
        """
        config = self._read_json(run_dir / "run_config.json") or {}
        profile_path = config.get("profile_path")
        if not profile_path:
            return {}
        path = Path(str(profile_path))
        if not path.is_file():
            return {}
        try:
            from evals.locomo.config import load_profile

            profile = load_profile(path)
            sections = dict(profile.sections)
            if profile.locale:
                sections.setdefault("agent_profile", {})["locale"] = (
                    profile.locale
                )
            return sections
        except (OSError, ValueError) as exc:
            print(f"[Evaluation] profile 読み込み失敗 ({path}): {exc}")
            return {}

    def compare_runs(self, run_ids: list[str]) -> dict[str, Any]:
        if len(run_ids) < 2:
            raise EvaluationJobError("select at least two runs")
        if len(run_ids) > 8:
            raise EvaluationJobError("compare at most eight runs")
        if len(set(run_ids)) != len(run_ids):
            raise EvaluationJobError("run_ids must be unique")

        rows = []
        question_maps: dict[
            tuple[str, str], dict[str, dict[str, Any]]
        ] = {}
        for run_id in run_ids:
            if not _SAFE_RUN_ID.fullmatch(run_id):
                raise EvaluationJobError(f"invalid run_id: {run_id}")
            run_dir = self.output_dir / run_id
            summary = self._summarize_run(run_dir)
            if not summary.get("has_scores"):
                raise EvaluationJobError(f"scores not found for run: {run_id}")
            rows.append(summary)
            scores = self._read_json(run_dir / "scores.json") or {}
            for question in scores.get("questions", []):
                question_id = str(question.get("question_id") or "")
                sample_id = str(question.get("sample_id") or "")
                if not question_id or not sample_id:
                    continue
                question_maps.setdefault((sample_id, question_id), {})[
                    run_id
                ] = {
                    "score": question.get("official_score"),
                    "prediction": question.get("prediction"),
                    "expected_answer": question.get("expected_answer"),
                    "question": question.get("question"),
                    "sample_id": question.get("sample_id"),
                    "retrieval_recall_at_3": question.get("recall_at_3"),
                    "hybrid_recall_at_3": question.get(
                        "hybrid_recall_at_3"
                    ),
                    "evidence_fusion_status": question.get(
                        "evidence_fusion_status"
                    ),
                    "evidence_fusion_fallback": question.get(
                        "evidence_fusion_fallback"
                    ),
                    "evidence_fusion_latency_ms": question.get(
                        "evidence_fusion_latency_ms"
                    ),
                }

        questions = []
        baseline = run_ids[0]
        comparison = run_ids[-1]
        for (sample_id, question_id), by_run in question_maps.items():
            base_score = by_run.get(baseline, {}).get("score")
            comparison_score = by_run.get(comparison, {}).get("score")
            delta = None
            if isinstance(base_score, (int, float)) and isinstance(
                comparison_score, (int, float)
            ):
                delta = comparison_score - base_score
            seed = next(iter(by_run.values()))
            questions.append(
                {
                    "question_id": question_id,
                    "sample_id": sample_id,
                    "question": seed.get("question"),
                    "expected_answer": seed.get("expected_answer"),
                    "delta": delta,
                    "runs": by_run,
                }
            )
        questions.sort(
            key=lambda item: (
                item["delta"] is None,
                item["delta"] if item["delta"] is not None else 0.0,
                item["sample_id"],
                item["question_id"],
            )
        )
        return {
            "baseline_run_id": baseline,
            "comparison_run_id": comparison,
            "runs": rows,
            "questions": questions,
        }

    def _launch(
        self,
        record: dict[str, Any],
        command: list[str],
    ) -> dict[str, Any]:
        # Capture the attempt boundary before the child can write a completion
        # artifact.  A cached post-hoc judge may finish before Popen returns.
        attempt_started_at = utc_now()
        log_path = Path(record["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            if log_path.stat().st_size:
                log_file.write(
                    f"\n=== attempt {record.get('attempt', 0) + 1} "
                    f"started {utc_now()} ===\n"
                )
                log_file.flush()
            popen_kwargs: dict[str, Any] = {
                "cwd": str(self.project_root),
                "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0,
                )
            else:
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **popen_kwargs)
            except OSError as exc:
                record["status"] = "failed"
                record["phase"] = "launch"
                record["message"] = f"Failed to start: {exc}"
                record["ended_at"] = utc_now()
                self._save_record(record)
                raise EvaluationJobError(str(exc)) from exc

        record["status"] = "running"
        record["phase"] = "setup"
        record["message"] = (
            "Retrieval replay process started"
            if record.get("job_type") == _RETRIEVAL_REPLAY_JOB_TYPE
            else (
                "Retrieval preparation process started"
                if self._record_workflow(record) == "retrieval_prep"
                else "Evaluation process started"
            )
        )
        record["started_at"] = attempt_started_at
        record["pid"] = process.pid
        record["process_created_at"] = self._process_create_time(process.pid)
        record["attempt"] = int(record.get("attempt", 0)) + 1
        self._processes[record["job_id"]] = process
        self._save_record(record)
        monitor = threading.Thread(
            target=self._monitor,
            args=(record["job_id"], process),
            name=f"locomo-eval-{record['job_id'][:8]}",
            daemon=True,
        )
        self._monitors[record["job_id"]] = monitor
        monitor.start()
        return self._public_record(record)

    def _monitor(self, job_id: str, process: subprocess.Popen) -> None:
        last_log_size = 0
        while process.poll() is None:
            self._update_progress_from_log(job_id, start_offset=last_log_size)
            try:
                last_log_size = Path(
                    self._records[job_id]["log_path"]
                ).stat().st_size
            except (KeyError, OSError):
                pass
            time.sleep(0.5)
        self._update_progress_from_log(job_id, start_offset=last_log_size)
        return_code = process.returncode
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            record["return_code"] = return_code
            record["ended_at"] = utc_now()
            record["pid"] = None
            record["process_created_at"] = None
            if record.get("stop_requested"):
                record["status"] = "stopped"
                record["phase"] = "stopped"
                record["message"] = "Evaluation stopped"
            elif (
                return_code == 0
                and self._record_judge_config(record) is not None
                and not self._judge_output_complete(record)
            ):
                record["status"] = "failed"
                record["phase"] = "judge"
                record["message"] = (
                    "Semantic judge exited without a matching result"
                )
            elif (
                return_code == 0
                and record.get("job_type") == _RETRIEVAL_REPLAY_JOB_TYPE
                and not self._retrieval_replay_output_complete(record)
            ):
                record["status"] = "failed"
                record["phase"] = "retrieval"
                record["message"] = (
                    "Retrieval replay exited without a matching result"
                )
            elif (
                return_code == 0
                and self._record_workflow(record) == "retrieval_prep"
                and not self._retrieval_prep_output_complete(record)
            ):
                record["status"] = "failed"
                record["phase"] = "retrieval-prep"
                record["message"] = (
                    "Retrieval preparation exited without a complete manifest"
                )
            elif return_code == 0:
                record["status"] = "completed"
                record["progress"] = 100.0
                record["phase"] = "complete"
                record["message"] = (
                    "Retrieval replay completed"
                    if record.get("job_type")
                    == _RETRIEVAL_REPLAY_JOB_TYPE
                    else (
                        "Retrieval preparation completed"
                        if self._record_workflow(record) == "retrieval_prep"
                        else "Evaluation completed"
                    )
                )
            else:
                record["status"] = "failed"
                record["phase"] = "failed"
                record["message"] = f"Evaluation failed (exit {return_code})"
            self._save_record(record)
            self._processes.pop(job_id, None)
            self._monitors.pop(job_id, None)

    def _update_progress_from_log(
        self,
        job_id: str,
        *,
        start_offset: int = 0,
    ) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            log_path = Path(record["log_path"])
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(start_offset)
                lines = stream.readlines()
        except OSError:
            return
        latest = None
        for line in lines:
            match = _PROGRESS_LINE.match(line.rstrip())
            if match:
                latest = (
                    float(match.group(1)),
                    match.group(2),
                    match.group(3),
                )
        if latest is None:
            return
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.get("status") not in ACTIVE_JOB_STATUSES:
                return
            record["progress"], record["phase"], record["message"] = latest
            self._save_record(record)

    def _refresh_records(self) -> None:
        for record in self._records.values():
            self._refresh_record(record)

    def _refresh_record(self, record: dict[str, Any]) -> None:
        if record.get("status") not in ACTIVE_JOB_STATUSES:
            return
        job_id = record["job_id"]
        process = self._processes.get(job_id)
        if process is not None:
            if process.poll() is None:
                return
            monitor = self._monitors.get(job_id)
            if monitor is not None and monitor.is_alive():
                return
        if self._pid_matches(
            record.get("pid"),
            record.get("process_created_at"),
        ):
            self._update_progress_from_log(job_id)
            return

        run_dir = Path(record["run_dir"])
        record["pid"] = None
        record["process_created_at"] = None
        record["ended_at"] = record.get("ended_at") or utc_now()
        if record.get("job_type") == _RETRIEVAL_REPLAY_JOB_TYPE:
            if self._retrieval_replay_output_complete(record):
                record["status"] = "completed"
                record["progress"] = 100.0
                record["phase"] = "complete"
                record["message"] = "Retrieval replay completed"
            elif record.get("stop_requested"):
                record["status"] = "stopped"
                record["phase"] = "stopped"
                record["message"] = "Retrieval replay stopped"
            else:
                record["status"] = "interrupted"
                record["phase"] = "interrupted"
                record["message"] = "Retrieval replay is no longer running"
        elif self._record_workflow(record) == "retrieval_prep":
            if self._retrieval_prep_output_complete(record):
                record["status"] = "completed"
                record["progress"] = 100.0
                record["phase"] = "complete"
                record["message"] = "Retrieval preparation completed"
            elif record.get("stop_requested"):
                record["status"] = "stopped"
                record["phase"] = "stopped"
                record["message"] = "Retrieval preparation stopped"
            else:
                record["status"] = "interrupted"
                record["phase"] = "interrupted"
                record["message"] = (
                    "Retrieval preparation is no longer running"
                )
        elif self._record_judge_config(record) is not None:
            if self._judge_output_complete(record):
                record["status"] = "completed"
                record["progress"] = 100.0
                record["phase"] = "complete"
                record["message"] = "Semantic judge completed"
            elif record.get("stop_requested"):
                record["status"] = "stopped"
                record["phase"] = "stopped"
                record["message"] = "Semantic judge stopped"
            else:
                record["status"] = "interrupted"
                record["phase"] = "interrupted"
                record["message"] = "Semantic judge is no longer running"
        elif (run_dir / "scores.json").is_file():
            record["status"] = "completed"
            record["progress"] = 100.0
            record["phase"] = "complete"
            record["message"] = "Evaluation completed"
        elif record.get("stop_requested"):
            record["status"] = "stopped"
            record["phase"] = "stopped"
            record["message"] = "Evaluation stopped"
        else:
            record["status"] = "interrupted"
            record["phase"] = "interrupted"
            record["message"] = "Evaluation process is no longer running"
        self._save_record(record)

    def _retrieval_prep_output_complete(
        self,
        record: dict[str, Any],
    ) -> bool:
        """Return whether a QA-free run has complete memory and questions."""
        run_dir = Path(record["run_dir"])
        config = self._read_json(run_dir / "run_config.json") or {}
        checkpoint = self._read_json(
            run_dir / "checkpoints" / "checkpoint.json"
        ) or {}
        manifest = self._read_json(
            run_dir / "results" / "retrieval_questions.json"
        ) or {}
        questions = manifest.get("questions")
        if config.get("workflow") != "retrieval_prep":
            return False
        if checkpoint.get("status") != "completed":
            return False
        if (
            manifest.get("workflow") != "retrieval_prep"
            or manifest.get("run_id") != record.get("run_id")
            or not isinstance(questions, list)
            or not questions
            or not all(isinstance(question, dict) for question in questions)
            or manifest.get("question_count") != len(questions)
        ):
            return False
        selected = list(config.get("selected_sample_ids") or [])
        return list(manifest.get("sample_ids") or []) == selected

    def _retrieval_replay_output_complete(
        self,
        record: dict[str, Any],
    ) -> bool:
        """Return whether this replay attempt wrote its matching artifact."""
        result_path = record.get("result_path") or (
            Path(record["run_dir"]) / "retrieval_replay.json"
        )
        result = self._read_json(Path(result_path))
        if not isinstance(result, dict) or result.get("status") != "completed":
            return False
        if result.get("job_id") != record.get("job_id"):
            return False
        request = record.get("request") or {}
        if result.get("modes") != request.get("modes"):
            return False
        if result.get("limit") != request.get("limit"):
            return False
        return self._judge_result_is_fresh(
            result.get("generated_at"),
            record.get("started_at"),
        )

    def _judge_output_complete(self, record: dict[str, Any]) -> bool:
        """Return whether the run has a complete result for this judge model."""
        run_dir = Path(record["run_dir"])
        job_type = record.get("job_type", "locomo")
        if job_type in {"dialogue_ab", "dialogue_ab_judge"}:
            scores = self._read_json(run_dir / "scores.json") or {}
            result = scores.get("semantic_judge")
            generated_at = (
                result.get("generated_at")
                if isinstance(result, dict)
                else None
            ) or scores.get("generated_at")
            actual_model = (
                result.get("model") if isinstance(result, dict) else None
            )
        elif job_type in {"locomo", "locomo_judge"}:
            result = self._read_json(run_dir / "semantic_scores.json")
            scores = self._read_json(run_dir / "scores.json")
            if isinstance(result, dict) and isinstance(scores, dict):
                from .semantic_judge_runner import (
                    semantic_scores_for_current_inputs,
                )

                result = semantic_scores_for_current_inputs(scores, result)
            generated_at = (
                result.get("generated_at")
                if isinstance(result, dict)
                else None
            )
            actual_model = None
            if isinstance(result, dict):
                actual_model = result.get("judge") or result.get("model")
                if not isinstance(actual_model, dict):
                    summary = result.get("summary") or {}
                    actual_model = (
                        summary.get("model")
                        if isinstance(summary, dict)
                        else None
                    )
        else:
            return False
        if not isinstance(result, dict) or result.get("status") != "completed":
            return False
        if not isinstance(actual_model, dict):
            return False
        if not self._judge_result_is_fresh(
            generated_at,
            record.get("started_at"),
        ):
            return False
        expected = self._record_judge_config(record) or {}
        if result.get("config_signature") != expected.get("config_signature"):
            return False
        if actual_model.get("model_name") != expected.get("model_name"):
            return False
        if (actual_model.get("connection") or None) != (
            expected.get("connection") or None
        ):
            return False
        expected_generation = expected.get("generation_config") or {}
        actual_generation = actual_model.get("generation_config") or {}
        return actual_generation.get("max_output_tokens", 2048) == (
            expected_generation.get("max_output_tokens", 2048)
        )

    @staticmethod
    def _judge_result_is_fresh(
        generated_at: Any,
        started_at: Any,
    ) -> bool:
        """Reject a matching aggregate left by an earlier judge attempt."""
        if not started_at:
            # Legacy records and unit-level callers predate attempt timestamps.
            return True
        if not isinstance(generated_at, str) or not generated_at.strip():
            return False
        try:
            generated = datetime.fromisoformat(
                generated_at.strip().replace("Z", "+00:00")
            )
            started = datetime.fromisoformat(
                str(started_at).strip().replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if generated.tzinfo is None or started.tzinfo is None:
            return False
        return generated >= started

    @staticmethod
    def _record_workflow(record: dict[str, Any]) -> str:
        request = record.get("request") or {}
        return str(record.get("workflow") or request.get("workflow") or "full")

    @staticmethod
    def _record_judge_config(
        record: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        request = record.get("request") or {}
        if record.get("job_type") in _JUDGE_JOB_TYPES:
            judge = request.get("judge")
        else:
            role_models = request.get("role_models") or {}
            judge = (
                role_models.get("judge")
                if isinstance(role_models, dict)
                else None
            )
        return judge if isinstance(judge, dict) else None

    def _summarize_run(self, run_dir: Path) -> dict[str, Any]:
        config = self._read_json(run_dir / "run_config.json")
        if not isinstance(config, dict):
            raise EvaluationJobError(f"invalid evaluation run: {run_dir.name}")
        checkpoint = self._read_json(
            run_dir / "checkpoints" / "checkpoint.json"
        ) or {}
        scores = self._read_json(run_dir / "scores.json")
        butly_scores = scores.get("butly", {}) if isinstance(scores, dict) else {}
        auxiliary = (
            scores.get("auxiliary", {}) if isinstance(scores, dict) else {}
        )
        official = scores.get("official", {}) if isinstance(scores, dict) else {}
        raw_semantic = self._read_json(run_dir / "semantic_scores.json")
        retrieval_replay = self._read_json(
            run_dir / "retrieval_replay.json"
        )
        retrieval_replay_modes = (
            [
                mode
                for mode in RETRIEVAL_REPLAY_MODES
                if isinstance(retrieval_replay.get(mode), dict)
            ]
            if isinstance(retrieval_replay, dict)
            and retrieval_replay.get("status") == "completed"
            else []
        )
        retrieval_manifest = self._read_json(
            run_dir / "results" / "retrieval_questions.json"
        )
        retrieval_questions = (
            retrieval_manifest.get("questions")
            if isinstance(retrieval_manifest, dict)
            else None
        )
        retrieval_ready = (
            config.get("workflow") == "retrieval_prep"
            and checkpoint.get("status") == "completed"
            and isinstance(retrieval_questions, list)
            and bool(retrieval_questions)
            and all(
                isinstance(question, dict)
                for question in retrieval_questions
            )
            and retrieval_manifest.get("workflow") == "retrieval_prep"
            and retrieval_manifest.get("run_id")
            == str(config.get("run_id") or run_dir.name)
            and list(retrieval_manifest.get("sample_ids") or [])
            == list(config.get("selected_sample_ids") or [])
            and retrieval_manifest.get("question_count")
            == len(retrieval_questions)
        )
        semantic_questions: list[dict[str, Any]] = []
        if isinstance(scores, dict):
            from .semantic_judge_runner import (
                build_semantic_question_details,
            )

            semantic, semantic_questions = build_semantic_question_details(
                scores,
                raw_semantic,
            )
        else:
            semantic = raw_semantic
        semantic = semantic or {}
        semantic_summary = (
            semantic.get("summary", {})
            if isinstance(semantic.get("summary"), dict)
            else {}
        )
        semantic_judge = semantic.get("judge") or semantic_summary.get("model")
        status = "created"
        if isinstance(scores, dict):
            status = "completed"
        elif retrieval_ready:
            status = "retrieval_ready"
        elif checkpoint.get("status") == "completed":
            status = "evaluation_completed"
        elif checkpoint:
            status = "interrupted"
        return {
            "run_id": str(config.get("run_id") or run_dir.name),
            "run_dir": str(run_dir),
            "created_at": config.get("created_at"),
            "status": status,
            "progress": (
                100.0
                if status in {"completed", "retrieval_ready"}
                else None
            ),
            "job_id": None,
            "has_scores": isinstance(scores, dict),
            "workflow": config.get("workflow") or "full",
            "selected_sample_ids": list(
                config.get("selected_sample_ids") or []
            ),
            "has_retrieval_questions": retrieval_ready,
            "has_retrieval_replay": bool(retrieval_replay_modes),
            "retrieval_replay_modes": retrieval_replay_modes,
            "retrieval_replay_generated_at": (
                retrieval_replay.get("generated_at")
                if retrieval_replay_modes
                else None
            ),
            "retrieval_question_count": (
                len(retrieval_questions) if retrieval_ready else None
            ),
            "overall": official.get("overall"),
            "question_count": (
                scores.get("question_count") if isinstance(scores, dict) else None
            ),
            "exact_match_rate": auxiliary.get("exact_match_rate"),
            "answer_containment_rate": auxiliary.get(
                "answer_containment_rate"
            ),
            "evidence_retrieval_rate": butly_scores.get(
                "evidence_retrieval_rate"
            ),
            # evidence は「全問」で割った値なので、RAG が発火しなかった run では
            # 検索品質と無関係に下がる。分母を読み違えないよう発火率を併記する。
            "rag_trigger_rate": butly_scores.get("rag_trigger_rate"),
            # 検索は走ったが注入しなかった run（retrieval_execution=always）を
            # 読めるようにする。ランキング品質は recall で見る（evidence は
            # 注入されたカードでしか測っていない）。
            "search_execution_rate": butly_scores.get("search_execution_rate"),
            "retrieval_recall_at_3": butly_scores.get("retrieval_recall_at_3"),
            "retrieval_query_rate": butly_scores.get("retrieval_query_rate"),
            "dual_query_original_recall_at_3": butly_scores.get(
                "dual_query_original_recall_at_3"
            ),
            "dual_query_rewrite_recall_at_3": butly_scores.get(
                "dual_query_rewrite_recall_at_3"
            ),
            "dual_query_rescue_rate_at_3": butly_scores.get(
                "dual_query_rescue_rate_at_3"
            ),
            "dual_query_harm_rate_at_3": butly_scores.get(
                "dual_query_harm_rate_at_3"
            ),
            "bm25_rescue_rate": butly_scores.get("bm25_rescue_rate"),
            "reranker_completion_rate": butly_scores.get(
                "reranker_completion_rate"
            ),
            "reranker_fallback_rate": butly_scores.get(
                "reranker_fallback_rate"
            ),
            "reranker_rescue_rate_at_3": butly_scores.get(
                "reranker_rescue_rate_at_3"
            ),
            "reranker_harm_rate_at_3": butly_scores.get(
                "reranker_harm_rate_at_3"
            ),
            "reranker_latency_ms_p95": butly_scores.get(
                "reranker_latency_ms_p95"
            ),
            "evidence_fusion_completion_rate": butly_scores.get(
                "evidence_fusion_completion_rate"
            ),
            "evidence_fusion_fallback_rate": butly_scores.get(
                "evidence_fusion_fallback_rate"
            ),
            "evidence_fusion_rescue_rate_at_3": butly_scores.get(
                "evidence_fusion_rescue_rate_at_3"
            ),
            "evidence_fusion_harm_rate_at_3": butly_scores.get(
                "evidence_fusion_harm_rate_at_3"
            ),
            "evidence_fusion_latency_ms_p95": butly_scores.get(
                "evidence_fusion_latency_ms_p95"
            ),
            # 分類器が空応答/パース失敗で倒れると need_intent が立たず RAG が
            # 丸ごと不発になる（Reasoning モデル + 小さい出力上限で起きる）。
            "classifier_fallback_rate": butly_scores.get(
                "classifier_fallback_rate"
            ),
            "qa_retry_total": butly_scores.get("qa_retry_total"),
            "qa_retry_question_rate": butly_scores.get(
                "qa_retry_question_rate"
            ),
            "qa_retry_reason_distribution": butly_scores.get(
                "qa_retry_reason_distribution"
            ),
            "latency_ms_mean": butly_scores.get("latency_ms_mean"),
            "prompt_tokens_total": butly_scores.get("prompt_tokens_total"),
            "completion_tokens_total": butly_scores.get(
                "completion_tokens_total"
            ),
            "knowledge_cards_created": butly_scores.get(
                "knowledge_cards_created"
            ),
            "sleeptime_failures": butly_scores.get("sleeptime_failures"),
            "qa_mode": config.get("qa_mode"),
            "locale": config.get("locale"),
            "sample_limit": config.get("sample_limit"),
            "session_limit": config.get("session_limit"),
            "question_limit": config.get("question_limit"),
            "source_run_id": config.get("memory_reused_from_run_id"),
            "stage3_bootstrap": bool(config.get("stage3_bootstrap")),
            "run_mode": None,
            "connection": config.get("connection"),
            "model_name": config.get("model_name"),
            "semantic_status": semantic.get("status"),
            "semantic_coverage": semantic.get("coverage"),
            "semantic_judged_count": semantic.get(
                "judged_count",
                semantic_summary.get("judged_count"),
            ),
            "semantic_error_count": semantic.get("error_count"),
            "semantic_score_mean": semantic_summary.get(
                "normalized_score_mean"
            ),
            "semantic_pass_rate": semantic_summary.get("pass_rate"),
            "semantic_review_count": (
                sum(
                    1
                    for item in semantic_questions
                    if item.get("review_required")
                )
                if semantic and semantic.get("status") != "stale"
                else None
            ),
            "semantic_judge_model": (
                semantic_judge.get("model_name")
                if isinstance(semantic_judge, dict)
                else None
            ),
        }

    def _summarize_dialogue_ab_run(
        self,
        run_dir: Path,
    ) -> dict[str, Any]:
        config = self._read_json(run_dir / "run_config.json")
        if not isinstance(config, dict) or config.get("run_type") != "dialogue_ab":
            raise EvaluationJobError(
                f"invalid dialogue A/B run: {run_dir.name}"
            )
        checkpoint = self._read_json(
            run_dir / "checkpoints" / "dialogue_ab.json"
        ) or {}
        scores = self._read_json(run_dir / "scores.json")
        policies = (
            scores.get("policies", {}) if isinstance(scores, dict) else {}
        )
        comparison = (
            scores.get("comparison", {}) if isinstance(scores, dict) else {}
        )
        semantic = (
            scores.get("semantic_judge", {})
            if isinstance(scores, dict)
            and isinstance(scores.get("semantic_judge"), dict)
            else {}
        )
        intent = (
            policies.get("intent_gated", {})
            if isinstance(policies.get("intent_gated"), dict)
            else {}
        )
        candidates = (
            policies.get("candidates", {})
            if isinstance(policies.get("candidates"), dict)
            else {}
        )
        status = "created"
        if isinstance(scores, dict):
            status = "completed"
        elif checkpoint.get("status") == "completed":
            status = "evaluation_completed"
        elif checkpoint:
            status = "interrupted"
        return {
            "run_id": str(config.get("run_id") or run_dir.name),
            "run_dir": str(run_dir),
            "created_at": config.get("created_at"),
            "status": status,
            "progress": 100.0 if status == "completed" else None,
            "job_id": None,
            "has_scores": isinstance(scores, dict),
            "dataset_id": config.get("dataset_id"),
            "prompt_count": (
                scores.get("prompt_count")
                if isinstance(scores, dict)
                else config.get("prompt_count")
            ),
            "knowledge_cards_created": (
                scores.get("knowledge_cards_created")
                if isinstance(scores, dict)
                else None
            ),
            "intent_rag_trigger_rate": intent.get("rag_trigger_rate"),
            "candidates_rag_trigger_rate": candidates.get(
                "rag_trigger_rate"
            ),
            "intent_prompt_tokens_mean": intent.get("prompt_tokens_mean"),
            "candidates_prompt_tokens_mean": candidates.get(
                "prompt_tokens_mean"
            ),
            "prompt_tokens_mean_delta": comparison.get(
                "prompt_tokens_mean_delta"
            ),
            "intent_required_recall": intent.get(
                "required_target_recall"
            ),
            "candidates_required_recall": candidates.get(
                "required_target_recall"
            ),
            "required_recall_delta": comparison.get(
                "required_target_recall_delta"
            ),
            "intent_irrelevant_mention_rate": intent.get(
                "irrelevant_seed_mention_rate"
            ),
            "candidates_irrelevant_mention_rate": candidates.get(
                "irrelevant_seed_mention_rate"
            ),
            "irrelevant_mention_delta": comparison.get(
                "irrelevant_seed_mention_rate_delta"
            ),
            "intent_latency_ms_mean": intent.get("latency_ms_mean"),
            "candidates_latency_ms_mean": candidates.get(
                "latency_ms_mean"
            ),
            "semantic_status": semantic.get("status"),
            "semantic_judged_count": semantic.get("judged_prompt_count"),
            "semantic_review_count": semantic.get("review_required_count"),
            "semantic_winner_counts": semantic.get("winner_counts"),
            "semantic_score_delta": (
                semantic.get("comparison", {}).get("normalized_score_delta")
                if isinstance(semantic.get("comparison"), dict)
                else None
            ),
            "semantic_judge_model": (
                semantic.get("model", {}).get("model_name")
                if isinstance(semantic.get("model"), dict)
                else None
            ),
        }

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        public = dict(record)
        public.pop("command", None)
        public.pop("process_created_at", None)
        return public

    def _require_job(self, job_id: str) -> dict[str, Any]:
        record = self._records.get(job_id)
        if record is None:
            raise KeyError(job_id)
        return record

    def _load_records(self) -> None:
        for path in self.jobs_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.exception("Failed to load evaluation job state: %s", path)
                continue
            if isinstance(record, dict) and record.get("job_id"):
                self._records[str(record["job_id"])] = record
        self._refresh_records()

    def _save_record(self, record: dict[str, Any]) -> None:
        self._records[record["job_id"]] = record
        atomic_write_text(
            self.jobs_dir / f"{record['job_id']}.json",
            json.dumps(
                record,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )

    @staticmethod
    def _read_json(path: Path) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _process_create_time(pid: int) -> Optional[float]:
        try:
            import psutil
        except ImportError:
            return None
        try:
            return psutil.Process(pid).create_time()
        except (psutil.Error, OSError):
            return None

    @staticmethod
    def _pid_matches(pid: Any, created_at: Any) -> bool:
        if not isinstance(pid, int) or pid < 1:
            return False
        try:
            import psutil
        except ImportError:
            return False
        try:
            process = psutil.Process(pid)
            if process.status() == psutil.STATUS_ZOMBIE:
                return False
            if created_at is None:
                return process.is_running()
            return (
                process.is_running()
                and abs(process.create_time() - float(created_at)) < 1.0
            )
        except (psutil.Error, OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _terminate_process(
        process: Optional[subprocess.Popen],
        pid: Any,
        created_at: Any,
    ) -> None:
        try:
            import psutil
        except ImportError:
            if process is not None and process.poll() is None:
                process.terminate()
            return
        try:
            target_pid = process.pid if process is not None else pid
            if not isinstance(target_pid, int):
                return
            target = psutil.Process(target_pid)
            if created_at is not None and (
                abs(target.create_time() - float(created_at)) >= 1.0
            ):
                return
            children = target.children(recursive=True)
            for child in children:
                child.terminate()
            target.terminate()
            _, alive = psutil.wait_procs([*children, target], timeout=5)
            for remaining in alive:
                remaining.kill()
        except (psutil.Error, OSError, TypeError, ValueError):
            logger.exception("Failed to stop evaluation process pid=%r", pid)
