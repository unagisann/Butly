"""Command-line frontend for the environment-independent LoCoMo runner."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Optional, Sequence

from .config import ReplayConfig, SUPPORTED_EVALUATION_LOCALES, WORKFLOWS
from .progress import (
    EVALUATION_PROGRESS_MAX,
    JUDGING_PROGRESS_MAX,
    SCORING_PROGRESS_MAX,
    ProgressReporter,
    create_console_progress,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Butly's LoCoMo evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Replay sessions, run QA, then score and report"
    )
    run_parser.add_argument("--dataset", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--run-id")
    run_parser.add_argument(
        "--sample-ids",
        nargs="*",
        default=[],
        help="Select exact sample IDs; explicit IDs override --sample-limit",
    )
    sample_group = run_parser.add_mutually_exclusive_group()
    sample_group.add_argument("--sample-limit", type=_positive_int, default=1)
    sample_group.add_argument(
        "--all-samples",
        action="store_true",
        help="Evaluate every selected LoCoMo sample",
    )
    session_group = run_parser.add_mutually_exclusive_group()
    session_group.add_argument("--session-limit", type=_positive_int)
    session_group.add_argument(
        "--all-sessions",
        action="store_true",
        help="Replay every session (also the default when no limit is given)",
    )
    question_group = run_parser.add_mutually_exclusive_group()
    question_group.add_argument("--question-limit", type=_positive_int, default=1)
    question_group.add_argument(
        "--all-questions",
        action="store_true",
        help="Answer every question in each selected sample",
    )
    run_parser.add_argument(
        "--qa-mode",
        choices=("independent", "sequential"),
        default="independent",
        help="Reset to the post-Sleeptime state for each QA, or keep QA history",
    )
    run_parser.add_argument(
        "--workflow",
        choices=WORKFLOWS,
        default="full",
        help=(
            "full runs QA/scoring; retrieval_prep stops after Replay/Sleeptime "
            "and writes questions for offline retrieval comparison"
        ),
    )
    run_parser.add_argument(
        "--locale",
        choices=SUPPORTED_EVALUATION_LOCALES,
        help="Evaluation prompt locale (overrides profile locale; default: en)",
    )
    run_parser.add_argument("--model-name")
    run_parser.add_argument("--connection")
    run_parser.add_argument("--profile", type=Path)
    run_parser.add_argument("--clean", action="store_true")
    run_parser.add_argument(
        "--skip-scoring",
        action="store_true",
        help="Stop after QA; score/report can be re-run separately",
    )

    resume_parser = subparsers.add_parser(
        "resume", help="Continue an interrupted run from its checkpoint"
    )
    resume_parser.add_argument("--run-dir", type=Path, required=True)
    resume_parser.add_argument("--skip-scoring", action="store_true")

    rerun_parser = subparsers.add_parser(
        "rerun-qa",
        help="Clone an independent run's post-Sleeptime memory and run QA only",
    )
    rerun_parser.add_argument("--source-run", type=Path, required=True)
    rerun_parser.add_argument(
        "--dataset",
        type=Path,
        help="Relocated dataset path; its SHA-256 must match the source run",
    )
    rerun_parser.add_argument(
        "--output-dir",
        type=Path,
        help="Parent for the new run (default: the source run's parent)",
    )
    rerun_parser.add_argument("--run-id")
    rerun_question_group = rerun_parser.add_mutually_exclusive_group()
    rerun_question_group.add_argument("--question-limit", type=_positive_int)
    rerun_question_group.add_argument(
        "--all-questions",
        action="store_true",
        help="Answer every question; otherwise inherit the source limit",
    )
    rerun_parser.add_argument(
        "--qa-mode",
        choices=("independent", "sequential"),
        default="independent",
        help="QA isolation for the new run (source must be independent)",
    )
    rerun_parser.add_argument(
        "--locale",
        choices=SUPPORTED_EVALUATION_LOCALES,
        help="Evaluation prompt locale (overrides profile/source locale)",
    )
    rerun_parser.add_argument("--model-name")
    rerun_parser.add_argument("--connection")
    rerun_parser.add_argument("--profile", type=Path)
    rerun_parser.add_argument(
        "--stage3-bootstrap",
        action="store_true",
        help="Stage 3 A/B の ON 側: カード同一性検証後に stage3-bootstrap で"
        "レビューキューを drain してから QA する",
    )
    rerun_parser.add_argument(
        "--skip-scoring",
        action="store_true",
        help="Stop after QA; score/report can be re-run separately",
    )

    score_parser = subparsers.add_parser(
        "score", help="(Re-)score a finished run from its qa_results.jsonl"
    )
    score_parser.add_argument("--run-dir", type=Path, required=True)
    score_parser.add_argument(
        "--dataset",
        type=Path,
        help="Unused (kept for compatibility); evidence coverage now reads "
        "the run's own workspace provenance",
    )

    report_parser = subparsers.add_parser(
        "report", help="Regenerate summary.md from an existing scores.json"
    )
    report_parser.add_argument("--run-dir", type=Path, required=True)

    judge_parser = subparsers.add_parser(
        "judge",
        help="Run or resume optional semantic judging for an official-scored run",
    )
    judge_parser.add_argument("--run-dir", type=Path, required=True)
    judge_parser.add_argument("--judge-model-name")
    judge_parser.add_argument("--judge-connection")
    judge_parser.add_argument(
        "--judge-max-output-tokens",
        type=_positive_int,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _command_run(args)
    if args.command == "resume":
        return _command_resume(args)
    if args.command == "rerun-qa":
        return _command_rerun_qa(args)
    if args.command == "score":
        return _command_score(args)
    if args.command == "report":
        return _command_report(args)
    if args.command == "judge":
        return _command_judge(args)
    raise ValueError(f"Unsupported command: {args.command}")


def _command_run(args: argparse.Namespace) -> int:
    config = _replay_config_from_args(args)
    from .replay import run_evaluation

    progress_reporter = create_console_progress()
    result = asyncio.run(
        run_evaluation(config, progress_reporter=progress_reporter)
    )
    return _finish(
        result,
        dataset=args.dataset,
        skip_scoring=args.skip_scoring,
        progress_reporter=progress_reporter,
    )


def _replay_config_from_args(args: argparse.Namespace) -> ReplayConfig:
    return ReplayConfig(
        dataset_path=args.dataset,
        output_dir=args.output_dir,
        run_id=args.run_id,
        sample_ids=tuple(args.sample_ids),
        sample_limit=(
            None if args.all_samples or args.sample_ids else args.sample_limit
        ),
        session_limit=None if args.all_sessions else args.session_limit,
        question_limit=None if args.all_questions else args.question_limit,
        qa_mode=args.qa_mode,
        workflow=args.workflow,
        locale=args.locale,
        model_name=args.model_name,
        connection=args.connection,
        profile_path=args.profile,
        clean=args.clean,
    )


def _command_resume(args: argparse.Namespace) -> int:
    from .replay import resume_evaluation

    progress_reporter = create_console_progress()
    result = asyncio.run(
        resume_evaluation(
            args.run_dir,
            progress_reporter=progress_reporter,
        )
    )
    run_config = json.loads(
        result.workspace.run_config_path.read_text(encoding="utf-8")
    )
    dataset = Path(run_config["dataset_path"])
    return _finish(
        result,
        dataset=dataset,
        skip_scoring=args.skip_scoring,
        progress_reporter=progress_reporter,
    )


def _command_rerun_qa(args: argparse.Namespace) -> int:
    from .replay import rerun_qa_from_memory

    progress_reporter = create_console_progress()
    result = asyncio.run(
        rerun_qa_from_memory(
            args.source_run,
            dataset_path=args.dataset,
            output_dir=args.output_dir,
            run_id=args.run_id,
            question_limit=args.question_limit,
            all_questions=args.all_questions,
            qa_mode=args.qa_mode,
            locale=args.locale,
            model_name=args.model_name,
            connection=args.connection,
            profile_path=args.profile,
            stage3_bootstrap=args.stage3_bootstrap,
            progress_reporter=progress_reporter,
        )
    )
    run_config = json.loads(
        result.workspace.run_config_path.read_text(encoding="utf-8")
    )
    return _finish(
        result,
        dataset=Path(run_config["dataset_path"]),
        skip_scoring=args.skip_scoring,
        progress_reporter=progress_reporter,
    )


def _command_score(args: argparse.Namespace) -> int:
    from .scorer import score_run

    progress_reporter = create_console_progress()
    progress_reporter.emit(0.0, "score", f"{args.run_dir} starting")
    scores = score_run(args.run_dir, dataset_path=args.dataset)
    progress_reporter.emit(
        100.0,
        "score",
        f"completed; overall={scores['official']['overall']:.4f}",
    )
    print(
        json.dumps(
            {
                "run_id": scores["run_id"],
                "question_count": scores["question_count"],
                "overall": scores["official"]["overall"],
                "scores_path": str(Path(args.run_dir) / "scores.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _command_report(args: argparse.Namespace) -> int:
    from .report import write_report

    progress_reporter = create_console_progress()
    progress_reporter.emit(0.0, "report", f"{args.run_dir} starting")
    summary_path = write_report(args.run_dir)
    progress_reporter.emit(100.0, "report", f"completed; {summary_path}")
    print(json.dumps({"summary_path": str(summary_path)}, ensure_ascii=False))
    return 0


def _command_judge(args: argparse.Namespace) -> int:
    from .report import write_report
    from .semantic_judge_runner import (
        LocomoJudgeError,
        resolve_judge_config,
        run_locomo_semantic_judge,
    )

    config = resolve_judge_config(
        args.run_dir,
        model_name=args.judge_model_name,
        connection=args.judge_connection,
        max_output_tokens=args.judge_max_output_tokens,
    )
    if config is None:
        raise LocomoJudgeError(
            "no judge is configured; pass --judge-model-name or add judge to "
            "the run profile"
        )
    progress_reporter = create_console_progress()
    progress_reporter.emit(0.0, "judge", f"{args.run_dir} starting")
    semantic_scores = run_locomo_semantic_judge(
        args.run_dir,
        config,
        progress=lambda completed, total, message: progress_reporter.emit(
            100.0 * completed / total,
            "judge",
            message,
            completed=completed,
            total=total,
        ),
    )
    summary_path = write_report(args.run_dir)
    progress_reporter.emit(
        100.0,
        "judge",
        f"{semantic_scores['status']}; report updated: {summary_path}",
    )
    if semantic_scores["status"] != "completed":
        raise LocomoJudgeError(
            "semantic judging is partial; "
            f"{semantic_scores['error_count']} question(s) must be retried"
        )
    print(
        json.dumps(
            {
                "run_id": semantic_scores["run_id"],
                "status": semantic_scores["status"],
                "judged_count": semantic_scores["judged_count"],
                "error_count": semantic_scores["error_count"],
                "coverage": semantic_scores["coverage"],
                "semantic_scores_path": str(
                    Path(args.run_dir) / "semantic_scores.json"
                ),
                "summary_path": str(summary_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _finish(
    result,
    *,
    dataset: Path,
    skip_scoring: bool,
    progress_reporter: ProgressReporter,
) -> int:
    payload = {
        "run_id": result.workspace.run_id,
        "run_dir": str(result.workspace.run_dir),
        "sample_ids": result.sample_ids,
        "replayed_sessions": result.replayed_sessions,
        "answered_questions": result.answered_questions,
    }
    workflow = getattr(result, "workflow", "full")
    if workflow == "retrieval_prep":
        manifest_path = (
            result.workspace.results_dir / "retrieval_questions.json"
        )
        payload.update(
            {
                "workflow": workflow,
                "retrieval_question_count": getattr(
                    result,
                    "prepared_questions",
                    0,
                ),
                "retrieval_questions_path": str(manifest_path),
            }
        )
        progress_reporter.emit(
            100.0,
            "complete",
            (
                "Replay and Sleeptime completed; "
                f"{payload['retrieval_question_count']} questions are ready for "
                "offline retrieval comparison"
            ),
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    if skip_scoring:
        progress_reporter.emit(
            100.0,
            "complete",
            "Scoring and report skipped (--skip-scoring)",
        )
    else:
        from .report import write_report
        from .scorer import score_run

        progress_reporter.emit(
            EVALUATION_PROGRESS_MAX,
            "score",
            "Official-compatible scoring starting",
        )
        scores = score_run(result.workspace.run_dir, dataset_path=dataset)
        progress_reporter.emit(
            SCORING_PROGRESS_MAX,
            "score",
            f"completed; overall={scores['official']['overall']:.4f}",
        )
        semantic_scores = _run_configured_judge(
            result.workspace.run_dir,
            progress_reporter,
        )
        progress_reporter.emit(
            (
                JUDGING_PROGRESS_MAX
                if semantic_scores is not None
                else SCORING_PROGRESS_MAX
            ),
            "report",
            "summary.md generation starting",
        )
        summary_path = write_report(result.workspace.run_dir)
        progress_reporter.emit(
            100.0,
            "complete",
            f"report completed; {summary_path}",
        )
        payload["overall_score"] = scores["official"]["overall"]
        payload["scores_path"] = str(result.workspace.run_dir / "scores.json")
        payload["summary_path"] = str(summary_path)
        if semantic_scores is not None:
            payload["semantic_judge_status"] = semantic_scores["status"]
            payload["semantic_scores_path"] = str(
                result.workspace.run_dir / "semantic_scores.json"
            )
            if semantic_scores["status"] != "completed":
                from .semantic_judge_runner import LocomoJudgeError

                raise LocomoJudgeError(
                    "semantic judging is partial; "
                    f"{semantic_scores['error_count']} question(s) must be retried"
                )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _run_configured_judge(
    run_dir: Path,
    progress_reporter: ProgressReporter,
) -> Optional[dict]:
    from .semantic_judge_runner import (
        resolve_judge_config,
        run_locomo_semantic_judge,
    )

    config = resolve_judge_config(run_dir)
    if config is None:
        return None
    progress_reporter.emit(
        SCORING_PROGRESS_MAX,
        "judge",
        "Semantic judging starting",
    )
    span = JUDGING_PROGRESS_MAX - SCORING_PROGRESS_MAX
    scores = run_locomo_semantic_judge(
        run_dir,
        config,
        progress=lambda completed, total, message: progress_reporter.emit(
            SCORING_PROGRESS_MAX + span * completed / total,
            "judge",
            message,
            completed=completed,
            total=total,
        ),
    )
    progress_reporter.emit(
        JUDGING_PROGRESS_MAX,
        "judge",
        (
            f"{scores['status']}; judged={scores['judged_count']}/"
            f"{scores['question_count']}, errors={scores['error_count']}"
        ),
    )
    return scores


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
