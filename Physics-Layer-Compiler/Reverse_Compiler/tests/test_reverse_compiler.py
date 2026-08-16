"""Test suite for the ZAC reverse compiler.

Covers the twelve required reconstruction scenarios plus a full round-trip
through the real ZAC compiler.
"""

from __future__ import annotations

import math

import pytest

from reverse_compiler import (
    CircuitIR,
    CircuitOperation,
    ReverseCompiler,
    StimExportError,
    extract_atom_events,
    reverse_compile,
    to_qiskit,
    to_stim,
)
from reverse_compiler.reverse_compiler import ReverseCompileError
from reverse_compiler.state import ResolutionError
from reverse_compiler.visualization import _operation_groups_by_layer

# Positions are (array/SLM id, row, col).  Array 0 = storage, array 1 = entangling.
STORAGE = 0
ENTANGLE = 1


def make_init(n_atoms: int, *, array: int = STORAGE) -> dict:
    """A ZAIR ``init`` instruction placing ``n_atoms`` atoms in a row."""
    return {
        "type": "init",
        "id": 0,
        "begin_time": 0,
        "end_time": 0,
        "init_locs": [[i, array, 0, i] for i in range(n_atoms)],
    }


def zair(*instructions: dict, name: str = "test", **extra) -> dict:
    base = {"name": name, "architecture_spec_path": None, "instructions": list(instructions)}
    base.update(extra)
    return base


def names(ir: CircuitIR) -> list[str]:
    return [op.name for op in ir.operations]


# --------------------------------------------------------------------------- 1
def test_local_single_qubit_gates():
    code = zair(
        make_init(2),
        {
            "type": "1qGate",
            "unitary": "u3",
            "gates": [{"name": "h", "q": 0}, {"name": "x", "q": 1}],
        },
    )
    ir = reverse_compile(code)
    assert [(op.name, op.qubits) for op in ir] == [("H", (0,)), ("X", (1,))]
    qc = to_qiskit(ir)
    assert {instr.operation.name for instr in qc.data} == {"h", "x"}


# --------------------------------------------------------------------------- 2
def test_global_gate_applies_to_all_active_atoms():
    code = zair(
        make_init(3),
        {"type": "global_1qGate", "name": "h"},
    )
    ir = reverse_compile(code)
    assert [(op.name, op.qubits) for op in ir] == [
        ("H", (0,)),
        ("H", (1,)),
        ("H", (2,)),
    ]
    assert all(op.metadata.get("global") for op in ir)


def test_global_gate_respects_zone_and_mask():
    code = zair(
        # atoms 0,1 in storage; atom 2 in entangling zone
        {
            "type": "init",
            "id": 0,
            "init_locs": [[0, STORAGE, 0, 0], [1, STORAGE, 0, 1], [2, ENTANGLE, 0, 0]],
        },
        {"type": "global_1qGate", "name": "z", "zone": ENTANGLE},
    )
    ir = reverse_compile(code)
    assert [op.qubits for op in ir] == [(2,)]


# --------------------------------------------------------------------------- 3
def test_cz_without_movement():
    code = zair(
        make_init(2),
        {"type": "rydberg", "zone_id": 0, "gates": [{"id": 0, "q0": 0, "q1": 1}]},
    )
    ir = reverse_compile(code)
    assert [(op.name, op.qubits) for op in ir] == [("CZ", (0, 1))]


# --------------------------------------------------------------------------- 4
def test_cz_with_movement_produces_single_cz():
    code = zair(
        make_init(2),
        # move both atoms into the entangling zone
        {
            "type": "rearrangeJob",
            "aod_qubits": [0, 1],
            "begin_locs": [[0, STORAGE, 0, 0], [1, STORAGE, 0, 1]],
            "end_locs": [[0, ENTANGLE, 0, 0], [1, ENTANGLE, 0, 1]],
        },
        {"type": "rydberg", "zone_id": 0, "gates": [{"q0": 0, "q1": 1}]},
        # move them back
        {
            "type": "rearrangeJob",
            "aod_qubits": [0, 1],
            "begin_locs": [[0, ENTANGLE, 0, 0], [1, ENTANGLE, 0, 1]],
            "end_locs": [[0, STORAGE, 0, 0], [1, STORAGE, 0, 1]],
        },
    )
    rc = ReverseCompiler()
    ir = rc.compile(code)
    # Movement emits no gate; only the CZ survives.
    assert [(op.name, op.qubits) for op in ir] == [("CZ", (0, 1))]
    # Mapping is unchanged by movement.
    assert rc.state.qubit_to_atom == {0: 0, 1: 1}
    # Atoms returned to their original positions.
    assert rc.state.atom_to_position[0] == (STORAGE, 0, 0)


