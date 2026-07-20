"""Static contract checks for the thin LoCoMo Colab launcher."""

import json
from pathlib import Path


NOTEBOOK = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "locomo"
    / "colab"
    / "butly_locomo_eval.ipynb"
)


def _code_cells() -> list[str]:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return [
        "".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    ]


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
    assert "ALL_SAMPLES = False" in parameters
    assert "ALL_SESSIONS = False" in parameters
    assert "ALL_QUESTIONS = False" in parameters
    assert "TIME_DECAY_RATE = 0.0" in parameters

    assert "'locale': LOCALE" in profile
    assert "'brain': dict(" in parameters
    assert "time_decay_rate=TIME_DECAY_RATE" in parameters
    assert "'fetch', 'origin'" in clone
    assert "'checkout', BRANCH" in clone
    assert "'pull', '--ff-only', 'origin', BRANCH" in clone
    assert "'--qa-mode', QA_MODE" in run
    assert "'--locale', LOCALE" in run
    assert "f'--all-{dimension}'" in run
    assert "f'--{dimension[:-1]}-limit'" in run
