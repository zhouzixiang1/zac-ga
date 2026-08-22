#!/usr/bin/env python3
"""End-to-end example: 30-qubit / 50-gate circuit → compiler → simulator.

Pipeline
--------
1. Build a 30-qubit quantum circuit with exactly 50 gates (H / RZ / CX).
2. Compile it with the neutral-atom "vibe coding" compiler
   (``natam_compiler``) into a ZAIR hardware schedule.
3. Save the schedule to ``demo30.json``.
4. Forward-simulate + validate it with the simulator engine and print a report.
5. (optional) ``--mp4 out.mp4`` renders the full animation.

Run::

    python examples/pipeline_30q.py
    python examples/pipeline_30q.py --mp4 demo30.mp4

Then open the schedule in the GUI::

    python run.py examples/demo30.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SIM_ROOT = os.path.dirname(HERE)
COMPILER_ROOT = os.path.join(
    os.path.dirname(SIM_ROOT), "100%_VIbe_Coding_Compiler"
)
for p in (SIM_ROOT, COMPILER_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


def build_circuit(n_qubits: int = 30, n_gates: int = 50, seed: int = 7):
    """A 30-qubit circuit with exactly ``n_gates`` mixed 1q/2q gates."""
    from qiskit import QuantumCircuit

    rng = random.Random(seed)
    qc = QuantumCircuit(n_qubits)
    placed = 0
    while placed < n_gates:
        r = rng.random()
        if r < 0.40:
            qc.h(rng.randrange(n_qubits))
            placed += 1
        elif r < 0.60:
            qc.rz(rng.uniform(0, math.pi), rng.randrange(n_qubits))
            placed += 1
        else:
            a = rng.randrange(n_qubits)
            b = rng.randrange(n_qubits)
            if a != b:
                qc.cx(a, b)
                placed += 1
    return qc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="30q/50-gate compiler→simulator demo")
    ap.add_argument("--qubits", type=int, default=30)
    ap.add_argument("--gates", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(HERE, "demo30.json"))
    ap.add_argument("--mp4", metavar="PATH", help="also render the animation to PATH")
    args = ap.parse_args(argv)

    # 1) circuit -----------------------------------------------------------
    qc = build_circuit(args.qubits, args.gates, args.seed)
    print(f"[1] circuit: {qc.num_qubits} qubits, {qc.size()} gates")

    # 2) compile with the neutral-atom compiler ----------------------------
    from natam_compiler import compile_circuit, to_zair

    result = compile_circuit(qc)
    zair = to_zair(result.program, name=f"demo{args.qubits}")
    kinds = Counter(i["type"] for i in zair["instructions"])
    print(f"[2] compiled → {len(zair['instructions'])} ZAIR instructions  {dict(kinds)}")
    for k, v in result.metrics.as_dict().items():
        print(f"        {k:22}: {v}")

    # 3) save schedule -----------------------------------------------------
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(zair, fh, indent=2)
    print(f"[3] saved schedule → {args.out}")

    # 4) forward-simulate + validate --------------------------------------
    from simulator import SimulationEngine

    engine = SimulationEngine(zair["instructions"])
    print(f"[4] simulator: {len(engine.init_positions)} atoms, {engine.num_steps} steps")
    kind_counts = Counter(s.kind for s in engine.steps)
    print(f"        step kinds: {dict(kind_counts)}")
    max_parallel = max((s.parallel_size for s in engine.steps), default=0)
    print(f"        max parallel group size: {max_parallel}")
    invalid = [s for s in engine.steps if not s.validation.ok]
    if engine.is_valid():
        print("        validation: ALL STEPS VALID ✓")
    else:
        print(f"        validation: {len(invalid)} invalid step(s):")
        for e in engine.all_errors():
            print(f"          ⚠ {e}")

    # 5) optional animation ------------------------------------------------
    if args.mp4:
        import matplotlib
        matplotlib.use("Agg")
        from simulator.frames import ExportSettings
        from simulator.video_export import export_animation

        print(f"[5] rendering MP4 → {args.mp4} …")
        export_animation(engine, args.mp4, "mp4",
                         ExportSettings(fps=24, move_seconds=0.6, hold_seconds=0.4))
        print(f"        wrote {args.mp4} ({os.path.getsize(args.mp4)} bytes)")

    print("\nOpen it in the GUI with:")
    print(f"    python run.py {os.path.relpath(args.out, SIM_ROOT)}")
    return 0 if engine.is_valid() else 1


if __name__ == "__main__":
    sys.exit(main())
