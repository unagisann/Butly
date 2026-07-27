"""Persistent subprocess jobs for the local LoCoMo evaluation console."""

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

from .workspace import PROJECT_ROOT


logger = logging.getLogger(__name__)

RUN_MODES = (
    "standard",
    "stage3-full",
    "stage3-source",
    "stage3-off",
    "stage3-on",
)
SEARCH_MODES = ("vector", "hybrid")
RETRIEVAL_REPLAY_MODES = ("bm25", "vector", "hybrid")
RETRIEVAL_EXECUTIONS = ("always", "intent_gated")
INJECTION_POLICIES = ("intent_gated", "retrieval_assisted", "candidates")
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "stopping"})
RESUMABLE_JOB_STATUSES = frozenset({"stopped", "failed", "interrupted"})
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PROGRESS_LINE = re.compile(
    r"^\[LoCoMo\s+([0-9.]+)%\]"
    r"(?:\s+\[[^\]]+\])?\s+(\S+)\s+\|\s+(.*)$"
)
_PROFILE_ROLES = ("chat", "gatekeeper", "summary", "knowledge", "embedding")


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


def build_profile_payload(request: dict[str, Any]) -> dict[str, Any]:
    """Build the same profile sections used by the Colab parameter cell."""
    run_mode = str(request.get("run_mode") or "standard")
    profile: dict[str, Any] = {
        "name": f"web_console_{request['run_id']}",
        "locale": request.get("locale") or "en",
    }

    role_models = request.get("role_models") or {}
    unknown_roles = set(role_models) - set(_PROFILE_ROLES)
    if unknown_roles:
        raise EvaluationJobError(
            f"unsupported role_models: {sorted(unknown_roles)}"
        )
    for role in _PROFILE_ROLES:
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
        profile[role] = role_config

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
    if search_mode == "hybrid":
        # BM25 側のパラメータは hybrid のときだけ書く。vector run の profile に
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
        "--locale",
        locale,
    ]
    for dimension in ("sample", "session", "question"):
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

    dataset_path = Path(str(normalized.get("dataset_path") or "")).expanduser()
    try:
        dataset_path = dataset_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise EvaluationJobError(f"dataset not found: {dataset_path}") from exc
    if not dataset_path.is_file():
        raise EvaluationJobError(f"dataset is not a file: {dataset_path}")
    normalized["dataset_path"] = str(dataset_path)

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
        ("rrf_k", 60),
    ):
        value = normalized.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EvaluationJobError(f"{name} must be a positive integer")
        normalized[name] = value

    df_ratio = normalized.get("bm25_max_df_ratio", 0.5)
    if isinstance(df_ratio, bool) or not isinstance(df_ratio, (int, float)):
        raise EvaluationJobError("bm25_max_df_ratio must be a number")
    if not 0.0 < float(df_ratio) <= 1.0:
        raise EvaluationJobError("bm25_max_df_ratio must be in (0.0, 1.0]")
    normalized["bm25_max_df_ratio"] = float(df_ratio)

    return normalized


class EvaluationJobManager:
    """Launch, persist, stop, resume, and inspect LoCoMo CLI processes."""

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
        self.jobs_dir = self.state_root / "jobs"
        self.profiles_dir = self.state_root / "profiles"
        self.python_executable = python_executable
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._monitors: dict[str, threading.Thread] = {}
        for directory in (self.output_dir, self.jobs_dir, self.profiles_dir):
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
        return {
            "output_dir": str(self.output_dir),
            "dataset_candidates": candidates,
            "run_modes": list(RUN_MODES),
            "search_modes": list(SEARCH_MODES),
            "retrieval_executions": list(RETRIEVAL_EXECUTIONS),
            "injection_policies": list(INJECTION_POLICIES),
            # APIキー本体はrequestへ入らない。前回のフォーム設定をBackend再起動後も
            # 復元できるよう、永続job recordの正規化済みrequestを返す。
            "last_request": last_request,
        }

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
                "job_id": job_id,
                "run_id": normalized["run_id"],
                "run_mode": normalized["run_mode"],
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
            jobs_by_run = {
                record["run_id"]: record for record in self._records.values()
            }
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

    def retrieval_replay(
        self,
        run_id: str,
        modes: list[str],
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        """既存 run の記憶に対して検索だけを回し、Recall@k を比較する。

        QA を回す前の足切り（検索改修計画 §8）。``bm25`` は embedding を
        呼ばないので即返る。``vector`` / ``hybrid`` は質問1件につき embedding を
        1回呼ぶため、run の規模に比例して時間がかかる。
        結果は run 直下の ``retrieval_replay.json`` に残す。
        """
        from evals.locomo.retrieval_replay import evaluate

        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise EvaluationJobError(f"invalid run_id: {run_id}")
        unknown = [m for m in modes if m not in RETRIEVAL_REPLAY_MODES]
        if not modes or unknown:
            raise EvaluationJobError(
                f"modes must be a subset of {list(RETRIEVAL_REPLAY_MODES)}"
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise EvaluationJobError("limit must be a positive integer")

        run_dir = self.output_dir / run_id
        if not (run_dir / "run_config.json").is_file():
            raise KeyError(run_id)

        override_config = self._run_profile_sections(run_dir)
        try:
            result = evaluate(
                run_dir,
                list(modes),
                limit=limit,
                override_config=override_config,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise EvaluationJobError(str(exc)) from exc

        result["generated_at"] = utc_now()
        result["limit"] = limit
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

            return dict(load_profile(path).sections)
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
        question_maps: dict[str, dict[str, dict[str, Any]]] = {}
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
                if not question_id:
                    continue
                question_maps.setdefault(question_id, {})[run_id] = {
                    "score": question.get("official_score"),
                    "prediction": question.get("prediction"),
                    "expected_answer": question.get("expected_answer"),
                    "question": question.get("question"),
                    "sample_id": question.get("sample_id"),
                }

        questions = []
        baseline = run_ids[0]
        comparison = run_ids[-1]
        for question_id, by_run in question_maps.items():
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
                    "sample_id": seed.get("sample_id"),
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
        record["message"] = "Evaluation process started"
        record["started_at"] = utc_now()
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
            elif return_code == 0:
                record["status"] = "completed"
                record["progress"] = 100.0
                record["phase"] = "complete"
                record["message"] = "Evaluation completed"
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
        if (run_dir / "scores.json").is_file():
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
            "bm25_rescue_rate": butly_scores.get("bm25_rescue_rate"),
            # 分類器が空応答/パース失敗で倒れると need_intent が立たず RAG が
            # 丸ごと不発になる（Reasoning モデル + 小さい出力上限で起きる）。
            "classifier_fallback_rate": butly_scores.get(
                "classifier_fallback_rate"
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