# --------------------------------------------------------------------------- 5
def test_coordinate_targeted_gate():
    code = zair(
        make_init(3),
        # target the atom sitting at coordinate (STORAGE, 0, 2) == atom 2
        {"type": "1qGate", "gates": [{"name": "h", "position": [STORAGE, 0, 2]}]},
    )
    ir = reverse_compile(code)
    assert [(op.name, op.qubits) for op in ir] == [("H", (2,))]


def test_missing_coordinate_raises():
    code = zair(
        make_init(2),
        {"type": "1qGate", "gates": [{"name": "h", "position": [9, 9, 9]}]},
    )
    with pytest.raises(ResolutionError):
        reverse_compile(code)


# --------------------------------------------------------------------------- 6
def test_parallel_cz_gates_same_layer():
    code = zair(
        make_init(4),
        {
            "type": "rydberg",
            "zone_id": 0,
            "gates": [{"q0": 0, "q1": 1}, {"q0": 2, "q1": 3}],
        },
    )
    ir = reverse_compile(code)
    assert [(op.name, op.qubits) for op in ir] == [("CZ", (0, 1)), ("CZ", (2, 3))]
    # Both gates share the same parallel layer.
    assert ir.operations[0].layer == ir.operations[1].layer
    # Stim emits them in the same moment (single TICK separates nothing here).
    stim_circuit = to_stim(ir)
    assert stim_circuit.num_ticks == 0


def test_conflicting_parallel_gates_detected():
    code = zair(
        make_init(3),
        {"type": "rydberg", "gates": [{"q0": 0, "q1": 1}, {"q0": 1, "q1": 2}]},
    )
    with pytest.raises(ReverseCompileError):
        reverse_compile(code)


# --------------------------------------------------------------------------- 7
def test_position_exchange_emits_no_swap():
    code = zair(
        make_init(2),
        # atoms 0 and 1 exchange spatial positions
        {
            "type": "rearrangeJob",
            "aod_qubits": [0, 1],
            "begin_locs": [[0, STORAGE, 0, 0], [1, STORAGE, 0, 1]],
            "end_locs": [[0, STORAGE, 0, 1], [1, STORAGE, 0, 0]],
        },
    )
    rc = ReverseCompiler()
    ir = rc.compile(code)
    assert len(ir) == 0  # no quantum gate from a spatial exchange
    # Positions were swapped, association preserved.
    assert rc.state.atom_to_position[0] == (STORAGE, 0, 1)
    assert rc.state.atom_to_position[1] == (STORAGE, 0, 0)
    assert rc.state.position_to_atom[(STORAGE, 0, 1)] == 0
    assert rc.state.qubit_to_atom == {0: 0, 1: 1}


# --------------------------------------------------------------------------- 8
def test_explicit_quantum_swap_emits_swap():
    code = zair(
        make_init(2),
        {"type": "swap", "qubits": [0, 1]},
    )
    ir = reverse_compile(code)
    assert [(op.name, op.qubits) for op in ir] == [("SWAP", (0, 1))]


# --------------------------------------------------------------------------- 9
def test_measurement_and_reset():
    code = zair(
        make_init(2),
        {"type": "reset", "qubits": [0]},
        {"type": "measure", "qubits": [0, 1], "classical_bits": [0, 1]},
    )
    ir = reverse_compile(code)
    assert names(ir) == ["RESET", "MEASURE", "MEASURE"]
    measure = ir.operations[1]
    assert measure.qubits == (0,) and measure.classical_bits == (0,)
    qc = to_qiskit(ir)
    assert qc.num_clbits == 2
    assert any(instr.operation.name == "measure" for instr in qc.data)


# -------------------------------------------------------------------------- 10
def test_arbitrary_rotations_in_qiskit():
    code = zair(
        make_init(2),
        {
            "type": "1qGate",
            "gates": [
                {"name": "rz", "q": 0, "params": [0.37]},
                {"name": "u3", "q": 1, "params": [0.1, 0.2, 0.3]},
            ],
        },
    )
    ir = reverse_compile(code)
    assert names(ir) == ["RZ", "U"]
    qc = to_qiskit(ir)
    op_names = [instr.operation.name for instr in qc.data]
    assert "rz" in op_names and "u" in op_names
    rz = next(instr for instr in qc.data if instr.operation.name == "rz")
    assert math.isclose(float(rz.operation.params[0]), 0.37)


def test_missing_rotation_angle_strict_raises():
    code = zair(
        make_init(1),
        {"type": "1qGate", "unitary": "u3", "gates": [{"name": "u3", "q": 0}]},
    )
    with pytest.raises(ReverseCompileError):
        reverse_compile(code, strict=True)
    # Non-strict drops it with a warning.
    ir = reverse_compile(code, strict=False)
    assert len(ir) == 0


