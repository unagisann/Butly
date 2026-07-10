"""Session-level checkpointing so interrupted runs resume without re-ingesting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path

from butly_core.io_utils import atomic_write_text


CHECKPOINT_FILE_NAME = "checkpoint.json"

STATUS_REPLAYING = "replaying"
STATUS_QA = "qa"
STATUS_COMPLETED = "completed"


class CheckpointError(ValueError):
    """Raised when an existing checkpoint cannot be trusted."""


@dataclass
class SampleProgress:
    instance_name: str
    replayed_sessions: list[str] = field(default_factory=list)
    sleeptime_completed: list[str] = field(default_factory=list)
    qa_completed: int = 0

    def to_dict(self) -> dict:
        return {
            "instance_name": self.instance_name,
            "replayed_sessions": list(self.replayed_sessions),
            "sleeptime_completed": list(self.sleeptime_completed),
            "qa_completed": self.qa_completed,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "SampleProgress":
        return cls(
            instance_name=str(payload["instance_name"]),
            replayed_sessions=[str(s) for s in payload.get("replayed_sessions", [])],
            sleeptime_completed=[
                str(s) for s in payload.get("sleeptime_completed", [])
            ],
            qa_completed=int(payload.get("qa_completed", 0)),
        )


@dataclass
class Checkpoint:
    run_id: str
    path: Path
    status: str = STATUS_REPLAYING
    samples: dict[str, SampleProgress] = field(default_factory=dict)

    @classmethod
    def create(cls, run_id: str, checkpoints_dir: Path) -> "Checkpoint":
        return cls(run_id=run_id, path=Path(checkpoints_dir) / CHECKPOINT_FILE_NAME)

    @classmethod
    def load(cls, run_id: str, checkpoints_dir: Path) -> "Checkpoint":
        """Load the run's checkpoint, or start fresh when none was written."""
        path = Path(checkpoints_dir) / CHECKPOINT_FILE_NAME
        if not path.is_file():
            return cls(run_id=run_id, path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CheckpointError(f"Corrupted checkpoint: {path}") from exc
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            raise CheckpointError(
                f"Checkpoint run_id mismatch in {path}: "
                f"expected {run_id!r}, found {payload.get('run_id')!r}"
            )
        samples = {
            sample_id: SampleProgress.from_dict(progress)
            for sample_id, progress in payload.get("samples", {}).items()
        }
        return cls(
            run_id=run_id,
            path=path,
            status=str(payload.get("status", STATUS_REPLAYING)),
            samples=samples,
        )

    def progress_for(self, sample_id: str, instance_name: str) -> SampleProgress:
        progress = self.samples.get(sample_id)
        if progress is None:
            progress = SampleProgress(instance_name=instance_name)
            self.samples[sample_id] = progress
        return progress

    def save(self) -> None:
        payload = {
            "schema_version": 1,
            "run_id": self.run_id,
            "status": self.status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "samples": {
                sample_id: progress.to_dict()
                for sample_id, progress in self.samples.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )
