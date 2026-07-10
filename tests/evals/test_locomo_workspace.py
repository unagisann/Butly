"""Isolation guarantees for LoCoMo evaluation workspaces."""


import pytest

from evals.locomo.workspace import (
    EvaluationWorkspace,
    PRODUCTION_INSTANCES_DIR,
    WorkspaceError,
)


def test_workspace_creates_run_scoped_directories(tmp_path):
    workspace = EvaluationWorkspace.create(tmp_path, run_id="mini-run")

    assert workspace.run_dir == tmp_path / "mini-run"
    assert workspace.data_dir == workspace.run_dir / "workspace"
    assert workspace.instances_dir == (
        workspace.data_dir / "butly_core" / "instances"
    )
    assert workspace.results_dir.is_dir()
    assert workspace.traces_dir.is_dir()
    assert workspace.snapshots_dir.is_dir()
    assert workspace.checkpoints_dir.is_dir()
    assert workspace.run_config_path.is_file()
    assert workspace.instances_dir.resolve() != PRODUCTION_INSTANCES_DIR.resolve()


def test_runtime_and_sleeptime_share_injected_paths(tmp_path):
    workspace = EvaluationWorkspace.create(tmp_path, run_id="shared-paths")

    runtime = workspace.create_runtime()
    sleeptime = workspace.create_sleeptime()

    assert runtime.data_dir == workspace.data_dir
    assert runtime.base_dir == workspace.data_dir
    assert runtime.instances_dir == workspace.instances_dir
    assert sleeptime.base_dir == workspace.data_dir
    assert sleeptime.instances_dir == workspace.instances_dir


def test_existing_run_requires_explicit_clean(tmp_path):
    workspace = EvaluationWorkspace.create(tmp_path, run_id="existing")
    marker = workspace.run_dir / "keep-me.txt"
    marker.write_text("result", encoding="utf-8")

    with pytest.raises(FileExistsError, match="existing"):
        EvaluationWorkspace.create(tmp_path, run_id="existing")

    recreated = EvaluationWorkspace.create(tmp_path, run_id="existing", clean=True)
    assert recreated.run_dir.is_dir()
    assert not marker.exists()


def test_clean_rejects_unrecognized_existing_directory(tmp_path):
    run_dir = tmp_path / "not-an-evaluation-run"
    run_dir.mkdir()
    marker = run_dir / "important.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="unrecognized evaluation run"):
        EvaluationWorkspace.create(
            tmp_path,
            run_id="not-an-evaluation-run",
            clean=True,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_workspace_rejects_production_instances_tree():
    with pytest.raises(WorkspaceError, match="production instances"):
        EvaluationWorkspace.create(
            PRODUCTION_INSTANCES_DIR,
            run_id="must-not-write-here",
        )


@pytest.mark.parametrize("run_id", ["../escape", "nested/run", "", "white space"])
def test_workspace_rejects_unsafe_run_id(tmp_path, run_id):
    with pytest.raises(WorkspaceError, match="run_id"):
        EvaluationWorkspace.create(tmp_path, run_id=run_id)
