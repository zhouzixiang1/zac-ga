"""Aggregate isolated HPCA compiler runs."""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from pathlib import Path

try:
    from . import worker  # noqa: F401
except ImportError:
    worker = None  # The worker is launched as a module in a subprocess.


COMPILERS = {"zac": "zac", "zac_search": "zac_search"}
FIELDS = [
    "compiler", "circuit", "status", "qubits", "two_qubit_gates", "move_batches",
    "compile_wall_seconds", "compiler_total_seconds", "cir_duration",
    "cir_fidelity", "error_type", "error",
]


def run_one(root: Path, compiler: str, qasm: Path, arch: Path, output: Path, setting: Path) -> dict:
    command = [
        sys.executable, "-m", "evaluation.worker",
        "--compiler-root", str(root / COMPILERS[compiler]),
        "--qasm", str(qasm), "--arch-spec", str(arch),
        "--output", str(output / compiler / qasm.stem), "--setting", str(setting),
    ]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    metrics_path = output / compiler / qasm.stem / "metrics.json"
    if metrics_path.exists():
        result = json.loads(metrics_path.read_text(encoding="utf-8"))
    else:
        result = {"status": "error", "compiler": compiler, "circuit": qasm.stem,
                  "error_type": "WorkerError", "error": completed.stderr[-2000:]}
    result["compiler"] = compiler
    result.setdefault("circuit", qasm.stem)
    if completed.returncode and result.get("status") == "ok":
        result["status"] = "error"
    return result


def normalize(result: dict) -> dict:
    simulation = result.get("simulation") or {}
    runtime = result.get("compiler_runtime") or {}
    row = {field: None for field in FIELDS}
    row.update({key: result.get(key) for key in ("compiler", "circuit", "status", "qubits", "two_qubit_gates", "move_batches",
                                                  "compile_wall_seconds", "error_type", "error")})
    row["compiler_total_seconds"] = runtime.get("total")
    row["cir_duration"] = simulation.get("cir_duration")
    row["cir_fidelity"] = simulation.get("cir_fidelity")
    return row


def mean(rows: list[dict], field: str) -> float | None:
    values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
    return sum(values) / len(values) if values else None


def geomean_ratio(rows: list[dict], field: str) -> float | None:
    values = [row[field] for row in rows if isinstance(row.get(field), (int, float)) and row[field] > 0]
    return math.exp(sum(math.log(value) for value in values) / len(values)) if values else None


def print_row(row: dict) -> None:
    print(
        f"{row['compiler']:<12}{row['circuit']:<24}{row['status']:<8}"
        f"{str(row['qubits'] or ''):>4}{str(row['two_qubit_gates'] or ''):>6}"
        f"{str(row['move_batches'] or ''):>6}"
        f"{row['compile_wall_seconds'] or 0:>10.3f}"
        f"{row['compiler_total_seconds'] or 0:>10.3f}"
        f"{row['cir_duration'] or 0:>12.3f}"
        f"{row['cir_fidelity'] or 0:>12.6f}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare local zac and zac_search on HPCA QASM circuits.")
    parser.add_argument("--bench-dir", type=Path, default=Path("benchmark/hpca"))
    parser.add_argument("--arch-spec", type=Path, default=Path("hardware_spec/full_architecture.json"))
    parser.add_argument("--setting", type=Path, default=Path("exp_setting/hpca_evaluation.json"))
    parser.add_argument("--output", type=Path, default=Path("result/evaluation/hpca"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-ops", type=int, default=10000)
    parser.add_argument("--compiler", choices=("zac", "zac_search", "both"), default="both")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    bench_dir = (root / args.bench_dir).resolve()
    arch = (root / args.arch_spec).resolve()
    setting = (root / args.setting).resolve()
    output = (root / args.output).resolve()
    qasms = sorted(bench_dir.glob("*.qasm"))
    if args.limit:
        qasms = qasms[:args.limit]
    if not qasms:
        raise SystemExit(f"no QASM files found in {bench_dir}")
    compilers = ("zac", "zac_search") if args.compiler == "both" else (args.compiler,)
    rows = []
    print(
        f"{'compiler':<12}{'circuit':<24}{'status':<8}{'q':>4}{'2q':>6}{'mv':>6}"
        f"{'wall(s)':>10}{'total(s)':>10}{'duration':>12}{'fidelity':>12}",
        flush=True,
    )
    print("-" * 114, flush=True)
    for qasm in qasms:
        if args.max_ops:
            try:
                from qiskit import QuantumCircuit
                if len(QuantumCircuit.from_qasm_str(qasm.read_text(encoding="utf-8")).data) > args.max_ops:
                    print(f"skip {qasm.stem}: operation limit", flush=True)
                    continue
            except Exception as exc:
                print(f"skip {qasm.stem}: {type(exc).__name__}: {exc}", flush=True)
                continue
        for compiler in compilers:
            # print(f"run {compiler} {qasm.stem}", flush=True)
            row = normalize(run_one(root, compiler, qasm, arch, output, setting))
            rows.append(row)
            print_row(row)

    output.mkdir(parents=True, exist_ok=True)
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {"rows": rows, "aggregate": {}}
    for compiler in compilers:
        successful = [row for row in rows if row["compiler"] == compiler and row["status"] == "ok"]
        summary["aggregate"][compiler] = {
            "success": len(successful), "failure": sum(1 for row in rows if row["compiler"] == compiler) - len(successful),
            "mean_compile_wall_seconds": mean(successful, "compile_wall_seconds"),
            "mean_cir_duration": mean(successful, "cir_duration"),
            "mean_cir_fidelity": mean(successful, "cir_fidelity"),
        }
    paired = []
    by_circuit = {row["circuit"]: {} for row in rows}
    for row in rows:
        by_circuit.setdefault(row["circuit"], {})[row["compiler"]] = row
    for pair in by_circuit.values():
        if pair.get("zac", {}).get("status") == "ok" and pair.get("zac_search", {}).get("status") == "ok":
            paired.append({"duration_ratio": pair["zac_search"]["cir_duration"] / pair["zac"]["cir_duration"]})
    summary["paired"] = {"cir_duration_zac_search_over_zac": geomean_ratio(paired, "duration_ratio")}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\naggregate results", flush=True)
    print("compiler       success failure mean_wall mean_duration mean_fidelity", flush=True)
    for compiler, values in summary["aggregate"].items():
        print(
            f"{compiler:<14}{values['success']:>7}{values['failure']:>8}"
            f"{values['mean_compile_wall_seconds'] or 0:>10.3f}"
            f"{values['mean_cir_duration'] or 0:>14.3f}"
            f"{values['mean_cir_fidelity'] or 0:>14.6f}",
            flush=True,
        )
    print(f"paired duration geomean (zac_search/zac): {summary['paired']['cir_duration_zac_search_over_zac']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