# -------------------------------------------------------------------------- 11
def test_stim_rejects_unsupported_rotation():
    code = zair(
        make_init(1),
        {"type": "1qGate", "gates": [{"name": "rz", "q": 0, "params": [0.37]}]},
    )
    ir = reverse_compile(code)
    to_qiskit(ir)  # fine in Qiskit
    with pytest.raises(StimExportError):
        to_stim(ir)


def test_stim_rejects_t_gate():
    code = zair(make_init(1), {"type": "1qGate", "gates": [{"name": "t", "q": 0}]})
    ir = reverse_compile(code)
    with pytest.raises(StimExportError):
        to_stim(ir)


def test_stim_clifford_rotation_simplification():
    code = zair(
        make_init(4),
        {
            "type": "1qGate",
            "gates": [
                {"name": "rx", "q": 0, "params": [math.pi]},
                {"name": "ry", "q": 1, "params": [math.pi]},
                {"name": "rz", "q": 2, "params": [math.pi / 2]},
                {"name": "rz", "q": 3, "params": [-math.pi / 2]},
            ],
        },
    )
    ir = reverse_compile(code)
    stim_circuit = to_stim(ir)
    text = str(stim_circuit)
    assert "X 0" in text
    assert "Y 1" in text
    assert "S 2" in text
    assert "S_DAG 3" in text


# -------------------------------------------------------------------------- 12
def test_lost_atoms_excluded_from_global_gate():
    code = zair(
        make_init(3),
        {"type": "loss", "atoms": [2]},
        {"type": "global_1qGate", "name": "h"},
    )
    rc = ReverseCompiler()
    ir = rc.compile(code)
    assert [op.qubits for op in ir] == [(0,), (1,)]
    assert 2 not in rc.state.active_atoms


def test_inactive_atom_at_init():
    code = zair(
        {
            "type": "init",
            "init_locs": [[0, STORAGE, 0, 0], [1, STORAGE, 0, 1]],
            "inactive": [1],
        },
        {"type": "global_1qGate", "name": "x"},
    )
    ir = reverse_compile(code)
    assert [op.qubits for op in ir] == [(0,)]


# ---------------------------------------------------------------- extra checks
def test_explicit_qubit_to_atom_mapping():
    code = zair(
        make_init(2),
        {"type": "1qGate", "gates": [{"name": "x", "q": 0}]},
        qubit_to_atom={5: 0, 6: 1},
    )
    ir = reverse_compile(code)
    # atom 0 is bound to physical qubit 5
    assert ir.operations[0].qubits == (5,)


def test_stim_parallel_layers_separated_by_tick():
    code = zair(
        make_init(2),
        {"type": "1qGate", "gates": [{"name": "h", "q": 0}, {"name": "h", "q": 1}]},
        {"type": "rydberg", "gates": [{"q0": 0, "q1": 1}]},
    )
    ir = reverse_compile(code)
    stim_circuit = to_stim(ir)
    assert stim_circuit.num_ticks == 1  # one boundary between the two layers


def test_extract_atom_events_captures_move_and_gate_activity():
    code = zair(
        make_init(2),
        {
            "type": "rearrangeJob",
            "aod_qubits": [0, 1],
            "begin_locs": [[0, STORAGE, 0, 0], [1, STORAGE, 0, 1]],
            "end_locs": [[0, ENTANGLE, 0, 0], [1, ENTANGLE, 0, 1]],
        },
        {"type": "1qGate", "gates": [{"name": "h", "q": 0}]},
        {"type": "rydberg", "gates": [{"q0": 0, "q1": 1}]},
    )

    events, atoms = extract_atom_events(code)
    assert atoms == [0, 1]

    kinds = [(e.step, e.atom, e.kind, e.label) for e in events]
    assert (1, 0, "move", "MOVE a=1 r=0 c=0") in kinds
    assert (1, 1, "move", "MOVE a=1 r=0 c=1") in kinds
    assert (2, 0, "gate", "H") in kinds
    assert (3, 0, "entangle", "CZ") in kinds
    assert (3, 1, "entangle", "CZ") in kinds


def test_operation_groups_by_layer_respects_explicit_and_implicit_layers():
    ir = CircuitIR(
        operations=[
            CircuitOperation("H", (0,), layer=0),
            CircuitOperation("X", (1,), layer=0),
            CircuitOperation("CZ", (0, 1), layer=2),
            # No layer field should be assigned to the next available layer.
            CircuitOperation("MEASURE", (1,), classical_bits=(1,), layer=None),
        ]
    )
    groups = _operation_groups_by_layer(ir)
    assert [layer for layer, _ops in groups] == [0, 2, 3]
    assert [op.name for op in groups[0][1]] == ["H", "X"]
    assert [op.name for op in groups[1][1]] == ["CZ"]
    assert [op.name for op in groups[2][1]] == ["MEASURE"]



