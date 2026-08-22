"""Incremental normalization of native ZAC/ZAIR instructions.

``evaluation.normalize_zair`` accepts a completed ZAIR object and therefore
retains every canonical event until it can sort the dependency-ordered native
instructions by physical time.  Large compilation cannot afford that trace-
sized list.  This module implements the same instruction decoder as a bounded
state machine: architecture data is parsed once, the current logical location
of each atom is retained, and each native instruction is released immediately.

The released stream is in native dependency order.  Consumers that need the
legacy chronological representation can sort with
:func:`zair_chronological_sort_key`; bounded consumers should instead use the
``dependency`` mode of :mod:`streaming.trace_pipeline`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
import math
from pathlib import Path
from typing import Any

from evaluation.adapters import (
    _coordinate_map,
    _load_architecture,
    _location_map,
    _logical_position,
    _logical_region,
    _native_interval,
)
from evaluation.model import (
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    TraceValidationError,
    UnsupportedOperationError,
)


_ZAIR_EVENT_ORDER = {
    EventType.INIT: 0,
    EventType.LOAD: 1,
    EventType.MOVE: 2,
    EventType.STORE: 3,
    EventType.ONE_QUBIT_GATE: 4,
    EventType.TWO_QUBIT_GATE: 5,
    EventType.WAIT: 6,
}


def zair_chronological_sort_key(
    event: CanonicalTraceEvent,
) -> tuple[float, float, int, int]:
    """Return the exact deterministic key used by ``normalize_zair``."""

    return (
        event.start_us,
        event.end_us,
        _ZAIR_EVENT_ORDER[event.event_type],
        -1 if event.source_index is None else event.source_index,
    )


class IncrementalZairNormalizer:
    """Decode ZAIR native instructions without retaining emitted events.

    Parameters
    ----------
    architecture:
        The same architecture mapping or JSON path accepted by
        :func:`evaluation.normalize_zair`.  Formal streaming compilation
        requires it so all coordinates and regions come from the real device.
        It is parsed exactly once by this constructor.
    model:
        Frozen physical model used for native duration checks.

    Notes
    -----
    The first consumed instruction must be ``init``.  ``source_index`` is the
    global zero-based instruction position, independent of chunk boundaries,
    exactly matching a completed ZAIR instruction list.
    """

    def __init__(
        self,
        architecture: Mapping[str, Any] | str | Path,
        *,
        model: FidelityModel | None = None,
    ):
        self.model = model or FidelityModel()
        self._view = _load_architecture(architecture)
        if self._view is None:  # Defensive: the public type excludes None.
            raise ValueError("incremental ZAIR normalization requires architecture")
        self._current: dict[int, tuple[int, int, int]] = {}
        self._next_source_index = 0
        self._initialized = False

    @property
    def n_qubits(self) -> int:
        return len(self._current)

    @property
    def instructions_consumed(self) -> int:
        return self._next_source_index

    @property
    def current_locations(self) -> Mapping[int, tuple[int, int, int]]:
        """A read-only snapshot of the O(qubits) logical-location ledger."""

        return dict(self._current)

    def consume(
        self, instruction: Mapping[str, Any]
    ) -> tuple[CanonicalTraceEvent, ...]:
        """Consume one native instruction and return its physical-phase events."""

        source_index = self._next_source_index
        if not isinstance(instruction, Mapping):
            raise TraceValidationError(
                f"ZAIR instruction {source_index} is not an object"
            )
        kind = instruction.get("type")
        if not self._initialized:
            if kind != "init":
                raise TraceValidationError("ZAIR first instruction must be init")
            result = self._consume_init(instruction)
        else:
            if kind == "init":
                raise TraceValidationError("ZAIR contains a second init instruction")
            result = self._consume_native(instruction, source_index)
        self._next_source_index += 1
        return result

    def consume_many(
        self, instructions: Iterable[Mapping[str, Any]]
    ) -> Iterator[CanonicalTraceEvent]:
        """Consume a native chunk and yield events without building a chunk list."""

        for instruction in instructions:
            yield from self.consume(instruction)

    def _consume_init(
        self, instruction: Mapping[str, Any]
    ) -> tuple[CanonicalTraceEvent, ...]:
        current = _location_map(instruction.get("init_locs"), "init_locs")
        if set(current) != set(range(len(current))):
            raise TraceValidationError("ZAIR init atom ids must be contiguous from zero")
        init_atoms = tuple(sorted(current))
        init_positions = tuple(
            _logical_position(self._view, current[q]) for q in init_atoms
        )
        if len(set(init_positions)) != len(init_positions):
            raise TraceValidationError("two atoms occupy the same ZAIR initial position")
        init_regions = tuple(
            _logical_region(self._view, current[q]) for q in init_atoms
        )
        self._current = current
        self._initialized = True
        return (CanonicalTraceEvent(
            EventType.INIT,
            0.0,
            0.0,
            atoms=init_atoms,
            end_positions=init_positions,
            end_regions=init_regions,
            source_index=0,
            metadata={"source_format": "zair"},
        ),)

    def _consume_native(
        self,
        instruction: Mapping[str, Any],
        source_index: int,
    ) -> tuple[CanonicalTraceEvent, ...]:
        kind = instruction.get("type")
        raw_metadata = {
            "source_format": "zair",
            "raw_begin_time": instruction.get("begin_time"),
            "raw_end_time": instruction.get("end_time"),
            "native_id": instruction.get("id"),
        }

        if kind == "1qGate":
            return self._consume_one_qubit(
                instruction, source_index, raw_metadata)
        if kind == "rydberg":
            return self._consume_rydberg(
                instruction, source_index, raw_metadata)
        if kind == "rearrangeJob":
            return self._consume_rearrange(
                instruction, source_index, raw_metadata)
        raise UnsupportedOperationError(
            f"unsupported ZAIR instruction {kind!r} at source index {source_index}"
        )

    def _consume_one_qubit(
        self,
        instruction: Mapping[str, Any],
        source_index: int,
        raw_metadata: Mapping[str, Any],
    ) -> tuple[CanonicalTraceEvent, ...]:
        gates = instruction.get("gates")
        if not isinstance(gates, list) or not gates:
            raise TraceValidationError(f"ZAIR 1qGate {source_index} has no gates")
        atoms = tuple(int(gate["q"]) for gate in gates)
        if any(q not in self._current for q in atoms):
            raise TraceValidationError("ZAIR 1qGate references an unknown atom")
        native_begin, native_end = _native_interval(
            instruction, f"ZAIR 1qGate {source_index}")
        expected_end = native_begin + len(gates) * self.model.one_qubit_duration_us
        if not math.isclose(native_end, expected_end, rel_tol=0.0, abs_tol=1e-7):
            raise TraceValidationError(
                "ZAIR 1qGate timeline is not the frozen 52-us model: "
                f"expected [{native_begin}, {expected_end}], got "
                f"[{native_begin}, {native_end}]"
            )
        result: list[CanonicalTraceEvent] = []
        for gate_index, (gate, q) in enumerate(zip(gates, atoms)):
            position = _logical_position(self._view, self._current[q])
            atom_region = _logical_region(self._view, self._current[q])
            begin = native_begin + gate_index * self.model.one_qubit_duration_us
            end = begin + self.model.one_qubit_duration_us
            result.append(CanonicalTraceEvent(
                EventType.ONE_QUBIT_GATE,
                begin,
                end,
                atoms=(q,),
                region="global_1q",
                start_positions=(position,),
                end_positions=(position,),
                start_regions=(atom_region,),
                end_regions=(atom_region,),
                gate_names=(str(gate.get("name", "u")),),
                source_index=source_index,
                metadata={**raw_metadata, "native_gate_index": gate_index},
            ))
        return tuple(result)

    def _consume_rydberg(
        self,
        instruction: Mapping[str, Any],
        source_index: int,
        raw_metadata: Mapping[str, Any],
    ) -> tuple[CanonicalTraceEvent, ...]:
        raw_gates = instruction.get("gates")
        if not isinstance(raw_gates, list) or not raw_gates:
            raise TraceValidationError(f"ZAIR rydberg {source_index} has no gates")
        pairs = tuple(
            (int(gate["q0"]), int(gate["q1"])) for gate in raw_gates
        )
        participants = tuple(q for pair in pairs for q in pair)
        if any(q not in self._current for q in participants):
            raise TraceValidationError("ZAIR rydberg references an unknown atom")
        region_atoms = tuple(sorted(
            q for q, location in self._current.items()
            if _logical_region(self._view, location) == "entanglement"
        ))
        if not set(participants).issubset(region_atoms):
            raise TraceValidationError(
                "ZAIR CZ participant is outside the entanglement zone")
        native_begin, native_end = _native_interval(
            instruction, f"ZAIR rydberg {source_index}")
        expected_end = native_begin + self.model.rydberg_duration_us
        if not math.isclose(native_end, expected_end, rel_tol=0.0, abs_tol=1e-7):
            raise TraceValidationError(
                f"ZAIR rydberg duration must be {self.model.rydberg_duration_us} us"
            )
        return (CanonicalTraceEvent(
            EventType.TWO_QUBIT_GATE,
            native_begin,
            native_end,
            atoms=participants,
            region=f"entanglement:{instruction.get('zone_id', 0)}",
            gate_pairs=pairs,
            region_atoms=region_atoms,
            gate_names=("cz",) * len(pairs),
            source_index=source_index,
            metadata=raw_metadata,
        ),)

    def _consume_rearrange(
        self,
        instruction: Mapping[str, Any],
        source_index: int,
        raw_metadata: Mapping[str, Any],
    ) -> tuple[CanonicalTraceEvent, ...]:
        atoms = tuple(int(q) for q in instruction.get("aod_qubits", []))
        if not atoms or len(set(atoms)) != len(atoms):
            raise TraceValidationError(
                "ZAIR rearrangeJob has no atoms or duplicate atoms")
        begin_locs = _location_map(instruction.get("begin_locs"), "begin_locs")
        end_locs = _location_map(instruction.get("end_locs"), "end_locs")
        if set(atoms) != set(begin_locs) or set(atoms) != set(end_locs):
            raise TraceValidationError(
                "ZAIR rearrangeJob atom/location ledgers disagree")
        for q in atoms:
            if q not in self._current or self._current[q] != begin_locs[q]:
                raise TraceValidationError(
                    f"ZAIR rearrangeJob begins atom {q} at a stale location")

        begin_positions = tuple(
            _logical_position(self._view, begin_locs[q]) for q in atoms)
        end_positions = tuple(
            _logical_position(self._view, end_locs[q]) for q in atoms)
        begin_regions = tuple(
            _logical_region(self._view, begin_locs[q]) for q in atoms)
        end_regions = tuple(
            _logical_region(self._view, end_locs[q]) for q in atoms)
        batch_id = f"zair:{instruction.get('id', source_index)}"
        job_begin, job_end = _native_interval(
            instruction, f"ZAIR rearrangeJob {source_index}")
        physical = {q: begin_positions[index] for index, q in enumerate(atoms)}
        held: set[int] = set()
        active_rows: dict[int, float] = {}
        active_columns: dict[int, float] = {}
        details = instruction.get("insts", [])
        if not isinstance(details, list) or not details:
            raise TraceValidationError("ZAIR rearrangeJob has no physical phases")
        activate_indices = [
            phase for phase, detail in enumerate(details)
            if detail.get("type") == "activate"
        ]
        has_move = False
        result: list[CanonicalTraceEvent] = []
        for phase, detail in enumerate(details):
            native_type = str(detail.get("type", ""))
            phase_metadata = {
                **raw_metadata,
                "native_phase": phase,
                "native_type": native_type,
            }

            if native_type == "activate":
                phase_begin, phase_end = _native_interval(
                    detail, f"ZAIR activate {source_index}:{phase}")
                if (phase_begin < job_begin - 1e-7 or
                        phase_end > job_end + 1e-7):
                    raise TraceValidationError(
                        "ZAIR activate lies outside its rearrangeJob")
                if not math.isclose(
                    phase_end - phase_begin,
                    self.model.transfer_duration_us,
                    rel_tol=0.0,
                    abs_tol=1e-7,
                ):
                    raise TraceValidationError(
                        "ZAIR activate duration must be "
                        f"{self.model.transfer_duration_us} us")
                for beam, coordinate in zip(
                        detail.get("row_id", []), detail.get("row_y", [])):
                    active_rows[int(beam)] = float(coordinate)
                for beam, coordinate in zip(
                        detail.get("col_id", []), detail.get("col_x", [])):
                    active_columns[int(beam)] = float(coordinate)
                row_y = {round(value, 7) for value in active_rows.values()}
                col_x = {round(value, 7) for value in active_columns.values()}
                loaded = tuple(
                    q for q in atoms
                    if q not in held
                    and round(physical[q][0], 7) in col_x
                    and round(physical[q][1], 7) in row_y
                )
                if not loaded and phase == activate_indices[-1]:
                    loaded = tuple(q for q in atoms if q not in held)
                if not loaded:
                    raise TraceValidationError(
                        "ZAIR activate phase cannot be matched to a movement atom")
                positions = tuple(physical[q] for q in loaded)
                regions = tuple(begin_regions[atoms.index(q)] for q in loaded)
                result.append(CanonicalTraceEvent(
                    EventType.LOAD,
                    phase_begin,
                    phase_end,
                    atoms=loaded,
                    start_positions=positions,
                    end_positions=positions,
                    start_regions=regions,
                    end_regions=("aod",) * len(loaded),
                    batch_id=batch_id,
                    source_index=source_index,
                    metadata=phase_metadata,
                ))
                held.update(loaded)

            elif native_type.startswith("move"):
                if not held:
                    raise TraceValidationError(
                        "ZAIR move phase occurs before atom activation")
                phase_begin_us, phase_end_us = _native_interval(
                    detail, f"ZAIR move {source_index}:{phase}")
                if (phase_begin_us < job_begin - 1e-7 or
                        phase_end_us > job_end + 1e-7):
                    raise TraceValidationError(
                        "ZAIR move lies outside its rearrangeJob")
                phase_duration = phase_end_us - phase_begin_us
                if phase_duration < -1e-9:
                    raise TraceValidationError(
                        "ZAIR move phase has negative duration")
                moved = tuple(q for q in atoms if q in held)
                phase_begin = tuple(physical[q] for q in moved)
                coordinates = _coordinate_map(detail.get("end_coord", []))
                for q, position in coordinates.items():
                    if q in physical:
                        physical[q] = position
                for beam, coordinate in zip(
                    detail.get("row_id", []), detail.get("row_y_end", [])
                ):
                    active_rows[int(beam)] = float(coordinate)
                for beam, coordinate in zip(
                    detail.get("col_id", []), detail.get("col_x_end", [])
                ):
                    active_columns[int(beam)] = float(coordinate)
                phase_end = tuple(physical[q] for q in moved)
                result.append(CanonicalTraceEvent(
                    EventType.MOVE,
                    phase_begin_us,
                    phase_end_us,
                    atoms=moved,
                    start_positions=phase_begin,
                    end_positions=phase_end,
                    start_regions=("aod",) * len(moved),
                    end_regions=("aod",) * len(moved),
                    batch_id=batch_id,
                    source_index=source_index,
                    metadata=phase_metadata,
                ))
                has_move = True

            elif native_type == "deactivate":
                if not held:
                    raise TraceValidationError(
                        "ZAIR deactivate phase has no held atoms")
                stored = tuple(q for q in atoms if q in held)
                positions = tuple(
                    end_positions[atoms.index(q)] for q in stored)
                regions = tuple(end_regions[atoms.index(q)] for q in stored)
                phase_begin, phase_end = _native_interval(
                    detail, f"ZAIR deactivate {source_index}:{phase}")
                if (phase_begin < job_begin - 1e-7 or
                        phase_end > job_end + 1e-7):
                    raise TraceValidationError(
                        "ZAIR deactivate lies outside its rearrangeJob")
                if not math.isclose(
                    phase_end - phase_begin,
                    self.model.transfer_duration_us,
                    rel_tol=0.0,
                    abs_tol=1e-7,
                ):
                    raise TraceValidationError(
                        "ZAIR deactivate duration must be "
                        f"{self.model.transfer_duration_us} us")
                result.append(CanonicalTraceEvent(
                    EventType.STORE,
                    phase_begin,
                    phase_end,
                    atoms=stored,
                    start_positions=positions,
                    end_positions=positions,
                    start_regions=("aod",) * len(stored),
                    end_regions=regions,
                    batch_id=batch_id,
                    source_index=source_index,
                    metadata=phase_metadata,
                ))
                held.clear()

            else:
                raise UnsupportedOperationError(
                    f"unsupported ZAIR rearrangement phase {native_type!r}")

        if held:
            raise TraceValidationError("ZAIR rearrangeJob ends with atoms held")
        if not has_move:
            raise TraceValidationError("ZAIR rearrangeJob contains no move phase")
        self._current.update(end_locs)
        all_positions = [
            _logical_position(self._view, location)
            for location in self._current.values()
        ]
        if len(set(all_positions)) != len(all_positions):
            raise TraceValidationError(
                "two ZAIR atoms occupy the same position after store")
        return tuple(result)

    def state_dict(self) -> dict[str, Any]:
        """Return the bounded location/source-index state for checkpointing."""

        return {
            "format": "incremental-zair-normalizer-v1",
            "model": self.model.to_dict(),
            "initialized": self._initialized,
            "next_source_index": self._next_source_index,
            "current": [
                [q, *location] for q, location in sorted(self._current.items())
            ],
        }

    @classmethod
    def from_state(
        cls,
        value: Mapping[str, Any],
        *,
        architecture: Mapping[str, Any] | str | Path,
    ) -> "IncrementalZairNormalizer":
        state = dict(value)
        if state.pop("format", None) != "incremental-zair-normalizer-v1":
            raise ValueError("unsupported incremental ZAIR normalizer checkpoint")
        normalizer = cls(
            architecture,
            model=FidelityModel.from_mapping(state.pop("model")),
        )
        normalizer._initialized = bool(state.pop("initialized"))
        normalizer._next_source_index = int(state.pop("next_source_index"))
        normalizer._current = _location_map(state.pop("current"), "current")
        if not normalizer._initialized and normalizer._current:
            raise ValueError("incremental ZAIR normalizer initialization state is invalid")
        if normalizer._initialized and normalizer._next_source_index < 1:
            raise ValueError("incremental ZAIR normalizer source index is invalid")
        if not normalizer._initialized and normalizer._next_source_index != 0:
            raise ValueError("uninitialized ZAIR normalizer has consumed instructions")
        unknown = set(state)
        if unknown:
            raise ValueError(
                f"unknown incremental ZAIR normalizer fields: {sorted(unknown)}")
        return normalizer


__all__ = [
    "IncrementalZairNormalizer",
    "zair_chronological_sort_key",
]
