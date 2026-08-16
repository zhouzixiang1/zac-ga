"""Command-line entry point for the forward simulator.

Examples::

    python -m simulator.cli --demo               # print demo schedule as JSON
    python -m simulator.cli schedule.json --check # validate a schedule
    python -m simulator.cli schedule.json --mp4 out.mp4
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import SimulationEngine
from .frames import ExportSettings
from .sample import sample_program
from .schema import load_instructions
from .video_export import export_animation


def _load(args) -> list[dict]:
    if args.demo:
        return sample_program(invalid=args.invalid)["instructions"]
    if not args.schedule:
        raise SystemExit("Provide a schedule file or use --demo.")
    return load_instructions(args.schedule)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Forward neutral-atom simulator")
    p.add_argument("schedule", nargs="?", help="ZAIR schedule JSON file")
    p.add_argument("--demo", action="store_true", help="use the built-in demo")
    p.add_argument("--invalid", action="store_true", help="demo with an invalid step")
    p.add_argument("--check", action="store_true", help="validate and report")
    p.add_argument("--print", dest="dump", action="store_true", help="print schedule JSON")
    p.add_argument("--mp4", metavar="PATH", help="export an MP4 to PATH")
    p.add_argument("--gif", metavar="PATH", help="export a GIF to PATH")
    args = p.parse_args(argv)

    instructions = _load(args)

    if args.dump:
        print(json.dumps({"instructions": instructions}, indent=2))
        return 0

    engine = SimulationEngine(instructions)
    print(f"atoms={len(engine.init_positions)}  steps={engine.num_steps}")
    for s in engine.steps:
        mark = "OK " if s.validation.ok else "ERR"
        print(f"  [{mark}] step {s.index + 1:>2}  {s.summary}")
        for e in s.validation.errors:
            print(f"          ⚠ {e}")
    if engine.global_errors:
        for e in engine.global_errors:
            print(f"  ⚠ {e}")

    if args.check:
        return 0 if engine.is_valid() else 1

    settings = ExportSettings()
    if args.mp4:
        export_animation(engine, args.mp4, "mp4", settings)
        print(f"wrote {args.mp4}")
    if args.gif:
        export_animation(engine, args.gif, "gif", settings)
        print(f"wrote {args.gif}")
    return 0 if engine.is_valid() else 1


if __name__ == "__main__":
    sys.exit(main())
