"""Live progress output contract for LoCoMo CLI runs."""

from io import StringIO

from evals.locomo.progress import create_console_progress


def test_evaluation_progress_maps_work_to_first_ninety_percent():
    stream = StringIO()
    reporter = create_console_progress(stream)

    reporter.emit_evaluation(1, 4, "qa", "conv-1 qa-1 starting")

    assert stream.getvalue() == (
        "[LoCoMo  22.5%] [1/4] qa         | conv-1 qa-1 starting\n"
    )


def test_progress_is_clamped_and_flushed_to_the_given_stream():
    stream = StringIO()
    reporter = create_console_progress(stream)

    reporter.emit(120.0, "complete", "done")

    assert stream.getvalue() == (
        "[LoCoMo 100.0%] complete   | done\n"
    )
