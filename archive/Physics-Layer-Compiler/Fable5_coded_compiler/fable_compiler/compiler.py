"""Hardware-aware compiler for the prompt's neutral-atom architecture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set, Union

from qiskit import QuantumCircuit

from .hardware import ENTANGLEMENT, HardwareSpec, Position, default_hardware
from .ir import Gate1Q, Gate2Q, LogicalCircuit, Measure, from_qasm, from_qiskit
from .metrics import Metrics, summarize
from .movement import batch_duration, plan_batches
from .search_emit import EmissionCandidate, MoveGraphCache, score_move_batches, score_move_graph
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


class Compiler:
    def __init__(self, hardware: HardwareSpec | None = None):
        self.hw = hardware or default_hardware()
        self.last_2q_move_score = None

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
        candidate = self._select_2q_candidate(homes, current, ordered, initial_targets)
        target_positions = candidate.target_positions

        participating = {q for gate in ordered for q in (gate.q0, gate.q1)}
        cycle_breakers = self._cycle_break_atoms(current, target_positions, participating)
        if cycle_breakers:
            self._release_atoms(program, homes, current, cycle_breakers)
        self._evict_target_blockers(program, homes, current, participating, set(target_positions.values()))
        stationary = {pos for atom, pos in current.items() if atom not in participating}

        moves_in: List[MoveOp] = []
        for atom in sorted(participating):
            if current[atom] != target_positions[atom]:
                moves_in.append(MoveOp(atom, current[atom], target_positions[atom]))
        moves_in = self._order_moves_to_clear_destinations(moves_in)
        move_batches = plan_batches(self.hw, moves_in, stationary)
        self.last_2q_move_score = score_move_batches(self.hw, move_batches, stationary)
        self._emit_move_batches(program, move_batches)
        for move in moves_in:
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

    def _select_2q_candidate(
        self,
        homes: Dict[int, Position],
        current: Dict[int, Position],
        ordered: List[Gate2Q],
        target_positions: Dict[int, Position],
    ) -> EmissionCandidate:
        return self._evaluate_2q_targets(homes, current, ordered, target_positions)

    def _evaluate_2q_targets(
        self,
        homes: Dict[int, Position],
        current: Dict[int, Position],
        ordered: List[Gate2Q],
        target_positions: Dict[int, Position],
        parent_graph_cache: MoveGraphCache | None = None,
    ) -> EmissionCandidate:
        sim_current = dict(current)
        participating = {q for gate in ordered for q in (gate.q0, gate.q1)}
        scoring_moves: List[MoveOp] = []

        cycle_breakers = self._cycle_break_atoms(sim_current, target_positions, participating)
        for atom in sorted(cycle_breakers):
            if sim_current[atom] != homes[atom]:
                move = MoveOp(atom, sim_current[atom], homes[atom])
                scoring_moves.append(move)
                sim_current[atom] = move.dst

        blockers = {
            atom
            for atom, pos in sim_current.items()
            if atom not in participating and pos in set(target_positions.values()) and pos != homes[atom]
        }
        for atom in sorted(blockers):
            if sim_current[atom] != homes[atom]:
                move = MoveOp(atom, sim_current[atom], homes[atom])
                scoring_moves.append(move)
                sim_current[atom] = move.dst

        stationary = {pos for atom, pos in sim_current.items() if atom not in participating}
        moves_in = [
            MoveOp(atom, sim_current[atom], target_positions[atom])
            for atom in sorted(participating)
            if sim_current[atom] != target_positions[atom]
        ]
        moves_in = self._order_moves_to_clear_destinations(moves_in)
        scoring_moves.extend(moves_in)
        scoring_atoms = {move.atom for move in scoring_moves}
        scoring_stationary = {
            pos
            for atom, pos in sim_current.items()
            if atom not in participating and atom not in scoring_atoms
        }
        score, graph_cache = score_move_graph(
            self.hw,
            tuple(scoring_moves),
            scoring_stationary,
            parent_graph_cache,
        )
        return EmissionCandidate(
            tuple(ordered),
            dict(target_positions),
            tuple(moves_in),
            (),
            frozenset(stationary),
            score,
            graph_cache=graph_cache,
        )

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
    ) -> None:
        blockers = {
            atom
            for atom, pos in current.items()
            if atom not in participating and pos in targets and pos != homes[atom]
        }
        self._release_atoms(program, homes, current, blockers)

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
    ) -> None:
        moves = [MoveOp(atom, current[atom], homes[atom]) for atom in sorted(atoms) if current[atom] != homes[atom]]
        stationary = {pos for atom, pos in current.items() if atom not in atoms}
        self._emit_moves(program, moves, stationary)
        for move in moves:
            current[move.atom] = move.dst

    def _emit_moves(self, program: HardwareProgram, moves: List[MoveOp], stationary: set[Position]) -> None:
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
    optimization_level: int = 1,
) -> CompileResult:
    return Compiler(hardware).compile(circuit, optimization_level)