"""Run-scoped, production-safe workspace creation for LoCoMo evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Optional

from butly_core.io_utils import atomic_write_bytes, atomic_write_text

from .artifacts import safe_artifact_name


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_INSTANCES_DIR = PROJECT_ROOT / "butly_core" / "instances"
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class WorkspaceError(ValueError):
    """Raised when a requested workspace could touch production data."""


@dataclass(frozen=True)
class EvaluationWorkspace:
    run_id: str
    run_dir: Path
    data_dir: Path
    instances_dir: Path
    results_dir: Path
    traces_dir: Path
    snapshots_dir: Path
    checkpoints_dir: Path
    run_config_path: Path

    @classmethod
    def create(
        cls,
        output_dir: Path,
        *,
        run_id: Optional[str] = None,
        clean: bool = False,
    ) -> "EvaluationWorkspace":
        """Create a new isolated run directory.

        Existing runs are retained unless ``clean=True`` is explicit. Evaluation
        data is rejected when its resolved path would live inside the production
        instances tree.
        """
        resolved_run_id = run_id if run_id is not None else _new_run_id()
        if not _SAFE_RUN_ID.fullmatch(resolved_run_id):
            raise WorkspaceError(
                "run_id must start with an alphanumeric character and contain "
                "only letters, numbers, '.', '_' or '-'"
            )

        root = Path(output_dir)
        run_dir = root / resolved_run_id
        data_dir = run_dir / "workspace"
        instances_dir = data_dir / "butly_core" / "instances"
        _assert_isolated(instances_dir)

        if run_dir.exists():
            if not clean:
                raise FileExistsError(
                    f"Evaluation run already exists: {run_dir}. "
                    "Pass clean=True to replace it."
                )
            _assert_safe_cleanup(run_dir, resolved_run_id)
            shutil.rmtree(run_dir)

        results_dir = run_dir / "results"
        traces_dir = run_dir / "traces"
        snapshots_dir = run_dir / "snapshots"
        checkpoints_dir = run_dir / "checkpoints"
        for directory in (
            instances_dir,
            results_dir,
            traces_dir,
            snapshots_dir,
            checkpoints_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        workspace = cls(
            run_id=resolved_run_id,
            run_dir=run_dir,
            data_dir=data_dir,
            instances_dir=instances_dir,
            results_dir=results_dir,
            traces_dir=traces_dir,
            snapshots_dir=snapshots_dir,
            checkpoints_dir=checkpoints_dir,
            run_config_path=run_dir / "run_config.json",
        )
        workspace.write_run_config(
            {
                "schema_version": 1,
                "run_id": resolved_run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return workspace

    @classmethod
    def open(
        cls,
        run_dir: Path,
        *,
        create_missing: bool = True,
    ) -> "EvaluationWorkspace":
        """Reattach to an existing run, optionally without filesystem writes."""
        run_path = Path(run_dir).resolve()
        config_path = run_path / "run_config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                f"Not an evaluation run directory (run_config.json missing or "
                f"unreadable): {run_path}"
            ) from exc
        run_id = config.get("run_id") if isinstance(config, dict) else None
        if not run_id:
            raise WorkspaceError(f"run_config.json has no run_id: {config_path}")

        data_dir = run_path / "workspace"
        instances_dir = data_dir / "butly_core" / "instances"
        _assert_isolated(instances_dir)
        workspace = cls(
            run_id=str(run_id),
            run_dir=run_path,
            data_dir=data_dir,
            instances_dir=instances_dir,
            results_dir=run_path / "results",
            traces_dir=run_path / "traces",
            snapshots_dir=run_path / "snapshots",
            checkpoints_dir=run_path / "checkpoints",
            run_config_path=config_path,
        )
        if create_missing:
            for directory in (
                workspace.instances_dir,
                workspace.results_dir,
                workspace.traces_dir,
                workspace.snapshots_dir,
                workspace.checkpoints_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
        return workspace

    def create_runtime(self):
        """Build a ButlyRuntime wired only to this run's data directory."""
        from butly_core.runtime import ButlyRuntime

        self.assert_isolated()
        return ButlyRuntime(
            data_dir=self.data_dir,
            base_dir=self.data_dir,
            instances_dir=self.instances_dir,
        )

    def create_sleeptime(self):
        """Build a ButlySleeptime wired only to this run's data directory."""
        from sleeptime import ButlySleeptime

        self.assert_isolated()
        return ButlySleeptime(
            base_dir=self.data_dir,
            instances_dir=self.instances_dir,
        )

    def write_run_config(self, payload: dict[str, Any]) -> None:
        """Atomically replace run_config.json without persisting credentials."""
        safe_payload = dict(payload)
        safe_payload["run_id"] = self.run_id
        atomic_write_text(
            self.run_config_path,
            json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True),
        )

    def assert_isolated(self) -> None:
        _assert_isolated(self.instances_dir)


