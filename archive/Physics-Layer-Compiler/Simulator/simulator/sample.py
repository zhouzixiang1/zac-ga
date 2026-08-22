"""Built-in demo instruction lists in the compiler's ZAIR schedule format.

``sample_program`` returns a schedule that exercises every feature the simulator
supports — parallel movement, a parallel single-qubit layer, a parallel Rydberg
(two-qubit) layer, and measurement — so the GUI has something meaningful to show
without needing the full compiler + Qiskit stack installed.

``from_compiler`` optionally builds a schedule directly from ``natam_compiler``
if that sibling package (and Qiskit) are importable.
"""

from __future__ import annotations

from typing import Any

from .hardware import ENTANGLE_ARRAY, STORAGE_ARRAY, Site


def _move(pairs: list[tuple[int, Site, Site]]) -> dict[str, Any]:
    return {
        "type": "rearrangeJob",
        "aod_qubits": [a for a, _, _ in pairs],
        "begin_locs": [s.as_loc(a) for a, s, _ in pairs],
        "end_locs": [d.as_loc(a) for a, _, d in pairs],
    }


def _1q(gates: list[tuple[str, int, list[float]]]) -> dict[str, Any]:
    return {
        "type": "1qGate",
        "unitary": "u",
        "gates": [
            {"name": name, "q": q, **({"params": p} if p else {})}
            for name, q, p in gates
        ],
    }


def _rydberg(pairs: list[tuple[int, int]], name: str = "cz") -> dict[str, Any]:
    return {
        "type": "rydberg",
        "zone_id": 0,
        "gates": [{"q0": a, "q1": b, "name": name} for a, b in pairs],
    }


def sample_program(invalid: bool = False) -> dict[str, Any]:
    """A 4-atom demo schedule.

    The four atoms sit in a 2x2 storage block (columns 0/1, rows 0/1).  Flow:
    parallel single-qubit prep → transport each *column* into the entanglement
    zone (one batch per column, so every atom on a shared AOD column tone moves
    together) → parallel Rydberg CZ on both column pairs → a parallel
    single-qubit layer → transport back to storage → measure all.

    Because each batch moves a whole column, the crossed-AOD tone product yields
    no ghost tweezers, so the schedule is fully AOD-feasible.

    With ``invalid=True`` the return-transport batch is corrupted into a
    destination conflict so the validator has something to flag.
    """
    S = STORAGE_ARRAY
    E = ENTANGLE_ARRAY

    # 2x2 storage block: columns 0/1 share AOD column tones with the CZ sites.
    s0 = Site(S, 0, 0)
    s1 = Site(S, 1, 0)
    s2 = Site(S, 0, 1)
    s3 = Site(S, 1, 1)

    # CZ pairs share a column across rows 0/1:  (0,1) in column 0, (2,3) in column 1
    e0 = Site(E, 0, 0)
    e1 = Site(E, 1, 0)
    e2 = Site(E, 0, 1)
    e3 = Site(E, 1, 1)

    init_locs = [s0.as_loc(0), s1.as_loc(1), s2.as_loc(2), s3.as_loc(3)]

    instructions: list[dict[str, Any]] = [
        {"type": "init", "init_locs": init_locs},
        # parallel single-qubit preparation
        _1q([("sx", 0, []), ("sx", 1, []), ("rz", 2, [1.5708]), ("sx", 3, [])]),
        # transport column 0 (atoms 0,1) into the entanglement zone together
        _move([(0, s0, e0), (1, s1, e1)]),
        # transport column 1 (atoms 2,3) into the entanglement zone together
        _move([(2, s2, e2), (3, s3, e3)]),
        # parallel Rydberg CZ on both column pairs
        _rydberg([(0, 1), (2, 3)]),
        # parallel single-qubit layer while in the entanglement zone
        _1q([("rz", 0, [0.7854]), ("rz", 2, [0.7854])]),
        # transport column 0 back to storage
        _move([(0, e0, s0), (1, e1, s1)]),
    ]

    if invalid:
        # atoms 2 and 3 both target the same storage site -> destination conflict
        instructions.append(_move([(2, e2, s2), (3, e3, s2)]))
    else:
        instructions.append(_move([(2, e2, s2), (3, e3, s3)]))

    instructions.append({"type": "measure", "qubits": [0, 1, 2, 3]})

    return {
        "name": "simulator_demo" + ("_invalid" if invalid else ""),
        "architecture_spec_path": None,
        "instructions": instructions,
    }


def from_compiler(circuit=None) -> dict[str, Any]:
    """Compile a Qiskit circuit to a ZAIR schedule via ``natam_compiler``.

    Requires the sibling ``100%_VIbe_Coding_Compiler`` package and Qiskit to be
    importable.  Falls back to raising ``ImportError`` if unavailable.
    """
    import os
    import sys

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pkg_root = os.path.join(here, "100%_VIbe_Coding_Compiler")
    if pkg_root not in sys.path and os.path.isdir(pkg_root):
        sys.path.insert(0, pkg_root)

    from natam_compiler.compiler import compile_circuit  # type: ignore
    from natam_compiler.zair import to_zair  # type: ignore

    if circuit is None:
        from qiskit import QuantumCircuit  # type: ignore

        circuit = QuantumCircuit(4)
        circuit.h(0)
        circuit.cx(0, 1)
        circuit.cx(1, 2)
        circuit.cx(2, 3)
        circuit.measure_all()

    result = compile_circuit(circuit)
    program = getattr(result, "program", result)
    return to_zair(program, name="compiler_output")
