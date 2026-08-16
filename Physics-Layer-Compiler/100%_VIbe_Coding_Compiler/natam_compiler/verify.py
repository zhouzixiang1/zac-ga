"""Automated functional-equivalence verification.

The compiled program is reverse-compiled (via the external ``Reverse_Compiler``
package) back into a circuit, which is then checked against the original:

* **Qiskit unitary check** -- exact operator equality up to global phase; used
  for small circuits of any gate set.
* **Stim tableau check** -- stabilizer-tableau equality for Clifford circuits;
  scales to large qubit counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from qiskit import QuantumCircuit

from .compiler import compile_circuit
from .hardware import HardwareSpec
from .ir import IRGate1Q, IRGate2Q, LogicalCircuit, from_qiskit
from .operations import HardwareProgram
from . import reverse as rev

MAX_UNITARY_QUBITS = 11


@dataclass
class VerificationResult:
    equivalent: bool
    method: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.equivalent


# --------------------------------------------------------------------------- #
#  Qiskit unitary equivalence
# --------------------------------------------------------------------------- #
def _strip_measurements(qc: QuantumCircuit) -> QuantumCircuit:
    out = qc.remove_final_measurements(inplace=False)
    return out if out is not None else qc


def qiskit_equivalent(a: QuantumCircuit, b: QuantumCircuit) -> bool:
    from qiskit.quantum_info import Operator

    return Operator(_strip_measurements(a)).equiv(Operator(_strip_measurements(b)))


# --------------------------------------------------------------------------- #
#  Stim tableau equivalence
# --------------------------------------------------------------------------- #
def _logical_to_ops(logical: LogicalCircuit):
    ops = []
    for op in logical.ops:
        if isinstance(op, IRGate1Q):
            ops.append((op.name, (op.qubit,), op.params))
        elif isinstance(op, IRGate2Q):
            ops.append((op.name, (op.q0, op.q1), ()))
        # measurements are dropped for tableau comparison
    return ops


def _strip_measure_stim(circuit):
    import stim

    out = stim.Circuit()
    for inst in circuit:
        if inst.name in ("M", "MR", "MX", "MY", "MZ", "R", "RX", "RY"):
            continue
        out.append(inst)
    return out


def _original_stim_tableau(logical: LogicalCircuit):
    pkg = rev._load_package()
    zair = pkg.encode_circuit(_logical_to_ops(logical), logical.num_qubits)
    ir = pkg.reverse_compile(zair)
    return _strip_measure_stim(pkg.to_stim(ir)).to_tableau()


def stim_equivalent(original_logical: LogicalCircuit, program: HardwareProgram) -> bool:
    reconstructed = _strip_measure_stim(rev.to_stim(program)).to_tableau()
    original = _original_stim_tableau(original_logical)
    return reconstructed == original


# --------------------------------------------------------------------------- #
#  Top-level verification
# --------------------------------------------------------------------------- #
def verify(
    original: QuantumCircuit,
    program: Optional[HardwareProgram] = None,
    hardware: Optional[HardwareSpec] = None,
    optimization_level: int = 1,
    prefer: str = "auto",
) -> VerificationResult:
    """Verify that ``program`` is functionally equivalent to ``original``.

    ``prefer`` may be ``"auto"``, ``"unitary"`` or ``"stim"``.
    """
    if program is None:
        program = compile_circuit(original, hardware, optimization_level).program

    original_logical = from_qiskit(original, optimization_level)
    small = original.num_qubits <= MAX_UNITARY_QUBITS
    want_stim = prefer == "stim" or (prefer == "auto" and not small)

    if want_stim:
        try:
            ok = stim_equivalent(original_logical, program)
            return VerificationResult(ok, "stim-tableau")
        except Exception as exc:  # non-Clifford or stim limitation
            if prefer == "stim":
                return VerificationResult(False, "stim-tableau", str(exc))
            if not small:
                return VerificationResult(
                    False,
                    "skipped",
                    f"{original.num_qubits} qubits exceeds the unitary limit and "
                    f"the circuit is non-Clifford ({exc})",
                )

    reconstructed = rev.to_qiskit(program)
    ok = qiskit_equivalent(original, reconstructed)
    return VerificationResult(ok, "qiskit-unitary")
