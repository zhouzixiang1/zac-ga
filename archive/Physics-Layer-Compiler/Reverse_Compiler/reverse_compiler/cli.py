"""Command-line interface for the ZAC reverse compiler.

Usage::

    python -m reverse_compiler.cli schedule_code.json --to qiskit
    python -m reverse_compiler.cli schedule_code.json --to stim --strict
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .exporters import StimExportError, to_qiskit, to_stim
from .reverse_compiler import ReverseCompileError, reverse_compile
from .visualization import plot_atom_operations, plot_circuit_structure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reverse-compile a ZAC schedule.")
    parser.add_argument("code", help="ZAC ZAIR *_code.json schedule file")
    parser.add_argument(
        "--to",
        choices=["ir", "qiskit", "stim"],
        default="ir",
        help="output representation (default: ir)",
    )
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        help="drop single-qubit gates whose angle the schedule does not record",
    )
    parser.add_argument(
        "--viz-atom-ops",
        metavar="PATH",
        help="save an atom-level move+gate timeline image to PATH",
    )
    parser.add_argument(
        "--viz-circuit",
        metavar="PATH",
        help="save a reconstructed circuit-structure image to PATH",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    with open(args.code) as f:
        zair = json.load(f)

    try:
        ir = reverse_compile(zair, strict=args.strict)
    except ReverseCompileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.to == "ir":
        for op in ir.operations:
            print(op)
    elif args.to == "qiskit":
        print(to_qiskit(ir))
    else:
        try:
            print(to_stim(ir), end="")
        except StimExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.viz_atom_ops:
        try:
            plot_atom_operations(zair, args.viz_atom_ops)
            print(f"saved atom operation plot: {args.viz_atom_ops}")
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.viz_circuit:
        try:
            plot_circuit_structure(ir, args.viz_circuit)
            print(f"saved circuit structure plot: {args.viz_circuit}")
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