class IndependentQAWorkspace:
    """Disposable local clone used to reset one instance before every QA."""

    def __init__(self, canonical_instance_dir: Path):
        canonical = Path(canonical_instance_dir).resolve()
        if not canonical.is_dir():
            raise WorkspaceError(f"Canonical QA instance not found: {canonical}")

        self.instance_name = canonical.name
        self._temporary_dir = tempfile.TemporaryDirectory(
            prefix=f"butly-locomo-qa-{self.instance_name}-"
        )
        self.root_dir = Path(self._temporary_dir.name)
        self.baseline_instance_dir = (
            self.root_dir / "baseline" / self.instance_name
        )
        self.data_dir = self.root_dir / "active"
        self.instances_dir = self.data_dir / "butly_core" / "instances"
        self.instance_dir = self.instances_dir / self.instance_name
        try:
            self.baseline_instance_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(canonical, self.baseline_instance_dir)
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "IndependentQAWorkspace":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def reset(self) -> Path:
        """Replace the active instance with an exact clean baseline clone."""
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        if self.instance_dir.exists():
            shutil.rmtree(self.instance_dir)
        shutil.copytree(self.baseline_instance_dir, self.instance_dir)
        for volatile_name in ("debug_logs", "traces"):
            volatile_dir = self.instance_dir / volatile_name
            if volatile_dir.exists():
                shutil.rmtree(volatile_dir)
        return self.instance_dir

    def create_runtime(self):
        """Build a fresh runtime so no per-instance component cache leaks."""
        from butly_core.runtime import ButlyRuntime

        if not self.instance_dir.is_dir():
            raise WorkspaceError("Independent QA workspace must be reset first")
        _assert_isolated(self.instances_dir)
        return ButlyRuntime(
            data_dir=self.data_dir,
            base_dir=self.data_dir,
            instances_dir=self.instances_dir,
        )

    def close(self) -> None:
        self._temporary_dir.cleanup()


