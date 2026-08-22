"""Logical circuit front-end."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple, Union

from qiskit import QuantumCircuit, transpile
from qiskit.qasm2 import LEGACY_CUSTOM_INSTRUCTIONS, loads as qasm2_loads


@dataclass(frozen=True)
class Gate1Q:
    name: str
    qubit: int
    params: Tuple[float, ...] = ()


@dataclass(frozen=True)
class Gate2Q:
    name: str
    q0: int
    q1: int


@dataclass(frozen=True)
class Measure:
    qubit: int
    clbit: int


LogicalOp = Union[Gate1Q, Gate2Q, Measure]


@dataclass
class LogicalCircuit:
    num_qubits: int
    num_clbits: int = 0
    ops: List[LogicalOp] = field(default_factory=list)


def _bit_index(bits, bit) -> int:
    return bits.index(bit)


def from_qiskit(circuit: QuantumCircuit, optimization_level: int = 1) -> LogicalCircuit:
    tqc = transpile(
        circuit,
        basis_gates=["u3", "cz", "measure"],
        optimization_level=optimization_level,
    )
    logical = LogicalCircuit(tqc.num_qubits, tqc.num_clbits)
    for instruction in tqc.data:
        inst = instruction.operation
        qargs = instruction.qubits
        cargs = instruction.clbits
        name = inst.name.lower()
        if name == "barrier":
            continue
        if name == "measure":
            logical.ops.append(
                Measure(_bit_index(tqc.qubits, qargs[0]), _bit_index(tqc.clbits, cargs[0]))
            )
        elif len(qargs) == 1:
            params = tuple(float(p) for p in inst.params)
            logical.ops.append(Gate1Q(name, _bit_index(tqc.qubits, qargs[0]), params))
        elif len(qargs) == 2:
            if name != "cz":
                raise ValueError(f"unsupported 2q gate after transpile: {name}")
            logical.ops.append(
                Gate2Q(name, _bit_index(tqc.qubits, qargs[0]), _bit_index(tqc.qubits, qargs[1]))
            )
        else:
            raise ValueError(f"unsupported instruction {name!r}")
    return logical


def from_qasm(source: str, optimization_level: int = 1) -> LogicalCircuit:
    return from_qiskit(
        qasm2_loads(source, custom_instructions=LEGACY_CUSTOM_INSTRUCTIONS),
        optimization_level,
    )