"""Isolation guarantees for LoCoMo evaluation workspaces."""


import pytest

from evals.locomo.artifacts import copy_latest_trace, safe_artifact_name
from evals.locomo.workspace import (
    EvaluationWorkspace,
    IndependentQAWorkspace,
    PRODUCTION_INSTANCES_DIR,
    SequentialQARecoveryPoint,
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


def test_independent_qa_workspace_resets_full_same_name_clone(tmp_path):
    canonical = tmp_path / "workspace" / "butly_core" / "instances" / "locomo_one"
    (canonical / "memory_archive" / "2_knowledgeized").mkdir(parents=True)
    (canonical / "config.json").write_text(
        '{"agent_profile": {"locale": "en"}}',
        encoding="utf-8",
    )
    (canonical / "butly_memory.db").write_bytes(b"synthetic-db")
    (canonical / "memory_archive" / "2_knowledgeized" / "turn.json").write_text(
        '{"messages": []}',
        encoding="utf-8",
    )
    (canonical / "traces").mkdir()
    (canonical / "traces" / "latest.json").write_text("stale", encoding="utf-8")

    with IndependentQAWorkspace(canonical) as qa_workspace:
        scratch_root = qa_workspace.root_dir
        first = qa_workspace.reset()

        assert first.name == canonical.name
        assert first != canonical
        assert (first / "butly_memory.db").read_bytes() == b"synthetic-db"
        assert (
            first / "memory_archive" / "2_knowledgeized" / "turn.json"
        ).is_file()
        assert not (first / "traces").exists()

        (first / "config.json").write_text("mutated", encoding="utf-8")
        qa_workspace.reset()

        assert (first / "config.json").read_text(encoding="utf-8") == (
            '{"agent_profile": {"locale": "en"}}'
        )
        assert canonical.joinpath("config.json").read_text(encoding="utf-8") == (
            '{"agent_profile": {"locale": "en"}}'
        )
        runtime = qa_workspace.create_runtime()
        assert runtime.instances_dir == qa_workspace.instances_dir
        assert runtime.get_instance_components("locomo_one")["memory"].instance_dir == (
            first
        )
    assert not scratch_root.exists()


def test_trace_copy_is_namespaced_by_sample(tmp_path):
    instance = tmp_path / "instance"
    traces = instance / "traces"
    traces.mkdir(parents=True)
    (traces / "latest.json").write_text('{"nodes": []}', encoding="utf-8")

    first = copy_latest_trace(
        instance,
        tmp_path / "run-traces",
        "qa-1",
        sample_id="conv-a",
    )
    second = copy_latest_trace(
        instance,
        tmp_path / "run-traces",
        "qa-1",
        sample_id="conv-b",
    )

    assert first == tmp_path / "run-traces" / "conv-a" / "qa-1.json"
    assert second == tmp_path / "run-traces" / "conv-b" / "qa-1.json"
    assert first.is_file()
    assert second.is_file()


def test_artifact_names_disambiguate_lossy_sample_ids():
    underscore = safe_artifact_name("conv_1")
    punctuation = safe_artifact_name("conv?1")

    assert underscore == "conv_1"
    assert punctuation.startswith("conv_1__")
    assert punctuation != underscore
    assert safe_artifact_name("conv?1") == punctuation


def test_sequential_qa_recovery_restores_instance_and_artifacts(tmp_path):
    workspace = EvaluationWorkspace.create(tmp_path, run_id="sequential-recovery")
    instance_name = "locomo_conv_1"
    instance_dir = workspace.instances_dir / instance_name
    instance_dir.mkdir()
    state_path = instance_dir / "state.txt"
    state_path.write_text("before", encoding="utf-8")
    qa_results = workspace.results_dir / "qa_results.jsonl"
    qa_results.write_text('{"question_id":"committed"}\n', encoding="utf-8")
    trace_path = workspace.traces_dir / "conv-1" / "q-2.json"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text('{"state":"before"}', encoding="utf-8")

    recovery = SequentialQARecoveryPoint(
        workspace,
        sample_id="conv-1",
        instance_name=instance_name,
    )
    recovery.begin(question_index=1, question_id="q-2")

    state_path.write_text("partial", encoding="utf-8")
    (instance_dir / "partial.txt").write_text("partial", encoding="utf-8")
    with qa_results.open("a", encoding="utf-8") as handle:
        handle.write('{"question_id":"q-2"}\n')
    trace_path.write_text('{"state":"partial"}', encoding="utf-8")

    assert recovery.reconcile(checkpoint_qa_completed=1) is True
    assert state_path.read_text(encoding="utf-8") == "before"
    assert not (instance_dir / "partial.txt").exists()
    assert qa_results.read_text(encoding="utf-8") == (
        '{"question_id":"committed"}\n'
    )
    assert trace_path.read_text(encoding="utf-8") == '{"state":"before"}'
    assert not recovery.root_dir.exists()


@pytest.mark.parametrize("run_id", ["../escape", "nested/run", "", "white space"])
def test_workspace_rejects_unsafe_run_id(tmp_path, run_id):
    with pytest.raises(WorkspaceError, match="run_id"):
        EvaluationWorkspace.create(tmp_path, run_id=run_id)
