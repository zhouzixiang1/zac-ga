"""Command-line entry point for native-GA deterministic tuning.

Examples::

    python -m experiments_v2.tuning_cli prepare --plan PLAN --root ARTIFACTS
    python -m experiments_v2.tuning_cli run-phase --plan PLAN --root ARTIFACTS \
        --phase screen --resume
    python -m experiments_v2.tuning_cli promote --root ARTIFACTS --phase screen
    python -m experiments_v2.tuning_cli finalize --plan PLAN --root ARTIFACTS

``prepare`` is fail-closed until the sibling
``ROOT.parent/initial-placement/selected_engine.json`` exists and passes the
pre-tuning core/config/repository audit.  Every later mutating phase revalidates
that selection and the frozen workspace before launching or promoting trials.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .plan import load_experiment_plan
from .tuning_runner import (
    finalize_tuning,
    prepare_tuning_workspace,
    promote_tuning_phase,
    recover_tuning_claim,
    run_tuning_phase,
    tuning_status,
)


def _print(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic native M3/M4 tuning executor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--plan", required=True)
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--dataset", default="qmap154")
    prepare.add_argument("--split-seed", type=int, default=0)
    prepare.add_argument("--schedule-seed", type=int, default=0)

    run = subparsers.add_parser("run-phase")
    run.add_argument("--plan", required=True)
    run.add_argument("--root", required=True)
    run.add_argument("--dataset", default="qmap154")
    run.add_argument(
        "--phase", required=True,
        choices=("screen", "successive_halving", "validation"))
    run.add_argument("--resume", action="store_true")
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--limit", type=int)
    run.add_argument("--worker-count", type=int, default=1)
    run.add_argument("--worker-index", type=int, default=0)

    recover = subparsers.add_parser("recover-claim")
    recover.add_argument("--root", required=True)
    recover.add_argument(
        "--phase", required=True,
        choices=("screen", "successive_halving", "validation"))
    recover.add_argument("--trial-id", required=True)

    promote = subparsers.add_parser("promote")
    promote.add_argument("--root", required=True)
    promote.add_argument(
        "--phase", required=True,
        choices=("screen", "successive_halving"))
    promote.add_argument("--schedule-seed", type=int, default=0)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--plan", required=True)
    finalize.add_argument("--root", required=True)
    finalize.add_argument(
        "--config-output",
        default=str(Path(__file__).resolve().parents[1] /
                    "exp_setting" / "native_ga_v1"))

    status = subparsers.add_parser("status")
    status.add_argument("--root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if args.command == "prepare":
        plan = load_experiment_plan(args.plan)
        report = prepare_tuning_workspace(
            plan, args.dataset, root, split_seed=args.split_seed,
            schedule_seed=args.schedule_seed)
    elif args.command == "run-phase":
        if args.retry_failed and not args.resume:
            raise ValueError("--retry-failed requires --resume")
        if args.limit is not None and args.limit < 0:
            raise ValueError("--limit must be non-negative")
        plan = load_experiment_plan(args.plan)
        report = run_tuning_phase(
            plan, args.dataset, root, args.phase, resume=args.resume,
            retry_failed=args.retry_failed, dry_run=args.dry_run,
            limit=args.limit, worker_count=args.worker_count,
            worker_index=args.worker_index)
    elif args.command == "recover-claim":
        report = recover_tuning_claim(root, args.phase, args.trial_id)
    elif args.command == "promote":
        report = promote_tuning_phase(
            root, args.phase, schedule_seed=args.schedule_seed)
    elif args.command == "finalize":
        plan = load_experiment_plan(args.plan)
        report = finalize_tuning(
            plan, root, Path(args.config_output).expanduser().resolve())
    elif args.command == "status":
        report = tuning_status(root)
    else:  # pragma: no cover - argparse owns this branch
        raise AssertionError(args.command)
    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
