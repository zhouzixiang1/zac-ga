"""Exporters from :class:`CircuitIR` to Qiskit and Stim circuits."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .circuit_ir import CircuitIR, CircuitOperation

if TYPE_CHECKING:  # pragma: no cover
    from qiskit import QuantumCircuit
    import stim


class StimExportError(RuntimeError):
    """Raised when an operation cannot be represented as a Clifford in Stim."""


_TWO_PI = 2.0 * math.pi


def _wrap(angle: float) -> float:
    """Wrap an angle into ``[0, 2*pi)``."""
    return angle % _TWO_PI


def _isclose(a: float, b: float) -> bool:
    return math.isclose(a, b, abs_tol=1e-9)


# ---------------------------------------------------------------------- Qiskit
def to_qiskit(circuit_ir: CircuitIR) -> "QuantumCircuit":
    """Build a Qiskit :class:`QuantumCircuit` from the IR.

    Arbitrary-angle rotations are supported.  Independent gates are appended in
    chronological order without introducing artificial dependencies.
    """
    from qiskit import QuantumCircuit

    num_qubits = max(circuit_ir.num_qubits, 1)
    qc = QuantumCircuit(num_qubits, circuit_ir.num_clbits, name=circuit_ir.name)

    for op in circuit_ir.operations:
        _emit_qiskit(qc, op)
    return qc


def _emit_qiskit(qc: "QuantumCircuit", op: CircuitOperation) -> None:
    name = op.name
    q = op.qubits
    p = op.params
    if name == "X":
        qc.x(q[0])
    elif name == "Y":
        qc.y(q[0])
    elif name == "Z":
        qc.z(q[0])
    elif name == "H":
        qc.h(q[0])
    elif name == "S":
        qc.s(q[0])
    elif name == "SDG":
        qc.sdg(q[0])
    elif name == "T":
        qc.t(q[0])
    elif name == "TDG":
        qc.tdg(q[0])
    elif name == "RX":
        qc.rx(p[0], q[0])
    elif name == "RY":
        qc.ry(p[0], q[0])
    elif name == "RZ":
        qc.rz(p[0], q[0])
    elif name == "U":
        if len(p) == 3:
            qc.u(p[0], p[1], p[2], q[0])
        elif len(p) == 2:  # u2(phi, lam) == U(pi/2, phi, lam)
            qc.u(math.pi / 2, p[0], p[1], q[0])
        elif len(p) == 1:  # u1(lam) == U(0, 0, lam)
            qc.u(0.0, 0.0, p[0], q[0])
        else:
            raise ValueError(f"U gate needs 1-3 params, got {p}")
    elif name == "CZ":
        qc.cz(q[0], q[1])
    elif name == "CX":
        qc.cx(q[0], q[1])
    elif name == "SWAP":
        qc.swap(q[0], q[1])
    elif name == "RESET":
        qc.reset(q[0])
    elif name == "MEASURE":
        qc.measure(q[0], op.classical_bits[0])
    elif name == "BARRIER":
        qc.barrier(*q)
    elif name == "DELAY":
        duration = p[0] if p else 0
        for qubit in q:
            qc.delay(duration, qubit)
    else:
        raise ValueError(f"unsupported gate for Qiskit export: {op}")


# ------------------------------------------------------------------------ Stim
# Direct, parameter-free Clifford gate name mapping (IR name -> stim name).
_STIM_DIRECT = {
    "X": "X",
    "Y": "Y",
    "Z": "Z",
    "H": "H",
    "S": "S",
    "SDG": "S_DAG",
    "CZ": "CZ",
    "CX": "CX",
    "SWAP": "SWAP",
    "RESET": "R",
}


def to_stim(circuit_ir: CircuitIR) -> "stim.Circuit":
    """Build a Stim :class:`stim.Circuit` from the IR.

    Only Clifford operations are accepted.  Exact-Clifford rotations are
    simplified (e.g. ``RX(pi) -> X``, ``RZ(pi/2) -> S``); any non-Clifford
    rotation raises :class:`StimExportError`.

    Parallel layers are preserved: operations in the same layer are emitted in
    the same moment, separated from the next layer by ``TICK``.
    """
    import stim

    circuit = stim.Circuit()
    current_layer: int | None = None
    used_in_layer: set[int] = set()

    for op in circuit_ir.operations:
        if op.name in ("BARRIER", "DELAY"):
            # Timing-only: act purely as a moment boundary.
            continue

        layer = op.layer if op.layer is not None else current_layer
        if current_layer is not None and layer != current_layer:
            circuit.append("TICK")
            used_in_layer.clear()
        current_layer = layer

        for qubit in op.qubits:
            if qubit in used_in_layer:
                raise StimExportError(
                    f"conflict: qubit {qubit} used twice in layer {layer}"
                )
            used_in_layer.add(qubit)

        _emit_stim(circuit, op)
    return circuit


def _emit_stim(circuit: "stim.Circuit", op: CircuitOperation) -> None:
    name = op.name
    if name in _STIM_DIRECT:
        circuit.append(_STIM_DIRECT[name], list(op.qubits))
        return
    if name == "MEASURE":
        circuit.append("M", list(op.qubits))
        return
    if name in ("RX", "RY", "RZ"):
        stim_gate = _clifford_rotation(name, op.params[0])
        if stim_gate is None:
            raise StimExportError(
                f"non-Clifford rotation {op} cannot be exported to Stim"
            )
        if stim_gate:  # empty string means identity -> skip
            circuit.append(stim_gate, list(op.qubits))
        return
    if name in ("T", "TDG", "U"):
        raise StimExportError(f"non-Clifford gate {op} cannot be exported to Stim")
    raise StimExportError(f"unsupported gate for Stim export: {op}")


def _clifford_rotation(axis: str, angle: float) -> str | None:
    """Map an exact-Clifford rotation to a Stim gate name.

    Returns the Stim gate name, ``""`` for the identity (zero angle), or
    ``None`` if the angle is not a Clifford multiple of ``pi/2``.
    """
    a = _wrap(angle)
    half_pi = math.pi / 2
    table = {
        "RX": {0.0: "", math.pi: "X", half_pi: "SQRT_X", 3 * half_pi: "SQRT_X_DAG"},
        "RY": {0.0: "", math.pi: "Y", half_pi: "SQRT_Y", 3 * half_pi: "SQRT_Y_DAG"},
        "RZ": {0.0: "", math.pi: "Z", half_pi: "S", 3 * half_pi: "S_DAG"},
    }[axis]
    for ref, gate in table.items():
        if _isclose(a, ref) or _isclose(a, _wrap(ref)):
            return gate
    return None
