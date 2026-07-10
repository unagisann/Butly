"""Command-line frontend for the environment-independent LoCoMo runner."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Optional, Sequence

from .config import ReplayConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Butly's LoCoMo evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Replay sessions, run QA, then score and report"
    )
    run_parser.add_argument("--dataset", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--sample-ids", nargs="*", default=[])
    run_parser.add_argument("--sample-limit", type=int, default=1)
    run_parser.add_argument("--session-limit", type=int)
    run_parser.add_argument("--question-limit", type=int, default=1)
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

    score_parser = subparsers.add_parser(
        "score", help="(Re-)score a finished run from its qa_results.jsonl"
    )
    score_parser.add_argument("--run-dir", type=Path, required=True)
    score_parser.add_argument(
        "--dataset",
        type=Path,
        help="Optional dataset path to enable evidence-coverage metrics",
    )

    report_parser = subparsers.add_parser(
        "report", help="Regenerate summary.md from an existing scores.json"
    )
    report_parser.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _command_run(args)
    if args.command == "resume":
        return _command_resume(args)
    if args.command == "score":
        return _command_score(args)
    if args.command == "report":
        return _command_report(args)
    raise ValueError(f"Unsupported command: {args.command}")


def _command_run(args: argparse.Namespace) -> int:
    config = ReplayConfig(
        dataset_path=args.dataset,
        output_dir=args.output_dir,
        run_id=args.run_id,
        sample_ids=tuple(args.sample_ids),
        sample_limit=args.sample_limit,
        session_limit=args.session_limit,
        question_limit=args.question_limit,
        model_name=args.model_name,
        connection=args.connection,
        profile_path=args.profile,
        clean=args.clean,
    )
    from .replay import run_evaluation

    result = asyncio.run(run_evaluation(config))
    return _finish(result, dataset=args.dataset, skip_scoring=args.skip_scoring)


def _command_resume(args: argparse.Namespace) -> int:
    from .replay import resume_evaluation

    result = asyncio.run(resume_evaluation(args.run_dir))
    run_config = json.loads(
        result.workspace.run_config_path.read_text(encoding="utf-8")
    )
    dataset = Path(run_config["dataset_path"])
    return _finish(result, dataset=dataset, skip_scoring=args.skip_scoring)


def _command_score(args: argparse.Namespace) -> int:
    from .scorer import score_run

    scores = score_run(args.run_dir, dataset_path=args.dataset)
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

    summary_path = write_report(args.run_dir)
    print(json.dumps({"summary_path": str(summary_path)}, ensure_ascii=False))
    return 0


def _finish(result, *, dataset: Path, skip_scoring: bool) -> int:
    payload = {
        "run_id": result.workspace.run_id,
        "run_dir": str(result.workspace.run_dir),
        "sample_ids": result.sample_ids,
        "replayed_sessions": result.replayed_sessions,
        "answered_questions": result.answered_questions,
    }
    if not skip_scoring:
        from .report import write_report
        from .scorer import score_run

        scores = score_run(result.workspace.run_dir, dataset_path=dataset)
        summary_path = write_report(result.workspace.run_dir)
        payload["overall_score"] = scores["official"]["overall"]
        payload["scores_path"] = str(result.workspace.run_dir / "scores.json")
        payload["summary_path"] = str(summary_path)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
