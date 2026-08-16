"""End-to-end usage example for the neutral-atom compiler.

Run from the ``100%_VIbe_Coding_Compiler`` folder::

    python examples/demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from natam_compiler import (
    HardwareSpec,
    animate_compilation,
    compile_circuit,
    default_hardware,
    to_zair,
    verify,
)
from natam_compiler import reverse as rev
from natam_compiler.benchmarks import format_table, ghz, qft, run_suite


def demo_single_circuit():
    print("=" * 70)
    print("Compiling a 5-qubit GHZ circuit on the default 40x20 / 2x20 machine")
    print("=" * 70)

    qc = ghz(5)
    result = compile_circuit(qc)

    # 1) Inspect the executable hardware-operation sequence (first stages).
    print("\n--- hardware operation sequence (first 12 stages) ---")
    head = "\n".join(result.program.to_text().splitlines()[:24])
    print(head)

    # 2) Performance metrics.
    print("\n--- metrics ---")
    for k, v in result.metrics.as_dict().items():
        print(f"  {k:20}: {v}")

    # 3) Reverse-compile via the external Reverse_Compiler package.
    print("\n--- reverse compilation ---")
    reconstructed = rev.to_qiskit(result.program)
    print(f"  reconstructed circuit has {reconstructed.size()} operations")

    # 4) Automated equivalence check.
    v = verify(qc, result.program)
    print(f"\n  verification: {'PASS' if v else 'FAIL'} via {v.method}")


def demo_custom_hardware():
    print("\n" + "=" * 70)
    print("Custom hardware: faster moves, larger entanglement zone")
    print("=" * 70)
    hw = default_hardware()
    hw.t_move_per_site = 0.8
    result = compile_circuit(qft(5), hw)
    print(f"  QFT-5  time={result.metrics.total_time_us:.1f} us  "
          f"F={result.metrics.estimated_fidelity:.4f}")


def demo_benchmarks():
    print("\n" + "=" * 70)
    print("Benchmark suite")
    print("=" * 70)
    print(format_table(run_suite(do_verify=True)))


def demo_animation():
    print("\n" + "=" * 70)
    print("Animating the compiled movement schedule")
    print("=" * 70)

    # A linear entangler produces several batched AOD moves worth animating.
    from natam_compiler.benchmarks import linear_entangler

    hw = default_hardware()
    # Shrink the batch capacity so parallel moves split into visible batches.
    hw.aod_max_atoms = 4
    result = compile_circuit(linear_entangler(6, layers=2), hw)

    m = result.metrics
    print(f"  moves={m.num_moves}  batches={m.num_move_batches}  "
          f"avg atoms/batch={m.move_parallelism:.2f}")

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "compilation.gif")
    animate_compilation(
        result,
        hw,
        output_path=out_path,
        format="gif",
        fps=20,
    )
    print(f"  animation written to {out_path}")


if __name__ == "__main__":
    demo_single_circuit()
    demo_custom_hardware()
    demo_benchmarks()
    demo_animation()
