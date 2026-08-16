"""Parsing helpers for the compiler-generated ZAIR instruction list.

The forward simulator consumes the *exact* schedule format emitted by
``natam_compiler.zair.to_zair`` (and understood by the reverse compiler):

    {"name": ..., "instructions": [<init>, <inst>, ...]}

Instruction types
-----------------
* ``init``         — initial layout: ``init_locs = [[id, a, r, c], ...]``
* ``rearrangeJob`` — parallel atom movement: ``begin_locs`` / ``end_locs`` (and
  optionally ``aod_qubits``).  Movement only, never a gate.
* ``1qGate``       — one parallel layer of single-qubit gates: ``gates = [{name, q, params?}]``
* ``rydberg``      — one parallel layer of two-qubit CZ/CX: ``gates = [{q0, q1, name?}]``
* ``swap``         — explicit quantum swap: ``qubits = [a, b]``
* ``measure``      — ``qubits = [...]`` (optionally ``classical_bits``)

Everything grouped inside one instruction is a single **parallel time step**.
"""

from __future__ import annotations

import json
from typing import Any


# ------------------------------------------------------------------ classify
def op_kind(inst: dict[str, Any]) -> str:
    """Short timeline label for an instruction."""
    itype = str(inst.get("type", ""))
    if itype == "init":
        return "INIT"
    if itype in {"rearrangeJob", "move", "MOVE"}:
        return "MOVE"
    if itype in {"1qGate", "global_1qGate"}:
        return "1Q"
    if itype in {"rydberg", "2qGate"}:
        return "2Q"
    if itype in {"swap", "SWAP"}:
        return "SWAP"
    if itype in {"measure", "MEASURE"}:
        return "MEASURE"
    return "OTHER"


def _fmt_param(params) -> str:
    if not params:
        return ""
    try:
        return "(" + ", ".join(f"{float(p):.3g}" for p in params) + ")"
    except Exception:
        return ""


def op_summary(inst: dict[str, Any]) -> str:
    """A human-readable one-line summary of an instruction."""
    kind = op_kind(inst)
    if kind == "INIT":
        return f"INIT  ·  {len(inst.get('init_locs', []))} atoms"
    if kind == "MOVE":
        locs = inst.get("end_locs", inst.get("locs", []))
        n = len(locs)
        if n > 1:
            return f"MOVE  ·  {n} atoms (parallel)"
        if n == 1:
            a, ar, r, c = locs[0][0], locs[0][1], locs[0][2], locs[0][3]
            zone = "E" if ar == 1 else "S"
            return f"MOVE  ·  atom {a} → {zone}(r{r}, c{c})"
        return "MOVE"
    if kind == "1Q":
        gates = inst.get("gates", [])
        if len(gates) > 1:
            names = "/".join(sorted({str(g.get('name', '?')).upper() for g in gates}))
            return f"1Q  ·  {len(gates)} gates (parallel) [{names}]"
        if gates:
            g = gates[0]
            name = str(g.get("name", inst.get("unitary", "?"))).upper()
            return f"1Q  ·  {name}{_fmt_param(g.get('params'))} on atom {g.get('q', '?')}"
        return "1Q gate"
    if kind == "2Q":
        gates = inst.get("gates", [])
        if len(gates) > 1:
            name = str(gates[0].get("name", "cz")).upper()
            return f"2Q  ·  {len(gates)}× {name} (parallel)"
        if gates:
            g = gates[0]
            name = str(g.get("name", "cz")).upper()
            return f"2Q  ·  {name} on atoms {g.get('q0')}, {g.get('q1')}"
        return "2Q gate"
    if kind == "SWAP":
        qs = inst.get("qubits", [])
        return f"SWAP  ·  atoms {qs[0]}, {qs[1]}" if len(qs) >= 2 else "SWAP"
    if kind == "MEASURE":
        qs = inst.get("qubits", inst.get("targets", []))
        return f"MEASURE  ·  {len(qs)} atom(s)"
    return str(inst.get("type", "op"))


def instruction_atoms(inst: dict[str, Any]) -> set[int]:
    """Atoms referenced by an instruction (for highlighting)."""
    kind = op_kind(inst)
    atoms: set[int] = set()
    if kind == "MOVE":
        for loc in inst.get("end_locs", inst.get("locs", [])):
            atoms.add(int(loc[0]))
    elif kind == "1Q":
        for g in inst.get("gates", []):
            if "q" in g:
                atoms.add(int(g["q"]))
    elif kind == "2Q":
        for g in inst.get("gates", []):
            atoms.add(int(g["q0"]))
            atoms.add(int(g["q1"]))
    elif kind == "SWAP":
        for q in inst.get("qubits", [])[:2]:
            atoms.add(int(q))
    elif kind == "MEASURE":
        for q in inst.get("qubits", inst.get("targets", [])):
            if isinstance(q, int):
                atoms.add(int(q))
    return atoms


# --------------------------------------------------------------------- loaders
def load_instructions(source: Any) -> list[dict[str, Any]]:
    """Return the instruction list from a variety of sources.

    ``source`` may be:
    * a path (``str`` / ``os.PathLike``) to a ZAIR JSON file,
    * a raw JSON string,
    * a ZAIR dict with an ``"instructions"`` key,
    * an already-parsed list of instruction dicts.
    """
    import os

    if isinstance(source, (list, tuple)):
        return list(source)
    if isinstance(source, dict):
        instrs = source.get("instructions")
        if instrs is None:
            raise ValueError("ZAIR dict has no 'instructions' key.")
        return list(instrs)
    if isinstance(source, (str, os.PathLike)):
        text = None
        spath = os.fspath(source)
        if os.path.exists(spath):
            with open(spath, "r", encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = str(source)
        data = json.loads(text)
        return load_instructions(data)
    raise TypeError(f"unsupported instruction source: {type(source)!r}")
