"""ZAIR schedule export."""

from __future__ import annotations

from typing import Dict, List

from .hardware import ENTANGLEMENT, STORAGE, Position
from .operations import HardwareProgram, InitOp, MeasureOp, MoveOp, OneQubitGate, TwoQubitGate

STORAGE_ARRAY = 0
ENTANGLEMENT_ARRAY = 1


def _loc(atom: int, pos: Position) -> List[int]:
    zone, col, row = pos
    array = STORAGE_ARRAY if zone == STORAGE else ENTANGLEMENT_ARRAY
    return [int(atom), int(array), int(row), int(col)]


def to_zair(program: HardwareProgram, name: str = "fable") -> Dict:
    instructions: List[Dict] = []
    for index, stage in enumerate(program.stages):
        if stage.kind == "init":
            instructions.append(
                {
                    "type": "init",
                    "id": index,
                    "begin_time": 0,
                    "end_time": 0,
                    "init_locs": [
                        _loc(op.atom, op.position) for op in stage.ops if isinstance(op, InitOp)
                    ],
                }
            )
        elif stage.kind == "move":
            moves = [op for op in stage.ops if isinstance(op, MoveOp)]
            instructions.append(
                {
                    "type": "rearrangeJob",
                    "aod_qubits": [op.atom for op in moves],
                    "begin_locs": [_loc(op.atom, op.src) for op in moves],
                    "end_locs": [_loc(op.atom, op.dst) for op in moves],
                }
            )
        elif stage.kind == "1q":
            gates = []
            for op in stage.ops:
                if isinstance(op, OneQubitGate):
                    gate = {"name": op.name, "q": op.atom}
                    if op.params:
                        gate["params"] = list(op.params)
                    gates.append(gate)
            instructions.append({"type": "1qGate", "unitary": "u3", "gates": gates})
        elif stage.kind == "2q":
            instructions.append(
                {
                    "type": "rydberg",
                    "zone_id": 0,
                    "gates": [
                        {"name": op.name, "q0": op.atom0, "q1": op.atom1}
                        for op in stage.ops
                        if isinstance(op, TwoQubitGate)
                    ],
                }
            )
        elif stage.kind == "measure":
            measures = [op for op in stage.ops if isinstance(op, MeasureOp)]
            instructions.append(
                {
                    "type": "measure",
                    "qubits": [op.atom for op in measures],
                    "classical_bits": [op.clbit for op in measures],
                }
            )
    return {
        "name": name,
        "architecture_spec_path": None,
        "hardware": "storage 40x20, entanglement 2x20 right of storage",
        "instructions": instructions,
    }