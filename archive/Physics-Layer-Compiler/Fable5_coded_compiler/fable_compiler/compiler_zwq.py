"""Hardware-aware compiler for the prompt's neutral-atom architecture."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Set, Union

from qiskit import QuantumCircuit

from .hardware import ENTANGLEMENT, HardwareSpec, Position, default_hardware
from .ir import Gate1Q, Gate2Q, LogicalCircuit, Measure, from_qasm, from_qiskit
from .metrics import Metrics, summarize
from .movement_zwq import MoveCall, batch_duration, plan_batches
from .search_emit import EmissionCandidate, score_move_batches
from .operations import (
    INIT,
    MEASURE,
    MOVE,
    ONEQ,
    TWOQ,
    HardwareProgram,
    InitOp,
    MeasureOp,
    MoveOp,
    OneQubitGate,
    Stage,
    TwoQubitGate,
)
from .placement import place
from .scheduler import schedule

CircuitInput = Union[QuantumCircuit, LogicalCircuit, str]


@dataclass(frozen=True)
class CompileResult:
    program: HardwareProgram
    metrics: Metrics
    logical: LogicalCircuit
    homes: Dict[int, Position]


def _to_logical(circuit: CircuitInput, optimization_level: int) -> LogicalCircuit:
    if isinstance(circuit, LogicalCircuit):
        return circuit
    if isinstance(circuit, QuantumCircuit):
        return from_qiskit(circuit, optimization_level)
    if isinstance(circuit, str):
        return from_qasm(circuit, optimization_level)
    raise TypeError(f"unsupported circuit input {type(circuit)!r}")


class _MovementGroup:
    def __init__(self, initial_current: Dict[int, Position], program: HardwareProgram | None = None) -> None:
        self.initial_current = initial_current
        self.program = program
        self.calls: List[MoveCall] = []


class Compiler:
    def __init__(self, hardware: HardwareSpec | None = None):
        self.hw = hardware or default_hardware()
        self.last_2q_move_score = None
        self._active_movement_group: _MovementGroup | None = None

    def compile(self, circuit: CircuitInput, optimization_level: int = 1) -> CompileResult:
        logical = _to_logical(circuit, optimization_level)
        homes = place(logical, self.hw)
        program = HardwareProgram(logical.num_qubits, logical.num_clbits)
        program.add(
            Stage(
                INIT,
                [InitOp(atom, position) for atom, position in sorted(homes.items())],
                duration=0.0,
                fidelity=1.0,
            )
        )
        slots = schedule(logical)
        current = dict(homes)
        for index, slot in enumerate(slots):
            future_2q = self._future_2q_qubits(slots[index + 1 :])
            self._emit_slot(program, slot, homes, current, future_2q)
        return CompileResult(program, summarize(program, self.hw), logical, homes)

    @contextmanager
    def _movement_group(self, program: HardwareProgram, current: Dict[int, Position]):
        self._active_movement_group = _MovementGroup(initial_current=dict(current), program=program)
        try:
            yield self._active_movement_group
        finally:
            self._active_movement_group = None

    def _emit_slot(
        self,
        program: HardwareProgram,
        slot: List[object],
        homes: Dict[int, Position],
        current: Dict[int, Position],
        future_2q: Set[int],
    ) -> None:
        oneq = [op for op in slot if isinstance(op, Gate1Q)]
        twoq = [op for op in slot if isinstance(op, Gate2Q)]
        meas = [op for op in slot if isinstance(op, Measure)]
        if oneq:
            program.add(
                Stage(
                    ONEQ,
                    [OneQubitGate(op.name, op.qubit, current[op.qubit], op.params) for op in oneq],
                    duration=self.hw.t_1q,
                    fidelity=self.hw.f_1q ** len(oneq),
                )
            )
        if twoq:
            self._emit_2q(program, twoq, homes, current)
            # self._release_dead_residents(program, homes, current, future_2q)
        if meas:
            self._release_atoms(program, homes, current, {op.qubit for op in meas})
            program.add(
                Stage(
                    MEASURE,
                    [MeasureOp(op.qubit, current[op.qubit], op.clbit) for op in meas],
                    duration=self.hw.t_measure,
                    fidelity=self.hw.f_measure ** len(meas),
                )
            )

    def _future_2q_qubits(self, slots: List[List[object]]) -> Set[int]:
        qubits: Set[int] = set()
        for slot in slots:
            for op in slot:
                if isinstance(op, Gate2Q):
                    qubits.update({op.q0, op.q1})
        return qubits

    def _emit_2q(
        self,
        program: HardwareProgram,
        gates: List[Gate2Q],
        homes: Dict[int, Position],
        current: Dict[int, Position],
    ) -> None:
        ordered = self._ordered_2q_gates(gates, current)
        initial_targets = self._build_2q_targets(ordered, current)

        participating = {q for gate in ordered for q in (gate.q0, gate.q1)}
        cycle_breakers = self._cycle_break_atoms(current, initial_targets, participating)
        pending_moves: List[MoveOp] = []
        if cycle_breakers:
            self._release_atoms(program, homes, current, cycle_breakers, pending_moves)
        self._evict_target_blockers(program, homes, current, participating, set(initial_targets.values()), pending_moves)
        moves_in: List[MoveOp] = []
        for atom in sorted(participating):
            if current[atom] != initial_targets[atom]:
                moves_in.append(MoveOp(atom, current[atom], initial_targets[atom]))
        moves_in = self._order_moves_to_clear_destinations(moves_in)
        pending_moves.extend(moves_in)
        moving_atoms = {move.atom for move in pending_moves}
        stationary = {
            pos
            for atom, pos in current.items()
            if atom not in participating and atom not in moving_atoms
        }
        move_batches = plan_batches(self.hw, pending_moves, stationary)
        self.last_2q_move_score = score_move_batches(self.hw, move_batches, stationary)
        self._emit_move_batches(program, move_batches)
        for move in pending_moves:
            current[move.atom] = move.dst

        program.add(
            Stage(
                TWOQ,
                [
                    TwoQubitGate("cz", gate.q0, gate.q1, current[gate.q0], current[gate.q1])
                    for gate in ordered
                ],
                duration=self.hw.t_2q,
                fidelity=self.hw.f_2q ** len(ordered),
            )
        )

    def _ordered_2q_gates(self, gates: List[Gate2Q], current: Dict[int, Position]) -> List[Gate2Q]:
        return sorted(gates, key=lambda gate: self._pair_key(gate, current))

    def _build_2q_targets(self, ordered: List[Gate2Q], current: Dict[int, Position]) -> Dict[int, Position]:
        return self._build_2q_targets_for_rows(ordered, current, self._assign_rows(ordered, current))

    def _build_2q_targets_for_rows(
        self,
        ordered: List[Gate2Q],
        current: Dict[int, Position],
        rows: List[int],
    ) -> Dict[int, Position]:
        target_positions: Dict[int, Position] = {}
        for gate, row in zip(ordered, rows):
            pos0, pos1 = (ENTANGLEMENT, 0, row), (ENTANGLEMENT, 1, row)
            q0_pos, q1_pos = current[gate.q0], current[gate.q1]
            if q0_pos[0] == ENTANGLEMENT and q0_pos[2] == row:
                target_positions[gate.q0] = q0_pos
                target_positions[gate.q1] = pos1 if q0_pos[1] == 0 else pos0
            elif q1_pos[0] == ENTANGLEMENT and q1_pos[2] == row:
                target_positions[gate.q1] = q1_pos
                target_positions[gate.q0] = pos1 if q1_pos[1] == 0 else pos0
            elif self.hw.coordinate(q0_pos)[0] <= self.hw.coordinate(q1_pos)[0]:
                target_positions[gate.q0] = pos0
                target_positions[gate.q1] = pos1
            else:
                target_positions[gate.q0] = pos1
                target_positions[gate.q1] = pos0
        return target_positions

    def _pair_key(self, gate: Gate2Q, positions: Dict[int, Position]) -> tuple[float, float]:
        x0, y0 = self.hw.coordinate(positions[gate.q0])
        x1, y1 = self.hw.coordinate(positions[gate.q1])
        return ((y0 + y1) / 2.0, (x0 + x1) / 2.0)

    def _assign_rows(self, gates: List[Gate2Q], current: Dict[int, Position]) -> List[int]:
        rows = self.hw.entanglement.rows
        used: set[int] = set()
        assigned: List[int] = []
        for gate in gates:
            resident_rows = [current[q][2] for q in (gate.q0, gate.q1) if current[q][0] == ENTANGLEMENT]
            preferred = resident_rows[0] if resident_rows else round(
                (self.hw.coordinate(current[gate.q0])[1] + self.hw.coordinate(current[gate.q1])[1]) / 2.0
            )
            candidates = sorted(range(rows), key=lambda row: (abs(row - preferred), row))
            row = next(candidate for candidate in candidates if candidate not in used)
            used.add(row)
            assigned.append(row)
        return assigned

    def _release_dead_residents(
        self,
        program: HardwareProgram,
        homes: Dict[int, Position],
        current: Dict[int, Position],
        future_2q: Set[int],
    ) -> None:
        atoms = {atom for atom, pos in current.items() if pos[0] == ENTANGLEMENT and atom not in future_2q}
        self._release_atoms(program, homes, current, atoms)

    def _evict_target_blockers(
        self,
        program: HardwareProgram,
        homes: Dict[int, Position],
        current: Dict[int, Position],
        participating: Set[int],
        targets: Set[Position],
        move_collection: List[MoveOp] | None = None,
    ) -> None:
        blockers = {
            atom
            for atom, pos in current.items()
            if atom not in participating and pos in targets and pos != homes[atom]
        }
        self._release_atoms(program, homes, current, blockers, move_collection)

    def _cycle_break_atoms(
        self,
        current: Dict[int, Position],
        targets: Dict[int, Position],
        participating: Set[int],
    ) -> Set[int]:
        source_owner = {
            current[atom]: atom
            for atom in participating
            if current[atom] != targets.get(atom, current[atom])
        }
        edges = {
            atom: source_owner[targets[atom]]
            for atom in participating
            if atom in targets
            and current[atom] != targets[atom]
            and targets[atom] in source_owner
            and source_owner[targets[atom]] != atom
        }

        breakers: Set[int] = set()
        globally_seen: Set[int] = set()
        for start in sorted(edges):
            if start in globally_seen:
                continue
            path: list[int] = []
            index: dict[int, int] = {}
            atom = start
            while atom in edges:
                if atom in index:
                    cycle = path[index[atom] :]
                    breakers.add(max(cycle))
                    break
                if atom in globally_seen:
                    break
                index[atom] = len(path)
                path.append(atom)
                atom = edges[atom]
            globally_seen.update(path)
        return breakers

    def _order_moves_to_clear_destinations(self, moves: List[MoveOp]) -> List[MoveOp]:
        by_source = {move.src: move for move in moves}
        ordered: List[MoveOp] = []
        visiting: set[MoveOp] = set()
        visited: set[MoveOp] = set()

        def visit(move: MoveOp) -> None:
            if move in visited:
                return
            if move in visiting:
                return
            visiting.add(move)
            blocker = by_source.get(move.dst)
            if blocker is not None:
                visit(blocker)
            visiting.remove(move)
            visited.add(move)
            ordered.append(move)

        for move in moves:
            visit(move)
        return ordered

    def _release_atoms(
        self,
        program: HardwareProgram,
        homes: Dict[int, Position],
        current: Dict[int, Position],
        atoms: Set[int],
        move_collection: List[MoveOp] | None = None,
    ) -> None:
        moves = [MoveOp(atom, current[atom], homes[atom]) for atom in sorted(atoms) if current[atom] != homes[atom]]
        if move_collection is not None:
            move_collection.extend(moves)
        else:
            stationary = {pos for atom, pos in current.items() if atom not in atoms}
            self._emit_moves(program, moves, stationary)
        for move in moves:
            current[move.atom] = move.dst

    def _emit_moves(self, program: HardwareProgram, moves: List[MoveOp], stationary: set[Position]) -> None:
        if self._active_movement_group is not None and moves:
            self._active_movement_group.calls.append(
                MoveCall(len(self._active_movement_group.calls), tuple(moves), frozenset(stationary))
            )
        self._emit_move_batches(program, plan_batches(self.hw, moves, stationary))

    def _emit_move_batches(self, program: HardwareProgram, batches: List[List[MoveOp]]) -> None:
        for batch in batches:
            # print("batch", batch)
            duration = 2.0 * self.hw.t_move_fixed + batch_duration(self.hw, batch)
            fidelity = self.hw.f_transfer ** len(batch)
            program.add(Stage(MOVE, list(batch), duration=duration, fidelity=fidelity))


def compile_circuit(
    circuit: CircuitInput,
    hardware: HardwareSpec | None = None,
    optimization_level: int = 3,
) -> CompileResult:
    return Compiler(hardware).compile(circuit, optimization_level)