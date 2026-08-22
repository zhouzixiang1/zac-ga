"""Command line entry point."""

from __future__ import annotations

import argparse
import json

from .compiler import compile_circuit
from .zair import to_zair


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile QASM to a Fable ZAIR schedule")
    parser.add_argument("qasm", help="OpenQASM 2 file")
    parser.add_argument("--output", "-o", help="write ZAIR JSON here")
    parser.add_argument("--metrics", action="store_true", help="print compile metrics")
    args = parser.parse_args(argv)

    with open(args.qasm, "r", encoding="utf-8") as handle:
        source = handle.read()
    result = compile_circuit(source)
    schedule = to_zair(result.program)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(schedule, handle, indent=2)
    else:
        print(json.dumps(schedule, indent=2))
    if args.metrics:
        print(json.dumps(result.metrics.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())