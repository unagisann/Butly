"""Checkpoint persistence and resume-safety tests."""

import json

import pytest

from evals.locomo.checkpoint import Checkpoint, CheckpointError, STATUS_QA


class TestCheckpointRoundtrip:
    def test_load_without_file_starts_fresh(self, tmp_path):
        checkpoint = Checkpoint.load("run-1", tmp_path)

        assert checkpoint.samples == {}
        assert checkpoint.status == "replaying"

    def test_save_and_load_preserves_progress(self, tmp_path):
        checkpoint = Checkpoint.create("run-1", tmp_path)
        progress = checkpoint.progress_for("conv-1", "locomo_conv_1")
        progress.replayed_sessions.append("session_1")
        progress.sleeptime_completed.append("session_1")
        progress.qa_completed = 2
        checkpoint.status = STATUS_QA
        checkpoint.save()

        loaded = Checkpoint.load("run-1", tmp_path)

        assert loaded.status == STATUS_QA
        restored = loaded.progress_for("conv-1", "locomo_conv_1")
        assert restored.replayed_sessions == ["session_1"]
        assert restored.sleeptime_completed == ["session_1"]
        assert restored.qa_completed == 2

    def test_save_is_atomic_json(self, tmp_path):
        checkpoint = Checkpoint.create("run-1", tmp_path)
        checkpoint.save()

        payload = json.loads(
            (tmp_path / "checkpoint.json").read_text(encoding="utf-8")
        )
        assert payload["run_id"] == "run-1"
        assert payload["schema_version"] == 1
        assert "updated_at" in payload


class TestCheckpointSafety:
    def test_run_id_mismatch_is_rejected(self, tmp_path):
        Checkpoint.create("run-1", tmp_path).save()

        with pytest.raises(CheckpointError):
            Checkpoint.load("other-run", tmp_path)

    def test_corrupted_checkpoint_is_rejected(self, tmp_path):
        (tmp_path / "checkpoint.json").write_text("{broken", encoding="utf-8")

        with pytest.raises(CheckpointError):
            Checkpoint.load("run-1", tmp_path)
