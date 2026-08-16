"""Benchmark circuits and a benchmarking harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from qiskit import QuantumCircuit

from .compiler import compile_circuit
from .hardware import HardwareSpec
from .metrics import Metrics
from .verify import verify


# --------------------------------------------------------------------------- #
#  Circuit generators
# --------------------------------------------------------------------------- #
def ghz(n: int) -> QuantumCircuit:
    qc = QuantumCircuit(n)
    qc.h(0)
    for i in range(n - 1):
        qc.cx(i, i + 1)
    return qc


def qft(n: int) -> QuantumCircuit:
    from qiskit.synthesis import synth_qft_full

    return synth_qft_full(n, do_swaps=True)


def bernstein_vazirani(secret: str) -> QuantumCircuit:
    n = len(secret)
    qc = QuantumCircuit(n + 1)
    qc.x(n)
    qc.h(range(n + 1))
    for i, bit in enumerate(reversed(secret)):
        if bit == "1":
            qc.cx(i, n)
    qc.h(range(n))
    return qc


def linear_entangler(n: int, layers: int = 3) -> QuantumCircuit:
    qc = QuantumCircuit(n)
    qc.h(range(n))
    for _ in range(layers):
        for i in range(0, n - 1, 2):
            qc.cx(i, i + 1)
        for i in range(1, n - 1, 2):
            qc.cx(i, i + 1)
    return qc


def random_clifford(n: int, seed: int = 0) -> QuantumCircuit:
    from qiskit.quantum_info import random_clifford as _rc

    return _rc(n, seed=seed).to_circuit()


def qasm_suite(directory: str) -> Dict[str, Callable[[], QuantumCircuit]]:
    """Build a suite from every ``.qasm`` file in ``directory``.

    Works with e.g. the ZAC benchmark sets (``ZAC-main/benchmark/hpca``).
    """
    from pathlib import Path

    def loader(path: Path) -> Callable[[], QuantumCircuit]:
        def load() -> QuantumCircuit:
            text = path.read_text(encoding="utf-8")
            try:
                return QuantumCircuit.from_qasm_str(text)
            except Exception:
                from qiskit.qasm3 import loads

                return loads(text)

        return load

    files = sorted(Path(directory).glob("*.qasm"))
    if not files:
        raise FileNotFoundError(f"no .qasm files in {directory}")
    return {p.stem.replace("_transpiled", ""): loader(p) for p in files}


# --------------------------------------------------------------------------- #
#  Harness
# --------------------------------------------------------------------------- #
@dataclass
class BenchmarkRecord:
    name: str
    num_qubits: int
    metrics: Metrics
    verified: bool
    method: str


DEFAULT_SUITE: Dict[str, Callable[[], QuantumCircuit]] = {
    "ghz_5": lambda: ghz(5),
    "ghz_10": lambda: ghz(10),
    "qft_5": lambda: qft(5),
    "bv_6": lambda: bernstein_vazirani("10110"),
    "linear_8": lambda: linear_entangler(8, layers=3),
    "clifford_6": lambda: random_clifford(6, seed=7),
    "clifford_20": lambda: random_clifford(20, seed=3),
}


def run_suite(
    suite: Dict[str, Callable[[], QuantumCircuit]] | None = None,
    hardware: HardwareSpec | None = None,
    do_verify: bool = True,
) -> List[BenchmarkRecord]:
    suite = suite or DEFAULT_SUITE
    records: List[BenchmarkRecord] = []
    for name, gen in suite.items():
        qc = gen()
        result = compile_circuit(qc, hardware)
        verified = True
        method = "none"
        if do_verify:
            v = verify(qc, result.program, hardware)
            verified = bool(v)
            method = v.method
        records.append(
            BenchmarkRecord(
                name=name,
                num_qubits=qc.num_qubits,
                metrics=result.metrics,
                verified=verified,
                method=method,
            )
        )
    return records


def format_table(records: List[BenchmarkRecord]) -> str:
    header = (
        f"{'circuit':<14}{'q':>4}{'stages':>8}{'time(us)':>12}"
        f"{'move':>10}{'2q':>6}{'par':>7}{'fidelity':>11}  verify"
    )
    lines = [header, "-" * len(header)]
    for r in records:
        m = r.metrics
        mark = "OK" if r.verified else ("SKIP" if r.method == "skipped" else "FAIL")
        lines.append(
            f"{r.name:<14}{r.num_qubits:>4}{m.num_stages:>8}"
            f"{m.total_time_us:>12.1f}{m.movement_distance:>10.1f}"
            f"{m.num_2q_gates:>6}{m.twoq_parallelism:>7.2f}"
            f"{m.estimated_fidelity:>11.5f}  {mark} ({r.method})"
        )
    return "\n".join(lines)
