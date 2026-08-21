"""Typed, implementation-independent events and fidelity results.

The rest of ZAC produces two different native schedule formats.  This module
deliberately contains no ZAC or MQT imports so that a trace can be archived and
rescored without recreating either compiler environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Mapping, Sequence


Position = tuple[float, float]
GatePair = tuple[int, int]


class TraceValidationError(ValueError):
    """The native or canonical trace is incomplete or physically ambiguous."""


class UnsupportedOperationError(TraceValidationError):
    """A native trace contains an operation outside the frozen experiment IR."""


class EventType(str, Enum):
    """Operations understood by the unified physical scorer."""

    INIT = "init"
    ONE_QUBIT_GATE = "one_qubit_gate"
    TWO_QUBIT_GATE = "two_qubit_gate"
    LOAD = "load"
    MOVE = "move"
    STORE = "store"
    WAIT = "wait"


def _positions(values: Sequence[Sequence[float]]) -> tuple[Position, ...]:
    result: list[Position] = []
    for value in values:
        if len(value) != 2:
            raise TraceValidationError(f"position must have two coordinates: {value!r}")
        position = (float(value[0]), float(value[1]))
        if not all(math.isfinite(v) for v in position):
            raise TraceValidationError(f"position must be finite: {value!r}")
        result.append(position)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CanonicalTraceEvent:
    """One event in the common ZAC/QMAP physical trace.

    ``atoms`` are the atoms directly operated on.  For a global Rydberg pulse,
    ``gate_pairs`` identifies the atoms executing CZ gates while
    ``region_atoms`` contains every atom illuminated in the entanglement zone;
    their set difference is therefore the idle-excitation population.

    Adapters emit physical coordinates in micrometres and normalized physical
    time in microseconds.  Loading and storing are separate events so transfer
    errors are counted exactly once per atom per phase.

    A ``one_qubit_gate`` event represents exactly one physical gate on one
    atom.  Adapters serialize a native multi-gate block into consecutive 52-us
    events because the frozen model assumes global sequential 1Q execution.
    """

    event_type: EventType | str
    start_us: float
    end_us: float
    atoms: tuple[int, ...] = ()
    region: str | None = None
    start_positions: tuple[Position, ...] = ()
    end_positions: tuple[Position, ...] = ()
    start_regions: tuple[str, ...] = ()
    end_regions: tuple[str, ...] = ()
    batch_id: str | None = None
    gate_pairs: tuple[GatePair, ...] = ()
    region_atoms: tuple[int, ...] = ()
    gate_names: tuple[str, ...] = ()
    source_index: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        try:
            event_type = EventType(self.event_type)
        except ValueError as exc:
            raise TraceValidationError(f"unknown canonical event type: {self.event_type!r}") from exc
        object.__setattr__(self, "event_type", event_type)

        start_us, end_us = float(self.start_us), float(self.end_us)
        if not math.isfinite(start_us) or not math.isfinite(end_us):
            raise TraceValidationError("event times must be finite")
        if start_us < 0 or end_us < start_us:
            raise TraceValidationError(
                f"invalid event interval [{start_us}, {end_us}] for {event_type.value}"
            )
        object.__setattr__(self, "start_us", start_us)
        object.__setattr__(self, "end_us", end_us)

        atoms = tuple(int(q) for q in self.atoms)
        region_atoms = tuple(int(q) for q in self.region_atoms)
        if any(q < 0 for q in atoms + region_atoms):
            raise TraceValidationError("atom identifiers must be non-negative")
        if len(set(atoms)) != len(atoms):
            raise TraceValidationError(f"duplicate atom in event: {atoms!r}")
        if len(set(region_atoms)) != len(region_atoms):
            raise TraceValidationError(f"duplicate region atom: {region_atoms!r}")
        object.__setattr__(self, "atoms", atoms)
        object.__setattr__(self, "region_atoms", region_atoms)

        start_positions = _positions(self.start_positions)
        end_positions = _positions(self.end_positions)
        start_regions = tuple(str(v) for v in self.start_regions)
        end_regions = tuple(str(v) for v in self.end_regions)
        for label, values in (
            ("start_positions", start_positions),
            ("end_positions", end_positions),
            ("start_regions", start_regions),
            ("end_regions", end_regions),
        ):
            if values and len(values) != len(atoms):
                raise TraceValidationError(
                    f"{label} has {len(values)} entries for {len(atoms)} atoms"
                )
        object.__setattr__(self, "start_positions", start_positions)
        object.__setattr__(self, "end_positions", end_positions)
        object.__setattr__(self, "start_regions", start_regions)
        object.__setattr__(self, "end_regions", end_regions)

        pairs = tuple((int(pair[0]), int(pair[1])) for pair in self.gate_pairs)
        for pair in pairs:
            if pair[0] < 0 or pair[1] < 0 or pair[0] == pair[1]:
                raise TraceValidationError(f"invalid two-qubit gate pair: {pair!r}")
        if len({q for pair in pairs for q in pair}) != 2 * len(pairs):
            raise TraceValidationError("an atom occurs in multiple gates in one Rydberg pulse")
        object.__setattr__(self, "gate_pairs", pairs)
        object.__setattr__(self, "gate_names", tuple(str(v) for v in self.gate_names))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.batch_id is not None:
            object.__setattr__(self, "batch_id", str(self.batch_id))

        if event_type is EventType.TWO_QUBIT_GATE:
            if not pairs:
                raise TraceValidationError("a two-qubit event must contain at least one gate pair")
            participants = tuple(q for pair in pairs for q in pair)
            if set(atoms) != set(participants):
                raise TraceValidationError(
                    "two-qubit event atoms must exactly match flattened gate_pairs"
                )
            illuminated = set(region_atoms or atoms)
            if not set(participants).issubset(illuminated):
                raise TraceValidationError("gate participant is absent from region_atoms")
        elif pairs or region_atoms:
            raise TraceValidationError(
                "gate_pairs and region_atoms are only valid for two-qubit events"
            )

        if event_type in {EventType.LOAD, EventType.MOVE, EventType.STORE}:
            if self.batch_id is None:
                raise TraceValidationError(f"{event_type.value} event requires batch_id")
            if not atoms:
                raise TraceValidationError(f"{event_type.value} event requires at least one atom")
        elif self.batch_id is not None:
            raise TraceValidationError(f"batch_id is invalid for {event_type.value}")

    @property
    def kind(self) -> str:
        """Compact string alias used by JSONL consumers."""

        return self.event_type.value

    @property
    def duration_us(self) -> float:
        return self.end_us - self.start_us

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "atoms": list(self.atoms),
            "region": self.region,
            "start_positions": [list(p) for p in self.start_positions],
            "end_positions": [list(p) for p in self.end_positions],
            "start_regions": list(self.start_regions),
            "end_regions": list(self.end_regions),
            "batch_id": self.batch_id,
            "gate_pairs": [list(pair) for pair in self.gate_pairs],
            "region_atoms": list(self.region_atoms),
            "gate_names": list(self.gate_names),
            "source_index": self.source_index,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalTraceEvent":
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class FidelityModel:
    """Frozen physical parameters from the ZAC paper, in microseconds."""

    one_qubit_fidelity: float = 0.9997
    two_qubit_fidelity: float = 0.995
    idle_excitation_fidelity: float = 0.9975
    transfer_fidelity: float = 0.999
    coherence_time_us: float = 1.5e6
    transfer_duration_us: float = 15.0
    rydberg_duration_us: float = 0.36
    one_qubit_duration_us: float = 52.0
    movement_acceleration: float = 0.00275

    def __post_init__(self) -> None:
        for name in (
            "one_qubit_fidelity",
            "two_qubit_fidelity",
            "idle_excitation_fidelity",
            "transfer_fidelity",
        ):
            value = float(getattr(self, name))
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1], got {value}")
            object.__setattr__(self, name, value)
        for name in (
            "coherence_time_us",
            "transfer_duration_us",
            "rydberg_duration_us",
            "one_qubit_duration_us",
            "movement_acceleration",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite, got {value}")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FidelityModel":
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class FidelityResult:
    """Unified four-metric result plus an auditable fidelity decomposition."""

    fidelity: float | None
    log_fidelity: float | None
    exponential_sensitivity_fidelity: float
    exponential_sensitivity_log_fidelity: float
    component_fidelity: Mapping[str, float | None]
    component_log_fidelity: Mapping[str, float | None]
    one_qubit_gates: int
    two_qubit_gates: int
    idle_excitations: int
    transfers: int
    duration_us: float
    move_batches: int
    move_time_us: float
    idle_time_us: tuple[float, ...]
    ood: bool
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fidelity": self.fidelity,
            "log_fidelity": self.log_fidelity,
            "exponential_sensitivity_fidelity": self.exponential_sensitivity_fidelity,
            "exponential_sensitivity_log_fidelity": self.exponential_sensitivity_log_fidelity,
            "components": {
                key: {
                    "fidelity": self.component_fidelity.get(key),
                    "log_fidelity": self.component_log_fidelity.get(key),
                }
                for key in self.component_log_fidelity
            },
            "counts": {
                "one_qubit_gates": self.one_qubit_gates,
                "two_qubit_gates": self.two_qubit_gates,
                "idle_excitations": self.idle_excitations,
                "transfers": self.transfers,
            },
            "duration_us": self.duration_us,
            "move_batches": self.move_batches,
            "move_time_us": self.move_time_us,
            "idle_time_us": list(self.idle_time_us),
            "ood": self.ood,
            "warnings": list(self.warnings),
        }
