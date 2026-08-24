"""Command line entry point for the streamlined native-GA quality race."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .quality_racing_runner import (
    load_plan_and_root,
    prepare_workspace,
    run_baselines,
    run_decisions,
    run_lookahead,
    run_profiles,
    run_shared_forward_check,
    run_validation,
    write_selection_artifacts,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run resident-ga-quality-racing-v1 fail-closed")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--workers", type=int, default=1,
        help="isolated quality workers; timing benchmarks remain serial")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in (
            "prepare", "baselines", "profiles", "decisions", "lookahead",
            "validation", "shared-forward-check", "freeze-selection"):
        sub.add_parser(command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan, root = load_plan_and_root(args.plan, args.root)
    resume = not args.no_resume
    if args.command == "prepare":
        result = prepare_workspace(plan, root)
    elif args.command == "baselines":
        result = run_baselines(
            plan, root, resume=resume, dry_run=args.dry_run,
            workers=args.workers)
    elif args.command == "profiles":
        result = run_profiles(
            plan, root, resume=resume, dry_run=args.dry_run,
            workers=args.workers)
    elif args.command == "decisions":
        result = run_decisions(
            plan, root, resume=resume, dry_run=args.dry_run,
            workers=args.workers)
    elif args.command == "lookahead":
        result = run_lookahead(
            plan, root, resume=resume, dry_run=args.dry_run,
            workers=args.workers)
    elif args.command == "validation":
        result = run_validation(
            plan, root, resume=resume, dry_run=args.dry_run,
            workers=args.workers)
    elif args.command == "shared-forward-check":
        result = run_shared_forward_check(
            plan, root, resume=resume, dry_run=args.dry_run,
            workers=args.workers)
    else:
        if args.dry_run:
            raise ValueError("freeze-selection does not support --dry-run")
        result = write_selection_artifacts(plan, root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
