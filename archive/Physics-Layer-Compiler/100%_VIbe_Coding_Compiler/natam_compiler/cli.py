"""Command-line interface for the neutral-atom compiler.

Examples::

    python -m natam_compiler.cli benchmark
    python -m natam_compiler.cli compile circuit.qasm --verify --text
    python -m natam_compiler.cli compile circuit.qasm --reverse-qasm out.qasm
"""

from __future__ import annotations

import argparse
import json
import sys

from . import reverse as rev
from .benchmarks import format_table, qasm_suite, run_suite
from .compiler import compile_circuit
from .hardware import default_hardware
from .verify import verify


def _read_circuit(path: str):
    from qiskit import QuantumCircuit

    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".qasm"):
        try:
            return QuantumCircuit.from_qasm_str(text)
        except Exception:
            from qiskit.qasm3 import loads

            return loads(text)
    raise ValueError("only .qasm input files are supported by the CLI")


def _cmd_compile(args: argparse.Namespace) -> int:
    qc = _read_circuit(args.circuit)
    result = compile_circuit(qc)

    if args.text:
        print(result.program.to_text())
    print(json.dumps(result.metrics.as_dict(), indent=2))

    if args.verify:
        v = verify(qc, result.program)
        print(f"verification: {'PASS' if v else 'FAIL'} ({v.method})")
        if not v:
            return 1

    if args.reverse_qasm:
        reconstructed = rev.to_qiskit(result.program)
        from qiskit.qasm2 import dumps

        with open(args.reverse_qasm, "w", encoding="utf-8") as fh:
            fh.write(dumps(reconstructed))
        print(f"reverse-compiled QASM written to {args.reverse_qasm}")
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    suite = qasm_suite(args.qasm_dir) if args.qasm_dir else None
    records = run_suite(suite, do_verify=not args.no_verify)
    print(format_table(records))
    if not args.no_verify and not all(
        r.verified or r.method == "skipped" for r in records
    ):
        return 1
    return 0


def _cmd_hardware(_: argparse.Namespace) -> int:
    print(json.dumps(default_hardware().as_dict(), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="natam_compiler")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compile = sub.add_parser("compile", help="compile a QASM circuit")
    p_compile.add_argument("circuit")
    p_compile.add_argument("--text", action="store_true", help="print op sequence")
    p_compile.add_argument("--verify", action="store_true")
    p_compile.add_argument("--reverse-qasm", metavar="PATH")
    p_compile.set_defaults(func=_cmd_compile)

    p_bench = sub.add_parser("benchmark", help="run the benchmark suite")
    p_bench.add_argument("--no-verify", action="store_true")
    p_bench.add_argument(
        "--qasm-dir",
        metavar="DIR",
        help="benchmark every .qasm file in DIR instead of the built-in suite",
    )
    p_bench.set_defaults(func=_cmd_benchmark)

    p_hw = sub.add_parser("hardware", help="print the hardware spec")
    p_hw.set_defaults(func=_cmd_hardware)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
