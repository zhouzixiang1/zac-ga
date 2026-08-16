"""Head-to-head comparison: ZAC vs the natam compiler on the ZAC hpca suite.

Both compilers are judged by *ZAC's own cost model*:

* natam compiles each QASM circuit, exports ZAIR, and the schedule is
  timestamped with ZAC's physics (atom transfer 2x15 us per rearrange job,
  movement t = sqrt(d / 0.00275 um/us^2), 1q gate 52 us, Rydberg 0.36 us,
  storage pitch 3 um) and then evaluated by ZAC's ``Simulator`` class.
* ZAC numbers are read from its shipped results (``result/zac/tech_eval``).

Usage::

    python examples/compare_zac.py [--config qasm_sa_1000_reuse]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
COMPILER_DIR = HERE.parents[1]
ROOT = HERE.parents[2]
ZAC_DIR = ROOT / "ZAC-main"

sys.path.insert(0, str(COMPILER_DIR))
sys.path.insert(0, str(ZAC_DIR))

from qiskit import QuantumCircuit  # noqa: E402

from natam_compiler import compile_circuit, to_zair  # noqa: E402
from natam_compiler.hardware import default_hardware  # noqa: E402
from zac.simulator.simulator import Simulator  # noqa: E402

# ---- ZAC physics constants (full_architecture.json / architecture.py) ------
SITE_UM = 3.0            # storage lattice pitch (um)
ACCEL = 0.00275          # um / us^2  (ZAC Architecture.movement_duration)
T_TRANSFER = 15.0        # us per pick-up / drop-off
T_1Q = 52.0              # us
T_RYDBERG = 0.36         # us

SIM_SPEC = {
    "operation_duration": {"rydberg": T_RYDBERG, "1qGate": T_1Q,
                           "atom_transfer": T_TRANSFER, "transfer_time": T_TRANSFER},
    "operation_fidelity": {"two_qubit_gate": 0.995, "single_qubit_gate": 0.9997,
                           "atom_transfer": 0.999},
    "qubit_spec": {"T": 1.5e6},
    # natam ZAIR maps the entanglement zone to array id 1
    "entanglement_zones": [{"zone_id": 0, "slms": [{"id": 1}]}],
}


def movement_duration(dist_um: float) -> float:
    return math.sqrt(dist_um / ACCEL) if dist_um > 0 else 0.0


def timestamp_zair(zair: dict, hw) -> dict:
    """Assign ZAC-model begin/end times to a natam ZAIR schedule (in place)."""

    def um(loc):
        zone = "storage" if loc[1] == 0 else "entanglement"
        x, y = hw.coordinate((zone, loc[2], loc[3]))
        return x * SITE_UM, y * SITE_UM

    t = 0.0
    instructions = []
    for inst in zair["instructions"]:
        kind = inst["type"]
        if kind == "measure":
            continue  # ZAC's model has no measurement stage
        if kind == "init":
            inst["begin_time"], inst["end_time"] = 0.0, 0.0
            instructions.append(inst)
            continue
        if kind == "rearrangeJob":
            dist = max(
                math.dist(um(a), um(b))
                for a, b in zip(inst["begin_locs"], inst["end_locs"])
            )
            dur = 2.0 * T_TRANSFER + movement_duration(dist)
            inst["insts"] = []
        elif kind == "1qGate":
            dur = T_1Q
        elif kind == "rydberg":
            dur = T_RYDBERG
        else:
            raise ValueError(kind)
        inst["begin_time"], inst["end_time"] = t, t + dur
        t += dur
        instructions.append(inst)
    zair["instructions"] = instructions
    return zair


def natam_result(qasm_path: Path, hw) -> dict:
    qc = QuantumCircuit.from_qasm_str(qasm_path.read_text(encoding="utf-8"))
    zair = timestamp_zair(to_zair(compile_circuit(qc, hw).program), hw)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(zair, fh)
        tmp = fh.name
    sim = Simulator()
    sim.set_arch_spec(SIM_SPEC)
    sim.parse(tmp)
    out = sim.simulate()
    Path(tmp).unlink()
    out["num_qubits"] = qc.num_qubits
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="qasm_sa_1000_reuse",
                        help="ZAC result directory under result/zac/tech_eval")
    args = parser.parse_args()

    bench_dir = ZAC_DIR / "benchmark" / "hpca"
    zac_dir = ZAC_DIR / "result" / "zac" / "tech_eval" / args.config / "fidelity"
    hw = default_hardware()

    header = (
        f"{'circuit':<16}{'q':>4}"
        f"{'ZAC t(us)':>12}{'natam t(us)':>13}{'t ratio':>9}"
        f"{'ZAC fid':>10}{'natam fid':>11}"
    )
    print(f"ZAC config: {args.config}   (cost model: ZAC simulator)")
    print(header)
    print("-" * len(header))

    t_ratios, zac_fids, nat_fids = [], [], []
    for qasm in sorted(bench_dir.glob("*.qasm")):
        ref_file = zac_dir / f"{qasm.stem}_fidelity.json"
        if not ref_file.exists():
            continue
        zac = json.loads(ref_file.read_text())
        nat = natam_result(qasm, hw)
        name = qasm.stem.replace("_transpiled", "")
        ratio = nat["cir_duration"] / zac["cir_duration"]
        t_ratios.append(ratio)
        zac_fids.append(zac["cir_fidelity"])
        nat_fids.append(nat["cir_fidelity"])
        print(
            f"{name:<16}{nat['num_qubits']:>4}"
            f"{zac['cir_duration']:>12.1f}{nat['cir_duration']:>13.1f}{ratio:>9.2f}"
            f"{zac['cir_fidelity']:>10.4f}{nat['cir_fidelity']:>11.4f}"
        )

    n = len(t_ratios)
    geo_t = math.exp(sum(math.log(r) for r in t_ratios) / n)
    print("-" * len(header))
    print(f"geomean natam/ZAC time ratio: {geo_t:.2f}"
          f"   |   mean fidelity  ZAC {sum(zac_fids)/n:.4f}"
          f"  natam {sum(nat_fids)/n:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
