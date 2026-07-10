"""Run-scoped, production-safe workspace creation for LoCoMo evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
from typing import Any, Optional

from butly_core.io_utils import atomic_write_text


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
    def open(cls, run_dir: Path) -> "EvaluationWorkspace":
        """Reattach to an existing run directory (used by resume/score)."""
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
