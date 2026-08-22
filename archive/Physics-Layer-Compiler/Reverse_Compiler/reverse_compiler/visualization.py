"""Visualization helpers for reverse-compiled schedules and circuits.

This module provides two plotting entry points:

* ``plot_atom_operations`` visualizes atom-level hardware events (moves + gates)
  directly from a ZAIR schedule.
* ``plot_circuit_structure`` visualizes the reconstructed quantum circuit from a
  :class:`~reverse_compiler.circuit_ir.CircuitIR`.

The plotting backend is optional and imported lazily. Callers only need
``matplotlib`` when they request a plot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .circuit_ir import CircuitIR, CircuitOperation


@dataclass(frozen=True)
class AtomEvent:
    """A hardware event associated with one atom at one schedule step."""

    step: int
    atom: int
    kind: str
    label: str


def _require_matplotlib():
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "matplotlib is required for visualization. "
            "Install it with: pip install matplotlib"
        ) from exc
    return plt


def _iter_targets(inst: dict[str, Any]) -> list[int]:
    targets = inst.get("qubits", inst.get("targets", []))
    values: list[int] = []
    for t in targets:
        if isinstance(t, bool):
            continue
        if isinstance(t, int):
            values.append(int(t))
        elif isinstance(t, dict) and "atom" in t:
            values.append(int(t["atom"]))
        elif isinstance(t, dict) and "qubit" in t:
            values.append(int(t["qubit"]))
    return values


def _all_atoms_in_schedule(zair: dict[str, Any]) -> set[int]:
    atoms: set[int] = set()
    explicit = zair.get("qubit_to_atom")
    if isinstance(explicit, dict):
        atoms.update(int(a) for a in explicit.values())

    for inst in zair.get("instructions", []):
        itype = str(inst.get("type", ""))
        if itype == "init":
            for loc in inst.get("init_locs", []):
                atoms.add(int(loc[0]))
            for atom in inst.get("inactive", []) + inst.get("lost", []):
                atoms.add(int(atom))
            continue
        if itype in {"rearrangeJob", "move", "MOVE"}:
            for loc in inst.get("end_locs", inst.get("locs", [])):
                atoms.add(int(loc[0]))
            if "atom" in inst:
                atoms.add(int(inst["atom"]))
            continue
        if itype in {"1qGate", "rydberg"}:
            for gate in inst.get("gates", []):
                for key in ("q", "q0", "q1", "atom"):
                    if key in gate:
                        atoms.add(int(gate[key]))
            continue
        if itype == "global_1qGate":
            for atom in inst.get("atoms", inst.get("mask", [])):
                atoms.add(int(atom))
            continue
        if itype in {
            "swap",
            "SWAP",
            "measure",
            "MEASURE",
            "reset",
            "RESET",
            "barrier",
            "BARRIER",
            "delay",
            "DELAY",
            "loss",
            "deactivate",
            "activate",
        }:
            atoms.update(_iter_targets(inst))
            atoms.update(int(a) for a in inst.get("atoms", []))
    return atoms


def extract_atom_events(zair: dict[str, Any]) -> tuple[list[AtomEvent], list[int]]:
    """Extract atom-level events from a schedule.

    Returns:
        (events, atoms), where atoms is sorted ascending and events is sorted by
        ``(step, atom)``.
    """
    atoms = sorted(_all_atoms_in_schedule(zair))
    events: list[AtomEvent] = []

    for step, inst in enumerate(zair.get("instructions", [])):
        itype = str(inst.get("type", ""))

        if itype in {"rearrangeJob", "move", "MOVE"}:
            for loc in inst.get("end_locs", inst.get("locs", [])):
                atom = int(loc[0])
                pos = (int(loc[1]), int(loc[2]), int(loc[3]))
                label = f"MOVE a={pos[0]} r={pos[1]} c={pos[2]}"
                events.append(AtomEvent(step=step, atom=atom, kind="move", label=label))
            if "atom" in inst and "position" in inst:
                atom = int(inst["atom"])
                pos = tuple(int(v) for v in inst["position"])
                label = f"MOVE a={pos[0]} r={pos[1]} c={pos[2]}"
                events.append(AtomEvent(step=step, atom=atom, kind="move", label=label))
            continue

        if itype == "1qGate":
            for gate in inst.get("gates", []):
                if "q" not in gate and "atom" not in gate:
                    continue
                atom = int(gate.get("q", gate.get("atom")))
                gname = str(gate.get("name", inst.get("unitary", "1Q"))).upper()
                events.append(AtomEvent(step=step, atom=atom, kind="gate", label=gname))
            continue

        if itype == "global_1qGate":
            gname = str(inst.get("name", inst.get("unitary", "GLOBAL_1Q"))).upper()
            target_atoms = inst.get("atoms", inst.get("mask", atoms))
            for atom in target_atoms:
                events.append(
                    AtomEvent(step=step, atom=int(atom), kind="global", label=f"G:{gname}")
                )
            continue

        if itype in {"rydberg", "2qGate"}:
            for gate in inst.get("gates", []):
                name = str(gate.get("name", "CZ")).upper()
                q0 = int(gate.get("q0"))
                q1 = int(gate.get("q1"))
                events.append(AtomEvent(step=step, atom=q0, kind="entangle", label=name))
                events.append(AtomEvent(step=step, atom=q1, kind="entangle", label=name))
            continue

        if itype in {"swap", "SWAP"}:
            qs = _iter_targets(inst)
            for atom in qs[:2]:
                events.append(AtomEvent(step=step, atom=atom, kind="entangle", label="SWAP"))
            continue

        if itype in {"measure", "MEASURE"}:
            for atom in _iter_targets(inst):
                events.append(AtomEvent(step=step, atom=atom, kind="measure", label="MEAS"))
            continue

        if itype in {"reset", "RESET"}:
            for atom in _iter_targets(inst):
                events.append(AtomEvent(step=step, atom=atom, kind="reset", label="RESET"))
            continue

    events.sort(key=lambda e: (e.step, e.atom, e.kind))
    return events, atoms


def plot_atom_operations(
    zair: dict[str, Any],
    output_path: str,
    *,
    title: str | None = None,
    figsize: tuple[float, float] = (12.0, 5.0),
) -> str:
    """Plot atom-level hardware events (move + gate activity) to an image file."""
    plt = _require_matplotlib()
    events, atoms = extract_atom_events(zair)
    atom_to_y = {atom: idx for idx, atom in enumerate(atoms)}

    fig, ax = plt.subplots(figsize=figsize)
    ax.set_facecolor("#f8f8f8")

    style = {
        "move": ("#f28e2b", "s"),
        "gate": ("#1f77b4", "o"),
        "global": ("#2ca02c", "D"),
        "entangle": ("#d62728", "^"),
        "measure": ("#9467bd", "v"),
        "reset": ("#8c564b", "P"),
    }

    by_kind: dict[str, list[AtomEvent]] = {}
    for ev in events:
        by_kind.setdefault(ev.kind, []).append(ev)

    for kind, evs in by_kind.items():
        color, marker = style.get(kind, ("#4d4d4d", "o"))
        xs = [e.step for e in evs]
        ys = [atom_to_y[e.atom] for e in evs]
        ax.scatter(xs, ys, c=color, marker=marker, s=55, alpha=0.9, label=kind)

    # Annotate only non-global events to avoid clutter.
    for ev in events:
        if ev.kind == "global":
            continue
        y = atom_to_y[ev.atom]
        ax.text(ev.step + 0.08, y + 0.08, ev.label, fontsize=7, color="#333333")

    ax.set_xlabel("Schedule Step")
    ax.set_ylabel("Atom")
    ax.set_yticks(list(range(len(atoms))))
    ax.set_yticklabels([str(a) for a in atoms])
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    ax.set_title(title or "Atom Operations Timeline (Move + Gates)")

    if by_kind:
        ax.legend(loc="upper right", frameon=True)

    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)
    return output_path


def _operation_groups_by_layer(ir: CircuitIR) -> list[tuple[int, list[CircuitOperation]]]:
    """Group operations by layer while preserving insertion order within a layer."""
    grouped: dict[int, list[CircuitOperation]] = {}
    next_layer = 0
    for op in ir.operations:
        if op.layer is None:
            layer = next_layer
            next_layer += 1
        else:
            layer = op.layer
            next_layer = max(next_layer, layer + 1)
        grouped.setdefault(layer, []).append(op)
    return sorted(grouped.items(), key=lambda x: x[0])


def plot_circuit_structure(
    circuit_ir: CircuitIR,
    output_path: str,
    *,
    title: str | None = None,
    figsize: tuple[float, float] = (12.0, 5.0),
) -> str:
    """Plot the reconstructed circuit structure to an image file."""
    plt = _require_matplotlib()

    layers = _operation_groups_by_layer(circuit_ir)
    n_qubits = max(circuit_ir.num_qubits, 1)
    n_layers = max(len(layers), 1)

    fig, ax = plt.subplots(figsize=figsize)
    ax.set_facecolor("#fbfbfb")

    for q in range(n_qubits):
        ax.hlines(y=q, xmin=-0.2, xmax=n_layers - 0.2, color="#777777", linewidth=1.0)
        ax.text(-0.45, q, f"q{q}", va="center", ha="right", fontsize=9)

    box_face = "#dbeafe"
    for x, (_layer, ops) in enumerate(layers):
        for op in ops:
            if len(op.qubits) == 1:
                q = op.qubits[0]
                rect = plt.Rectangle(
                    (x - 0.18, q - 0.16), 0.36, 0.32, facecolor=box_face, edgecolor="#1d4ed8"
                )
                ax.add_patch(rect)
                ax.text(x, q, op.name, ha="center", va="center", fontsize=8)
            elif len(op.qubits) == 2:
                q0, q1 = sorted(op.qubits)
                ax.vlines(x=x, ymin=q0, ymax=q1, color="#dc2626", linewidth=1.6)
                ax.scatter([x, x], [q0, q1], c="#dc2626", s=24)
                ax.text(x + 0.05, (q0 + q1) / 2.0 + 0.06, op.name, fontsize=8, color="#991b1b")
            else:
                qmin = min(op.qubits)
                qmax = max(op.qubits)
                rect = plt.Rectangle(
                    (x - 0.22, qmin - 0.18),
                    0.44,
                    (qmax - qmin) + 0.36,
                    facecolor="#e5e7eb",
                    edgecolor="#374151",
                )
                ax.add_patch(rect)
                ax.text(x, (qmin + qmax) / 2.0, op.name, ha="center", va="center", fontsize=7)

    ax.set_xlim(-0.6, n_layers - 0.1)
    ax.set_ylim(-0.6, n_qubits - 0.4)
    ax.set_xlabel("Layer")
    ax.set_yticks([])
    ax.set_xticks(list(range(n_layers)))
    ax.set_xticklabels([str(i) for i in range(n_layers)])
    ax.set_title(title or "Reconstructed Quantum Circuit Structure")

    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)
    return output_path


__all__ = [
    "AtomEvent",
    "extract_atom_events",
    "plot_atom_operations",
    "plot_circuit_structure",
]
