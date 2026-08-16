"""Reference forward encoder: physical circuit -> hardware schedule.

This is a small, **self-contained** "forward compiler" that turns a physical-qubit
circuit into our hardware-instruction schedule (the same JSON-like dict the
:class:`~reverse_compiler.reverse_compiler.ReverseCompiler` consumes).

It deliberately borrows ZAC's *idea* — every qubit is bound to an atom that lives
in a storage zone and is transported into an entangling zone to perform a
two-qubit gate — but it shares **no code** with ZAC.  Its main jobs are:

* document, in executable form, how the hardware instructions are constructed; and
* enable a ZAC-independent round-trip test that preserves rotation angles.

Unlike the stock ZAC output, single-qubit gates here carry their rotation
parameters (``params``), so the round trip can reconstruct arbitrary rotations
exactly.
"""

from __future__ import annotations

from typing import Iterable, Sequence

Gate = tuple[str, tuple[int, ...], tuple[float, ...]]

_TWO_QUBIT = {"cz", "cx", "cnot"}


def _norm(op) -> Gate:
    """Normalise an op spec to ``(name, qubits, params)``."""
    if len(op) == 2:
        name, qubits = op
        params: Sequence[float] = ()
    else:
        name, qubits, params = op
    return str(name).lower(), tuple(int(q) for q in qubits), tuple(float(p) for p in params)


def encode_circuit(
    ops: Iterable,
    n_qubits: int,
    *,
    storage_array: int = 0,
    entangle_array: int = 1,
    name: str = "encoded",
) -> dict:
    """Encode a physical circuit into a hardware schedule.

    Args:
        ops: iterable of ``(name, qubits)`` or ``(name, qubits, params)``.
            Two-qubit entangling gates (``cz``/``cx``) are realised by moving the
            atoms into the entangling zone and back.  ``swap`` is emitted as an
            explicit *quantum* swap instruction (not a spatial move).
        n_qubits: number of physical qubits (== number of atoms).
        storage_array / entangle_array: SLM/array ids for the two zones.
        name: schedule name.

    Returns:
        A hardware-schedule dict with an ``init`` followed by the per-gate
        instructions.
    """
    # qubit i is bound to atom i, initially at storage position (a, 0, i).
    pos: dict[int, list[int]] = {i: [storage_array, 0, i] for i in range(n_qubits)}
    instructions: list[dict] = [
        {
            "type": "init",
            "id": 0,
            "begin_time": 0,
            "end_time": 0,
            "init_locs": [[i, *pos[i]] for i in range(n_qubits)],
        }
    ]

    for op in ops:
        gname, qubits, params = _norm(op)

        if gname in _TWO_QUBIT:
            a, b = qubits
            begin = [[a, *pos[a]], [b, *pos[b]]]
            ent = {a: [entangle_array, 0, 0], b: [entangle_array, 0, 1]}
            end = [[a, *ent[a]], [b, *ent[b]]]
            # move into the entangling zone
            instructions.append(
                {
                    "type": "rearrangeJob",
                    "aod_qubits": [a, b],
                    "begin_locs": begin,
                    "end_locs": end,
                }
            )
            # the entangling pulse
            instructions.append(
                {
                    "type": "rydberg",
                    "zone_id": 0,
                    "gates": [{"q0": a, "q1": b, "name": "cz" if gname == "cz" else gname}],
                }
            )
            # move back to storage
            instructions.append(
                {
                    "type": "rearrangeJob",
                    "aod_qubits": [a, b],
                    "begin_locs": end,
                    "end_locs": begin,
                }
            )
        elif gname == "swap":
            instructions.append({"type": "swap", "qubits": list(qubits)})
        elif gname == "measure":
            instructions.append(
                {"type": "measure", "qubits": list(qubits), "classical_bits": list(qubits)}
            )
        elif gname == "reset":
            instructions.append({"type": "reset", "qubits": list(qubits)})
        else:
            # single-qubit gate (carries rotation params if any)
            gate = {"name": gname, "q": qubits[0]}
            if params:
                gate["params"] = list(params)
            instructions.append(
                {"type": "1qGate", "unitary": gname, "gates": [gate]}
            )

    return {"name": name, "architecture_spec_path": None, "instructions": instructions}
