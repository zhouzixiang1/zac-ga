"""End-to-end tests for the neutral-atom compiler and the round-trip
verification through the external Reverse_Compiler package.
"""

import math

import pytest
from qiskit import QuantumCircuit

from natam_compiler import (
    Compiler,
    HardwareSpec,
    compile_circuit,
    default_hardware,
    to_zair,
    verify,
)
from natam_compiler import reverse as rev
from natam_compiler.benchmarks import (
    bernstein_vazirani,
    ghz,
    linear_entangler,
    qft,
    random_clifford,
    run_suite,
)
from natam_compiler.operations import InitOp, MoveOp, TwoQubitGate


# --------------------------------------------------------------------------- #
#  Hardware model
# --------------------------------------------------------------------------- #
def test_default_hardware_dimensions():
    hw = default_hardware()
    assert (hw.storage.rows, hw.storage.cols) == (40, 20)
    assert (hw.entanglement.rows, hw.entanglement.cols) == (2, 20)
    assert hw.storage.capacity == 800
    assert hw.interaction_columns() == 20


def test_distance_crossing_zone_costs_more():
    hw = default_hardware()
    within = hw.distance(("storage", 0, 0), ("storage", 0, 1))
    across = hw.distance(("storage", 0, 0), ("entanglement", 1, 0))
    assert across > within


# --------------------------------------------------------------------------- #
#  Compilation structure
# --------------------------------------------------------------------------- #
def test_program_starts_with_init():
    result = compile_circuit(ghz(4))
    first = result.program.stages[0]
    assert first.kind == "init"
    assert all(isinstance(op, InitOp) for op in first.ops)
    assert len(first.ops) == 4


def test_two_qubit_gates_run_in_entanglement_zone():
    result = compile_circuit(ghz(3))
    czs = [op for _, op in result.program.operations() if isinstance(op, TwoQubitGate)]
    assert czs, "expected at least one CZ"
    for cz in czs:
        assert cz.pos0[0] == "entanglement"
        assert cz.pos1[0] == "entanglement"


def test_capacity_error_for_too_many_qubits():
    tiny = HardwareSpec()
    # Shrink storage so the circuit cannot fit.
    from natam_compiler.hardware import Zone

    tiny.storage = Zone("storage", 1, 2)
    with pytest.raises(ValueError):
        compile_circuit(ghz(5), tiny)


# --------------------------------------------------------------------------- #
#  ZAIR export
# --------------------------------------------------------------------------- #
def test_zair_export_shape():
    result = compile_circuit(ghz(3))
    zair = to_zair(result.program)
    assert zair["instructions"][0]["type"] == "init"
    types = {inst["type"] for inst in zair["instructions"]}
    assert "rydberg" in types
    assert "rearrangeJob" in types


# --------------------------------------------------------------------------- #
#  Round-trip equivalence
# --------------------------------------------------------------------------- #
SMALL_CIRCUITS = {
    "ghz_5": ghz(5),
    "qft_4": qft(4),
    "bv_5": bernstein_vazirani("1011"),
    "linear_6": linear_entangler(6, layers=2),
    "clifford_6": random_clifford(6, seed=11),
}


@pytest.mark.parametrize("name", list(SMALL_CIRCUITS))
def test_roundtrip_unitary(name):
    qc = SMALL_CIRCUITS[name]
    result = compile_circuit(qc)
    v = verify(qc, result.program)
    assert v.equivalent, f"{name} failed via {v.method}: {v.detail}"


def test_roundtrip_stim_large_clifford():
    qc = random_clifford(25, seed=5)
    result = compile_circuit(qc)
    v = verify(qc, result.program, prefer="stim")
    assert v.method == "stim-tableau"
    assert v.equivalent


def test_reverse_to_qiskit_matches_original():
    qc = ghz(4)
    result = compile_circuit(qc)
    reconstructed = rev.to_qiskit(result.program)
    from qiskit.quantum_info import Operator

    assert Operator(qc).equiv(Operator(reconstructed))


# --------------------------------------------------------------------------- #
#  Non-Clifford circuit still verifies via unitary path
# --------------------------------------------------------------------------- #
def test_non_clifford_roundtrip():
    qc = QuantumCircuit(3)
    qc.h(0)
    qc.t(0)
    qc.rx(0.37, 1)
    qc.cx(0, 1)
    qc.cz(1, 2)
    qc.ry(1.1, 2)
    v = verify(qc)
    assert v.method == "qiskit-unitary"
    assert v.equivalent


# --------------------------------------------------------------------------- #
#  QASM input
# --------------------------------------------------------------------------- #
def test_qasm_input():
    qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
h q[0];
cx q[0],q[1];
cx q[1],q[2];
"""
    result = compile_circuit(qasm)
    assert result.metrics.num_2q_gates == 2


# --------------------------------------------------------------------------- #
#  Metrics sanity + benchmark suite
# --------------------------------------------------------------------------- #
def test_metrics_positive():
    m = compile_circuit(linear_entangler(8, layers=3)).metrics
    assert m.total_time_us > 0
    assert m.movement_distance > 0
    assert 0 < m.estimated_fidelity <= 1
    assert m.num_2q_gates > 0


def test_benchmark_suite_all_verified():
    records = run_suite(do_verify=True)
    assert records
    for r in records:
        assert r.verified, f"{r.name} not verified ({r.method})"
