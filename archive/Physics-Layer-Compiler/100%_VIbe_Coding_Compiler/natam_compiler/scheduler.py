"""ASAP list scheduling.

The logical circuit is partitioned into time slots.  Operations in the same
slot act on disjoint qubits and have no data dependency, so they may run in
parallel on the hardware.  This exposes the parallelism that the compiler later
maps onto simultaneous moves, single-qubit pulses and Rydberg pulses.
"""

from __future__ import annotations

from typing import List

from .ir import IRGate1Q, IRGate2Q, IRMeasure, IROp, LogicalCircuit


def _qubits(op: IROp) -> List[int]:
    if isinstance(op, IRGate1Q):
        return [op.qubit]
    if isinstance(op, IRGate2Q):
        return [op.q0, op.q1]
    if isinstance(op, IRMeasure):
        return [op.qubit]
    raise TypeError(type(op))


def schedule(circuit: LogicalCircuit) -> List[List[IROp]]:
    """Return a list of slots; each slot is a list of parallel operations."""
    qubit_time = [0] * circuit.num_qubits
    slots: List[List[IROp]] = []

    for op in circuit.ops:
        qubits = _qubits(op)
        start = max(qubit_time[q] for q in qubits)
        while len(slots) <= start:
            slots.append([])
        slots[start].append(op)
        for q in qubits:
            qubit_time[q] = start + 1

    return slots
