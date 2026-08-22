"""Print Fable compiler variant internal fidelity and runtime estimates for QASM benchmarks."""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve()
FABLE_DIR = HERE.parents[1]
ROOT = HERE.parents[2]
ZAC_DIR = ROOT / "ZAC-main"

sys.path.insert(0, str(FABLE_DIR))

from qiskit import QuantumCircuit  # noqa: E402

from fable_compiler import compile_circuit as compile_baseline, default_hardware  # noqa: E402
from fable_compiler.compiler_search import compile_circuit as compile_search  # noqa: E402
from fable_compiler.compiler_zwq import compile_circuit as compile_zwq  # noqa: E402

OPTIMIZATION_LEVEL = 1


def compile_row(qasm_path: Path, qc: QuantumCircuit, hw, compiler_name: str, compile_fn: Callable) -> dict:
    start = time.perf_counter()
    result = compile_fn(qc, hw, optimization_level=OPTIMIZATION_LEVEL)
    runtime = time.perf_counter() - start
    metrics = result.metrics
    return {
        "compiler": compiler_name,
        "circuit": qasm_path.stem.replace("_transpiled", ""),
        "qubits": qc.num_qubits,
        "runtime": runtime,
        "stages": metrics.stages,
        "total_time": metrics.total_time,
        "estimated_fidelity": metrics.estimated_fidelity,
        "num_moves": metrics.num_moves,
        "num_move_batches": metrics.num_move_batches,
        "num_2q": metrics.num_2q,
        "rydberg_pulses": metrics.rydberg_pulses,
        "cz_parallelism": metrics.cz_parallelism,
        "movement_distance": metrics.movement_distance,
    }


def selected_compilers(mode: str) -> list[tuple[str, Callable]]:
    search_random = lambda qc, hw, optimization_level=OPTIMIZATION_LEVEL: compile_search(
        qc, hw, optimization_level=optimization_level, random_initial=True, timeout=50.0
    )
    compilers = {
        "baseline": [("compiler", compile_baseline)],
        "zwq": [("compiler_zwq", compile_zwq)],
        "search": [("compiler_search", search_random)],
        "both": [("compiler", compile_baseline), ("compiler_zwq", compile_zwq), ("compiler_search", search_random)],
        "all": [("compiler", compile_baseline), ("compiler_zwq", compile_zwq), ("compiler_search", search_random)],
    }
    return compilers[mode]


def table_header() -> str:
    return (
        f"{'compiler':<14}{'circuit':<16}{'q':>4}{'runtime':>10}{'stg':>6}{'time':>11}{'fidelity':>13}"
        f"{'moves':>8}{'mvB':>6}{'2q':>7}{'pulse':>7}{'par':>7}"
    )


def format_row(row: dict) -> str:
    return (
        f"{row['compiler']:<14}{row['circuit']:<16}{row['qubits']:>4}{row['runtime']:>10.3f}"
        f"{row['stages']:>6}{row['total_time']:>11.1f}{row['estimated_fidelity']:>13.6g}"
        f"{row['num_moves']:>8}{row['num_move_batches']:>6}"
        f"{row['num_2q']:>7}{row['rydberg_pulses']:>7}"
        f"{row['cz_parallelism']:>7.2f}"
    )


def print_averages(rows: list[dict], fieldnames: list[str], header: str) -> None:
    print("-" * len(header), flush=True)
    for compiler in sorted({row["compiler"] for row in rows}):
        compiler_rows = [row for row in rows if row["compiler"] == compiler]
        averages = {
            key: sum(row[key] for row in compiler_rows) / len(compiler_rows)
            for key in fieldnames
            if key not in {"compiler", "circuit"}
        }
        print(
            f"{compiler:<14}{'AVERAGE':<16}{averages['qubits']:>4.1f}{averages['runtime']:>10.3f}"
            f"{averages['stages']:>6.1f}{averages['total_time']:>11.1f}{averages['estimated_fidelity']:>13.6g}"
            f"{averages['num_moves']:>8.1f}{averages['num_move_batches']:>6.1f}"
            f"{averages['num_2q']:>7.1f}{averages['rydberg_pulses']:>7.1f}"
            f"{averages['cz_parallelism']:>7.2f}",
            flush=True,
        )
        total_move_stages = sum(row["num_move_batches"] for row in compiler_rows)
        print(f"{compiler:<14}{'TOTAL_MOVE_STAGES':<16}{total_move_stages:>4}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bench-dir",
        type=Path,
        # default=ZAC_DIR / "benchmark" / "hpca",
        default=ROOT / "benchmark" / "examples",
        help="directory containing QASM benchmark circuits",
    )
    parser.add_argument(
        "--compiler",
        choices=("baseline", "zwq", "search", "both", "all"),
        default="both",
        help="which Fable compiler variant to evaluate",
    )
    parser.add_argument("--limit", type=int, default=0, help="only run the first N circuits")
    parser.add_argument(
        "--max-ops",
        type=int,
        default=10000,
        help="skip circuits with more than this many Qiskit operations; use 0 to disable",
    )
    parser.add_argument("--csv", type=Path, default=None, help="optional CSV output path")
    args = parser.parse_args(argv)

    hw = default_hardware()
    qasms = sorted(args.bench_dir.glob("*.qasm"))
    if args.limit:
        qasms = qasms[: args.limit]
    if not qasms:
        raise SystemExit(f"no QASM files found in {args.bench_dir}")

    fieldnames = [
        "compiler",
        "circuit",
        "qubits",
        "runtime",
        "stages",
        "total_time",
        "estimated_fidelity",
        "num_moves",
        "num_move_batches",
        "num_2q",
        "rydberg_pulses",
        "cz_parallelism",
        "movement_distance",
    ]

    rows = []
    compilers = selected_compilers(args.compiler)
    header = table_header()
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for qasm in qasms:
        qc = QuantumCircuit.from_qasm_str(qasm.read_text(encoding="utf-8"))
        op_count = len(qc.data)
        if args.max_ops and op_count > args.max_ops:
            print(f"skip {qasm.stem.replace('_transpiled', '')}: ops={op_count} > {args.max_ops}", flush=True)
            continue
        for compiler_name, compile_fn in compilers:
            row = compile_row(qasm, qc, hw, compiler_name, compile_fn)
            rows.append(row)
            print(format_row(row), flush=True)

    if rows:
        print_averages(rows, fieldnames, header)
    else:
        print("no circuits compiled", flush=True)

    if args.csv is not None:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