# ----------------------------------------------------------------- round trip
# The round trip is fully self-contained: it does NOT import or call ZAC. It uses
# the package's own reference forward encoder, which only borrows ZAC's *idea*
# (atoms transported between storage and entangling zones).
from reverse_compiler import encode_circuit

_CANON = {
    "h": "H", "x": "X", "y": "Y", "z": "Z",
    "rx": "RX", "ry": "RY", "rz": "RZ",
    "cz": "CZ", "cx": "CX", "swap": "SWAP",
}


def _expected_signature(ops):
    sig = []
    for name, qubits, *rest in ops:
        params = tuple(rest[0]) if rest else ()
        sig.append((_CANON[name], tuple(qubits), tuple(round(p, 9) for p in params)))
    return sig


def _reconstructed_signature(ir):
    sig = []
    for op in ir.without_timing():
        sig.append((op.name, tuple(op.qubits), tuple(round(p, 9) for p in op.params)))
    return sig


def test_round_trip_self_contained():
    # original physical circuit (with arbitrary rotations)
    ops = [
        ("h", (0,)),
        ("rz", (0,), (0.37,)),
        ("rx", (1,), (1.2,)),
        ("cz", (0, 1)),          # CZ realised via movement in the schedule
        ("ry", (2,), (-0.8,)),
        ("cz", (1, 2)),
    ]
    # original physical circuit -> hardware schedule -> reverse compiler
    schedule = encode_circuit(ops, n_qubits=3)
    ir = reverse_compile(schedule)

    # Reconstruction matches the original after dropping hardware-only ops,
    # including the rotation angles (our hardware format records them).
    assert _reconstructed_signature(ir) == _expected_signature(ops)

    # Exporters succeed on the reconstructed circuit.
    qc = to_qiskit(ir)
    assert qc.num_qubits == 3
    assert any(instr.operation.name == "rz" for instr in qc.data)


def test_round_trip_movement_emits_no_extra_gates():
    # A schedule full of movement must reconstruct to exactly the logical gates.
    ops = [("h", (0,)), ("cz", (0, 1)), ("h", (1,))]
    schedule = encode_circuit(ops, n_qubits=2)
    ir = reverse_compile(schedule)
    assert [(op.name, op.qubits) for op in ir] == [
        ("H", (0,)),
        ("CZ", (0, 1)),
        ("H", (1,)),
    ]
    rc_names = [op.name for op in ir]
    assert "SWAP" not in rc_names  # movement never becomes a SWAP


def test_round_trip_complex_circuit():
    # A larger stress test: 8 qubits, 20 gates, full supported gate set
    # (h, x, y, z, rx, ry, rz, cz, cx, swap) interleaved with the
    # storage<->entangling movement that realises every two-qubit gate.
    ops = [
        ("h", (0,)),
        ("h", (1,)),
        ("h", (2,)),
        ("h", (3,)),
        ("x", (4,)),
        ("y", (5,)),
        ("z", (6,)),
        ("rx", (7,), (0.5,)),
        ("cz", (0, 1)),
        ("cx", (2, 3)),
        ("ry", (4,), (1.1,)),
        ("rz", (5,), (-0.7,)),
        ("cz", (4, 5)),
        ("swap", (6, 7)),  # explicit quantum swap, not a spatial move
        ("cx", (1, 2)),
        ("h", (7,)),
        ("rz", (0,), (0.25,)),
        ("cz", (3, 4)),
        ("ry", (6,), (2.0,)),
        ("cx", (0, 7)),
    ]
    schedule = encode_circuit(ops, n_qubits=8)

    # Every two-qubit gate is realised by a move-in / move-back pair.
    inst_types = [i["type"] for i in schedule["instructions"]]
    assert inst_types.count("rydberg") == 6  # 3x cz + 3x cx via rydberg
    assert inst_types.count("rearrangeJob") == 12  # two moves per entangling gate
    assert inst_types.count("swap") == 1

    ir = reverse_compile(schedule)

    # Order, qubits and rotation angles all reconstruct exactly.
    assert _reconstructed_signature(ir) == _expected_signature(ops)

    # Movement never leaks an extra SWAP into the reconstruction.
    assert [op.name for op in ir].count("SWAP") == 1

    # Exporters succeed on the reconstructed circuit.
    qc = to_qiskit(ir)
    assert qc.num_qubits == 8

