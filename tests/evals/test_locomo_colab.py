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
    assert "CONTEXT_CURRENT_TIME = True" in parameters
    assert "CONTEXT_MID_TERM = True" in parameters
    assert "CONTEXT_SESSION_DIGEST = True" in parameters
    assert "CONTEXT_RAG = True" in parameters
    assert "SOURCE_MEMORY_RUN_ID = ''" in parameters
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
