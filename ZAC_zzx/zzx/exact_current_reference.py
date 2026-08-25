"""Production-router reference for exact current-boundary scheduling.

The native search must not call Python once per chromosome.  This module is
therefore deliberately a *reference and state owner*, not the formal hot path:

* Python owns one bounded production-router state across physical layers.
* A source-prefix fork emits the fixed ``out -> CZ -> parent 1Q`` prefix with no
  back movement and exposes its compact :class:`SchedulerLedgerSnapshot`.
* A candidate differential fork emits the executable source back movement and
  the target ``out -> CZ -> parent 1Q`` prefix, then computes the exact absolute
  idle log-ratio used for C++ differential tests.
* Committing a winner advances the persistent reference by exactly one source
  layer.  Its final timing hash must equal the independently routed trace.

All geometry expansion, ghost-safe batching, dependencies, AOD assignment and
timestamps come from the production ``ZACRouteTransitionDriver``.  No timing
rule is duplicated here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence

from streaming.zac_route_transition import ZACRouteTransitionDriver

from .scheduler_ledger import SchedulerLedgerSnapshot


Location = tuple[int, int, int]
FrozenMapping = tuple[Location, ...]


def _freeze_mapping(mapping: Sequence[Sequence[int]]) -> FrozenMapping:
    result = tuple(tuple(int(value) for value in site) for site in mapping)
    if any(len(site) != 3 for site in result):
        raise ValueError("scheduler reference mapping sites must be triples")
    if len(set(result)) != len(result):
        raise ValueError("scheduler reference mapping repeats a site")
    return result


def _freeze_gates(gates: Sequence[Sequence[int]]) -> tuple[tuple[int, int], ...]:
    return tuple((int(gate[0]), int(gate[1])) for gate in gates)


def _freeze_one_qubit(
        gates: Sequence[Sequence[Any]]) -> tuple[tuple[str, int], ...]:
    return tuple((str(gate[0]), int(gate[1])) for gate in gates)


def _coherence_log_ratio(
        before: Sequence[float], after: Sequence[float], t2_us: float,
) -> float:
    """Return the search-time coherence increment for one boundary.

    The paper's linear factor is authoritative while every absolute idle time
    is strictly below ``T2``.  At and beyond that boundary the final linear
    fidelity is out of domain, but rejecting every later placement candidate
    would prevent the compiler from completing the trace that the independent
    scorer must diagnose.  Search therefore switches the *whole boundary*
    comparison to the registered exponential sensitivity increment
    ``sum(after - before) / T2``.  This is a continuation for candidate ranking,
    not a clipped or fabricated linear fidelity.
    """
    t2 = float(t2_us)
    if not math.isfinite(t2) or t2 <= 0.0:
        raise ValueError("coherence T2 must be finite and positive")
    if len(before) != len(after):
        raise ValueError("scheduler idle vectors differ in width")
    frozen_before = tuple(float(value) for value in before)
    frozen_after = tuple(float(value) for value in after)
    if any(not math.isfinite(value) or value < 0.0
           for value in (*frozen_before, *frozen_after)):
        raise ValueError(
            "absolute coherence idle times must be finite and non-negative")
    if any(prior >= t2 or current >= t2
           for prior, current in zip(frozen_before, frozen_after)):
        return sum(current - prior for prior, current in zip(
            frozen_before, frozen_after)) / t2
    value = 0.0
    for prior, current in zip(frozen_before, frozen_after):
        value += math.log1p(-prior / t2) - math.log1p(-current / t2)
    return value


@dataclass(frozen=True, slots=True)
class ExactBoundaryPrefix:
    source_layer: int
    source_boundary: FrozenMapping
    source_gate_mapping: FrozenMapping
    scheduler: SchedulerLedgerSnapshot
    qubit_dependency_end_us: tuple[float, ...]
    back_dependency_end_us: tuple[float, ...]
    site_dependency_activation_finish_us: tuple[
        tuple[Location, float], ...]


@dataclass(frozen=True, slots=True)
class ExactBoundaryCandidate:
    source_layer: int
    coherence_negative_log_fidelity: float
    idle_before_us: tuple[float, ...]
    idle_after_us: tuple[float, ...]
    scheduler_after: SchedulerLedgerSnapshot
    source_instruction_count: int
    target_instruction_count: int
    source_back_batches: tuple[tuple[int, ...], ...]
    target_out_batches: tuple[tuple[int, ...], ...]
    source_back_time_us: float
    target_out_time_us: float
    zero_duration_shiftback_count: int
    zero_duration_shiftback_distance_um: float


class ExactCurrentReferenceScheduler:
    """Bounded Python owner for production-exact boundary scheduler state."""

    STATE_FORMAT = "exact-current-reference-state-v1"

    def __init__(
            self,
            architecture,
            initial_mapping: Sequence[Sequence[int]],
            *,
            leading_one_qubit_gates: Sequence[Sequence[Any]] = (),
            coloring_exact_threshold: int = 24,
    ) -> None:
        self.architecture = architecture
        self.initial_mapping = _freeze_mapping(initial_mapping)
        self.driver = ZACRouteTransitionDriver(
            architecture,
            self.initial_mapping,
            initial_one_qubit_gates=_freeze_one_qubit(
                leading_one_qubit_gates),
            placer_kind="resident",
            coloring_exact_threshold=coloring_exact_threshold,
        )

    @property
    def next_layer(self) -> int:
        return self.driver.next_layer

    @property
    def current_boundary(self) -> FrozenMapping:
        return self.driver.current_boundary

    @property
    def scheduler_snapshot(self) -> SchedulerLedgerSnapshot:
        return self.driver.scheduler.snapshot()

    @staticmethod
    def _gate_ids(gates: Sequence[Sequence[int]]) -> tuple[int, ...]:
        # Logical gate ids do not enter any scheduler timing rule.  A stable
        # layer-local sequence keeps the reference independent of trace-sized
        # gate-id storage while retaining exact native instruction structure.
        return tuple(range(len(gates)))

    def _fork(self) -> ZACRouteTransitionDriver:
        return ZACRouteTransitionDriver.from_state(
            self.architecture,
            self.initial_mapping,
            deepcopy(self.driver.state_dict()),
        )

    def prepare_source_prefix(
            self,
            source_layer: int,
            source_gate_mapping: Sequence[Sequence[int]],
            source_gates: Sequence[Sequence[int]],
            source_one_qubit_gates: Sequence[Sequence[Any]] = (),
    ) -> ExactBoundaryPrefix:
        source_layer = int(source_layer)
        if source_layer != self.next_layer:
            raise ValueError(
                f"scheduler reference expected layer {self.next_layer}, "
                f"received {source_layer}")
        gate_mapping = _freeze_mapping(source_gate_mapping)
        gates = _freeze_gates(source_gates)
        fork = self._fork()
        routed = fork.route_layer(
            source_layer,
            self.current_boundary,
            gate_mapping,
            gate_mapping,
            gates,
            self._gate_ids(gates),
            _freeze_one_qubit(source_one_qubit_gates),
        )
        compiler = fork.compiler
        qubit_dependency_end = tuple(
            float(fork.scheduler.instructions[int(instruction_id)].end_us)
            for instruction_id in compiler.qubit_dependency)
        source_participants = {atom for gate in gates for atom in gate}
        gate_timings = tuple(
            fork.scheduler.instructions[int(instruction["id"])]
            for instruction in routed.instructions
            if instruction.get("type") in {"rydberg", "1qGate"}
        )
        if not gate_timings:
            raise RuntimeError("source scheduler prefix contains no gate instruction")
        source_gate_barrier = gate_timings[-1].end_us
        back_dependency_end = tuple(
            dependency if atom in source_participants
            else max(dependency, source_gate_barrier)
            for atom, dependency in enumerate(qubit_dependency_end)
        )
        site_dependency = []
        for location, instruction_id in sorted(
                compiler.site_dependency.items()):
            timing = fork.scheduler.instructions[int(instruction_id)]
            if timing.activation_finish_us is None:
                raise RuntimeError(
                    "scheduler site dependency lacks AOD activation timing")
            site_dependency.append((
                tuple(int(value) for value in location),
                float(timing.activation_finish_us),
            ))
        return ExactBoundaryPrefix(
            source_layer=source_layer,
            source_boundary=self.current_boundary,
            source_gate_mapping=gate_mapping,
            scheduler=fork.scheduler.snapshot(),
            qubit_dependency_end_us=qubit_dependency_end,
            back_dependency_end_us=back_dependency_end,
            site_dependency_activation_finish_us=tuple(site_dependency),
        )

    def evaluate_candidate(
            self,
            *,
            prefix: ExactBoundaryPrefix,
            boundary_mapping: Sequence[Sequence[int]],
            source_gates: Sequence[Sequence[int]],
            source_one_qubit_gates: Sequence[Sequence[Any]],
            target_gate_mapping: Sequence[Sequence[int]],
            target_gates: Sequence[Sequence[int]],
            target_one_qubit_gates: Sequence[Sequence[Any]],
            coherence_t2_us: float = 1_500_000.0,
    ) -> ExactBoundaryCandidate:
        if prefix.source_layer != self.next_layer:
            raise ValueError("candidate prefix is stale")
        boundary = _freeze_mapping(boundary_mapping)
        target_gate = _freeze_mapping(target_gate_mapping)
        source = _freeze_gates(source_gates)
        target = _freeze_gates(target_gates)
        fork = self._fork()
        source_result = fork.route_layer(
            prefix.source_layer,
            prefix.source_boundary,
            prefix.source_gate_mapping,
            boundary,
            source,
            self._gate_ids(source),
            _freeze_one_qubit(source_one_qubit_gates),
        )
        target_result = fork.route_layer(
            prefix.source_layer + 1,
            boundary,
            target_gate,
            target_gate,
            target,
            self._gate_ids(target),
            _freeze_one_qubit(target_one_qubit_gates),
        )

        def gate_bounds(instructions):
            gates = [index for index, instruction in enumerate(instructions)
                     if instruction.get("type") in {"rydberg", "1qGate"}]
            if not gates:
                raise RuntimeError("candidate route contains no gate instruction")
            return gates[0], gates[-1]

        source_first_gate, source_last_gate = gate_bounds(
            source_result.instructions)
        target_first_gate, _target_last_gate = gate_bounds(
            target_result.instructions)
        source_back = tuple(
            instruction for index, instruction in enumerate(
                source_result.instructions)
            if index > source_last_gate
            and instruction.get("type") == "rearrangeJob")
        target_out = tuple(
            instruction for index, instruction in enumerate(
                target_result.instructions)
            if index < target_first_gate
            and instruction.get("type") == "rearrangeJob")

        def batch_atoms(instructions):
            return tuple(tuple(sorted(int(atom) for atom in
                                      instruction.get("aod_qubits", ())))
                         for instruction in instructions)

        def move_time(instructions):
            return sum(float(instruction["end_time"])
                       - float(instruction["begin_time"])
                       for instruction in instructions)

        zero_shiftbacks = []
        for instruction in (*source_back, *target_out):
            for detail in instruction.get("insts", ()):
                if (str(detail.get("type", "")).split(":", 1)[0] != "move"
                        or detail.get("move_type") != "before"):
                    continue
                if abs(float(detail.get("end_time", 0.0))
                       - float(detail.get("begin_time", 0.0))) > 1e-12:
                    continue
                distance = sum(abs(float(end) - float(begin))
                               for begin, end in zip(
                                   detail.get("col_x_begin", ()),
                                   detail.get("col_x_end", ())))
                zero_shiftbacks.append(distance)
        before = prefix.scheduler.idle_time_us
        after = fork.scheduler.idle_time_us
        return ExactBoundaryCandidate(
            source_layer=prefix.source_layer,
            coherence_negative_log_fidelity=_coherence_log_ratio(
                before, after, coherence_t2_us),
            idle_before_us=before,
            idle_after_us=after,
            scheduler_after=fork.scheduler.snapshot(),
            source_instruction_count=len(source_result.instructions),
            target_instruction_count=len(target_result.instructions),
            source_back_batches=batch_atoms(source_back),
            target_out_batches=batch_atoms(target_out),
            source_back_time_us=move_time(source_back),
            target_out_time_us=move_time(target_out),
            zero_duration_shiftback_count=len(zero_shiftbacks),
            zero_duration_shiftback_distance_um=sum(zero_shiftbacks),
        )

    def commit_source(
            self,
            source_layer: int,
            source_gate_mapping: Sequence[Sequence[int]],
            boundary_mapping: Sequence[Sequence[int]],
            source_gates: Sequence[Sequence[int]],
            source_one_qubit_gates: Sequence[Sequence[Any]] = (),
    ) -> None:
        source_layer = int(source_layer)
        if source_layer != self.next_layer:
            raise ValueError(
                f"scheduler reference expected layer {self.next_layer}, "
                f"received {source_layer}")
        gates = _freeze_gates(source_gates)
        self.driver.route_layer(
            source_layer,
            self.current_boundary,
            _freeze_mapping(source_gate_mapping),
            _freeze_mapping(boundary_mapping),
            gates,
            self._gate_ids(gates),
            _freeze_one_qubit(source_one_qubit_gates),
        )

    def state_dict(self) -> dict[str, Any]:
        return {
            "format": self.STATE_FORMAT,
            "initial_mapping": self.initial_mapping,
            "driver": self.driver.state_dict(),
        }

    @classmethod
    def from_state(
            cls,
            architecture,
            initial_mapping: Sequence[Sequence[int]],
            state: Mapping[str, Any],
    ) -> "ExactCurrentReferenceScheduler":
        if state.get("format") != cls.STATE_FORMAT:
            raise ValueError("unsupported exact-current reference state")
        initial = _freeze_mapping(initial_mapping)
        if initial != _freeze_mapping(state["initial_mapping"]):
            raise ValueError("exact-current reference initial mapping mismatch")
        value = cls.__new__(cls)
        value.architecture = architecture
        value.initial_mapping = initial
        value.driver = ZACRouteTransitionDriver.from_state(
            architecture, initial, deepcopy(state["driver"]))
        return value


__all__ = [
    "ExactBoundaryCandidate",
    "ExactBoundaryPrefix",
    "ExactCurrentReferenceScheduler",
]
