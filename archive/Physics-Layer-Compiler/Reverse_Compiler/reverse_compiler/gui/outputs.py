"""Reverse-compile a schedule and build all output artefacts + validation.

Clearly separates the three distinct products of a hardware schedule:
* the **quantum circuit** (reconstructed gates),
* the **atom-movement schedule** (hardware only, never a gate), and
* the **physical-qubit mapping history** (qubit <-> atom, positions over time).
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from ..circuit_ir import CircuitIR
from ..exporters import StimExportError, to_qiskit, to_stim
from ..reverse_compiler import ReverseCompileError, reverse_compile
from .state import (
    GuiState,
    Site,
    StateError,
    entangle_pair_ok,
    op_kind,
    valid_site,
)

logger = logging.getLogger(__name__)


@dataclass
class ValidationReport:
    physical_qubit_count: int = 0
    gate_1q_count: int = 0
    gate_2q_count: int = 0
    movement_count: int = 0
    measure_count: int = 0
    unsupported_count: int = 0
    mapping_conflicts: list[str] = field(default_factory=list)
    issues: list[tuple[int, str]] = field(default_factory=list)  # (step, reason)
    passed: bool = True

    def as_text(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"VALIDATION RESULT: {status}",
            "-" * 44,
            f"Physical qubit count      : {self.physical_qubit_count}",
            f"Reconstructed 1Q gates    : {self.gate_1q_count}",
            f"Reconstructed 2Q gates    : {self.gate_2q_count}",
            f"Movement count            : {self.movement_count}",
            f"Measurements              : {self.measure_count}",
            f"Unsupported operations    : {self.unsupported_count}",
            f"Mapping conflicts         : {len(self.mapping_conflicts)}",
        ]
        if self.mapping_conflicts:
            lines.append("")
            lines.append("Mapping conflicts:")
            lines.extend(f"  • {c}" for c in self.mapping_conflicts)
        if self.issues:
            lines.append("")
            lines.append("Issues (step → reason):")
            for step, reason in self.issues:
                where = "INIT" if step == 0 else f"op #{step}"
                lines.append(f"  • [{where}] {reason}")
        if self.passed and not self.issues and not self.mapping_conflicts:
            lines.append("")
            lines.append("No problems detected. Schedule is consistent.")
        return "\n".join(lines)


@dataclass
class ReverseResult:
    ir: Optional[CircuitIR] = None
    error: Optional[str] = None
    qasm: str = ""
    qiskit_text: str = ""
    stim_text: str = ""
    diagram_path: Optional[str] = None
    validation: ValidationReport = field(default_factory=ValidationReport)
    movement_schedule: str = ""
    mapping_history: str = ""

    @property
    def ok(self) -> bool:
        return self.ir is not None and self.error is None


# --------------------------------------------------------------------- helpers
def _validate(state: GuiState, ir: Optional[CircuitIR], reverse_error: Optional[str]) -> ValidationReport:
    report = ValidationReport()

    # Per-step structural validation over the authored timeline.
    positions = dict(state.positions_at(0))
    seen: dict[tuple[int, int, int], int] = {}
    for atom, site in positions.items():
        seen.setdefault(site.key, atom)

    for i, inst in enumerate(state.operations, start=1):
        kind = op_kind(inst)
        if kind == "MOVE":
            report.movement_count += 1
            end_locs = inst.get("end_locs", inst.get("locs", []))
            # Rectangular-grid (AOD) rule for simultaneous multi-atom moves.
            if len(end_locs) > 1:
                group = []
                for loc in end_locs:
                    a = int(loc[0])
                    src = positions.get(a)
                    if src is not None:
                        group.append((a, src, Site(int(loc[1]), int(loc[2]), int(loc[3]))))
                try:
                    GuiState._validate_parallel_move(group)
                except StateError as exc:
                    report.mapping_conflicts.append(f"op #{i}: {exc}")

            # The move is simultaneous: vacate every moving atom first, then place
            # them, so overlapping shifts (e.g. a column sliding by one) are valid.
            movers = [int(loc[0]) for loc in end_locs]
            for a in movers:
                old = positions.get(a)
                if old is not None and seen.get(old.key) == a:
                    del seen[old.key]
            landed: dict[tuple[int, int, int], int] = {}
            for loc in end_locs:
                atom = int(loc[0])
                dst = Site(int(loc[1]), int(loc[2]), int(loc[3]))
                if not valid_site(dst):
                    report.issues.append((i, f"move destination out of range for atom {atom}"))
                occ = seen.get(dst.key)
                if occ is not None and occ != atom:
                    report.mapping_conflicts.append(
                        f"op #{i}: atom {atom} moved onto site occupied by (stationary) atom {occ}"
                    )
                if dst.key in landed and landed[dst.key] != atom:
                    report.mapping_conflicts.append(
                        f"op #{i}: atoms {landed[dst.key]} and {atom} land on the same site"
                    )
                landed[dst.key] = atom
                positions[atom] = dst
                seen[dst.key] = atom
        elif kind == "1Q":
            gates = inst.get("gates", [])
            report.gate_1q_count += len(gates)
            layer_used: set[int] = set()
            for g in gates:
                atom = int(g.get("q", -1))
                if atom not in positions:
                    report.issues.append((i, f"1Q gate targets missing atom {atom}"))
                if atom in layer_used:
                    report.mapping_conflicts.append(
                        f"op #{i}: atom {atom} used twice in the same parallel 1Q layer"
                    )
                layer_used.add(atom)
                if str(g.get("name", "")).lower() in {"rx", "ry", "rz"} and not g.get("params"):
                    report.issues.append((i, f"rotation on atom {atom} has no angle"))
        elif kind == "2Q":
            gates = inst.get("gates", [])
            report.gate_2q_count += len(gates)
            layer_used = set()
            for g in gates:
                a0, a1 = int(g["q0"]), int(g["q1"])
                if a0 in layer_used or a1 in layer_used:
                    report.mapping_conflicts.append(
                        f"op #{i}: atom reused in the same parallel 2Q layer ({a0},{a1})"
                    )
                layer_used.update((a0, a1))
                p0, p1 = positions.get(a0), positions.get(a1)
                if p0 is None or p1 is None:
                    report.issues.append((i, f"2Q gate targets missing atom(s) {a0},{a1}"))
                    continue
                if not entangle_pair_ok(p0, p1):
                    report.mapping_conflicts.append(
                        f"op #{i}: CZ/CX atoms {a0},{a1} violate entanglement-zone rule"
                    )
        elif kind == "SWAP":
            report.gate_2q_count += 1
        elif kind == "MEASURE":
            report.measure_count += 1
            for q in inst.get("qubits", []):
                if int(q) not in positions:
                    report.issues.append((i, f"measure targets missing atom {q}"))
        else:
            report.unsupported_count += 1
            report.issues.append((i, f"unsupported operation type {inst.get('type')!r}"))

    if ir is not None:
        report.physical_qubit_count = ir.metadata.get("num_atoms", ir.num_qubits)
    else:
        report.physical_qubit_count = len(state.positions_at(0))

    if reverse_error:
        report.issues.append((0, f"reverse compile failed: {reverse_error}"))

    report.passed = (
        reverse_error is None
        and not report.mapping_conflicts
        and not report.issues
        and report.unsupported_count == 0
    )
    return report


def _movement_schedule_text(state: GuiState) -> str:
    lines = ["# Atom movement schedule (hardware only — NOT quantum gates)"]
    any_move = False
    for i, inst in enumerate(state.operations, start=1):
        if op_kind(inst) != "MOVE":
            continue
        any_move = True
        for atom, src, dst in state.move_endpoints(i - 1):
            lines.append(
                f"step {i:>3}: atom {atom} : "
                f"(a{src.array}, r{src.row}, c{src.col}) → (a{dst.array}, r{dst.row}, c{dst.col})"
            )
    if not any_move:
        lines.append("(no movement instructions)")
    return "\n".join(lines)


def _mapping_history_text(state: GuiState) -> str:
    lines = ["# Physical-qubit mapping history",
             "# qubit == atom (fixed 1:1); movement changes position only.",
             ""]
    init = state.positions_at(0)
    lines.append("Initial binding & layout:")
    for atom in sorted(init):
        s = init[atom]
        lines.append(f"  qubit {atom} = atom {atom} @ (a{s.array}, r{s.row}, c{s.col})")

    # Position timeline for atoms that ever move.
    movers: dict[int, list[str]] = {}
    for i in range(len(state.operations)):
        if op_kind(state.operations[i]) != "MOVE":
            continue
        for atom, src, dst in state.move_endpoints(i):
            movers.setdefault(atom, []).append(
                f"    step {i + 1}: → (a{dst.array}, r{dst.row}, c{dst.col})"
            )
    if movers:
        lines.append("")
        lines.append("Position changes:")
        for atom in sorted(movers):
            lines.append(f"  atom {atom}:")
            lines.extend(movers[atom])
    return "\n".join(lines)


def _build_qasm(ir: CircuitIR) -> str:
    try:
        from qiskit import qasm2

        return qasm2.dumps(to_qiskit(ir))
    except Exception as exc:  # pragma: no cover - fallback path
        try:
            from qiskit import qasm3

            return qasm3.dumps(to_qiskit(ir))
        except Exception as exc2:
            return f"// QASM export failed: {exc}\n// qasm3 fallback failed: {exc2}"


def _build_qiskit_text(ir: CircuitIR) -> str:
    try:
        return str(to_qiskit(ir).draw(output="text"))
    except Exception as exc:  # pragma: no cover
        return f"Qiskit draw failed: {exc}"


def _build_stim_text(ir: CircuitIR) -> str:
    try:
        return str(to_stim(ir))
    except StimExportError as exc:
        return (
            "# Stim export unavailable for this circuit:\n"
            f"# {exc}\n"
            "# (Stim only supports Clifford operations.)"
        )
    except Exception as exc:  # pragma: no cover
        return f"# Stim export failed: {exc}"


def _build_diagram(ir: CircuitIR) -> Optional[str]:
    from ..visualization import plot_circuit_structure

    try:
        tmp = tempfile.NamedTemporaryFile(prefix="rc_circuit_", suffix=".png", delete=False)
        tmp.close()
        plot_circuit_structure(ir, tmp.name, title="Reconstructed circuit structure")
        return tmp.name
    except Exception:  # pragma: no cover
        logger.exception("circuit diagram render failed")
        return None


def run_reverse(state: GuiState, *, strict: bool = False) -> ReverseResult:
    """Reverse-compile the current schedule and build every output artefact."""
    result = ReverseResult()
    zair = state.build_zair()

    ir: Optional[CircuitIR] = None
    reverse_error: Optional[str] = None
    try:
        ir = reverse_compile(zair, strict=strict)
    except (ReverseCompileError, Exception) as exc:  # noqa: BLE001 - surface all
        reverse_error = str(exc)
        logger.warning("reverse compile failed: %s", exc)

    result.ir = ir
    result.error = reverse_error
    result.validation = _validate(state, ir, reverse_error)
    result.movement_schedule = _movement_schedule_text(state)
    result.mapping_history = _mapping_history_text(state)

    if ir is not None:
        result.qasm = _build_qasm(ir)
        result.qiskit_text = _build_qiskit_text(ir)
        result.stim_text = _build_stim_text(ir)
        result.diagram_path = _build_diagram(ir)
    return result
