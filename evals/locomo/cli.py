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
    run_parser = subparsers.add_parser("run", help="Replay sessions and run QA")
    run_parser.add_argument("--dataset", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--sample-ids", nargs="*", default=[])
    run_parser.add_argument("--sample-limit", type=int, default=1)
    run_parser.add_argument("--session-limit", type=int)
    run_parser.add_argument("--question-limit", type=int, default=1)
    run_parser.add_argument("--model-name")
    run_parser.add_argument("--connection")
    run_parser.add_argument("--clean", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        raise ValueError(f"Unsupported command: {args.command}")
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
        clean=args.clean,
    )
    from .replay import run_evaluation

    result = asyncio.run(run_evaluation(config))
    print(
        json.dumps(
            {
                "run_id": result.workspace.run_id,
                "run_dir": str(result.workspace.run_dir),
                "sample_ids": result.sample_ids,
                "replayed_sessions": result.replayed_sessions,
                "answered_questions": result.answered_questions,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
