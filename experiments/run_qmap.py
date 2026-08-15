"""Run MQT-QMAP zoned compilers on ICCAD'25 Table I benchmarks and compare to paper truth.

Usage (with the qmap venv):
  .venv_qmap/bin/python experiments/run_qmap.py --benchmarks qasmbench --configs agnostic,astar,ids
  .venv_qmap/bin/python experiments/run_qmap.py --smoke          # seca 11 only, all configs
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))

from spec_convert import convert  # noqa: E402

from mqt.bench import BenchmarkLevel, get_benchmark  # noqa: E402
from mqt.core import load  # noqa: E402
from mqt.qmap.na.zoned import (  # noqa: E402
    RoutingAgnosticCompiler,
    RoutingAwareCompiler,
    ZonedNeutralAtomArchitecture,
)
try:  # mqt.qmap >= 3.3 exposes method enums; 3.2.0 (ICCAD'25 paper version) does not
    from mqt.qmap.na.zoned import PlacementMethod, RoutingMethod

    HAS_METHOD_ENUMS = True
except ImportError:
    HAS_METHOD_ENUMS = False
from naviz_eval import NavizEvaluator  # noqa: E402

ZAC_ARCH = ROOT / "ZAC" / "hardware_spec" / "full_architecture.json"
HPCA_DIR = ROOT / "ZAC" / "benchmark" / "hpca"
RESULTS_DIR = ROOT / "experiments" / "results"
TRUTH = ROOT / "experiments" / "paper_truth" / "qmap_table1.csv"

# Table I QASMBench rows: (bench, qubits) -> filename prefix
QASMBENCH = [
    ("ising", 42), ("ising", 98),
    ("qft", 18), ("qft", 29),
    ("bv", 30), ("bv", 70),
    ("wstate", 27), ("seca", 11),
    ("ghz", 40), ("ghz", 78),
    ("multiply", 13), ("cat", 22), ("cat", 35),
    ("swaptest", 25), ("knn", 31),
]
# Table I MQT Bench rows
MQTBENCH = [
    ("graphstate", 60), ("graphstate", 100),
    ("wstate", 200),
    ("qft", 200), ("qft", 500),
    ("qpeexact", 200),
]


def find_qasm(bench: str, n: int) -> Path:
    prefix = "swap_test" if bench == "swaptest" else bench
    matches = [p for p in HPCA_DIR.glob(f"{prefix}_n{n}*.qasm")]
    assert matches, f"no qasm for {bench} n{n}"
    return matches[0]


def preprocess(path: Path):
    from qiskit import QuantumCircuit, transpile

    circ = QuantumCircuit.from_qasm_file(str(path))
    flattened = QuantumCircuit(circ.num_qubits, circ.num_clbits)
    flattened.compose(circ, inplace=True)
    transpiled = transpile(
        flattened, basis_gates=["cz", "id", "u2", "u1", "u3"],
        optimization_level=3, seed_transpiler=0,
    )
    stripped = QuantumCircuit(*transpiled.qregs, *transpiled.cregs)
    for instr in transpiled.data:
        if instr.operation.name not in {"measure", "barrier"}:
            stripped.append(instr)
    return load(stripped)


def make_compiler(kind: str, arch, source: str):
    if kind == "agnostic":
        return RoutingAgnosticCompiler(arch, log_level="ERROR")
    # paper Table I parameters: QASMBench a=0.2,b=0.2,d=0.6 / MQT Bench a=0.2,b=0.8,d=0.9
    if source == "qasmbench":
        df, dv = 0.6, 0.2
    else:
        df, dv = 0.9, 0.8
    kw = dict(log_level="ERROR", deepening_factor=df, deepening_value=dv,
              lookahead_factor=0.2, reuse_level=5.0, max_nodes=50_000_000)
    if kind == "astar":
        if HAS_METHOD_ENUMS:
            kw.update(placement_method=PlacementMethod.astar, routing_method=RoutingMethod.strict)
        return RoutingAwareCompiler(arch, **kw)
    if kind == "ids":
        if not HAS_METHOD_ENUMS:
            raise ValueError("ids placement requires mqt.qmap >= 3.3")
        return RoutingAwareCompiler(arch, placement_method=PlacementMethod.ids,
                                    log_level="ERROR", routing_method=RoutingMethod.strict)
    raise ValueError(kind)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmarks", default="qasmbench", choices=["qasmbench", "mqtbench", "all"])
    ap.add_argument("--configs", default="agnostic,astar,ids")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = RESULTS_DIR / ("qmap_smoke.csv" if args.smoke else f"qmap_{args.benchmarks}.csv")

    spec = convert(json.load(open(ZAC_ARCH)))
    arch = ZonedNeutralAtomArchitecture.from_json_string(json.dumps(spec))
    evaluator = NavizEvaluator(spec)

    circuits: list[tuple[str, str, int, object]] = []
    if args.smoke:
        circuits.append(("qasmbench", "seca", 11, preprocess(find_qasm("seca", 11))))
    else:
        if args.benchmarks in ("qasmbench", "all"):
            for bench, n in QASMBENCH:
                circuits.append(("qasmbench", bench, n, preprocess(find_qasm(bench, n))))
        if args.benchmarks in ("mqtbench", "all"):
            for bench, n in MQTBENCH:
                print(f"[mqtbench] generating {bench} n{n} ...", flush=True)
                circ = get_benchmark(bench, BenchmarkLevel.INDEP, n)
                from qiskit import QuantumCircuit, transpile

                flattened = QuantumCircuit(circ.num_qubits, circ.num_clbits)
                flattened.compose(circ, inplace=True)
                t = transpile(flattened, basis_gates=["cz", "id", "u2", "u1", "u3"],
                              optimization_level=3, seed_transpiler=0)
                stripped = QuantumCircuit(*t.qregs, *t.cregs)
                for instr in t.data:
                    if instr.operation.name not in {"measure", "barrier"}:
                        stripped.append(instr)
                circuits.append(("mqtbench", bench, n, load(stripped)))

    configs = args.configs.split(",")
    rows = []
    for source, bench, n, qc in circuits:
        for kind in configs:
            tag = f"{source}:{bench} n{n} [{kind}]"
            try:
                compiler = make_compiler(kind, arch, source)
                code = compiler.compile(qc)
                stats = compiler.stats()
                code = "\n".join(l for l in code.splitlines() if not l.startswith("@+ u"))
                evaluator.reset()
                metrics = evaluator.evaluate(code)
                ls = stats.get("layoutSynthesizerStatistics", {})
                rows.append({
                    "source": source, "circuit": bench, "qubits": n, "config": kind,
                    "placement_time": ls.get("placementTime"),
                    "routing_time": ls.get("routingTime"),
                    "total_time": stats.get("totalTime"),
                    "steps": metrics["rearrangement_steps"],
                    "rearr_ms": round(metrics["rearrangement_duration"], 2),
                    "two_qubit_gate_layer": metrics["two_qubit_gate_layer"],
                    "max_gates_in_layer": metrics["max_two_qubit_gates"],
                    "status": "ok",
                })
                print(f"[OK] {tag}: steps={metrics['rearrangement_steps']} "
                      f"rearr={metrics['rearrangement_duration']:.2f}ms "
                      f"place={ls.get('placementTime')}", flush=True)
            except Exception as e:  # noqa: BLE001
                rows.append({"source": source, "circuit": bench, "qubits": n, "config": kind,
                             "status": f"error: {e!s:.120}"})
                print(f"[FAIL] {tag}: {e!s:.160}", flush=True)
            with open(out_csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

    # compare with paper truth
    truth = list(csv.DictReader(open(TRUTH)))
    tmap = {(t["source"], t["circuit"], int(t["qubits"])): t for t in truth if t["qubits"]}
    print("\n=== compare to ICCAD'25 Table I (steps / rearr ms) ===")
    print(f"{'circuit':24s} {'cfg':9s} {'ours':>12s} {'paper':>12s} {'steps Δ':>8s}")
    for r in rows:
        if r.get("status") != "ok":
            continue
        key = (r["source"], r["circuit"], r["qubits"])
        t = tmap.get(key)
        if not t:
            continue
        if r["config"] == "agnostic":
            p_steps, p_rearr = t["ra_steps"], t["ra_rearr_ms"]
        else:
            p_steps, p_rearr = t["rw_steps"], t["rw_rearr_ms"]
        ds = r["steps"] - int(p_steps)
        print(f"{r['circuit']+' n'+str(r['qubits']):24s} {r['config']:9s} "
              f"{r['steps']:5d}/{r['rearr_ms']:6.1f} {int(p_steps):5d}/{float(p_rearr):6.1f} "
              f"{ds:+8d}")


if __name__ == "__main__":
    main()
