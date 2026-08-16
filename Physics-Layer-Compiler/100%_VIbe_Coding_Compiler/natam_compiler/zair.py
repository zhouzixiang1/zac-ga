"""Export a :class:`~natam_compiler.operations.HardwareProgram` to a ZAIR
hardware-schedule dict.

ZAIR (the ZAC intermediate representation) is the JSON-like instruction format
consumed by the standalone :mod:`reverse_compiler` package living in the
sibling ``Reverse_Compiler`` folder.  By emitting ZAIR we get a fully
independent reverse compiler and round-trip verifier "for free".

Position encoding: ZAIR locations are ``[id, array, row, col]`` where ``array``
is the SLM/zone id.  We map the storage zone to array ``0`` and the
entanglement zone to array ``1``.
"""

from __future__ import annotations

from typing import Dict, List

from .hardware import ENTANGLEMENT, STORAGE, Position
from .operations import (
    HardwareProgram,
    InitOp,
    MeasureOp,
    MoveOp,
    OneQubitGate,
    TwoQubitGate,
)

STORAGE_ARRAY = 0
ENTANGLE_ARRAY = 1


def _loc(atom: int, pos: Position) -> List[int]:
    zone, row, col = pos
    array = STORAGE_ARRAY if zone == STORAGE else ENTANGLE_ARRAY
    return [int(atom), int(array), int(row), int(col)]


def to_zair(program: HardwareProgram, name: str = "natam") -> Dict:
    """Convert a compiled program into a ZAIR schedule dict."""
    instructions: List[Dict] = []

    for idx, stage in enumerate(program.stages):
        if stage.kind == "init":
            instructions.append(
                {
                    "type": "init",
                    "id": idx,
                    "begin_time": 0,
                    "end_time": 0,
                    "init_locs": [
                        _loc(op.atom, op.position)
                        for op in stage.ops
                        if isinstance(op, InitOp)
                    ],
                }
            )
        elif stage.kind == "move":
            moves = [op for op in stage.ops if isinstance(op, MoveOp)]
            if not moves:
                continue
            instructions.append(
                {
                    "type": "rearrangeJob",
                    "aod_qubits": [m.atom for m in moves],
                    "begin_locs": [_loc(m.atom, m.src) for m in moves],
                    "end_locs": [_loc(m.atom, m.dst) for m in moves],
                }
            )
        elif stage.kind == "1q":
            gates = []
            for op in stage.ops:
                if not isinstance(op, OneQubitGate):
                    continue
                gate = {"name": op.name, "q": op.atom}
                if op.params:
                    gate["params"] = list(op.params)
                gates.append(gate)
            instructions.append({"type": "1qGate", "unitary": "u", "gates": gates})
        elif stage.kind == "2q":
            gates = [
                {"q0": op.atom0, "q1": op.atom1, "name": op.name}
                for op in stage.ops
                if isinstance(op, TwoQubitGate)
            ]
            instructions.append({"type": "rydberg", "zone_id": 0, "gates": gates})
        elif stage.kind == "measure":
            meas = [op for op in stage.ops if isinstance(op, MeasureOp)]
            instructions.append(
                {
                    "type": "measure",
                    "qubits": [op.atom for op in meas],
                    "classical_bits": [op.clbit for op in meas],
                }
            )

    return {
        "name": name,
        "architecture_spec_path": None,
        "instructions": instructions,
    }