class SequentialQARecoveryPoint:
    """Durable rollback point for one in-flight sequential QA turn."""

    def __init__(
        self,
        workspace: EvaluationWorkspace,
        *,
        sample_id: str,
        instance_name: str,
    ):
        self.workspace = workspace
        self.sample_id = sample_id
        self.instance_name = instance_name
        self.instance_dir = workspace.instances_dir / instance_name
        sample_digest = hashlib.sha256(sample_id.encode("utf-8")).hexdigest()[:12]
        self.root_dir = (
            workspace.checkpoints_dir
            / "sequential_qa"
            / f"{instance_name}-{sample_digest}"
        )
        self.baseline_instance_dir = self.root_dir / "instance"
        self.metadata_path = self.root_dir / "recovery.json"
        self._staging_dir = self.root_dir.with_name(self.root_dir.name + ".tmp")
        self._discarded_dir = self.root_dir.with_name(
            self.root_dir.name + ".discarded"
        )
        self._restore_staging_dir = self.instance_dir.with_name(
            f".{instance_name}.qa-restore.tmp"
        )
        self._restore_retired_dir = self.instance_dir.with_name(
            f".{instance_name}.qa-restore.retired"
        )

    def begin(self, *, question_index: int, question_id: str) -> None:
        """Persist the canonical pre-question state before chat mutates it."""
        if question_index < 0:
            raise WorkspaceError("question_index must be non-negative")
        if self.root_dir.exists():
            raise WorkspaceError(
                "Sequential QA recovery point already exists; reconcile it "
                "before starting another question"
            )
        if not self.instance_dir.is_dir():
            raise WorkspaceError(
                f"Sequential QA instance not found: {self.instance_dir}"
            )

        self._cleanup_directory(self._staging_dir)
        self._cleanup_directory(self._discarded_dir)
        self._staging_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(
                self.instance_dir,
                self._staging_dir / "instance",
            )
            trace_path = self._trace_path(question_id)
            trace_existed = trace_path.is_file()
            if trace_existed:
                shutil.copy2(trace_path, self._staging_dir / "trace.json")
            qa_results_path = self.workspace.results_dir / "qa_results.jsonl"
            metadata = {
                "schema_version": 1,
                "sample_id": self.sample_id,
                "instance_name": self.instance_name,
                "question_index": question_index,
                "question_id": question_id,
                "qa_results_size": (
                    qa_results_path.stat().st_size
                    if qa_results_path.is_file()
                    else 0
                ),
                "trace_existed": trace_existed,
            }
            atomic_write_text(
                self._staging_dir / "recovery.json",
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            )
            os.replace(self._staging_dir, self.root_dir)
        except Exception:
            self._cleanup_directory(self._staging_dir)
            raise

    def reconcile(self, *, checkpoint_qa_completed: int) -> bool:
        """Rollback an uncommitted QA or discard a committed stale marker.

        The checkpoint is the commit record. A recovery point whose question
        index equals ``qa_completed`` belongs to an interrupted question and is
        restored. If the checkpoint has advanced past it, only the stale
        recovery point is removed.
        """
        self._cleanup_directory(self._staging_dir)
        self._cleanup_directory(self._discarded_dir)
        if not self.root_dir.exists():
            return False

        metadata = self._load_metadata()
        question_index = metadata["question_index"]
        if checkpoint_qa_completed < question_index:
            raise WorkspaceError(
                "Sequential QA checkpoint precedes its recovery point: "
                f"qa_completed={checkpoint_qa_completed}, "
                f"question_index={question_index}"
            )

        rolled_back = checkpoint_qa_completed == question_index
        if rolled_back:
            self._restore_instance()
            self._restore_artifacts(metadata)
        self.clear()
        return rolled_back

    def clear(self) -> None:
        """Atomically retire the recovery point after checkpoint commit."""
        if not self.root_dir.exists():
            return
        self._cleanup_directory(self._discarded_dir)
        os.replace(self.root_dir, self._discarded_dir)
        self._cleanup_directory(self._discarded_dir)

    def _load_metadata(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                f"Unreadable sequential QA recovery point: {self.metadata_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise WorkspaceError(
                f"Invalid sequential QA recovery point: {self.metadata_path}"
            )
        if (
            payload.get("sample_id") != self.sample_id
            or payload.get("instance_name") != self.instance_name
        ):
            raise WorkspaceError(
                f"Sequential QA recovery point identity mismatch: "
                f"{self.metadata_path}"
            )
        try:
            question_index = int(payload["question_index"])
            qa_results_size = int(payload["qa_results_size"])
            question_id = str(payload["question_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(
                f"Invalid sequential QA recovery metadata: {self.metadata_path}"
            ) from exc
        if question_index < 0 or qa_results_size < 0 or not question_id:
            raise WorkspaceError(
                f"Invalid sequential QA recovery metadata: {self.metadata_path}"
            )
        return {
            **payload,
            "question_index": question_index,
            "qa_results_size": qa_results_size,
            "question_id": question_id,
            "trace_existed": bool(payload.get("trace_existed", False)),
        }

    def _restore_instance(self) -> None:
        if not self.baseline_instance_dir.is_dir():
            raise WorkspaceError(
                "Sequential QA recovery point has no instance baseline: "
                f"{self.baseline_instance_dir}"
            )

        self._cleanup_directory(self._restore_staging_dir)
        shutil.copytree(
            self.baseline_instance_dir,
            self._restore_staging_dir,
        )
        self._cleanup_directory(self._restore_retired_dir)
        if self.instance_dir.exists():
            os.replace(self.instance_dir, self._restore_retired_dir)
        os.replace(self._restore_staging_dir, self.instance_dir)
        self._cleanup_directory(self._restore_retired_dir)

    def _restore_artifacts(self, metadata: dict[str, Any]) -> None:
        qa_results_path = self.workspace.results_dir / "qa_results.jsonl"
        expected_size = metadata["qa_results_size"]
        if qa_results_path.exists():
            actual_size = qa_results_path.stat().st_size
            if actual_size < expected_size:
                raise WorkspaceError(
                    "qa_results.jsonl is shorter than the sequential QA "
                    f"recovery offset: {actual_size} < {expected_size}"
                )
            with qa_results_path.open("rb") as handle:
                committed_prefix = handle.read(expected_size)
            atomic_write_bytes(qa_results_path, committed_prefix)
        elif expected_size:
            raise WorkspaceError(
                "qa_results.jsonl is missing but the sequential QA recovery "
                f"offset is {expected_size}"
            )

        trace_path = self._trace_path(metadata["question_id"])
        if metadata["trace_existed"]:
            trace_backup = self.root_dir / "trace.json"
            if not trace_backup.is_file():
                raise WorkspaceError(
                    f"Sequential QA trace backup is missing: {trace_backup}"
                )
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(trace_backup, trace_path)
        elif trace_path.exists():
            trace_path.unlink()

    def _trace_path(self, question_id: str) -> Path:
        return (
            self.workspace.traces_dir
            / safe_artifact_name(self.sample_id)
            / f"{safe_artifact_name(question_id)}.json"
        )

    @staticmethod
    def _cleanup_directory(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("locomo_%Y%m%d_%H%M%S_%f")


def _assert_isolated(instances_dir: Path) -> None:
    candidate = instances_dir.resolve()
    production = PRODUCTION_INSTANCES_DIR.resolve()
    if candidate == production or production in candidate.parents:
        raise WorkspaceError(
            f"Evaluation workspace must not use the production instances tree: "
            f"{candidate}"
        )


def _assert_safe_cleanup(run_dir: Path, run_id: str) -> None:
    candidate = run_dir.resolve()
    project = PROJECT_ROOT.resolve()
    if candidate == project or candidate in project.parents:
        raise WorkspaceError(f"Refusing to clean project path: {candidate}")

    config_path = candidate / "run_config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            f"Refusing to clean unrecognized evaluation run: {candidate}"
        ) from exc
    if not isinstance(config, dict) or config.get("run_id") != run_id:
        raise WorkspaceError(
            f"Refusing to clean evaluation run with mismatched run_id: {candidate}"
        )
