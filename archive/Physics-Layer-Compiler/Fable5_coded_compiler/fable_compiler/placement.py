"""Initial atom placement for storage."""

from __future__ import annotations

from typing import Dict

from .hardware import STORAGE, HardwareSpec, Position
from .ir import Gate2Q, LogicalCircuit


def place(logical: LogicalCircuit, hardware: HardwareSpec) -> Dict[int, Position]:
    if logical.num_qubits > hardware.storage.capacity:
        raise ValueError(
            f"circuit needs {logical.num_qubits} qubits, storage holds {hardware.storage.capacity}"
        )
    homes: Dict[int, Position] = {}
    occupied: set[Position] = set()

    matched: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for op in logical.ops:
        if isinstance(op, Gate2Q) and op.q0 not in matched and op.q1 not in matched:
            pairs.append((op.q0, op.q1))
            matched.update({op.q0, op.q1})

    for pair_index, (q0, q1) in enumerate(pairs):
        row = pair_index % hardware.storage.rows
        col = 2 * (pair_index // hardware.storage.rows)
        if col + 1 >= hardware.storage.cols:
            raise ValueError("not enough adjacent storage columns for interacting pairs")
        pos0 = (STORAGE, col, row)
        pos1 = (STORAGE, col + 1, row)
        homes[q0] = pos0
        homes[q1] = pos1
        occupied.update({pos0, pos1})

    next_index = 0
    for qubit in range(logical.num_qubits):
        if qubit in homes:
            continue
        while True:
            pos = hardware.storage_index_to_site(next_index)
            next_index += 1
            if pos not in occupied:
                homes[qubit] = pos
                occupied.add(pos)
                break

    return homes