"""The hardware-aware compiler.

Pipeline:

    front-end (IR)  ->  placement  ->  scheduling  ->  routing / movement
                    ->  hardware-operation sequence (+ metrics)

Two-qubit gates are the only operations that require the entanglement zone, so
the router transports the relevant atom pair from their storage homes into a
free column of the 2-row entanglement region, fires a (parallel) Rydberg CZ
pulse, then returns the atoms home.  Up to ``hardware.interaction_columns()``
CZ gates run per Rydberg pulse; larger parallel layers are split into chunks.
Single-qubit gates and measurements are performed in place at the atom home.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Union

from qiskit import QuantumCircuit

from .hardware import ENTANGLEMENT, HardwareSpec, Position, default_hardware
from .ir import (
    IRGate1Q,
    IRGate2Q,
    IRMeasure,
    LogicalCircuit,
    from_qasm,
    from_qiskit,
)
from .metrics import Metrics, summarize
from .movement import batch_duration, plan_batches
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


@dataclass
class CompileResult:
    program: HardwareProgram
    metrics: Metrics
    logical: LogicalCircuit


def _to_logical(circuit: CircuitInput, optimization_level: int) -> LogicalCircuit:
    if isinstance(circuit, LogicalCircuit):
        return circuit
    if isinstance(circuit, QuantumCircuit):
        return from_qiskit(circuit, optimization_level)
    if isinstance(circuit, str):
        return from_qasm(circuit, optimization_level)
    raise TypeError(f"unsupported circuit input: {type(circuit)!r}")


class Compiler:
    """Compiles a logical circuit into a hardware-operation sequence."""

    def __init__(self, hardware: HardwareSpec | None = None):
        self.hw = hardware or default_hardware()

    # ------------------------------------------------------------------ API
    def compile(
        self, circuit: CircuitInput, optimization_level: int = 1
    ) -> CompileResult:
        logical = _to_logical(circuit, optimization_level)
        homes = place(logical, self.hw)
        slots = schedule(logical)

        program = HardwareProgram(
            num_qubits=logical.num_qubits, num_clbits=logical.num_clbits
        )
        self._emit_init(program, homes)

        for slot in slots:
            self._emit_slot(program, slot, homes)

        metrics = summarize(program, self.hw)
        return CompileResult(program=program, metrics=metrics, logical=logical)

    # ------------------------------------------------------------- emitters
    def _emit_init(self, program: HardwareProgram, homes: Dict[int, Position]):
        ops = [InitOp(atom=q, position=pos) for q, pos in sorted(homes.items())]
        program.add(Stage(kind=INIT, ops=ops, duration=0.0, fidelity=1.0))

    def _emit_slot(
        self,
        program: HardwareProgram,
        slot: List[object],
        homes: Dict[int, Position],
    ):
        oneq = [op for op in slot if isinstance(op, IRGate1Q)]
        twoq = [op for op in slot if isinstance(op, IRGate2Q)]
        meas = [op for op in slot if isinstance(op, IRMeasure)]

        if oneq:
            self._emit_1q(program, oneq, homes)
        if twoq:
            self._emit_2q(program, twoq, homes)
        if meas:
            self._emit_measure(program, meas, homes)

    def _emit_1q(self, program, oneq, homes):
        ops = [
            OneQubitGate(
                name=g.name, params=g.params, atom=g.qubit, position=homes[g.qubit]
            )
            for g in oneq
        ]
        program.add(
            Stage(
                kind=ONEQ,
                ops=ops,
                duration=self.hw.t_1q,
                fidelity=self.hw.f_1q ** len(ops),
            )
        )

    def _emit_2q(self, program, twoq, homes):
        capacity = self.hw.interaction_columns()
        # Order the (commuting) CZ pairs by the spatial position of their atoms
        # so that consecutive entanglement columns are fed by atoms that are
        # already in the same left-to-right order.  Trajectories then run
        # (near-)parallel without crossing, letting the AOD move the whole
        # chunk in a single batch instead of many tiny ones.
        ordered = sorted(twoq, key=lambda g: self._pair_key(g, homes))
        for start in range(0, len(ordered), capacity):
            chunk = ordered[start : start + capacity]
            self._emit_2q_chunk(program, chunk, homes)

    def _pair_key(self, gate, homes):
        (x0, y0) = self.hw.coordinate(homes[gate.q0])
        (x1, y1) = self.hw.coordinate(homes[gate.q1])
        # Sort primarily by mean column, tie-break by mean row.
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    def _assign_columns(self, chunk, homes) -> List[int]:
        """Choose one entanglement column per CZ pair, minimizing travel.

        The pairs arrive sorted by mean storage column; columns are assigned
        in the same increasing order (so trajectories never cross) while
        minimizing the summed horizontal transport of both atoms.  Solved
        exactly with a small order-preserving assignment DP.
        """
        num_cols = self.hw.entanglement.cols
        k = len(chunk)

        def cost(gate, col):
            x0 = self.hw.coordinate(homes[gate.q0])[0]
            x1 = self.hw.coordinate(homes[gate.q1])[0]
            return abs(x0 - col) + abs(x1 - col)

        INF = float("inf")
        # dp[c] = best cost for the current pair placed at column c
        dp = [cost(chunk[0], c) for c in range(num_cols)]
        choice: List[List[int]] = []
        for i in range(1, k):
            best_prev = [INF] * num_cols  # min dp[c'] over c' < c
            arg_prev = [-1] * num_cols
            running, arg = INF, -1
            for c in range(num_cols):
                best_prev[c], arg_prev[c] = running, arg
                if dp[c] < running:
                    running, arg = dp[c], c
            ndp = [INF] * num_cols
            for c in range(i, num_cols):
                if best_prev[c] < INF:
                    ndp[c] = best_prev[c] + cost(chunk[i], c)
            choice.append(arg_prev)
            dp = ndp

        cols = [min(range(num_cols), key=lambda c: dp[c])]
        for i in range(k - 1, 0, -1):
            cols.append(choice[i - 1][cols[-1]])
        cols.reverse()
        return cols

    def _emit_2q_chunk(self, program, chunk, homes):
        ent_positions = []  # per pair: (pos0, pos1) aligned to (q0, q1)
        for col, gate in zip(self._assign_columns(chunk, homes), chunk):
            # Assign the two atoms to rows 0/1 so their vertical order matches
            # their storage order; this avoids intra-pair crossing.
            if self.hw.coordinate(homes[gate.q0])[1] <= self.hw.coordinate(
                homes[gate.q1]
            )[1]:
                pos0 = (ENTANGLEMENT, 0, col)
                pos1 = (ENTANGLEMENT, 1, col)
            else:
                pos0 = (ENTANGLEMENT, 1, col)
                pos1 = (ENTANGLEMENT, 0, col)
            ent_positions.append((pos0, pos1))

        # Atoms that stay parked at home while this chunk is transported.
        participating = set()
        for gate in chunk:
            participating.add(gate.q0)
            participating.add(gate.q1)
        stationary = {
            pos for atom, pos in homes.items() if atom not in participating
        }

        # 1) transport atoms into the entanglement zone (parallel, batched)
        moves_in = []
        for gate, (pos0, pos1) in zip(chunk, ent_positions):
            moves_in.append(MoveOp(atom=gate.q0, src=homes[gate.q0], dst=pos0))
            moves_in.append(MoveOp(atom=gate.q1, src=homes[gate.q1], dst=pos1))
        self._emit_moves(program, moves_in, stationary)

        # 2) parallel Rydberg CZ pulse
        cz_ops = [
            TwoQubitGate(
                name="cz",
                atom0=gate.q0,
                atom1=gate.q1,
                pos0=pos0,
                pos1=pos1,
            )
            for gate, (pos0, pos1) in zip(chunk, ent_positions)
        ]
        program.add(
            Stage(
                kind=TWOQ,
                ops=cz_ops,
                duration=self.hw.t_2q,
                fidelity=self.hw.f_2q ** len(cz_ops),
            )
        )

        # 3) transport atoms back home (parallel, batched)
        moves_out = []
        for gate, (pos0, pos1) in zip(chunk, ent_positions):
            moves_out.append(MoveOp(atom=gate.q0, src=pos0, dst=homes[gate.q0]))
            moves_out.append(MoveOp(atom=gate.q1, src=pos1, dst=homes[gate.q1]))
        self._emit_moves(program, moves_out, stationary)

    def _emit_measure(self, program, meas, homes):
        ops = [
            MeasureOp(atom=g.qubit, position=homes[g.qubit], clbit=g.clbit)
            for g in meas
        ]
        program.add(
            Stage(
                kind=MEASURE,
                ops=ops,
                duration=self.hw.t_measure,
                fidelity=self.hw.f_measure ** len(ops),
            )
        )

    # ------------------------------------------------------------- helpers
    def _emit_moves(self, program, moves, stationary):
        """Split ``moves`` into AOD-compatible batches and emit one stage each.

        Each batch is a maximal set of moves that can run in parallel without
        colliding, crossing (when trap crossing is disabled) or exceeding the
        AOD's atom-per-batch capacity.  Incompatible moves land in later
        batches, so this both maximizes parallelism and minimizes batch count.
        """
        for batch in plan_batches(self.hw, moves, stationary):
            program.add(self._move_stage(batch))

    def _move_stage(self, moves: List[MoveOp]) -> Stage:
        if not moves:
            return Stage(kind=MOVE, ops=[], duration=0.0, fidelity=1.0)
        # A batch moves in parallel, so its duration is set by the longest
        # single trajectory (smooth, kinematically-limited) plus AOD overhead.
        travel = batch_duration(self.hw, moves)
        duration = self.hw.t_move_fixed + travel
        decoherence = math.exp(-duration / self.hw.t2_coherence) ** len(moves)
        fidelity = (self.hw.f_transfer ** len(moves)) * decoherence
        return Stage(kind=MOVE, ops=moves, duration=duration, fidelity=fidelity)


def compile_circuit(
    circuit: CircuitInput,
    hardware: HardwareSpec | None = None,
    optimization_level: int = 1,
) -> CompileResult:
    """Convenience wrapper around :class:`Compiler`."""
    return Compiler(hardware).compile(circuit, optimization_level)
