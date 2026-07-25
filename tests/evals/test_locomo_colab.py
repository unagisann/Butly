"""Static contract checks for the thin LoCoMo Colab launcher."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


NOTEBOOK = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "locomo"
    / "colab"
    / "butly_locomo_eval.ipynb"
)


def _code_cell_objects() -> list[dict]:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return [
        cell
        for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    ]


def _code_cells() -> list[str]:
    return ["".join(cell.get("source", [])) for cell in _code_cell_objects()]


def test_colab_exposes_qa_scope_mode_and_locale_to_cli():
    cells = _code_cells()
    parameters = next(cell for cell in cells if "QA_MODE =" in cell)
    clone = next(cell for cell in cells if "git', 'clone'" in cell)
    profile = next(cell for cell in cells if "profile = {" in cell)
    run = next(cell for cell in cells if "scope_args = []" in cell)

    assert "QA_MODE = 'independent'" in parameters
    assert "LOCALE = 'en'" in parameters
    run_id_line = next(
        line for line in parameters.splitlines() if line.startswith("RUN_ID =")
    )
    assert "v12" not in run_id_line
    assert '# @param {type:"string"}' in run_id_line
    assert (
        "RUN_MODE = 'standard'  # @param "
        '["standard", "stage3-full", "stage3-source", "stage3-off", '
        '"stage3-on"]'
        in parameters
    )
    assert "REPO_URL = 'https://github.com/unagisann/Butly.git'  # @param" in parameters
    assert "BRANCH = 'main'  # @param" in parameters
    assert "DRIVE_ROOT = '/content/drive/MyDrive/butly-evals'  # @param" in parameters
    assert "DATASET_RELATIVE_PATH = 'data/locomo10.json'  # @param" in parameters
    assert "ALL_SAMPLES = False" in parameters
    assert "ALL_SESSIONS = False" in parameters
    assert "ALL_QUESTIONS = False" in parameters
    assert "TIME_DECAY_RATE = 0.0" in parameters
    assert "CONTEXT_CURRENT_TIME = True" in parameters
    assert "CONTEXT_MID_TERM = True" in parameters
    assert "CONTEXT_SESSION_DIGEST = True" in parameters
    assert "CONTEXT_RAG = True" in parameters
    assert "SOURCE_MEMORY_RUN_ID = ''" in parameters
    assert "STAGE3_BATCH_SIZE = 10" in parameters
    assert "STAGE3_BOOTSTRAP_MAX_CARDS = 2000" in parameters
    assert "CHAT_TEMPERATURE = 0.7" in parameters
    assert "GATEKEEPER_TEMPERATURE = 0.0" in parameters
    assert "SUMMARY_TEMPERATURE = 0.3" in parameters
    assert "KNOWLEDGE_TEMPERATURE = 0.2" in parameters

    assert "'locale': LOCALE" in profile
    assert "'brain': dict(" in parameters
    assert "time_decay_rate=TIME_DECAY_RATE" in parameters
    assert "use_rag=CONTEXT_RAG" in parameters
    assert "'context_levels': dict(" in parameters
    assert "current_time='high' if CONTEXT_CURRENT_TIME else 'off'" in parameters
    assert "mid_term='high' if CONTEXT_MID_TERM else 'off'" in parameters
    assert (
        "session_digest='high' if CONTEXT_SESSION_DIGEST else 'off'"
        in parameters
    )
    assert "rag='high' if CONTEXT_RAG else 'off'" in parameters
    assert "generation_config=dict(temperature=CHAT_TEMPERATURE)" in parameters
    assert (
        "stage3_enabled = RUN_MODE in {'stage3-full', 'stage3-on'}"
        in parameters
    )
    assert "knowledge_maturation_enabled=stage3_enabled" in parameters
    assert "knowledge_maturation_batch_size=STAGE3_BATCH_SIZE" in parameters
    assert "'update_targets': {'knowledge_maturation': stage3_enabled}" in parameters
    assert "'fetch', 'origin'" in clone
    assert "'checkout', BRANCH" in clone
    assert "'pull', '--ff-only', 'origin', BRANCH" in clone
    assert "'--qa-mode', QA_MODE" in run
    assert "'--locale', LOCALE" in run
    assert "f'--all-{dimension}'" in run
    assert "f'--{dimension[:-1]}-limit'" in run
    assert "'rerun-qa'" in run
    assert "'--source-run'" in run
    assert "SOURCE_MEMORY_RUN_ID.strip()" in run
    assert "formal Stage 3 A/B requires QA_MODE=independent" in run
    assert (
        "run_mode in {'stage3-full', 'stage3-source'} and "
        "source_memory_run_id"
        in run
    )
    assert "requires blank SOURCE_MEMORY_RUN_ID" in run
    assert "requires SOURCE_MEMORY_RUN_ID" in run
    assert "command.append('--stage3-bootstrap')" in run


def test_colab_parameters_render_as_a_form_and_resume_rejects_partial_stage3():
    cells = _code_cell_objects()
    parameters = next(
        cell for cell in cells if "RUN_MODE =" in "".join(cell.get("source", []))
    )
    resume = next(
        "".join(cell.get("source", []))
        for cell in cells
        if "evals.locomo.cli resume" in "".join(cell.get("source", []))
    )

    assert parameters.get("metadata", {}).get("cellView") == "form"
    assert "display-mode: \"form\"" in "".join(parameters["source"])
    assert "refuses partial nodes" in resume


def test_colab_stage3_full_enables_per_session_maturation():
    parameters = next(cell for cell in _code_cells() if "PROFILE_EXTRAS =" in cell)
    parameters = parameters.replace(
        "RUN_MODE = 'standard'",
        "RUN_MODE = 'stage3-full'",
        1,
    )
    namespace = {}

    exec(compile(parameters, "colab-parameters-cell", "exec"), namespace)

    extras = namespace["PROFILE_EXTRAS"]
    assert extras["memory"]["knowledge_maturation_enabled"] is True
    assert extras["memory"]["knowledge_maturation_batch_size"] == 10
    assert extras["sleeptime"]["update_targets"]["knowledge_maturation"] is True


@pytest.mark.parametrize(
    ("run_mode", "source_run_id", "subcommand", "uses_bootstrap"),
    [
        ("stage3-full", "", "run", False),
        ("stage3-source", "", "run", False),
        ("stage3-off", "source-run", "rerun-qa", False),
        ("stage3-on", "source-run", "rerun-qa", True),
    ],
)
def test_colab_stage3_mode_routes_to_the_expected_cli(
    run_mode,
    source_run_id,
    subcommand,
    uses_bootstrap,
):
    run_cell = next(cell for cell in _code_cells() if "scope_args = []" in cell)
    namespace = {
        "ALL_SAMPLES": False,
        "SAMPLE_LIMIT": 1,
        "ALL_SESSIONS": False,
        "SESSION_LIMIT": 1,
        "ALL_QUESTIONS": False,
        "QUESTION_LIMIT": 1,
        "RUN_ID": f"{run_mode}-run",
        "RUN_MODE": run_mode,
        "SOURCE_MEMORY_RUN_ID": source_run_id,
        "QA_MODE": "independent",
        "LOCALE": "en",
        "DRIVE_ROOT": "/drive/evals",
        "DATASET_PATH": "/drive/evals/data/locomo.json",
    }

    with patch("subprocess.run") as run:
        exec(compile(run_cell, "colab-run-cell", "exec"), namespace)

    command = run.call_args.args[0]
    assert command[3] == subcommand
    assert ("--stage3-bootstrap" in command) is uses_bootstrap


def test_colab_stage3_on_requires_a_source_run_id():
    run_cell = next(cell for cell in _code_cells() if "scope_args = []" in cell)
    namespace = {
        "ALL_SAMPLES": False,
        "SAMPLE_LIMIT": 1,
        "ALL_SESSIONS": False,
        "SESSION_LIMIT": 1,
        "ALL_QUESTIONS": False,
        "QUESTION_LIMIT": 1,
        "RUN_ID": "stage3-on-run",
        "RUN_MODE": "stage3-on",
        "SOURCE_MEMORY_RUN_ID": "",
        "QA_MODE": "independent",
        "LOCALE": "en",
        "DRIVE_ROOT": "/drive/evals",
        "DATASET_PATH": "/drive/evals/data/locomo.json",
    }

    with pytest.raises(ValueError, match="requires SOURCE_MEMORY_RUN_ID"):
        exec(compile(run_cell, "colab-run-cell", "exec"), namespace)


def test_colab_stage3_full_is_single_run_and_rejects_source_memory():
    run_cell = next(cell for cell in _code_cells() if "scope_args = []" in cell)
    namespace = {
        "ALL_SAMPLES": False,
        "SAMPLE_LIMIT": 1,
        "ALL_SESSIONS": False,
        "SESSION_LIMIT": 1,
        "ALL_QUESTIONS": False,
        "QUESTION_LIMIT": 1,
        "RUN_ID": "stage3-full-run",
        "RUN_MODE": "stage3-full",
        "SOURCE_MEMORY_RUN_ID": "source-run",
        "QA_MODE": "sequential",
        "LOCALE": "en",
        "DRIVE_ROOT": "/drive/evals",
        "DATASET_PATH": "/drive/evals/data/locomo.json",
    }

    with pytest.raises(ValueError, match="requires blank SOURCE_MEMORY_RUN_ID"):
        exec(compile(run_cell, "colab-run-cell", "exec"), namespace)
