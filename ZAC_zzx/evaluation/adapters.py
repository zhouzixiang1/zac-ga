"""Adapters from ZAC ZAIR JSON and MQT-QMAP NA code to canonical events."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
import gzip
import json
import math
from pathlib import Path
import re
from typing import Any

from .model import (
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    GatePair,
    Position,
    TraceValidationError,
    UnsupportedOperationError,
)


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_ATOM_DECL = re.compile(rf"^atom\s+\(({_NUMBER}),\s*({_NUMBER})\)\s+([A-Za-z_]\w*)$")
_ATOM_NAME = re.compile(r"^atom(\d+)$")
_MOVE_ITEM = re.compile(rf"^\(({_NUMBER}),\s*({_NUMBER})\)\s+([A-Za-z_]\w*)$")
_NA_OPERATION = re.compile(r"^@\+\s+([A-Za-z_]\w*)(?=\s|$)")


def _position_key(position: Position) -> Position:
    return round(position[0], 7), round(position[1], 7)


class _ArchitectureView:
    def __init__(self, spec: Mapping[str, Any]):
        self.spec = dict(spec)
        self.slms: dict[int, tuple[Position, Position]] = {}
        self.entanglement_arrays: set[int] = set()
        self.entanglement_sites: set[Position] = set()
        for zone_name in ("storage_zones", "entanglement_zones"):
            for zone in self.spec.get(zone_name, []):
                for slm in zone.get("slms", []):
                    idx = int(slm["id"])
                    separation = slm.get("site_seperation", slm.get("site_separation"))
                    if not separation or "location" not in slm:
                        raise TraceValidationError(f"incomplete SLM specification for id={idx}")
                    self.slms[idx] = (
                        (float(slm["location"][0]), float(slm["location"][1])),
                        (float(separation[0]), float(separation[1])),
                    )
                    if zone_name == "entanglement_zones":
                        self.entanglement_arrays.add(idx)
                        rows, columns = int(slm.get("r", 0)), int(slm.get("c", 0))
                        for row in range(rows):
                            for column in range(columns):
                                self.entanglement_sites.add(
                                    (
                                        float(slm["location"][0]) + float(separation[0]) * column,
                                        float(slm["location"][1]) + float(separation[1]) * row,
                                    )
                                )
        self.entanglement_site_keys = {
            _position_key(position) for position in self.entanglement_sites
        }

        # Each Rydberg seat is described by two paired SLM grids.  Sites with
        # the same (row, column) in consecutive grids form the only legal CZ
        # pair.  Deriving this once from the architecture avoids guessing pairs
        # from whichever atoms happen to be adjacent in the current layout.
        self.entanglement_partners: dict[Position, Position] = {}
        for zone in self.spec.get("entanglement_zones", []):
            slms = list(zone.get("slms", []))
            if len(slms) % 2:
                raise TraceValidationError(
                    "entanglement zone must declare an even number of paired SLM grids"
                )
            for pair_index in range(0, len(slms), 2):
                left, right = slms[pair_index : pair_index + 2]
                left_separation = left.get("site_seperation", left.get("site_separation"))
                right_separation = right.get("site_seperation", right.get("site_separation"))
                if not left_separation or not right_separation:
                    raise TraceValidationError("paired entanglement SLM is missing site separation")
                left_shape = (int(left.get("r", 0)), int(left.get("c", 0)))
                right_shape = (int(right.get("r", 0)), int(right.get("c", 0)))
                if left_shape != right_shape or left_shape[0] <= 0 or left_shape[1] <= 0:
                    raise TraceValidationError(
                        "paired entanglement SLM grids must have the same positive shape"
                    )
                if any(
                    not math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-7)
                    for a, b in zip(left_separation, right_separation)
                ):
                    raise TraceValidationError(
                        "paired entanglement SLM grids must have identical site separation"
                    )
                for row in range(left_shape[0]):
                    for column in range(left_shape[1]):
                        left_key = _position_key((
                            float(left["location"][0]) + float(left_separation[0]) * column,
                            float(left["location"][1]) + float(left_separation[1]) * row,
                        ))
                        right_key = _position_key((
                            float(right["location"][0]) + float(right_separation[0]) * column,
                            float(right["location"][1]) + float(right_separation[1]) * row,
                        ))
                        if left_key == right_key:
                            raise TraceValidationError(
                                "paired entanglement SLM grids contain an overlapping site"
                            )
                        for site, partner in ((left_key, right_key), (right_key, left_key)):
                            previous = self.entanglement_partners.get(site)
                            if previous is not None and previous != partner:
                                raise TraceValidationError(
                                    f"ambiguous entanglement pair topology at site {site}"
                                )
                            self.entanglement_partners[site] = partner

        self.rydberg_rectangles: list[tuple[float, float, float, float]] = []
        for rectangle in self.spec.get("rydberg_range", []):
            if len(rectangle) != 2 or len(rectangle[0]) != 2 or len(rectangle[1]) != 2:
                raise TraceValidationError(f"invalid rydberg_range rectangle: {rectangle!r}")
            x0, y0 = float(rectangle[0][0]), float(rectangle[0][1])
            x1, y1 = float(rectangle[1][0]), float(rectangle[1][1])
            self.rydberg_rectangles.append((min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)))

    def logical_position(self, location: Sequence[int | float]) -> Position:
        if len(location) != 3:
            raise TraceValidationError(f"logical location must be [array,row,col]: {location!r}")
        array, row, col = int(location[0]), int(location[1]), int(location[2])
        if array not in self.slms:
            raise TraceValidationError(f"unknown SLM array id {array}")
        origin, separation = self.slms[array]
        return origin[0] + separation[0] * col, origin[1] + separation[1] * row

    def logical_region(self, location: Sequence[int | float]) -> str:
        return "entanglement" if int(location[0]) in self.entanglement_arrays else "storage"

    def coordinate_region(self, position: Position) -> str:
        x, y = position
        if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, x1, y0, y1 in self.rydberg_rectangles):
            return "entanglement"
        if (round(x, 7), round(y, 7)) in self.entanglement_site_keys:
            return "entanglement"
        return "storage"

    def is_legal_entanglement_pair(self, left: Position, right: Position) -> bool:
        return self.entanglement_partners.get(_position_key(left)) == _position_key(right)


def _read_path_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def _read_json_source(source: Mapping[str, Any] | str | Path) -> tuple[dict[str, Any], Path | None]:
    if isinstance(source, Mapping):
        return dict(source), None
    if isinstance(source, Path):
        return json.loads(_read_path_text(source)), source.resolve()
    candidate = Path(source)
    if "\n" not in source and candidate.exists():
        return json.loads(_read_path_text(candidate)), candidate.resolve()
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise TraceValidationError(f"ZAIR source is neither a JSON path nor JSON text: {source!r}") from exc
    if not isinstance(value, dict):
        raise TraceValidationError("ZAIR root must be a JSON object")
    return value, None


def _read_text_source(source: str | Path) -> str:
    if isinstance(source, Path):
        return _read_path_text(source)
    candidate = Path(source)
    if "\n" not in source and candidate.exists():
        return _read_path_text(candidate)
    return source


def _load_architecture(value: Mapping[str, Any] | str | Path | None) -> _ArchitectureView | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return _ArchitectureView(value)
    path = Path(value)
    if not path.exists():
        raise TraceValidationError(f"architecture file does not exist: {path}")
    return _ArchitectureView(json.loads(path.read_text()))


def _architecture_from_zair(
    code: Mapping[str, Any], source_path: Path | None, explicit: Mapping[str, Any] | str | Path | None
) -> _ArchitectureView | None:
    if explicit is not None:
        return _load_architecture(explicit)
    raw = code.get("architecture_spec_path")
    if not raw:
        return None
    requested = Path(raw)
    candidates = [requested]
    if source_path is not None:
        candidates.extend(parent / requested for parent in source_path.parents)
    for candidate in candidates:
        if candidate.exists():
            return _load_architecture(candidate)
    return None


def _logical_position(view: _ArchitectureView | None, location: Sequence[int | float]) -> Position:
    if view is not None:
        return view.logical_position(location)
    # ZAIR's canonical layout is [SLM,row,col].  This fallback keeps the event
    # usable in tests, while production runs should always provide architecture.
    return float(location[2]), float(location[1])


def _logical_region(view: _ArchitectureView | None, location: Sequence[int | float]) -> str:
    if view is not None:
        return view.logical_region(location)
    return "storage" if int(location[0]) == 0 else "entanglement"


def _location_map(records: Any, label: str) -> dict[int, tuple[int, int, int]]:
    if not isinstance(records, list):
        raise TraceValidationError(f"{label} must be a list")
    result: dict[int, tuple[int, int, int]] = {}
    for record in records:
        if not isinstance(record, list) or len(record) != 4:
            raise TraceValidationError(f"invalid {label} record: {record!r}")
        q = int(record[0])
        if q in result:
            raise TraceValidationError(f"duplicate atom {q} in {label}")
        result[q] = (int(record[1]), int(record[2]), int(record[3]))
    return result


def _coordinate_map(value: Any) -> dict[int, Position]:
    result: dict[int, Position] = {}

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            if {"id", "x", "y"}.issubset(node):
                result[int(node["id"])] = (float(node["x"]), float(node["y"]))
            else:
                for child in node.values():
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return result


def _native_interval(value: Mapping[str, Any], label: str) -> tuple[float, float]:
    """Read a compiler-authored physical interval and fail closed.

    Schema-2 main runs use the shared architecture with a 52-us 1Q duration, so
    the native ZAC dependency scheduler already contains the physical timeline.
    Reconstructing a new serial timeline here would destroy legal overlap between
    an AOD movement and an unrelated laser operation and would overstate
    coherence loss.
    """
    if "begin_time" not in value or "end_time" not in value:
        raise TraceValidationError(f"{label} is missing begin_time/end_time")
    begin, end = float(value["begin_time"]), float(value["end_time"])
    if not math.isfinite(begin) or not math.isfinite(end) or begin < 0 or end < begin:
        raise TraceValidationError(f"invalid {label} interval [{begin}, {end}]")
    return begin, end


def normalize_zair(
    source: Mapping[str, Any] | str | Path,
    *,
    architecture: Mapping[str, Any] | str | Path | None = None,
    model: FidelityModel | None = None,
) -> Iterator[CanonicalTraceEvent]:
    """Normalize a ZAC/ZAIR code JSON file.

    Formal Schema-2 traces are compiled with the shared 52-us architecture.
    Their native timestamps are therefore authoritative and are preserved,
    including legal overlap of independent work.  A multi-gate 1Q block is
    expanded into consecutive 52-us events inside its native interval.  Legacy
    0.625-us traces fail the duration check and remain reproduction-only data.
    """

    model = model or FidelityModel()
    code, source_path = _read_json_source(source)
    view = _architecture_from_zair(code, source_path, architecture)
    instructions = code.get("instructions")
    if not isinstance(instructions, list) or not instructions:
        raise TraceValidationError("ZAIR instructions must be a non-empty list")
    if instructions[0].get("type") != "init":
        raise TraceValidationError("ZAIR first instruction must be init")

    current = _location_map(instructions[0].get("init_locs"), "init_locs")
    if set(current) != set(range(len(current))):
        raise TraceValidationError("ZAIR init atom ids must be contiguous from zero")
    init_atoms = tuple(sorted(current))
    init_positions = tuple(_logical_position(view, current[q]) for q in init_atoms)
    if len(set(init_positions)) != len(init_positions):
        raise TraceValidationError("two atoms occupy the same ZAIR initial position")
    init_regions = tuple(_logical_region(view, current[q]) for q in init_atoms)
    events: list[CanonicalTraceEvent] = [CanonicalTraceEvent(
        EventType.INIT,
        0.0,
        0.0,
        atoms=init_atoms,
        end_positions=init_positions,
        end_regions=init_regions,
        source_index=0,
        metadata={"source_format": "zair"},
    )]

    for source_index, instruction in enumerate(instructions[1:], start=1):
        if not isinstance(instruction, Mapping):
            raise TraceValidationError(f"ZAIR instruction {source_index} is not an object")
        kind = instruction.get("type")
        raw_metadata = {
            "source_format": "zair",
            "raw_begin_time": instruction.get("begin_time"),
            "raw_end_time": instruction.get("end_time"),
            "native_id": instruction.get("id"),
        }

        if kind == "1qGate":
            gates = instruction.get("gates")
            if not isinstance(gates, list) or not gates:
                raise TraceValidationError(f"ZAIR 1qGate {source_index} has no gates")
            atoms = tuple(int(gate["q"]) for gate in gates)
            if any(q not in current for q in atoms):
                raise TraceValidationError("ZAIR 1qGate references an unknown atom")
            native_begin, native_end = _native_interval(
                instruction, f"ZAIR 1qGate {source_index}")
            expected_end = native_begin + len(gates) * model.one_qubit_duration_us
            if not math.isclose(native_end, expected_end, rel_tol=0.0, abs_tol=1e-7):
                raise TraceValidationError(
                    "ZAIR 1qGate timeline is not the frozen 52-us model: "
                    f"expected [{native_begin}, {expected_end}], got "
                    f"[{native_begin}, {native_end}]"
                )
            for gate_index, (gate, q) in enumerate(zip(gates, atoms)):
                position = _logical_position(view, current[q])
                atom_region = _logical_region(view, current[q])
                begin = native_begin + gate_index * model.one_qubit_duration_us
                end = begin + model.one_qubit_duration_us
                events.append(CanonicalTraceEvent(
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

        elif kind == "rydberg":
            raw_gates = instruction.get("gates")
            if not isinstance(raw_gates, list) or not raw_gates:
                raise TraceValidationError(f"ZAIR rydberg {source_index} has no gates")
            pairs = tuple((int(gate["q0"]), int(gate["q1"])) for gate in raw_gates)
            participants = tuple(q for pair in pairs for q in pair)
            if any(q not in current for q in participants):
                raise TraceValidationError("ZAIR rydberg references an unknown atom")
            region_atoms = tuple(
                sorted(q for q, location in current.items() if _logical_region(view, location) == "entanglement")
            )
            if not set(participants).issubset(region_atoms):
                raise TraceValidationError("ZAIR CZ participant is outside the entanglement zone")
            native_begin, native_end = _native_interval(
                instruction, f"ZAIR rydberg {source_index}")
            expected_end = native_begin + model.rydberg_duration_us
            if not math.isclose(native_end, expected_end, rel_tol=0.0, abs_tol=1e-7):
                raise TraceValidationError(
                    f"ZAIR rydberg duration must be {model.rydberg_duration_us} us"
                )
            events.append(CanonicalTraceEvent(
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
            ))

        elif kind == "rearrangeJob":
            atoms = tuple(int(q) for q in instruction.get("aod_qubits", []))
            if not atoms or len(set(atoms)) != len(atoms):
                raise TraceValidationError("ZAIR rearrangeJob has no atoms or duplicate atoms")
            begin_locs = _location_map(instruction.get("begin_locs"), "begin_locs")
            end_locs = _location_map(instruction.get("end_locs"), "end_locs")
            if set(atoms) != set(begin_locs) or set(atoms) != set(end_locs):
                raise TraceValidationError("ZAIR rearrangeJob atom/location ledgers disagree")
            for q in atoms:
                if q not in current or current[q] != begin_locs[q]:
                    raise TraceValidationError(
                        f"ZAIR rearrangeJob begins atom {q} at a stale location"
                    )

            begin_positions = tuple(_logical_position(view, begin_locs[q]) for q in atoms)
            end_positions = tuple(_logical_position(view, end_locs[q]) for q in atoms)
            begin_regions = tuple(_logical_region(view, begin_locs[q]) for q in atoms)
            end_regions = tuple(_logical_region(view, end_locs[q]) for q in atoms)
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
                phase for phase, detail in enumerate(details) if detail.get("type") == "activate"
            ]
            has_move = False
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
                        raise TraceValidationError("ZAIR activate lies outside its rearrangeJob")
                    if not math.isclose(
                            phase_end - phase_begin, model.transfer_duration_us,
                            rel_tol=0.0, abs_tol=1e-7):
                        raise TraceValidationError(
                            f"ZAIR activate duration must be {model.transfer_duration_us} us")
                    for beam, coordinate in zip(detail.get("row_id", []), detail.get("row_y", [])):
                        active_rows[int(beam)] = float(coordinate)
                    for beam, coordinate in zip(detail.get("col_id", []), detail.get("col_x", [])):
                        active_columns[int(beam)] = float(coordinate)
                    row_y = {round(value, 7) for value in active_rows.values()}
                    col_x = {round(value, 7) for value in active_columns.values()}
                    loaded = tuple(
                        q
                        for q in atoms
                        if q not in held
                        and round(physical[q][0], 7) in col_x
                        and round(physical[q][1], 7) in row_y
                    )
                    # Hand-authored/legacy traces occasionally omit beam
                    # coordinates.  Only the final activation can safely take
                    # all remaining movers without inventing a phase split.
                    if not loaded and phase == activate_indices[-1]:
                        loaded = tuple(q for q in atoms if q not in held)
                    if not loaded:
                        raise TraceValidationError(
                            "ZAIR activate phase cannot be matched to a movement atom"
                        )
                    positions = tuple(physical[q] for q in loaded)
                    regions = tuple(begin_regions[atoms.index(q)] for q in loaded)
                    events.append(CanonicalTraceEvent(
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
                        raise TraceValidationError("ZAIR move phase occurs before atom activation")
                    phase_begin_us, phase_end_us = _native_interval(
                        detail, f"ZAIR move {source_index}:{phase}")
                    if (phase_begin_us < job_begin - 1e-7 or
                            phase_end_us > job_end + 1e-7):
                        raise TraceValidationError("ZAIR move lies outside its rearrangeJob")
                    phase_duration = phase_end_us - phase_begin_us
                    if phase_duration < -1e-9:
                        raise TraceValidationError("ZAIR move phase has negative duration")
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
                    events.append(CanonicalTraceEvent(
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
                        raise TraceValidationError("ZAIR deactivate phase has no held atoms")
                    stored = tuple(q for q in atoms if q in held)
                    positions = tuple(end_positions[atoms.index(q)] for q in stored)
                    regions = tuple(end_regions[atoms.index(q)] for q in stored)
                    phase_begin, phase_end = _native_interval(
                        detail, f"ZAIR deactivate {source_index}:{phase}")
                    if (phase_begin < job_begin - 1e-7 or
                            phase_end > job_end + 1e-7):
                        raise TraceValidationError("ZAIR deactivate lies outside its rearrangeJob")
                    if not math.isclose(
                            phase_end - phase_begin, model.transfer_duration_us,
                            rel_tol=0.0, abs_tol=1e-7):
                        raise TraceValidationError(
                            f"ZAIR deactivate duration must be {model.transfer_duration_us} us")
                    events.append(CanonicalTraceEvent(
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
                        f"unsupported ZAIR rearrangement phase {native_type!r}"
                    )

            if held:
                raise TraceValidationError("ZAIR rearrangeJob ends with atoms held")
            if not has_move:
                raise TraceValidationError("ZAIR rearrangeJob contains no move phase")
            current.update(end_locs)
            all_positions = [_logical_position(view, location) for location in current.values()]
            if len(set(all_positions)) != len(all_positions):
                raise TraceValidationError("two ZAIR atoms occupy the same position after store")

        elif kind == "init":
            raise TraceValidationError("ZAIR contains a second init instruction")
        else:
            raise UnsupportedOperationError(
                f"unsupported ZAIR instruction {kind!r} at source index {source_index}"
            )

    # The compiler stores instructions in dependency order, not necessarily in
    # physical start-time order.  Sorting is deterministic and lets the streaming
    # scorer validate actual overlaps atom by atom.
    event_order = {
        EventType.INIT: 0,
        EventType.LOAD: 1,
        EventType.MOVE: 2,
        EventType.STORE: 3,
        EventType.ONE_QUBIT_GATE: 4,
        EventType.TWO_QUBIT_GATE: 5,
        EventType.WAIT: 6,
    }
    events.sort(key=lambda event: (
        event.start_us,
        event.end_us,
        event_order[event.event_type],
        -1 if event.source_index is None else event.source_index,
    ))
    last_one_qubit_end = -1.0
    for event in events:
        if event.event_type is EventType.ONE_QUBIT_GATE:
            if event.start_us < last_one_qubit_end - 1e-9:
                raise TraceValidationError("ZAIR global 1Q gates overlap")
            last_one_qubit_end = event.end_us
        yield event


def _payload(
    lines: list[str], index: int, operation_match: re.Match[str]
) -> tuple[list[str], int]:
    line = lines[index]
    operation = operation_match.group(1)
    suffix = line[operation_match.end() :].strip()
    if suffix == "[":
        values: list[str] = []
        index += 1
        while index < len(lines) and lines[index] != "]":
            values.append(lines[index])
            index += 1
        if index >= len(lines):
            raise TraceValidationError(f"unterminated NA block for {operation}")
        return values, index + 1
    if not suffix:
        raise TraceValidationError(f"empty NA operation {line!r}")
    return [suffix], index + 1


def _known_atoms(
    values: Iterable[str], name_to_id: Mapping[str, int], *, unique: bool = True
) -> list[str]:
    result: list[str] = []
    for value in values:
        tokens = re.findall(r"[A-Za-z_]\w*", value)
        matches = [token for token in tokens if token in name_to_id]
        if len(matches) != 1:
            raise TraceValidationError(f"expected exactly one atom in NA item: {value!r}")
        result.append(matches[0])
    if unique and len(set(result)) != len(result):
        raise TraceValidationError(f"duplicate atom in NA operation: {result!r}")
    return result


def _infer_pairs(
    zone_names: list[str],
    locations: Mapping[str, Position],
    name_to_id: Mapping[str, int],
    view: _ArchitectureView,
) -> tuple[GatePair, ...]:
    occupants = {_position_key(locations[name]): name for name in zone_names}
    pairs: list[GatePair] = []
    for site, name in occupants.items():
        partner_site = view.entanglement_partners.get(site)
        partner = occupants.get(partner_site) if partner_site is not None else None
        if partner is None or name_to_id[name] >= name_to_id[partner]:
            continue
        pairs.append((name_to_id[name], name_to_id[partner]))
    return tuple(sorted(pairs))


def normalize_na(
    source: str | Path,
    *,
    architecture: Mapping[str, Any] | str | Path | None = None,
    model: FidelityModel | None = None,
    gate_pairs: Iterable[Sequence[Sequence[int]]] | None = None,
) -> Iterator[CanonicalTraceEvent]:
    """Normalize MQT-QMAP NA code while preserving every 1Q instruction.

    The optional ``gate_pairs`` ledger supplies one list of logical pairs per
    ``@+ cz`` pulse.  Both inferred and explicit pairs are checked against the
    paired SLM-site topology in ``architecture``; a CZ cannot be normalized
    without an architecture.
    """

    model = model or FidelityModel()
    view = _load_architecture(architecture)
    text = _read_text_source(source)
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "//"))
    ]
    if not lines:
        raise TraceValidationError("NA source is empty")

    names: list[str] = []
    locations: dict[str, Position] = {}
    index = 0
    while index < len(lines):
        match = _ATOM_DECL.match(lines[index])
        if not match:
            break
        name = match.group(3)
        if name in locations:
            raise TraceValidationError(f"duplicate NA atom declaration: {name}")
        names.append(name)
        locations[name] = (float(match.group(1)), float(match.group(2)))
        index += 1
    if not names:
        raise TraceValidationError("NA source must start with atom declarations")
    if len(set(locations.values())) != len(locations):
        raise TraceValidationError("two NA atoms occupy the same initial position")

    name_to_id: dict[str, int] = {}
    id_to_name: dict[int, str] = {}
    for name in names:
        atom_match = _ATOM_NAME.fullmatch(name)
        if atom_match is None:
            raise TraceValidationError(
                f"NA atom name must encode its logical id as atomN: {name!r}"
            )
        logical_id = int(atom_match.group(1))
        if logical_id in id_to_name:
            raise TraceValidationError(
                f"duplicate NA logical atom id {logical_id}: "
                f"{id_to_name[logical_id]!r}, {name!r}"
            )
        name_to_id[name] = logical_id
        id_to_name[logical_id] = name
    expected_ids = set(range(len(names)))
    if set(id_to_name) != expected_ids:
        raise TraceValidationError(
            "NA atom names must form a logical-id bijection over "
            f"0..{len(names) - 1}; got {sorted(id_to_name)}"
        )
    # QMAP declaration order can be spatial rather than logical.  Canonical
    # INIT ledgers must always be ordered by the numeric atomN identity.
    names = [id_to_name[q] for q in range(len(names))]
    if view is None:
        regions = {name: "storage" for name in names}
    else:
        regions = {name: view.coordinate_region(locations[name]) for name in names}
    init_atoms = tuple(range(len(names)))
    yield CanonicalTraceEvent(
        EventType.INIT,
        0.0,
        0.0,
        atoms=init_atoms,
        end_positions=tuple(locations[name] for name in names),
        end_regions=tuple(regions[name] for name in names),
        source_index=0,
        metadata={"source_format": "na", "atom_names": names},
    )

    cursor = 0.0
    held: set[str] = set()
    batch_counter = 0
    batch_id: str | None = None
    batch_origin: dict[str, str] = {}
    moved_in_batch: set[str] = set()
    explicit_pairs = iter(gate_pairs) if gate_pairs is not None else None
    cz_index = 0

    while index < len(lines):
        line = lines[index]
        source_index = index
        operation_match = _NA_OPERATION.match(line)
        operation = operation_match.group(1) if operation_match is not None else None

        if operation == "load":
            assert operation_match is not None
            values, index = _payload(lines, index, operation_match)
            atoms = _known_atoms(values, name_to_id)
            if any(name in held for name in atoms):
                raise TraceValidationError("NA attempts to load an atom already held by the AOD")
            if not held:
                batch_id = f"na:{batch_counter}"
                batch_counter += 1
                batch_origin = {}
                moved_in_batch = set()
            assert batch_id is not None
            batch_origin.update({name: regions[name] for name in atoms})
            begin = tuple(locations[name] for name in atoms)
            begin_regions = tuple(regions[name] for name in atoms)
            end = cursor + model.transfer_duration_us
            yield CanonicalTraceEvent(
                EventType.LOAD,
                cursor,
                end,
                atoms=tuple(name_to_id[name] for name in atoms),
                start_positions=begin,
                end_positions=begin,
                start_regions=begin_regions,
                end_regions=("aod",) * len(atoms),
                batch_id=batch_id,
                source_index=source_index,
                metadata={"source_format": "na", "atom_names": atoms},
            )
            held.update(atoms)
            for name in atoms:
                regions[name] = "aod"
            cursor = end

        elif operation == "move":
            if not held or batch_id is None:
                raise TraceValidationError("NA move occurs outside a load/store batch")
            assert operation_match is not None
            values, index = _payload(lines, index, operation_match)
            move_names: list[str] = []
            destinations: list[Position] = []
            for value in values:
                match = _MOVE_ITEM.match(value)
                if not match or match.group(3) not in name_to_id:
                    raise TraceValidationError(f"invalid NA move item: {value!r}")
                name = match.group(3)
                if name not in held:
                    raise TraceValidationError(f"NA moves atom {name} without loading it")
                move_names.append(name)
                destinations.append((float(match.group(1)), float(match.group(2))))
            if len(set(move_names)) != len(move_names):
                raise TraceValidationError("duplicate atom in NA move")
            starts = tuple(locations[name] for name in move_names)
            max_distance = max(
                math.dist(start, destination) for start, destination in zip(starts, destinations)
            )
            move_duration = math.sqrt(max_distance / model.movement_acceleration)
            end = cursor + move_duration
            yield CanonicalTraceEvent(
                EventType.MOVE,
                cursor,
                end,
                atoms=tuple(name_to_id[name] for name in move_names),
                start_positions=starts,
                end_positions=tuple(destinations),
                start_regions=("aod",) * len(move_names),
                end_regions=("aod",) * len(move_names),
                batch_id=batch_id,
                source_index=source_index,
                metadata={"source_format": "na", "atom_names": move_names},
            )
            for name, destination in zip(move_names, destinations):
                locations[name] = destination
            moved_in_batch.update(move_names)
            cursor = end

        elif operation == "store":
            if not held or batch_id is None:
                raise TraceValidationError("NA store occurs outside a load/store batch")
            assert operation_match is not None
            values, index = _payload(lines, index, operation_match)
            atoms = _known_atoms(values, name_to_id)
            if any(name not in held for name in atoms):
                raise TraceValidationError("NA stores an atom not held by the AOD")
            if any(name not in moved_in_batch for name in atoms):
                raise TraceValidationError("NA stores an atom that has not moved in this batch")
            if view is None:
                destination_regions = tuple(
                    "storage" if batch_origin[name] == "entanglement" else "entanglement"
                    for name in atoms
                )
            else:
                destination_regions = tuple(view.coordinate_region(locations[name]) for name in atoms)
            positions = tuple(locations[name] for name in atoms)
            end = cursor + model.transfer_duration_us
            yield CanonicalTraceEvent(
                EventType.STORE,
                cursor,
                end,
                atoms=tuple(name_to_id[name] for name in atoms),
                start_positions=positions,
                end_positions=positions,
                start_regions=("aod",) * len(atoms),
                end_regions=destination_regions,
                batch_id=batch_id,
                source_index=source_index,
                metadata={"source_format": "na", "atom_names": atoms},
            )
            for name, destination_region in zip(atoms, destination_regions):
                regions[name] = destination_region
                held.remove(name)
            cursor = end
            if not held:
                if len(set(locations.values())) != len(locations):
                    raise TraceValidationError("two NA atoms occupy the same position after store")
                batch_id = None
                batch_origin = {}
                moved_in_batch = set()

        elif operation == "cz":
            if held:
                raise TraceValidationError("NA CZ occurs while atoms are still held by the AOD")
            if view is None:
                raise TraceValidationError(
                    "NA CZ normalization requires architecture pair topology"
                )
            assert operation_match is not None
            index += 1
            zone_names = [name for name in names if regions[name] == "entanglement"]
            physical_pairs = _infer_pairs(zone_names, locations, name_to_id, view)
            if explicit_pairs is None:
                pairs = physical_pairs
            else:
                try:
                    raw_pairs = next(explicit_pairs)
                except StopIteration as exc:
                    raise TraceValidationError("NA gate-pair ledger has fewer entries than CZ pulses") from exc
                parsed_pairs: list[GatePair] = []
                for pair in raw_pairs:
                    if len(pair) != 2:
                        raise TraceValidationError(
                            f"NA gate-pair ledger entry must have two atoms: {pair!r}"
                        )
                    parsed_pairs.append((int(pair[0]), int(pair[1])))
                pairs = tuple(parsed_pairs)
            if not pairs:
                raise TraceValidationError("NA CZ has no executable gate pair in the entanglement zone")
            participants = tuple(q for pair in pairs for q in pair)
            region_atoms = tuple(name_to_id[name] for name in zone_names)
            if not set(participants).issubset(region_atoms):
                raise TraceValidationError("NA gate-pair ledger references an atom outside the zone")
            for left, right in pairs:
                if left not in id_to_name or right not in id_to_name:
                    raise TraceValidationError(
                        f"NA gate-pair ledger references an unknown atom: {(left, right)!r}"
                    )
                if not view.is_legal_entanglement_pair(
                    locations[id_to_name[left]], locations[id_to_name[right]]
                ):
                    raise TraceValidationError(
                        f"NA CZ pair {(left, right)!r} is not a legal architecture pair"
                    )
            if explicit_pairs is not None:
                explicit_pair_set = {frozenset(pair) for pair in pairs}
                physical_pair_set = {frozenset(pair) for pair in physical_pairs}
                if explicit_pair_set != physical_pair_set:
                    raise TraceValidationError(
                        "NA gate-pair ledger does not match the executable "
                        "architecture pairs occupied during the CZ pulse"
                    )
            end = cursor + model.rydberg_duration_us
            yield CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE,
                cursor,
                end,
                atoms=participants,
                region=line[operation_match.end() :].strip() or "entanglement",
                gate_pairs=pairs,
                region_atoms=region_atoms,
                gate_names=("cz",) * len(pairs),
                source_index=source_index,
                metadata={"source_format": "na", "cz_index": cz_index},
            )
            cz_index += 1
            cursor = end

        elif operation in {"u", "rz", "ry", "sh"}:
            if held:
                raise TraceValidationError("NA one-qubit gate occurs while an atom is held by the AOD")
            assert operation_match is not None
            values, index = _payload(lines, index, operation_match)
            atoms = _known_atoms(values, name_to_id, unique=False)
            for gate_index, name in enumerate(atoms):
                position = locations[name]
                atom_region = regions[name]
                end = cursor + model.one_qubit_duration_us
                yield CanonicalTraceEvent(
                    EventType.ONE_QUBIT_GATE,
                    cursor,
                    end,
                    atoms=(name_to_id[name],),
                    region="global_1q",
                    start_positions=(position,),
                    end_positions=(position,),
                    start_regions=(atom_region,),
                    end_regions=(atom_region,),
                    gate_names=(operation,),
                    source_index=source_index,
                    metadata={
                        "source_format": "na",
                        "atom_names": [name],
                        "native_gate_index": gate_index,
                    },
                )
                cursor = end

        elif operation is not None:
            raise UnsupportedOperationError(
                f"unsupported NA operation {operation!r} at line {source_index + 1}"
            )
        elif line.startswith("atom "):
            raise TraceValidationError("NA atom declarations must precede all operations")
        elif line == "]":
            raise TraceValidationError("unexpected closing bracket in NA source")
        else:
            raise UnsupportedOperationError(f"unsupported NA operation at line {source_index + 1}: {line!r}")

    if held:
        raise TraceValidationError(f"NA trace ends with atoms still held: {sorted(held)}")
    if explicit_pairs is not None:
        try:
            next(explicit_pairs)
        except StopIteration:
            pass
        else:
            raise TraceValidationError("NA gate-pair ledger has more entries than CZ pulses")
