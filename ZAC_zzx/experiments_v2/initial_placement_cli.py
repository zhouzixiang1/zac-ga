"""CLI for the deterministic shared initial-placement gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .initial_placement_runner import (
    apply_initial_selection_to_plan,
    finalize_initial_placement_gate,
    initial_placement_status,
    prepare_initial_placement_workspace,
    run_initial_placement_gate,
)
from .plan import load_experiment_plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the shared SA-vs-GA initial-placement gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--plan", required=True)
    prepare.add_argument("--dataset", default="qmap154")
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--split-seed", type=int, default=0)
    prepare.add_argument("--schedule-seed", type=int, default=0)

    run = subparsers.add_parser("run")
    run.add_argument("--plan", required=True)
    run.add_argument("--dataset", default="qmap154")
    run.add_argument("--root", required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--limit", type=int)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--root", required=True)

    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--root", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root)
    if args.command == "prepare":
        report = prepare_initial_placement_workspace(
            load_experiment_plan(args.plan), args.dataset, root,
            split_seed=args.split_seed, schedule_seed=args.schedule_seed)
    elif args.command == "run":
        report = run_initial_placement_gate(
            load_experiment_plan(args.plan), args.dataset, root,
            resume=args.resume, dry_run=args.dry_run, limit=args.limit)
    elif args.command == "finalize":
        report = finalize_initial_placement_gate(root)
    elif args.command == "apply":
        report = apply_initial_selection_to_plan(
            load_experiment_plan(args.plan), root / "selected_engine.json")
    elif args.command == "status":
        report = initial_placement_status(root)
    else:  # pragma: no cover
        raise AssertionError(args.command)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
