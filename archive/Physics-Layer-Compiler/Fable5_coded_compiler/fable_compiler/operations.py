"""Hardware operation data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Tuple

from .hardware import Position

INIT = "init"
MOVE = "move"
ONEQ = "1q"
TWOQ = "2q"
MEASURE = "measure"


@dataclass(frozen=True)
class InitOp:
    atom: int
    position: Position


@dataclass(frozen=True)
class MoveOp:
    atom: int
    src: Position
    dst: Position


@dataclass(frozen=True)
class OneQubitGate:
    name: str
    atom: int
    position: Position
    params: Tuple[float, ...] = ()


@dataclass(frozen=True)
class TwoQubitGate:
    name: str
    atom0: int
    atom1: int
    pos0: Position
    pos1: Position


@dataclass(frozen=True)
class MeasureOp:
    atom: int
    position: Position
    clbit: int


@dataclass
class Stage:
    kind: str
    ops: List[object] = field(default_factory=list)
    duration: float = 0.0
    fidelity: float = 1.0


@dataclass
class HardwareProgram:
    num_qubits: int
    num_clbits: int = 0
    stages: List[Stage] = field(default_factory=list)

    def add(self, stage: Stage) -> None:
        if stage.ops or stage.kind == INIT:
            self.stages.append(stage)

    def all_moves(self) -> Iterable[MoveOp]:
        for stage in self.stages:
            for op in stage.ops:
                if isinstance(op, MoveOp):
                    yield op