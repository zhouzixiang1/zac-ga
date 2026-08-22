"""Exact bounded-state driver for the production formal ZAC routers.

The production router assigns globally numbered native instructions and follows
dependencies by indexing ``result_json['instructions'][global_id]``.  Keeping a
normal Python list therefore makes routing memory grow with circuit depth even
though future layers only need the instructions named by the current qubit,
site, AOD, Rydberg, and global-1Q dependency ledgers.

This module supplies a list-compatible :class:`InstructionWindow` whose
``len()`` is the next global instruction id while its storage contains only
active dependency summaries plus the layer currently being generated.  The
driver presents each input mapping triplet as local layer zero to
``ZAC_zzx.route_qubit_mis``.  ``placer_kind='resident'`` dispatches unchanged
to ``_route_resident`` and coloring, while ``placer_kind='zac'`` executes the
formal M1 baseline branch with its registered greedy batcher.  Native
instruction ids and dependency state remain global, and the two route-log
entries are relabelled with the true physical layer after production returns.

No routing or timing rule is reimplemented here.  Ghost-safe coloring,
waypoint repair, native expansion, AOD assignment, Rydberg/1Q generation, and
dependency timing all execute in the existing production methods.  The only
output transformation is the same rearrangement flattening performed by
``Router_mixin.flatten_rearrangment_instruction`` before a batch trace is
published.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from itertools import chain
from typing import Any, Sequence

from zzx.zac_zzx import ZAC_zzx


Location = tuple[int, int, int]
FrozenMapping = tuple[Location, ...]


def _freeze_mapping(mapping: Sequence[Sequence[int]]) -> FrozenMapping:
    result = tuple(tuple(int(value) for value in location) for location in mapping)
    if any(len(location) != 3 for location in result):
        raise ValueError("every mapping location must be an (slm,row,column) triple")
    if len(set(result)) != len(result):
        raise ValueError("mapping contains duplicate physical sites")
    return result


def _mutable_mapping(mapping: FrozenMapping) -> list[Location]:
    return [tuple(location) for location in mapping]


def _instruction_summary(instruction: dict[str, Any]) -> dict[str, Any]:
    """Retain exactly the fields production ``get_begin_time`` may read later."""

    summary = {
        "id": int(instruction["id"]),
        "type": str(instruction["type"]),
        "begin_time": float(instruction.get("begin_time", 0.0)),
        "end_time": float(instruction.get("end_time", 0.0)),
    }
    if instruction["type"] == "rearrangeJob":
        # A future site dependency reads only the end of the old activate phase.
        # Preserve all activate records (normally one) to retain the production
        # max() behavior without holding rows, coordinates, or movement payloads.
        summary["insts"] = [
            {
                "type": str(detail["type"]),
                "begin_time": float(detail.get("begin_time", 0.0)),
                "end_time": float(detail.get("end_time", 0.0)),
            }
            for detail in instruction.get("insts", ())
            if str(detail.get("type", "")).split(":")[0] == "activate"
        ]
    return summary


class InstructionWindow:
    """Global-id instruction sequence with bounded active storage.

    ``len(window)`` intentionally returns the next global id, not the number of
    resident records.  This is the contract used everywhere in the native ZAC
    generator to allocate instruction ids and to scan the just-appended layer.
    ``active_count`` reports the actual in-memory record count.
    """

    def __init__(self):
        self._next_id = 0
        self._records: dict[int, dict[str, Any]] = {}
        self.peak_active_count = 0

    def __len__(self) -> int:
        return self._next_id

    @property
    def active_count(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._next_id = 0
        self._records.clear()
        self.peak_active_count = 0

    def append(self, instruction: dict[str, Any]) -> None:
        instruction_id = int(instruction.get("id", self._next_id))
        if instruction_id != self._next_id:
            raise ValueError(
                f"native instruction id {instruction_id} is not next global id "
                f"{self._next_id}")
        self._records[instruction_id] = instruction
        self._next_id += 1
        self.peak_active_count = max(self.peak_active_count, self.active_count)

    def _absolute_index(self, index: int) -> int:
        absolute = int(index)
        if absolute < 0:
            absolute += self._next_id
        if absolute < 0 or absolute >= self._next_id:
            raise IndexError("instruction index out of range")
        return absolute

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self._next_id)
            return [self[item] for item in range(start, stop, step)]
        absolute = self._absolute_index(index)
        try:
            return self._records[absolute]
        except KeyError as exc:
            raise KeyError(
                f"instruction {absolute} was pruned but is still being accessed") from exc

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for instruction_id in sorted(self._records):
            yield self._records[instruction_id]

    def output_chunk(self, start_id: int) -> tuple[dict[str, Any], ...]:
        """Flatten and deep-copy the newly generated publishable instructions."""

        if start_id < 0 or start_id > self._next_id:
            raise IndexError("chunk start is outside the global instruction range")
        output: list[dict[str, Any]] = []
        for instruction_id in range(start_id, self._next_id):
            instruction = self[instruction_id]
            if instruction.get("type") == "rearrangeJob":
                # Exact copy of Router_mixin.flatten_rearrangment_instruction,
                # scoped to this unflushed chunk so old summaries are untouched.
                instruction["aod_qubits"] = list(chain.from_iterable(
                    instruction["aod_qubits"]))
                instruction["begin_locs"] = list(chain.from_iterable(
                    instruction["begin_locs"]))
                instruction["end_locs"] = list(chain.from_iterable(
                    instruction["end_locs"]))
            output.append(deepcopy(instruction))
        return tuple(output)

    def prune_to(self, referenced_ids: set[int]) -> None:
        """Keep summaries only for instructions named by persistent ledgers."""

        keep = {int(value) for value in referenced_ids}
        missing = sorted(value for value in keep if value not in self._records)
        if missing:
            raise KeyError(f"active dependency instructions already pruned: {missing}")
        self._records = {
            instruction_id: _instruction_summary(self._records[instruction_id])
            for instruction_id in sorted(keep)
        }
        self.peak_active_count = max(self.peak_active_count, self.active_count)


@dataclass(frozen=True)
class ZACRouteTransitionResult:
    """Publishable output and bounded-state diagnostics for one physical layer."""

    layer: int
    instructions: tuple[dict[str, Any], ...]
    route_log: tuple[dict[str, Any], ...]
    runtime_us: float
    ghost_splits: int
    active_instruction_count: int
    peak_active_instruction_count: int


class ZACRouteTransitionDriver:
    """Drive a production formal ZAC router one mapping triplet at a time."""

    def __init__(
        self,
        architecture: Any,
        initial_mapping: Sequence[Sequence[int]],
        *,
        initial_one_qubit_gates: Sequence[Sequence[Any]] = (),
        window_size: int = 1000,
        placer_kind: str = "resident",
    ):
        self.architecture = architecture
        self.initial_mapping = _freeze_mapping(initial_mapping)
        if not self.initial_mapping:
            raise ValueError("router requires at least one atom")
        if int(window_size) <= 0:
            raise ValueError("window_size must be positive")
        if placer_kind not in {"zac", "resident"}:
            raise ValueError("placer_kind must be exactly 'zac' or 'resident'")
        self.placer_kind = placer_kind
        self.n_qubits = len(self.initial_mapping)
        self.next_layer = 0
        self.current_boundary = self.initial_mapping
        self.instructions = InstructionWindow()

        compiler = ZAC_zzx()
        compiler.architecture = architecture
        compiler.n_q = self.n_qubits
        compiler.placer_kind = placer_kind
        compiler.routing_strategy = (
            "greedy" if placer_kind == "zac" else "coloring")
        compiler.dynamic_placement = True
        compiler.reuse = True
        compiler.use_window = True
        compiler.window_size = int(window_size)
        compiler.qubit_mapping = [_mutable_mapping(self.initial_mapping)]
        compiler.gate_scheduling = []
        compiler.gate_scheduling_idx = []
        compiler.gate_1q_scheduling = []
        compiler.dict_g_1q_parent = {
            -1: self._one_qubit_gates(initial_one_qubit_gates),
        }
        compiler.result_json["instructions"] = self.instructions
        compiler.result_json["runtime"] = 0.0
        compiler.zzx_route_log = []
        compiler.zzx_ghost_splits = 0

        # Exact initialization from Router_mixin.route_qubit, followed by the
        # overridden write_initial_instruction that resets the global 1Q beam.
        compiler.aod_end_time = [
            (0, index) for index in range(len(architecture.dict_AOD))]
        compiler.aod_dependency = [
            0 for _ in range(len(architecture.dict_AOD))]
        compiler.rydberg_dependency = [
            0 for _ in range(len(architecture.entanglement_zone))]
        compiler.qubit_dependency = [0 for _ in range(self.n_qubits)]
        compiler.site_dependency = {}
        compiler.write_initial_instruction()
        self.compiler = compiler

        self.initial_instructions = self.instructions.output_chunk(0)
        self._prune()

    def _one_qubit_gates(
        self, gates: Sequence[Sequence[Any]],
    ) -> list[tuple[str, int]]:
        result: list[tuple[str, int]] = []
        for gate in gates:
            if len(gate) != 2:
                raise ValueError("1Q parent entry must be (name, qubit)")
            name, raw_qubit = str(gate[0]), int(gate[1])
            if not 0 <= raw_qubit < self.n_qubits:
                raise ValueError(f"1Q gate references unknown atom {raw_qubit}")
            result.append((name, raw_qubit))
        return result

    def _two_qubit_stage(
        self, gates: Sequence[Sequence[int]], gate_ids: Sequence[int],
    ) -> tuple[list[list[int]], list[int]]:
        if len(gates) != len(gate_ids):
            raise ValueError("CZ gate list and frozen gate-id list differ in length")
        if not gates:
            raise ValueError("physical ZAC stage cannot be empty")
        materialized: list[list[int]] = []
        ids: list[int] = []
        participants: set[int] = set()
        for gate, gate_id in zip(gates, gate_ids):
            if len(gate) != 2:
                raise ValueError("CZ entry must contain two atoms")
            q0, q1 = int(gate[0]), int(gate[1])
            if q0 == q1 or not (0 <= q0 < self.n_qubits) or not (
                    0 <= q1 < self.n_qubits):
                raise ValueError(f"invalid CZ gate {(q0, q1)}")
            if q0 in participants or q1 in participants:
                raise ValueError("physical ZAC stage contains overlapping CZ gates")
            participants.update((q0, q1))
            materialized.append([q0, q1])
            ids.append(int(gate_id))
        return materialized, ids

    def _active_dependency_ids(self) -> set[int]:
        compiler = self.compiler
        active = set(int(value) for value in compiler.qubit_dependency)
        active.update(int(value) for value in compiler.site_dependency.values())
        active.update(int(value) for value in compiler.aod_dependency)
        active.update(int(value) for value in compiler.rydberg_dependency)
        global_1q = getattr(compiler, "_last_global_1q_instruction", None)
        if global_1q is not None:
            active.add(int(global_1q))
        return active

    def _prune(self) -> None:
        self.instructions.prune_to(self._active_dependency_ids())

    def route_layer(
        self,
        layer: int,
        b_l: Sequence[Sequence[int]],
        g_l: Sequence[Sequence[int]],
        b_l_plus_1: Sequence[Sequence[int]],
        gates: Sequence[Sequence[int]],
        gate_ids: Sequence[int],
        parent_one_qubit_gates: Sequence[Sequence[Any]] = (),
    ) -> ZACRouteTransitionResult:
        """Generate, flush, and prune one exact resident native route chunk."""

        layer = int(layer)
        if layer != self.next_layer:
            raise ValueError(
                f"expected physical layer {self.next_layer}, received {layer}")
        boundary = _freeze_mapping(b_l)
        gate_mapping = _freeze_mapping(g_l)
        final_boundary = _freeze_mapping(b_l_plus_1)
        if boundary != self.current_boundary:
            raise ValueError("B_L does not continue the prior layer boundary")
        if not (len(boundary) == len(gate_mapping) == len(final_boundary)
                == self.n_qubits):
            raise ValueError("mapping triplet width differs from initial mapping")
        stage, ids = self._two_qubit_stage(gates, gate_ids)
        participants = {q for gate in stage for q in gate}
        for q in range(self.n_qubits):
            if q not in participants and gate_mapping[q] != boundary[q]:
                raise ValueError(
                    f"non-participant atom {q} moves in B_L -> G_L")
        one_qubit = self._one_qubit_gates(parent_one_qubit_gates)

        compiler = self.compiler
        compiler.qubit_mapping = [
            _mutable_mapping(boundary),
            _mutable_mapping(gate_mapping),
            _mutable_mapping(final_boundary),
        ]
        compiler.gate_scheduling = [stage]
        compiler.gate_scheduling_idx = [ids]
        compiler.gate_1q_scheduling = [one_qubit]

        instruction_start = len(self.instructions)
        log_start = len(compiler.zzx_route_log)
        # Resident dispatches unchanged to _route_resident.  M1 stays in the
        # production common ghost-safe branch with the registered greedy mode.
        compiler.route_qubit_mis(0)
        new_logs = compiler.zzx_route_log[log_start:]
        for entry in new_logs:
            entry["layer"] = layer

        chunk = self.instructions.output_chunk(instruction_start)
        route_log = tuple(deepcopy(entry) for entry in new_logs)
        # Logs are an output stream, not compiler state.  Keeping them inside the
        # production object would otherwise grow by two dictionaries per layer.
        del compiler.zzx_route_log[log_start:]

        self.current_boundary = final_boundary
        self.next_layer += 1
        self._prune()
        return ZACRouteTransitionResult(
            layer=layer,
            instructions=chunk,
            route_log=route_log,
            runtime_us=float(compiler.result_json["runtime"]),
            ghost_splits=int(getattr(compiler, "zzx_ghost_splits", 0)),
            active_instruction_count=self.instructions.active_count,
            peak_active_instruction_count=self.instructions.peak_active_count,
        )

    # State-machine spelling consistent with the placement transition kernel.
    step = route_layer


__all__ = [
    "InstructionWindow",
    "ZACRouteTransitionDriver",
    "ZACRouteTransitionResult",
]
