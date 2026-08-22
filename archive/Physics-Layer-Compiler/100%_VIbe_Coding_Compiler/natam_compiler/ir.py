"""Intermediate representation and circuit front-end.

The compiler accepts either a Qiskit :class:`~qiskit.circuit.QuantumCircuit`
or an OpenQASM string and normalizes it into a flat, hardware-friendly
:class:`LogicalCircuit`.

The single-qubit basis ``{rz, sx, x}`` plus the native two-qubit ``cz`` gate is
universal, and -- importantly -- it keeps Clifford inputs expressed with
Clifford-exact gates (``rz`` angles that are multiples of ``pi/2`` and ``sx``).
That lets the downstream reverse compiler export Clifford circuits to Stim,
while arbitrary-angle ``rz`` gates make the basis universal for everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple, Union

from qiskit import QuantumCircuit, transpile

# Universal single-qubit basis (IBM-style) + native Rydberg CZ entangler.
BASIS_GATES = ["rz", "sx", "x", "cz"]


@dataclass
class IRGate1Q:
    qubit: int
    name: str                       # "rz", "sx" or "x"
    params: Tuple[float, ...] = ()


@dataclass
class IRGate2Q:
    q0: int
    q1: int
    name: str = "cz"


@dataclass
class IRMeasure:
    qubit: int
    clbit: int


IROp = Union[IRGate1Q, IRGate2Q, IRMeasure]


@dataclass
class LogicalCircuit:
    num_qubits: int
    num_clbits: int
    ops: List[IROp] = field(default_factory=list)

    def two_qubit_pairs(self) -> List[Tuple[int, int]]:
        return [(o.q0, o.q1) for o in self.ops if isinstance(o, IRGate2Q)]


def from_qiskit(circuit: QuantumCircuit, optimization_level: int = 1) -> LogicalCircuit:
    """Normalize a Qiskit circuit into a :class:`LogicalCircuit`."""
    decomposed = transpile(
        circuit,
        basis_gates=BASIS_GATES,
        optimization_level=optimization_level,
    )
    num_qubits = decomposed.num_qubits
    num_clbits = decomposed.num_clbits
    qindex = {bit: i for i, bit in enumerate(decomposed.qubits)}
    cindex = {bit: i for i, bit in enumerate(decomposed.clbits)}

    ops: List[IROp] = []
    for instr in decomposed.data:
        op = instr.operation
        name = op.name
        qubits = [qindex[q] for q in instr.qubits]
        if name == "measure":
            ops.append(IRMeasure(qubits[0], cindex[instr.clbits[0]]))
        elif name == "barrier":
            continue
        elif name == "cz":
            ops.append(IRGate2Q(qubits[0], qubits[1]))
        elif name == "rz":
            ops.append(IRGate1Q(qubits[0], "rz", (float(op.params[0]),)))
        elif name in ("sx", "x"):
            ops.append(IRGate1Q(qubits[0], name))
        elif name in ("id", "delay"):
            continue
        else:
            raise ValueError(f"unexpected gate after transpilation: {name!r}")

    return LogicalCircuit(num_qubits, num_clbits, ops)


def from_qasm(qasm: str, optimization_level: int = 1) -> LogicalCircuit:
    """Parse an OpenQASM 2/3 string into a :class:`LogicalCircuit`."""
    circuit = _load_qasm(qasm)
    return from_qiskit(circuit, optimization_level)


def _load_qasm(qasm: str) -> QuantumCircuit:
    # Try OpenQASM 2 first, then fall back to OpenQASM 3.
    try:
        return QuantumCircuit.from_qasm_str(qasm)
    except Exception:  # pragma: no cover - depends on qiskit-qasm3 availability
        from qiskit.qasm3 import loads as qasm3_loads

        return qasm3_loads(qasm)
