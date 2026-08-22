"""Dependency-preserving scheduler with homogeneous hardware shots."""

from __future__ import annotations

from typing import List

from .hardware import default_hardware
from .ir import Gate1Q, Gate2Q, LogicalCircuit, LogicalOp, Measure


def touched(op: LogicalOp) -> set[int]:
    if isinstance(op, Gate1Q):
        return {op.qubit}
    if isinstance(op, Gate2Q):
        return {op.q0, op.q1}
    if isinstance(op, Measure):
        return {op.qubit}
    raise TypeError(type(op))


def _is_2q_shot_op(op: LogicalOp) -> bool:
    return isinstance(op, Gate2Q)


def schedule_1q(logical: LogicalCircuit) -> List[List[LogicalOp]]:
    slots: List[List[LogicalOp]] = []
    qubit_next_slot = [0 for _ in range(logical.num_qubits)]
    for op in logical.ops:
        qs = touched(op)
        slot_index = max(qubit_next_slot[q] for q in qs)
        while len(slots) <= slot_index:
            slots.append([])
        slots[slot_index].append(op)
        for q in qs:
            qubit_next_slot[q] = slot_index + 1
    return slots


def schedule(logical: LogicalCircuit) -> List[List[LogicalOp]]:
    """Schedule ops into homogeneous shots.

    Each shot contains either only 2Q gates, or only 1Q/measure operations.
    The scheduler preserves per-qubit program order, then scans forward through
    the ready operations and packs as many non-conflicting operations of the
    same shot type as possible. For 2Q shots, the number of gates in one layer
    is capped by the entanglement-zone row capacity.
    """
    ops = logical.ops
    if not ops:
        return []

    hw = default_hardware()
    twoq_layer_capacity = hw.interaction_rows()

    qubits_by_op = [touched(op) for op in ops]
    dependencies_left = [0 for _ in ops]
    successors: List[List[int]] = [[] for _ in ops]
    previous_on_qubit: list[int | None] = [None for _ in range(logical.num_qubits)]

    for index, qs in enumerate(qubits_by_op):
        predecessors = {previous_on_qubit[q] for q in qs if previous_on_qubit[q] is not None}
        dependencies_left[index] = len(predecessors)
        for predecessor in predecessors:
            successors[predecessor].append(index)
        for q in qs:
            previous_on_qubit[q] = index

    slots: List[List[LogicalOp]] = []
    unscheduled = set(range(len(ops)))

    while unscheduled:
        ready = [index for index in sorted(unscheduled) if dependencies_left[index] == 0]
        shot_is_2q = _is_2q_shot_op(ops[ready[0]])
        used_qubits: set[int] = set()
        slot_indices: List[int] = []
        max_ops = twoq_layer_capacity if shot_is_2q else None

        for index in ready:
            if _is_2q_shot_op(ops[index]) != shot_is_2q:
                continue
            if max_ops is not None and len(slot_indices) >= 1.0 * max_ops:
                break
            qs = qubits_by_op[index]
            if qs.isdisjoint(used_qubits):
                slot_indices.append(index)
                used_qubits.update(qs)

        slots.append([ops[index] for index in slot_indices])
        for index in slot_indices:
            unscheduled.remove(index)
            for successor in successors[index]:
                dependencies_left[successor] -= 1

    return slots
