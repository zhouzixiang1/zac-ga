"""Command-line entry points for the balanced Chinese-paper experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .paper_protocol import (
    PAPER_TIMING_SCHEDULE_SEED,
    PAPER_WORKERS,
    command_paper_freeze,
    command_run_paper_ablation,
    command_run_paper_main,
    command_run_paper_sensitivity,
    command_run_paper_timing,
)
from .plan import ExperimentPlan, load_experiment_plan


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = PACKAGE_ROOT / "experiments_v2" / "experiment_plan_v2.json"


def _default_root(plan: ExperimentPlan) -> Path:
    return (plan.output_root / "paper-zh-v1").resolve()


def _default_source_manifest() -> Path:
    return (PACKAGE_ROOT / "results" / "native_ga_v1" /
            "source_manifest.json").resolve()


def _default_abi8_wheel(plan: ExperimentPlan) -> Path:
    return (plan.output_root / "build" / "wheels" /
            "zac_native-0.5.32-cp310-cp310-macosx_26_0_arm64.whl").resolve()


def _default_paper_directory(plan: ExperimentPlan) -> Path:
    candidates = [
        parent / "IEEE_conference_template"
        for parent in plan.repo_root.parents
    ]
    existing = [path.resolve() for path in candidates if path.is_dir()]
    if len(existing) != 1:
        raise FileNotFoundError(
            "cannot resolve one IEEE_conference_template above repo_root: "
            f"{existing}")
    return existing[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m experiments_v2.paper_cli",
        description="ZAC18/QMAP154 balanced Chinese-paper experiment",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("paper-freeze")
    freeze.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    freeze.add_argument("--output-root", type=Path)
    freeze.add_argument("--source-manifest", type=Path)
    freeze.add_argument("--abi8-wheel", type=Path)
    freeze.add_argument(
        "--allow-dirty", action="store_true",
        help="development-only; formal compiler attempts still fail closed")

    main = subparsers.add_parser("run-paper-main")
    main.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    main.add_argument("--output-root", type=Path)
    main.add_argument("--freeze", type=Path)
    main.add_argument("--workers", type=int, default=PAPER_WORKERS)
    main.add_argument("--resume", action="store_true")
    main.add_argument("--dry-run", action="store_true")
    main.add_argument("--parity-only", action="store_true")

    ablation = subparsers.add_parser("run-paper-ablation")
    ablation.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    ablation.add_argument("--output-root", type=Path)
    ablation.add_argument("--freeze", type=Path)
    ablation.add_argument("--abi9-wheel", type=Path, required=True)
    ablation.add_argument("--native-python", type=Path, required=True)
    ablation.add_argument(
        "--components", nargs="+", default=["lookahead", "greedy"],
        help="lookahead, greedy, or both")
    ablation.add_argument("--workers", type=int, default=PAPER_WORKERS)
    ablation.add_argument("--resume", action="store_true")
    ablation.add_argument("--dry-run", action="store_true")

    sensitivity = subparsers.add_parser("run-paper-sensitivity")
    sensitivity.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    sensitivity.add_argument("--output-root", type=Path)
    sensitivity.add_argument("--freeze", type=Path)
    sensitivity.add_argument("--native-wheel", type=Path, required=True)
    sensitivity.add_argument("--native-abi-version", type=int, required=True)
    sensitivity.add_argument("--native-python", type=Path, required=True)
    sensitivity.add_argument("--workers", type=int, default=PAPER_WORKERS)
    sensitivity.add_argument("--resume", action="store_true")
    sensitivity.add_argument("--dry-run", action="store_true")

    timing = subparsers.add_parser("run-paper-timing")
    timing.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    timing.add_argument("--output-root", type=Path)
    timing.add_argument("--freeze", type=Path)
    timing.add_argument("--resume", action="store_true")
    timing.add_argument("--dry-run", action="store_true")
    timing.add_argument(
        "--schedule-seed", type=int, default=PAPER_TIMING_SCHEDULE_SEED)

    aggregate = subparsers.add_parser("aggregate-paper")
    aggregate.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    aggregate.add_argument("--output-root", type=Path)
    aggregate.add_argument("--freeze", type=Path)
    aggregate.add_argument("--delivery-root", type=Path)
    aggregate.add_argument("--paper-directory", type=Path)
    aggregate.add_argument(
        "--no-paper-publish", action="store_true",
        help=("write only the versioned aggregate directory; do not copy "
              "macros or derived figure data into the manuscript repository"))
    return parser


def _paths(args: argparse.Namespace, plan: ExperimentPlan) -> tuple[Path, Path]:
    root = (args.output_root or _default_root(plan)).resolve()
    freeze = (args.freeze or root / "freeze" / "freeze_manifest.json").resolve()
    return root, freeze


def _aggregate(args: argparse.Namespace, plan: ExperimentPlan,
               root: Path, freeze: Path) -> Mapping[str, Any]:
    # Imported lazily so experiment execution remains available while the
    # renderer/statistics module is developed and tested independently.
    try:
        from .paper_aggregate import command_aggregate_paper
    except ImportError as error:  # pragma: no cover - integration guard
        raise RuntimeError("paper aggregation module is not installed") from error
    delivery = (args.delivery_root or
                PACKAGE_ROOT / "results" / "paper_zh_v2").resolve()
    paper = (None if args.no_paper_publish else
             args.paper_directory.resolve() if args.paper_directory else
             _default_paper_directory(plan))
    return command_aggregate_paper(
        plan=plan, freeze_path=freeze, artifact_root=root,
        output_root=delivery, paper_directory=paper,
        publish_to_paper=not args.no_paper_publish)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    plan = load_experiment_plan(args.plan)
    if args.command == "paper-freeze":
        root = (args.output_root or _default_root(plan)).resolve()
        result = command_paper_freeze(
            plan, output_root=root,
            source_manifest=(args.source_manifest or _default_source_manifest()),
            abi8_wheel=(args.abi8_wheel or _default_abi8_wheel(plan)),
            require_clean_git=not args.allow_dirty,
        )
    else:
        root, freeze = _paths(args, plan)
        if args.command == "run-paper-main":
            result = command_run_paper_main(
                plan, freeze, output_root=root, workers=args.workers,
                resume=args.resume, dry_run=args.dry_run,
                parity_only=args.parity_only)
        elif args.command == "run-paper-ablation":
            result = command_run_paper_ablation(
                plan, freeze, output_root=root, abi9_wheel=args.abi9_wheel,
                native_python=args.native_python,
                components=args.components, workers=args.workers,
                resume=args.resume, dry_run=args.dry_run)
        elif args.command == "run-paper-sensitivity":
            result = command_run_paper_sensitivity(
                plan, freeze, output_root=root,
                native_wheel=args.native_wheel,
                native_abi_version=args.native_abi_version,
                native_python=args.native_python,
                workers=args.workers, resume=args.resume,
                dry_run=args.dry_run)
        elif args.command == "run-paper-timing":
            result = command_run_paper_timing(
                plan, freeze, output_root=root, resume=args.resume,
                dry_run=args.dry_run, schedule_seed=args.schedule_seed)
        elif args.command == "aggregate-paper":
            result = _aggregate(args, plan, root, freeze)
        else:  # pragma: no cover - argparse owns exhaustiveness
            raise AssertionError(args.command)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["DEFAULT_PLAN", "main", "_parser"]
