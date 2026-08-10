"""Live console progress reporting for LoCoMo CLI commands."""

from __future__ import annotations

import logging
import sys
from typing import Optional, TextIO


EVALUATION_PROGRESS_MAX = 90.0
SCORING_PROGRESS_MAX = 96.0
JUDGING_PROGRESS_MAX = 99.0


class ProgressReporter:
    """Emit flushed, human-readable progress without polluting JSON stdout."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(
        self,
        percent: float,
        phase: str,
        message: str,
        *,
        completed: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        bounded_percent = min(max(percent, 0.0), 100.0)
        step_text = ""
        if completed is not None and total is not None:
            step_text = f" [{completed}/{total}]"
        self._logger.info(
            "[LoCoMo %5.1f%%]%s %-10s | %s",
            bounded_percent,
            step_text,
            phase,
            message,
        )

    def emit_evaluation(
        self,
        completed: int,
        total: int,
        phase: str,
        message: str,
    ) -> None:
        ratio = 1.0 if total == 0 else completed / total
        self.emit(
            ratio * EVALUATION_PROGRESS_MAX,
            phase,
            message,
            completed=completed,
            total=total,
        )


def create_console_progress(
    stream: Optional[TextIO] = None,
) -> ProgressReporter:
    """Create an isolated stderr logger.

    The stream handler flushes after every progress row.
    """
    logger = logging.Logger("evals.locomo.progress", level=logging.INFO)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return ProgressReporter(logger)
