"""Performance metrics for a compiled program."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict

from .hardware import HardwareSpec
from .operations import (
    MeasureOp,
    MoveOp,
    OneQubitGate,
    MOVE,
    TWOQ,
    TwoQubitGate,
    HardwareProgram,
)


@dataclass
class Metrics:
    num_qubits: int
    num_stages: int
    total_time_us: float
    estimated_fidelity: float
    movement_distance: float
    num_1q_gates: int
    num_2q_gates: int
    num_moves: int
    num_measurements: int
    twoq_parallelism: float        # avg CZ gates per Rydberg stage
    twoq_stage_count: int
    num_move_batches: int          # number of parallel AOD movement batches
    move_parallelism: float        # avg atoms moved per batch

    def as_dict(self) -> Dict:
        return asdict(self)


def summarize(program: HardwareProgram, hw: HardwareSpec) -> Metrics:
    total_time = 0.0
    fidelity = 1.0
    movement = 0.0
    n1q = n2q = nmove = nmeas = 0
    twoq_stages = 0
    move_batches = 0

    for stage in program.stages:
        total_time += stage.duration
        fidelity *= stage.fidelity
        if stage.kind == TWOQ and stage.ops:
            twoq_stages += 1
        if stage.kind == MOVE and stage.ops:
            move_batches += 1
        for op in stage.ops:
            if isinstance(op, OneQubitGate):
                n1q += 1
            elif isinstance(op, TwoQubitGate):
                n2q += 1
            elif isinstance(op, MoveOp):
                nmove += 1
                movement += hw.distance(op.src, op.dst)
            elif isinstance(op, MeasureOp):
                nmeas += 1

    parallelism = (n2q / twoq_stages) if twoq_stages else 0.0
    move_parallelism = (nmove / move_batches) if move_batches else 0.0
    return Metrics(
        num_qubits=program.num_qubits,
        num_stages=len(program.stages),
        total_time_us=total_time,
        estimated_fidelity=fidelity,
        movement_distance=movement,
        num_1q_gates=n1q,
        num_2q_gates=n2q,
        num_moves=nmove,
        num_measurements=nmeas,
        twoq_parallelism=parallelism,
        twoq_stage_count=twoq_stages,
        num_move_batches=move_batches,
        move_parallelism=move_parallelism,
    )
