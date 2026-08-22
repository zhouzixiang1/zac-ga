"""Executable hardware-operation model.

A compiled program is an ordered list of :class:`Stage` objects.  Each stage
holds a set of operations that the machine executes *in parallel*.  The five
operation kinds map directly onto neutral-atom hardware primitives:

* :class:`InitOp`   -- declare the initial atom -> site assignment,
* :class:`MoveOp`   -- AOD transport of an atom between sites,
* :class:`OneQubitGate` -- a local single-qubit rotation,
* :class:`TwoQubitGate` -- a Rydberg CZ between two atoms in the entanglement zone,
* :class:`MeasureOp` -- projective readout.

Operations reference physical :data:`~natam_compiler.hardware.Position` values
*and* the logical atom id, so the sequence is both executable and verifiable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .hardware import Position

# Stage kinds
INIT = "init"
MOVE = "move"
ONEQ = "1q"
TWOQ = "2q"
MEASURE = "measure"


@dataclass
class InitOp:
    atom: int
    position: Position


@dataclass
class MoveOp:
    atom: int
    src: Position
    dst: Position


@dataclass
class OneQubitGate:
    name: str                       # e.g. "u3", "h", "rz"
    params: Tuple[float, ...]
    atom: int
    position: Position


@dataclass
class TwoQubitGate:
    name: str                       # always "cz" in this compiler
    atom0: int
    atom1: int
    pos0: Position
    pos1: Position


@dataclass
class MeasureOp:
    atom: int
    position: Position
    clbit: int


@dataclass
class Stage:
    kind: str
    ops: List[object] = field(default_factory=list)
    duration: float = 0.0           # microseconds
    fidelity: float = 1.0           # success probability of this stage


@dataclass
class HardwareProgram:
    """An executable, hardware-level operation sequence."""

    num_qubits: int
    num_clbits: int
    stages: List[Stage] = field(default_factory=list)

    # ------------------------------------------------------------------ utils
    def add(self, stage: Stage) -> None:
        self.stages.append(stage)

    def operations(self):
        """Iterate over (stage, op) pairs in execution order."""
        for stage in self.stages:
            for op in stage.ops:
                yield stage, op

    def count(self, op_type) -> int:
        return sum(isinstance(op, op_type) for _, op in self.operations())

    # ------------------------------------------------------------- text dump
    def to_text(self) -> str:
        lines: List[str] = []
        for i, stage in enumerate(self.stages):
            lines.append(
                f"# stage {i:>3}  {stage.kind:<8} "
                f"t={stage.duration:8.2f}us  F={stage.fidelity:.6f}"
            )
            for op in stage.ops:
                lines.append("    " + _op_to_text(op))
        return "\n".join(lines)


def _fmt_pos(pos: Position) -> str:
    z, r, c = pos
    return f"{z[0]}({r},{c})"


def _op_to_text(op) -> str:
    if isinstance(op, InitOp):
        return f"INIT   a{op.atom} @ {_fmt_pos(op.position)}"
    if isinstance(op, MoveOp):
        return f"MOVE   a{op.atom} {_fmt_pos(op.src)} -> {_fmt_pos(op.dst)}"
    if isinstance(op, OneQubitGate):
        p = ",".join(f"{x:.3f}" for x in op.params)
        return f"1Q     {op.name}({p}) a{op.atom} @ {_fmt_pos(op.position)}"
    if isinstance(op, TwoQubitGate):
        return (
            f"2Q     {op.name} a{op.atom0}@{_fmt_pos(op.pos0)} "
            f"a{op.atom1}@{_fmt_pos(op.pos1)}"
        )
    if isinstance(op, MeasureOp):
        return f"MEAS   a{op.atom} @ {_fmt_pos(op.position)} -> c{op.clbit}"
    return repr(op)
