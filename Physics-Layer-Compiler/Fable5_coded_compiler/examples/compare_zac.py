"""Compare Fable against shipped ZAC results on the ZAC HPCA QASM suite.

Both compilers are scored with ZAC's simulator cost model. Fable schedules are
compiled to ZAIR, timestamped with the same movement/gate constants used by ZAC,
then evaluated by ``zac.simulator.simulator.Simulator``. ZAC numbers are read
from ``ZAC-main/result/zac/tech_eval/<config>/fidelity``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
FABLE_DIR = HERE.parents[1]
ROOT = HERE.parents[2]
ZAC_DIR = ROOT / "ZAC-main"

sys.path.insert(0, str(FABLE_DIR))
sys.path.insert(0, str(ZAC_DIR))

from qiskit import QuantumCircuit  # noqa: E402

from fable_compiler import compile_circuit, default_hardware, to_zair  # noqa: E402
from fable_compiler.compiler_search import compile_circuit as compile_search  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402
from zac.simulator.simulator import Simulator  # noqa: E402
from zac.zac import ZAC  # noqa: E402

OPTIMIZATION_LEVEL = 1

SITE_UM = 3.0
ACCEL = 0.00275
T_TRANSFER = 15.0
T_1Q = 52.0
T_RYDBERG = 0.36

SIM_SPEC = {
    "operation_duration": {
        "rydberg": T_RYDBERG,
        "1qGate": T_1Q,
        "atom_transfer": T_TRANSFER,
        "transfer_time": T_TRANSFER,
    },
    "operation_fidelity": {
        "two_qubit_gate": 0.995,
        "single_qubit_gate": 0.9997,
        "atom_transfer": 0.999,
    },
    "qubit_spec": {"T": 1.5e6},
    "entanglement_zones": [{"zone_id": 0, "slms": [{"id": 1}]}],
}


def movement_duration(dist_um: float) -> float:
    return math.sqrt(dist_um / ACCEL) if dist_um > 0.0 else 0.0


def timestamp_zair(zair: dict, hw) -> dict:
    def um(loc: list[int]) -> tuple[float, float]:
        zone = "storage" if loc[1] == 0 else "entanglement"
        # ZAIR location is [id, array, row, col]; Fable Position is (zone, col, row).
        x, y = hw.coordinate((zone, loc[3], loc[2]))
        return x * SITE_UM, y * SITE_UM

    t = 0.0
    instructions = []
    for original in zair["instructions"]:
        inst = dict(original)
        kind = inst["type"]
        if kind == "measure":
            continue
        if kind == "init":
            inst["begin_time"] = 0.0
            inst["end_time"] = 0.0
            instructions.append(inst)
            continue
        if kind == "rearrangeJob":
            dist = max(
                math.dist(um(begin), um(end))
                for begin, end in zip(inst["begin_locs"], inst["end_locs"])
            )
            duration = 2.0 * T_TRANSFER + movement_duration(dist)
            inst["insts"] = []
        elif kind == "1qGate":
            duration = T_1Q
        elif kind == "rydberg":
            duration = T_RYDBERG
        else:
            raise ValueError(kind)
        inst["begin_time"] = t
        inst["end_time"] = t + duration
        t += duration
        instructions.append(inst)

    stamped = dict(zair)
    stamped["instructions"] = instructions
    return stamped


def search_with_timeout(qc, hw, optimization_level=OPTIMIZATION_LEVEL):
    return compile_search(qc, hw, optimization_level=optimization_level, timeout=10.0)


def fable_result(qc: QuantumCircuit, hw, compile_fn=compile_circuit) -> dict:
    result = compile_fn(qc, hw, optimization_level=OPTIMIZATION_LEVEL)
    zair = timestamp_zair(to_zair(result.program), hw)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(zair, handle)
        temp_path = handle.name
    try:
        sim = Simulator()
        sim.set_arch_spec(SIM_SPEC)
        sim.parse(temp_path)
        out = sim.simulate()
    finally:
        Path(temp_path).unlink(missing_ok=True)

    out["num_qubits"] = qc.num_qubits
    out["fable_move_batches"] = result.metrics.num_move_batches
    out["fable_rydberg_pulses"] = result.metrics.rydberg_pulses
    out["fable_1q_stages"] = sum(1 for stage in result.program.stages if stage.kind == "1q")
    return out


def zac_result(qasm_path: Path, config: str) -> dict:
    arch_spec_path = ZAC_DIR / "hardware_spec" / "full_architecture.json"
    spec = json.loads(arch_spec_path.read_text(encoding="utf-8"))
    arch = Architecture(spec)
    arch.preprocessing()

    result_dir = ZAC_DIR / "result" / "zac" / "tech_eval" / config
    for child in ("code", "time", "fidelity"):
        (result_dir / child).mkdir(parents=True, exist_ok=True)

    zac_setting = {
        "name": qasm_path.stem,
        "arch_spec": str(arch_spec_path),
        "dependency": True,
        "dir": str(result_dir).replace("\\", "/") + "/",
        "routing_strategy": "maximalis_sort",
        "scheduling": "asap",
        "trivial_placement": False,
        "dynamic_placement": True,
        "use_window": True,
        "window_size": 1000,
        "reuse": True,
        "use_verifier": True,
    }

    zac_compiler = ZAC()
    zac_compiler.parse_setting(zac_setting)
    zac_compiler.set_architecture_spec_path(str(arch_spec_path))
    zac_compiler.set_architecture(arch)
    with contextlib.redirect_stdout(io.StringIO()):
        zac_compiler.set_program(str(qasm_path))
        zac_compiler.solve(save_file=True)

    sim = Simulator()
    sim.set_arch_spec(spec)
    sim.parse(zac_compiler.code_filename)
    out = sim.simulate()
    fidelity_path = result_dir / "fidelity" / f"{qasm_path.stem}_fidelity.json"
    fidelity_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="qasm_sa_1000_reuse")
    parser.add_argument("--limit", type=int, default=0, help="only run the first N circuits")
    parser.add_argument(
        "--max-ops",
        type=int,
        default=10000,
        help="skip circuits with more than this many Qiskit operations; use 0 to disable",
    )
    args = parser.parse_args(argv)

    bench_dir = ROOT / "benchmark" / "grover"  # examples hpca grover
    # bench_dir = ZAC_DIR / "benchmark" / "hpca",
    zac_dir = ZAC_DIR / "result" / "zac" / "tech_eval" / args.config / "fidelity"
    hw = default_hardware()

    header = (
        f"{'compiler':<16}{'circuit':<16}{'q':>4}{'ZAC us':>11}{'Fable us':>12}{'ratio':>8}"
        f"{'ZAC fid':>10}{'Fable fid':>11}{'mv':>5}{'pulse':>7}{'1qS':>5}"
    )
    print(f"ZAC config: {args.config}   (cost model: ZAC simulator)")
    print(header)
    print("-" * len(header))

    rows = []
    compilers = [("compiler", compile_circuit), ("compiler_search", search_with_timeout)]
    for index, qasm in enumerate(sorted(bench_dir.glob("*.qasm"))):
        if args.limit and index >= args.limit:
            break
        name = qasm.stem.replace("_transpiled", "")
        try:
            qc = QuantumCircuit.from_qasm_str(qasm.read_text(encoding="utf-8"))
            op_count = len(qc.data)
            if args.max_ops and op_count > args.max_ops:
                print(f"skip {name}: ops={op_count} > {args.max_ops}", flush=True)
                continue
            ref_file = zac_dir / f"{qasm.stem}_fidelity.json"
            if ref_file.exists():
                zac = json.loads(ref_file.read_text(encoding="utf-8"))
            else:
                zac = zac_result(qasm, args.config)
            if zac["cir_duration"] <= 0:
                raise ZeroDivisionError("ZAC circuit duration is zero")
            for compiler_name, compile_fn in compilers:
                fable = fable_result(qc, hw, compile_fn)
                ratio = fable["cir_duration"] / zac["cir_duration"]
                rows.append((compiler_name, ratio, zac["cir_fidelity"], fable["cir_fidelity"], name))
                print(
                    f"{compiler_name:<16}{name:<16}{fable['num_qubits']:>4}"
                    f"{zac['cir_duration']:>11.1f}{fable['cir_duration']:>12.1f}{ratio:>8.2f}"
                    f"{zac['cir_fidelity']:>10.4f}{fable['cir_fidelity']:>11.4f}"
                    f"{fable['fable_move_batches']:>5}{fable['fable_rydberg_pulses']:>7}"
                    f"{fable['fable_1q_stages']:>5}"
                )
        except Exception as exc:
            print(f"skip {name}: {type(exc).__name__}: {exc}", flush=True)
            continue

    if not rows:
        raise SystemExit("no benchmark rows found")
    print("-" * len(header))
    for compiler_name in sorted({row[0] for row in rows}):
        compiler_rows = [row for row in rows if row[0] == compiler_name]
        geomean = math.exp(sum(math.log(row[1]) for row in compiler_rows) / len(compiler_rows))
        mean_zac_fid = sum(row[2] for row in compiler_rows) / len(compiler_rows)
        mean_fable_fid = sum(row[3] for row in compiler_rows) / len(compiler_rows)
        print(
            f"{compiler_name}: n {len(compiler_rows)} geomean Fable/ZAC time ratio {geomean:.3f}"
            f"   mean fidelity ZAC {mean_zac_fid:.4f} Fable {mean_fable_fid:.4f}"
        )
        worst = sorted(compiler_rows, key=lambda row: row[1], reverse=True)[:5]
        print("worst ratios: " + ", ".join(f"{name} {ratio:.2f}x" for _compiler, ratio, _z, _f, name in worst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())