"""Pure ASAP scheduler and coherence ledger for ZAC native instructions.

This module is deliberately independent from :mod:`zzx.zplacer` and the
stateful router mixins.  It mirrors the resource/dependency timing contract of
``zac/router/router.py`` while exposing the state needed by a future boundary
solver:

* one ASAP clock per AOD;
* one global single-qubit clock;
* one Rydberg clock per entanglement zone;
* instruction dependency end times, including the router's special site
  activation/deactivation constraint;
* per-atom active-union time and the absolute idle time induced by the current
  trace makespan.

The last point is intentionally expressed as an *absolute* value.  Independent
work can be scheduled inside an already-existing tail on another resource, so
adding a new instruction may reduce an atom's idle time relative to an earlier
prefix.  A non-negative ``prior + delta`` ledger cannot represent that legal
overlap.

The prototype consumes already-expanded AOD phases.  Geometry expansion and
ghost-safe batch construction remain the router's responsibility; each LOAD or
STORE phase must identify the atoms that are physically active in that phase.
MOVE phases advance time but do not add active time under the frozen ZAC
coherence model.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable, Mapping, Sequence


_TIME_TOLERANCE_US = 1e-9
_SNAPSHOT_VERSION = 2
_TIMING_HASH_FORMAT = "zac-scheduler-timing-chain-v1"


def _non_negative_finite(value: float, label: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


def _atom_tuple(values: Iterable[int], n_atoms: int, label: str) -> tuple[int, ...]:
    atoms = tuple(int(value) for value in values)
    if len(set(atoms)) != len(atoms):
        raise ValueError(f"{label} contains duplicate atoms")
    if any(atom < 0 or atom >= n_atoms for atom in atoms):
        raise ValueError(f"{label} references an atom outside the ledger")
    return atoms


def _dependency_ids(value) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, bool):
        raise ValueError("boolean is not an instruction dependency")
    if isinstance(value, int):
        return (int(value),)
    return tuple(int(item) for item in value)


@dataclass(frozen=True, slots=True)
class ExpandedAODPhase:
    """One relative physical phase inside an expanded rearrangement job."""

    kind: str
    duration_us: float
    atoms: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        kind = str(self.kind).split(":", 1)[0].lower()
        aliases = {"activate": "load", "deactivate": "store"}
        kind = aliases.get(kind, kind)
        if kind not in {"load", "move", "store"}:
            raise ValueError(f"unsupported expanded AOD phase {self.kind!r}")
        duration = _non_negative_finite(self.duration_us, "AOD phase duration")
        atoms = tuple(int(atom) for atom in self.atoms)
        if len(set(atoms)) != len(atoms):
            raise ValueError("expanded AOD phase contains duplicate atoms")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "duration_us", duration)
        object.__setattr__(self, "atoms", atoms)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "duration_us": self.duration_us,
            "atoms": list(self.atoms),
        }


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """A normalized scheduled event emitted by :class:`PureSchedulerLedger`."""

    event_type: str
    start_us: float
    end_us: float
    atoms: tuple[int, ...]
    instruction_id: int
    phase_index: int = -1
    resource_id: int = -1

    @property
    def duration_us(self) -> float:
        return self.end_us - self.start_us

    @property
    def is_atom_active(self) -> bool:
        return self.event_type in {"one_qubit_gate", "two_qubit_gate", "load", "store"}

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "atoms": list(self.atoms),
            "instruction_id": self.instruction_id,
            "phase_index": self.phase_index,
            "resource_id": self.resource_id,
        }


@dataclass(frozen=True, slots=True)
class InstructionTiming:
    """Minimal dependency record retained across scheduler snapshots."""

    instruction_id: int
    kind: str
    begin_us: float
    end_us: float
    resource_id: int = -1
    activation_finish_us: float | None = None
    deactivation_offset_us: float | None = None

    def to_dict(self) -> dict:
        return {
            "instruction_id": self.instruction_id,
            "kind": self.kind,
            "begin_us": self.begin_us,
            "end_us": self.end_us,
            "resource_id": self.resource_id,
            "activation_finish_us": self.activation_finish_us,
            "deactivation_offset_us": self.deactivation_offset_us,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> "InstructionTiming":
        return cls(
            instruction_id=int(value["instruction_id"]),
            kind=str(value["kind"]),
            begin_us=float(value["begin_us"]),
            end_us=float(value["end_us"]),
            resource_id=int(value.get("resource_id", -1)),
            activation_finish_us=(
                None if value.get("activation_finish_us") is None
                else float(value["activation_finish_us"])),
            deactivation_offset_us=(
                None if value.get("deactivation_offset_us") is None
                else float(value["deactivation_offset_us"])),
        )


@dataclass(frozen=True, slots=True)
class SchedulerLedgerSnapshot:
    """JSON-serializable state sufficient to fork or resume the pure scheduler."""

    version: int
    n_atoms: int
    trace_end_us: float
    active_union_us: tuple[float, ...]
    atom_active_end_us: tuple[float, ...]
    aod_end_us: tuple[float, ...]
    one_qubit_end_us: float
    rydberg_end_us: tuple[float, ...]
    instructions: tuple[InstructionTiming, ...]
    next_instruction_id: int
    timing_count: int
    timing_sha256: str
    one_qubit_duration_us: float
    rydberg_duration_us: float
    one_qubit_common_us: float

    @property
    def idle_time_us(self) -> tuple[float, ...]:
        return tuple(
            max(0.0, self.trace_end_us - active)
            for active in self.active_union_us)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "n_atoms": self.n_atoms,
            "trace_end_us": self.trace_end_us,
            "active_union_us": list(self.active_union_us),
            "atom_active_end_us": list(self.atom_active_end_us),
            "aod_end_us": list(self.aod_end_us),
            "one_qubit_end_us": self.one_qubit_end_us,
            "rydberg_end_us": list(self.rydberg_end_us),
            "instructions": [item.to_dict() for item in self.instructions],
            "next_instruction_id": self.next_instruction_id,
            "timing_count": self.timing_count,
            "timing_sha256": self.timing_sha256,
            "one_qubit_duration_us": self.one_qubit_duration_us,
            "rydberg_duration_us": self.rydberg_duration_us,
            "one_qubit_common_us": self.one_qubit_common_us,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> "SchedulerLedgerSnapshot":
        version = int(value["version"])
        if version != _SNAPSHOT_VERSION:
            raise ValueError(f"unsupported scheduler-ledger snapshot version {version}")
        return cls(
            version=version,
            n_atoms=int(value["n_atoms"]),
            trace_end_us=float(value["trace_end_us"]),
            active_union_us=tuple(float(item) for item in value["active_union_us"]),
            atom_active_end_us=tuple(
                float(item) for item in value["atom_active_end_us"]),
            aod_end_us=tuple(float(item) for item in value["aod_end_us"]),
            one_qubit_end_us=float(value["one_qubit_end_us"]),
            rydberg_end_us=tuple(
                float(item) for item in value["rydberg_end_us"]),
            instructions=tuple(
                InstructionTiming.from_dict(item)
                for item in value["instructions"]),
            next_instruction_id=int(value["next_instruction_id"]),
            timing_count=int(value["timing_count"]),
            timing_sha256=str(value["timing_sha256"]),
            one_qubit_duration_us=float(value["one_qubit_duration_us"]),
            rydberg_duration_us=float(value["rydberg_duration_us"]),
            one_qubit_common_us=float(value["one_qubit_common_us"]),
        )


@dataclass(frozen=True, slots=True)
class ExactCurrentEvaluation:
    """Absolute-idle differential for one fully expanded candidate suffix."""

    coherence_negative_log_fidelity: float
    idle_before_us: tuple[float, ...]
    idle_after_us: tuple[float, ...]
    trace_end_before_us: float
    trace_end_after_us: float
    scheduler_after: SchedulerLedgerSnapshot


def infer_zair_phase_atoms(instruction: Mapping) -> dict[int, tuple[int, ...]]:
    """Recover the held atom set of every expanded ZAIR AOD phase.

    The batch router flattens ``aod_qubits`` before publishing an instruction,
    but expanded coordinates retain the row membership.  This helper supports
    both the nested pre-flatten form and the final flat form without consulting
    timestamps or mutable placement state.
    """
    if str(instruction.get("type", "")) != "rearrangeJob":
        return {}
    details = tuple(instruction.get("insts") or ())
    if not details:
        raise ValueError("ZAIR rearrangeJob has no expanded phases")
    raw_atoms = instruction.get("aod_qubits") or ()
    if raw_atoms and isinstance(raw_atoms[0], (list, tuple)):
        rows = tuple(tuple(int(atom) for atom in row) for row in raw_atoms)
        atoms = tuple(atom for row in rows for atom in row)
    else:
        atoms = tuple(int(atom) for atom in raw_atoms)
        best_rows: tuple[tuple[int, ...], ...] = ()
        for detail in details:
            for name in ("begin_coord", "end_coord"):
                coordinates = detail.get(name)
                if not isinstance(coordinates, list):
                    continue
                candidate = tuple(
                    tuple(int(item["id"]) for item in row)
                    for row in coordinates
                    if isinstance(row, list)
                )
                if sum(map(len, candidate)) > sum(map(len, best_rows)):
                    best_rows = candidate
        rows = best_rows
    if not atoms or len(set(atoms)) != len(atoms):
        raise ValueError("ZAIR rearrangeJob has no atoms or duplicate atoms")
    if not rows or set(atom for row in rows for atom in row) != set(atoms):
        raise ValueError("cannot recover ZAIR expanded row membership")

    held: set[int] = set()
    result: dict[int, tuple[int, ...]] = {}
    for phase_index, detail in enumerate(details):
        kind = str(detail.get("type", "")).split(":", 1)[0]
        if kind == "activate":
            activated = {
                atom
                for raw_row in detail.get("row_id", ())
                for atom in rows[int(raw_row)]
            }
            loaded = tuple(
                atom for atom in atoms if atom in activated and atom not in held)
            if not loaded:
                raise ValueError("ZAIR activate phase has no recoverable atoms")
            held.update(loaded)
            result[phase_index] = loaded
        elif kind == "move":
            if not held:
                raise ValueError("ZAIR move phase occurs before activation")
            result[phase_index] = tuple(atom for atom in atoms if atom in held)
        elif kind == "deactivate":
            if not held:
                raise ValueError("ZAIR deactivate phase has no held atoms")
            result[phase_index] = tuple(atom for atom in atoms if atom in held)
            held.clear()
        else:
            raise ValueError(f"unsupported expanded ZAIR phase {kind!r}")
    if held:
        raise ValueError("ZAIR rearrangeJob ends while atoms remain held")
    return result


class PureSchedulerLedger:
    """Pure, forkable ASAP scheduler for expanded ZAC instructions.

    The class mutates only its own in-memory state.  ``fork`` performs a full
    snapshot round trip and is the intended candidate-evaluation primitive.
    """

    def __init__(
            self,
            n_atoms: int,
            *,
            n_aods: int = 1,
            n_rydberg_zones: int = 1,
            one_qubit_duration_us: float = 52.0,
            rydberg_duration_us: float = 0.36,
            one_qubit_common_us: float = 0.0,
            keep_events: bool = True,
    ) -> None:
        if isinstance(n_atoms, bool) or int(n_atoms) < 0:
            raise ValueError("n_atoms must be a non-negative integer")
        if isinstance(n_aods, bool) or int(n_aods) <= 0:
            raise ValueError("n_aods must be a positive integer")
        if isinstance(n_rydberg_zones, bool) or int(n_rydberg_zones) <= 0:
            raise ValueError("n_rydberg_zones must be a positive integer")
        self.n_atoms = int(n_atoms)
        self.one_qubit_duration_us = _non_negative_finite(
            one_qubit_duration_us, "one-qubit duration")
        self.rydberg_duration_us = _non_negative_finite(
            rydberg_duration_us, "Rydberg duration")
        self.one_qubit_common_us = _non_negative_finite(
            one_qubit_common_us, "one-qubit common duration")
        self.keep_events = bool(keep_events)
        self.trace_end_us = 0.0
        self.active_union_us = [0.0] * self.n_atoms
        self.atom_active_end_us = [0.0] * self.n_atoms
        self.aod_end_us = [0.0] * int(n_aods)
        self.one_qubit_end_us = 0.0
        self.rydberg_end_us = [0.0] * int(n_rydberg_zones)
        self.instructions: dict[int, InstructionTiming] = {}
        self.events: list[ScheduledEvent] = []
        self.last_instruction_events: tuple[ScheduledEvent, ...] = ()
        self.next_instruction_id = 0
        self.timing_count = 0
        self._timing_digest = hashlib.sha256(
            _TIMING_HASH_FORMAT.encode("utf-8")).digest()

    # ------------------------------------------------------------------ state
    @property
    def idle_time_us(self) -> tuple[float, ...]:
        return tuple(
            max(0.0, self.trace_end_us - active)
            for active in self.active_union_us)

    @property
    def timing_sha256(self) -> str:
        return self._timing_digest.hex()

    def snapshot(self) -> SchedulerLedgerSnapshot:
        return SchedulerLedgerSnapshot(
            version=_SNAPSHOT_VERSION,
            n_atoms=self.n_atoms,
            trace_end_us=self.trace_end_us,
            active_union_us=tuple(self.active_union_us),
            atom_active_end_us=tuple(self.atom_active_end_us),
            aod_end_us=tuple(self.aod_end_us),
            one_qubit_end_us=self.one_qubit_end_us,
            rydberg_end_us=tuple(self.rydberg_end_us),
            instructions=tuple(
                self.instructions[key] for key in sorted(self.instructions)),
            next_instruction_id=self.next_instruction_id,
            timing_count=self.timing_count,
            timing_sha256=self.timing_sha256,
            one_qubit_duration_us=self.one_qubit_duration_us,
            rydberg_duration_us=self.rydberg_duration_us,
            one_qubit_common_us=self.one_qubit_common_us,
        )

    @classmethod
    def from_snapshot(
            cls,
            snapshot: SchedulerLedgerSnapshot | Mapping,
            *,
            one_qubit_duration_us: float | None = None,
            rydberg_duration_us: float | None = None,
            one_qubit_common_us: float | None = None,
            keep_events: bool = True,
    ) -> "PureSchedulerLedger":
        if not isinstance(snapshot, SchedulerLedgerSnapshot):
            snapshot = SchedulerLedgerSnapshot.from_dict(snapshot)
        value = cls(
            snapshot.n_atoms,
            n_aods=len(snapshot.aod_end_us),
            n_rydberg_zones=len(snapshot.rydberg_end_us),
            one_qubit_duration_us=(
                snapshot.one_qubit_duration_us
                if one_qubit_duration_us is None else one_qubit_duration_us),
            rydberg_duration_us=(
                snapshot.rydberg_duration_us
                if rydberg_duration_us is None else rydberg_duration_us),
            one_qubit_common_us=(
                snapshot.one_qubit_common_us
                if one_qubit_common_us is None else one_qubit_common_us),
            keep_events=keep_events,
        )
        if (len(snapshot.active_union_us) != snapshot.n_atoms or
                len(snapshot.atom_active_end_us) != snapshot.n_atoms):
            raise ValueError("scheduler snapshot atom vectors are not aligned")
        value.trace_end_us = _non_negative_finite(
            snapshot.trace_end_us, "snapshot trace end")
        value.active_union_us = [
            _non_negative_finite(item, "snapshot active union")
            for item in snapshot.active_union_us]
        value.atom_active_end_us = [
            _non_negative_finite(item, "snapshot atom active end")
            for item in snapshot.atom_active_end_us]
        value.aod_end_us = [
            _non_negative_finite(item, "snapshot AOD end")
            for item in snapshot.aod_end_us]
        value.one_qubit_end_us = _non_negative_finite(
            snapshot.one_qubit_end_us, "snapshot one-qubit end")
        value.rydberg_end_us = [
            _non_negative_finite(item, "snapshot Rydberg end")
            for item in snapshot.rydberg_end_us]
        value.instructions = {
            item.instruction_id: item for item in snapshot.instructions}
        if len(value.instructions) != len(snapshot.instructions):
            raise ValueError("scheduler snapshot repeats an instruction id")
        value.next_instruction_id = int(snapshot.next_instruction_id)
        value.timing_count = int(snapshot.timing_count)
        if value.next_instruction_id < 0 or value.timing_count < 0:
            raise ValueError("scheduler snapshot counters must be non-negative")
        if value.timing_count != value.next_instruction_id:
            raise ValueError(
                "scheduler snapshot timing count differs from next instruction id")
        if (len(snapshot.timing_sha256) != 64 or any(
                character not in "0123456789abcdef"
                for character in snapshot.timing_sha256)):
            raise ValueError("scheduler snapshot timing hash is invalid")
        value._timing_digest = bytes.fromhex(snapshot.timing_sha256)
        if any(instruction_id >= value.next_instruction_id
               for instruction_id in value.instructions):
            raise ValueError("scheduler snapshot contains a future instruction")
        return value

    def fork(self) -> "PureSchedulerLedger":
        return self.from_snapshot(
            self.snapshot(),
            one_qubit_duration_us=self.one_qubit_duration_us,
            rydberg_duration_us=self.rydberg_duration_us,
            one_qubit_common_us=self.one_qubit_common_us,
            keep_events=self.keep_events,
        )

    def prune_instructions(self, referenced_ids: Iterable[int]) -> None:
        """Retain only dependency records needed by a future instruction.

        Resource clocks, active union, the absolute trace tail, and the rolling
        timing hash are prefix summaries and therefore remain exact after this
        bounded-memory pruning.  Unknown references fail closed so a checkpoint
        can never silently discard a live dependency.
        """
        keep = {int(value) for value in referenced_ids}
        missing = sorted(keep - set(self.instructions))
        if missing:
            raise ValueError(
                f"scheduler cannot retain unknown dependencies {missing}")
        self.instructions = {
            instruction_id: self.instructions[instruction_id]
            for instruction_id in sorted(keep)
        }

    # -------------------------------------------------------------- primitives
    def _new_instruction(self, instruction_id: int) -> int:
        instruction_id = int(instruction_id)
        if instruction_id != self.next_instruction_id:
            raise ValueError(
                f"scheduler expected instruction id {self.next_instruction_id}, "
                f"received {instruction_id}")
        return instruction_id

    def _dependency_end(self, dependencies: Iterable[int]) -> float:
        end = 0.0
        for dependency in dependencies:
            dependency = int(dependency)
            if dependency not in self.instructions:
                raise ValueError(f"unknown instruction dependency {dependency}")
            end = max(end, self.instructions[dependency].end_us)
        return end

    def _site_dependency_begin(
            self,
            dependencies: Iterable[int],
            deactivation_offset_us: float,
    ) -> float:
        begin = 0.0
        for dependency in dependencies:
            dependency = int(dependency)
            if dependency not in self.instructions:
                raise ValueError(f"unknown site dependency {dependency}")
            prior = self.instructions[dependency]
            if prior.kind == "aod":
                if prior.activation_finish_us is None:
                    raise ValueError(
                        f"AOD site dependency {dependency} lacks activation timing")
                begin = max(
                    begin,
                    prior.activation_finish_us - deactivation_offset_us)
            else:
                begin = max(begin, prior.end_us)
        return begin

    def _record_event(self, event: ScheduledEvent) -> None:
        if event.end_us < event.start_us - _TIME_TOLERANCE_US:
            raise ValueError("scheduled event has negative duration")
        atoms = _atom_tuple(
            event.atoms, self.n_atoms, f"{event.event_type} event")
        if atoms != event.atoms:
            raise AssertionError("event atom normalization drift")
        if event.is_atom_active:
            for atom in atoms:
                if event.start_us < (
                        self.atom_active_end_us[atom] - _TIME_TOLERANCE_US):
                    raise ValueError(
                        f"atom {atom} has overlapping active intervals")
                self.active_union_us[atom] += event.duration_us
                self.atom_active_end_us[atom] = max(
                    self.atom_active_end_us[atom], event.end_us)
        self.trace_end_us = max(self.trace_end_us, event.end_us)
        self.events.append(event)

    def _commit_timing(self, timing: InstructionTiming) -> InstructionTiming:
        self.instructions[timing.instruction_id] = timing
        self.trace_end_us = max(self.trace_end_us, timing.end_us)
        emitted = [
            event for event in self.events
            if event.instruction_id == timing.instruction_id
        ]
        self.last_instruction_events = tuple(emitted)
        record = {
            "timing": timing.to_dict(),
            "events": [event.to_dict() for event in emitted],
        }
        encoded = json.dumps(
            record, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._timing_digest = hashlib.sha256(
            self._timing_digest
            + len(encoded).to_bytes(8, "big")
            + encoded).digest()
        self.timing_count += 1
        self.next_instruction_id += 1
        if not self.keep_events:
            self.events.clear()
        return timing

    # ----------------------------------------------------------- public inputs
    def schedule_init(self, instruction_id: int = 0) -> InstructionTiming:
        instruction_id = self._new_instruction(instruction_id)
        return self._commit_timing(InstructionTiming(
            instruction_id, "init", 0.0, 0.0))

    def schedule_one_qubit(
            self,
            instruction_id: int,
            atoms: Sequence[int],
            *,
            dependencies: Iterable[int] = (),
    ) -> InstructionTiming:
        instruction_id = self._new_instruction(instruction_id)
        # A compiler-authored global block may contain several consecutive gates
        # on the same atom.  They are distinct serial operations, not a duplicate
        # participant error (router.py intentionally handles this case).
        atoms = tuple(int(atom) for atom in atoms)
        if any(atom < 0 or atom >= self.n_atoms for atom in atoms):
            raise ValueError(
                "one-qubit instruction references an atom outside the ledger")
        if not atoms:
            raise ValueError("one-qubit instruction must contain at least one gate")
        begin = max(
            self.one_qubit_end_us,
            self._dependency_end(dependencies))
        cursor = begin
        for gate_index, atom in enumerate(atoms):
            end = cursor + self.one_qubit_duration_us
            self._record_event(ScheduledEvent(
                "one_qubit_gate", cursor, end, (atom,), instruction_id,
                phase_index=gate_index, resource_id=0))
            cursor = end
        end = cursor + self.one_qubit_common_us
        self.one_qubit_end_us = end
        return self._commit_timing(InstructionTiming(
            instruction_id, "one_qubit", begin, end, resource_id=0))

    def schedule_cz(
            self,
            instruction_id: int,
            participants: Sequence[int],
            *,
            zone_id: int = 0,
            dependencies: Iterable[int] = (),
    ) -> InstructionTiming:
        instruction_id = self._new_instruction(instruction_id)
        participants = _atom_tuple(
            participants, self.n_atoms, "Rydberg instruction")
        if not participants:
            raise ValueError("Rydberg instruction must contain participants")
        zone_id = int(zone_id)
        if zone_id < 0 or zone_id >= len(self.rydberg_end_us):
            raise ValueError("Rydberg zone id is outside the ledger")
        begin = max(
            self.rydberg_end_us[zone_id],
            self._dependency_end(dependencies))
        end = begin + self.rydberg_duration_us
        self._record_event(ScheduledEvent(
            "two_qubit_gate", begin, end, participants, instruction_id,
            resource_id=zone_id))
        self.rydberg_end_us[zone_id] = end
        return self._commit_timing(InstructionTiming(
            instruction_id, "cz", begin, end, resource_id=zone_id))

    def schedule_aod(
            self,
            instruction_id: int,
            phases: Sequence[ExpandedAODPhase],
            *,
            dependencies: Iterable[int] = (),
            site_dependencies: Iterable[int] = (),
            aod_id: int | None = None,
    ) -> InstructionTiming:
        instruction_id = self._new_instruction(instruction_id)
        phases = tuple(
            phase if isinstance(phase, ExpandedAODPhase)
            else ExpandedAODPhase(**phase)
            for phase in phases)
        if not phases:
            raise ValueError("AOD instruction must contain expanded phases")
        for phase in phases:
            _atom_tuple(phase.atoms, self.n_atoms, f"AOD {phase.kind} phase")
        if not any(phase.kind == "move" for phase in phases):
            raise ValueError("AOD instruction must contain a MOVE phase")
        if not any(phase.kind == "load" for phase in phases):
            raise ValueError("AOD instruction must contain a LOAD phase")
        if not any(phase.kind == "store" for phase in phases):
            raise ValueError("AOD instruction must contain a STORE phase")

        offsets = []
        cursor = 0.0
        activation_finish_offset = 0.0
        deactivation_offset = None
        for phase in phases:
            start = cursor
            cursor += phase.duration_us
            offsets.append((start, cursor))
            if phase.kind == "load":
                activation_finish_offset = max(
                    activation_finish_offset, cursor)
            elif phase.kind == "store" and deactivation_offset is None:
                deactivation_offset = start
        if deactivation_offset is None:
            raise AssertionError("STORE presence check drift")

        # The production router can name the current rearrangeJob as a site
        # dependency when one atom in a multi-atom batch moves into a site
        # vacated by another atom in that same batch.  Its legacy timing rule
        # evaluates this as
        # ``begin + activation_finish_offset - deactivation_offset``.  Because
        # every LOAD finishes before the first STORE begins, that constraint is
        # tautological and must not be looked up as a prior instruction.  Keep
        # the ordering assertion explicit, then remove only the exact self id;
        # every genuinely prior site dependency remains fail-closed.
        site_dependencies = tuple(int(value) for value in site_dependencies)
        if instruction_id in site_dependencies:
            if (activation_finish_offset
                    > deactivation_offset + _TIME_TOLERANCE_US):
                raise ValueError(
                    "AOD self site dependency crosses its own STORE phase")
            site_dependencies = tuple(
                value for value in site_dependencies
                if value != instruction_id)

        selected_aod = min(
            range(len(self.aod_end_us)),
            key=lambda index: (self.aod_end_us[index], index))
        if aod_id is not None:
            aod_id = int(aod_id)
            if aod_id < 0 or aod_id >= len(self.aod_end_us):
                raise ValueError("AOD id is outside the ledger")
            if aod_id != selected_aod:
                raise ValueError(
                    f"AOD {aod_id} is not the router ASAP choice {selected_aod}")
            selected_aod = aod_id

        begin = max(
            self.aod_end_us[selected_aod],
            self._dependency_end(dependencies),
            self._site_dependency_begin(
                site_dependencies, deactivation_offset))
        for phase_index, (phase, (start, end)) in enumerate(zip(phases, offsets)):
            self._record_event(ScheduledEvent(
                phase.kind, begin + start, begin + end, phase.atoms,
                instruction_id, phase_index=phase_index,
                resource_id=selected_aod))
        end = begin + cursor
        self.aod_end_us[selected_aod] = end
        return self._commit_timing(InstructionTiming(
            instruction_id, "aod", begin, end, resource_id=selected_aod,
            activation_finish_us=begin + activation_finish_offset,
            deactivation_offset_us=deactivation_offset))

    # ---------------------------------------------------------- ZAIR adapter
    def schedule_zair_instruction(
            self,
            instruction: Mapping,
            *,
            phase_atoms: Mapping[int, Sequence[int]] | None = None,
    ) -> InstructionTiming:
        """Schedule one raw ZAIR instruction without reading its timestamps.

        ``phase_atoms`` maps expanded phase index to the atoms held/loaded/stored
        in that phase.  Formal router integration can provide this directly from
        ``expand_arrangement``.  A final flattened ZAIR file loses the original
        pickup-row grouping, so real-trace differential tests recover this small
        map from the canonical adapter.
        """
        kind = str(instruction.get("type", ""))
        instruction_id = int(instruction.get("id", -1))
        dependency = instruction.get("dependency") or {}
        general = []
        for name, values in dependency.items():
            if name == "site":
                continue
            general.extend(_dependency_ids(values))
        site = _dependency_ids(dependency.get("site"))

        if kind == "init":
            return self.schedule_init(instruction_id)
        if kind == "1qGate":
            gates = instruction.get("gates") or ()
            return self.schedule_one_qubit(
                instruction_id,
                tuple(int(gate["q"]) for gate in gates),
                dependencies=general)
        if kind == "rydberg":
            gates = instruction.get("gates") or ()
            participants = tuple(
                atom for gate in gates
                for atom in (int(gate["q0"]), int(gate["q1"])))
            return self.schedule_cz(
                instruction_id, participants,
                zone_id=int(instruction.get("zone_id", 0)),
                dependencies=general)
        if kind != "rearrangeJob":
            raise ValueError(f"unsupported ZAIR instruction {kind!r}")

        details = instruction.get("insts") or ()
        if not details:
            raise ValueError("ZAIR rearrangeJob has no expanded phases")
        phase_atoms = (
            infer_zair_phase_atoms(instruction)
            if phase_atoms is None else phase_atoms)
        raw_atoms = instruction.get("aod_qubits", ())
        if raw_atoms and isinstance(raw_atoms[0], (list, tuple)):
            all_atoms = tuple(
                int(atom) for row in raw_atoms for atom in row)
        else:
            all_atoms = tuple(int(atom) for atom in raw_atoms)
        load_count = sum(
            str(detail.get("type", "")).split(":", 1)[0] == "activate"
            for detail in details)
        phases = []
        for phase_index, detail in enumerate(details):
            raw_kind = str(detail.get("type", "")).split(":", 1)[0]
            if "begin_time" in detail and "end_time" in detail:
                duration = float(detail["end_time"]) - float(detail["begin_time"])
            elif "duration_us" in detail:
                duration = float(detail["duration_us"])
            else:
                raise ValueError("expanded ZAIR phase lacks a duration")
            atoms = tuple(int(atom) for atom in phase_atoms.get(phase_index, ()))
            if not atoms:
                if raw_kind == "activate" and load_count == 1:
                    atoms = all_atoms
                elif raw_kind == "deactivate":
                    atoms = all_atoms
                elif raw_kind.startswith("move"):
                    atoms = all_atoms
                elif raw_kind == "activate":
                    raise ValueError(
                        "multi-row flattened AOD requires phase_atoms for LOAD")
            phases.append(ExpandedAODPhase(raw_kind, duration, atoms))
        raw_aod_id = instruction.get("aod_id")
        aod_id = None if raw_aod_id is None or int(raw_aod_id) < 0 else int(raw_aod_id)
        return self.schedule_aod(
            instruction_id, phases, dependencies=general,
            site_dependencies=site, aod_id=aod_id)

    def consume_zair_instruction(
            self,
            instruction: Mapping,
            *,
            phase_atoms: Mapping[int, Sequence[int]] | None = None,
            verify_timestamps: bool = True,
            tolerance_us: float = 1e-7,
    ) -> InstructionTiming:
        """Schedule one native instruction and verify every authored time.

        This is the production integration point.  The router remains the
        instruction author, while this independent scheduler must reproduce its
        absolute interval.  Any disagreement is fatal before the instruction is
        published or checkpointed.
        """
        timing = self.schedule_zair_instruction(
            instruction, phase_atoms=phase_atoms)
        if not verify_timestamps:
            return timing
        if "begin_time" not in instruction or "end_time" not in instruction:
            raise ValueError("native instruction lacks begin_time/end_time")
        authored = (
            float(instruction["begin_time"]), float(instruction["end_time"]))
        computed = (timing.begin_us, timing.end_us)
        if any(not math.isclose(left, right, rel_tol=0.0,
                                abs_tol=float(tolerance_us))
               for left, right in zip(computed, authored)):
            raise RuntimeError(
                "scheduler/router instruction time mismatch for "
                f"id {timing.instruction_id}: {computed!r} != {authored!r}")
        if timing.kind == "aod":
            details = tuple(instruction.get("insts") or ())
            events = self.last_instruction_events
            if len(events) != len(details):
                raise RuntimeError(
                    "scheduler/router expanded phase count mismatch")
            for phase_index, (event, detail) in enumerate(zip(events, details)):
                expected = (
                    float(detail["begin_time"]), float(detail["end_time"]))
                actual = (event.start_us, event.end_us)
                if any(not math.isclose(left, right, rel_tol=0.0,
                                        abs_tol=float(tolerance_us))
                       for left, right in zip(actual, expected)):
                    raise RuntimeError(
                        "scheduler/router expanded phase time mismatch for "
                        f"id {timing.instruction_id} phase {phase_index}: "
                        f"{actual!r} != {expected!r}")
        return timing


def evaluate_exact_current_candidate(
        snapshot: SchedulerLedgerSnapshot | Mapping,
        instructions: Iterable[Mapping],
        *,
        coherence_t2_us: float = 1_500_000.0,
        verify_timestamps: bool = False,
) -> ExactCurrentEvaluation:
    """Evaluate a candidate suffix using absolute scheduler idle before/after.

    The returned coherence term may be negative: independent active work can
    fill an existing resource tail and reduce absolute idle.  This behavior is
    required for equality with the final trace scorer and is precisely what the
    former non-negative ``prior + delta`` approximation could not represent.
    """
    t2 = _non_negative_finite(coherence_t2_us, "coherence T2")
    if t2 <= 0.0:
        raise ValueError("coherence T2 must be positive")
    ledger = PureSchedulerLedger.from_snapshot(snapshot, keep_events=False)
    before = ledger.idle_time_us
    trace_before = ledger.trace_end_us
    for instruction in instructions:
        ledger.consume_zair_instruction(
            instruction, verify_timestamps=verify_timestamps)
    after = ledger.idle_time_us
    nll = 0.0
    for prior, current in zip(before, after):
        if prior >= t2 or current >= t2:
            raise ValueError("linear coherence model is outside its T2 domain")
        nll += math.log1p(-prior / t2) - math.log1p(-current / t2)
    return ExactCurrentEvaluation(
        coherence_negative_log_fidelity=nll,
        idle_before_us=before,
        idle_after_us=after,
        trace_end_before_us=trace_before,
        trace_end_after_us=ledger.trace_end_us,
        scheduler_after=ledger.snapshot(),
    )


__all__ = [
    "ExpandedAODPhase",
    "ExactCurrentEvaluation",
    "InstructionTiming",
    "PureSchedulerLedger",
    "ScheduledEvent",
    "SchedulerLedgerSnapshot",
    "evaluate_exact_current_candidate",
    "infer_zair_phase_atoms",
]
