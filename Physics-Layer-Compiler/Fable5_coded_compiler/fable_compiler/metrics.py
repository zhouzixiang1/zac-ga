"""Program metric summaries."""

from __future__ import annotations

from dataclasses import dataclass


from .hardware import HardwareSpec
from .operations import (
    MEASURE,
    MOVE,
    ONEQ,
    TWOQ,
    HardwareProgram,
    MeasureOp,
    MoveOp,
    OneQubitGate,
    TwoQubitGate,
)


@dataclass(frozen=True)
class Metrics:
    stages: int
    total_time: float
    movement_distance: float
    num_moves: int
    num_move_batches: int
    num_2q: int
    rydberg_pulses: int
    cz_parallelism: float
    estimated_fidelity: float

    def as_dict(self) -> dict:
        return {
            "stages": self.stages,
            "total_time": self.total_time,
            "movement_distance": self.movement_distance,
            "num_moves": self.num_moves,
            "num_move_batches": self.num_move_batches,
            "num_2q": self.num_2q,
            "rydberg_pulses": self.rydberg_pulses,
            "cz_parallelism": self.cz_parallelism,
            "estimated_fidelity": self.estimated_fidelity,
        }


def summarize(program: HardwareProgram, hardware: HardwareSpec) -> Metrics:
    total_time = sum(stage.duration for stage in program.stages)
    fidelity = 1.0
    movement_distance = 0.0
    num_moves = 0
    num_move_batches = 0
    num_2q = 0
    rydberg_pulses = 0
    qubit_busy_time = [0.0 for _ in range(program.num_qubits)]

    for stage in program.stages:
        if stage.kind == MOVE:
            fidelity *= stage.fidelity
            num_move_batches += 1
            for op in stage.ops:
                if isinstance(op, MoveOp):
                    num_moves += 1
                    movement_distance += hardware.distance(op.src, op.dst)
                    qubit_busy_time[op.atom] += hardware.t_move_fixed * 2.0
        elif stage.kind == TWOQ:
            fidelity *= stage.fidelity
            rydberg_pulses += 1
            for op in stage.ops:
                if isinstance(op, TwoQubitGate):
                    num_2q += 1
                    qubit_busy_time[op.atom0] += stage.duration
                    qubit_busy_time[op.atom1] += stage.duration
        elif stage.kind == ONEQ:
            fidelity *= stage.fidelity
            for op in stage.ops:
                if isinstance(op, OneQubitGate):
                    qubit_busy_time[op.atom] += stage.duration
        elif stage.kind == MEASURE:
            fidelity *= stage.fidelity
            for op in stage.ops:
                if isinstance(op, MeasureOp):
                    qubit_busy_time[op.atom] += stage.duration
        else:
            fidelity *= stage.fidelity

    coherence = 1.0
    for busy_time in qubit_busy_time:
        idle_time = total_time - busy_time
        coherence *= 1 - idle_time / hardware.t2_coherence
    fidelity *= coherence

    return Metrics(
        stages=len(program.stages),
        total_time=total_time,
        movement_distance=movement_distance,
        num_moves=num_moves,
        num_move_batches=num_move_batches,
        num_2q=num_2q,
        rydberg_pulses=rydberg_pulses,
        cz_parallelism=(num_2q / rydberg_pulses) if rydberg_pulses else 0.0,
        estimated_fidelity=fidelity,
    )