"""Fail-closed ghost-safe physicalization for QMAP 3.2 NA output.

QMAP decides every atom endpoint and emits one or more LOAD/MOVE phases per
rearrangement.  Its compiler does not include stationary atoms in the movement
conflict graph.  This module preserves the endpoint placement, operation order,
and each atom's authored waypoint trajectory, but splits an invalid physical
batch into deterministic safe sub-batches.  If an individual authored path
still crosses a stationary atom, one vacant legal SLM coordinate is used as an
explicit AOD waypoint.  All extra phases remain visible to the unified scorer.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from typing import Iterable, Iterator, Mapping, Sequence, TextIO

from zzx.ghost import ghost_hits
from zzx.zcost import compatible_2d


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_ATOM_DECL = re.compile(rf"^atom\s+\(({_NUMBER}),\s*({_NUMBER})\)\s+([A-Za-z_]\w*)$")
_OPERATION = re.compile(r"^@\+\s+([A-Za-z_]\w*)(?=\s|$)")
_MOVE_ITEM = re.compile(rf"^\(({_NUMBER}),\s*({_NUMBER})\)\s+([A-Za-z_]\w*)$")


@dataclass(frozen=True)
class _Operation:
    kind: str
    atoms: tuple[str, ...]
    destinations: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class _MovementBatch:
    operations: tuple[_Operation, ...]
    atoms: tuple[str, ...]


def _payload(lines: Sequence[str], index: int, match: re.Match[str]
             ) -> tuple[list[str], int]:
    suffix = lines[index][match.end():].strip()
    if suffix == "[":
        values = []
        index += 1
        while index < len(lines) and lines[index] != "]":
            values.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ValueError(f"unterminated NA block at line {index}")
        return values, index + 1
    if not suffix:
        raise ValueError(f"empty NA operation: {lines[index]!r}")
    return [suffix], index + 1


def _atom_name(value: str, known: set[str]) -> str:
    matches = [token for token in re.findall(r"[A-Za-z_]\w*", value)
               if token in known]
    if len(matches) != 1:
        raise ValueError(f"expected one known atom in {value!r}")
    return matches[0]


def _parse(text: str) -> tuple[list[str], dict[str, tuple[float, float]], list[object]]:
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith(("#", "//"))]
    declarations = []
    positions: dict[str, tuple[float, float]] = {}
    index = 0
    while index < len(lines):
        match = _ATOM_DECL.fullmatch(lines[index])
        if match is None:
            break
        name = match.group(3)
        if name in positions:
            raise ValueError(f"duplicate NA atom declaration {name}")
        declarations.append(lines[index])
        positions[name] = (float(match.group(1)), float(match.group(2)))
        index += 1
    if not declarations or len(set(positions.values())) != len(positions):
        raise ValueError("NA declarations are empty or contain duplicate positions")

    known = set(positions)
    segments: list[object] = []
    held: set[str] = set()
    operations: list[_Operation] = []
    batch_atoms: list[str] = []
    while index < len(lines):
        line = lines[index]
        match = _OPERATION.match(line)
        kind = match.group(1) if match is not None else ""
        if kind not in {"load", "move", "store"}:
            if held:
                raise ValueError(f"non-movement operation occurs inside NA batch: {line}")
            segments.append(line)
            index += 1
            continue
        assert match is not None
        values, index = _payload(lines, index, match)
        if kind == "move":
            atoms = []
            destinations = []
            for value in values:
                item = _MOVE_ITEM.fullmatch(value)
                if item is None or item.group(3) not in known:
                    raise ValueError(f"invalid NA move item {value!r}")
                atoms.append(item.group(3))
                destinations.append((float(item.group(1)), float(item.group(2))))
            if not atoms or len(set(atoms)) != len(atoms) or not set(atoms) <= held:
                raise ValueError("NA move has duplicate, empty, or unheld atoms")
            operations.append(_Operation(kind, tuple(atoms), tuple(destinations)))
            continue

        atoms = tuple(_atom_name(value, known) for value in values)
        if not atoms or len(set(atoms)) != len(atoms):
            raise ValueError(f"invalid NA {kind} atom list")
        if kind == "load":
            if set(atoms) & held:
                raise ValueError("NA loads an already-held atom")
            held.update(atoms)
            for atom in atoms:
                if atom not in batch_atoms:
                    batch_atoms.append(atom)
        else:
            if not set(atoms) <= held:
                raise ValueError("NA stores an atom that is not held")
            held.difference_update(atoms)
        operations.append(_Operation(kind, atoms))
        if not held:
            segments.append(_MovementBatch(tuple(operations), tuple(batch_atoms)))
            operations = []
            batch_atoms = []
    if held or operations:
        raise ValueError("NA ends inside an incomplete movement batch")
    return declarations, positions, segments


def _final_positions(batch: _MovementBatch,
                     positions: Mapping[str, tuple[float, float]]
                     ) -> dict[str, tuple[float, float]]:
    result = {atom: positions[atom] for atom in batch.atoms}
    for operation in batch.operations:
        if operation.kind == "move":
            result.update(zip(operation.atoms, operation.destinations))
    return result


def _replay_subset(batch: _MovementBatch, subset: set[str],
                   positions: Mapping[str, tuple[float, float]]) -> bool:
    physical = {atom: positions[atom] for atom in subset}
    held: set[str] = set()
    moved: set[str] = set()
    for operation in batch.operations:
        selected = [atom for atom in operation.atoms if atom in subset]
        if not selected:
            continue
        if operation.kind == "load":
            if set(selected) & held:
                return False
            held.update(selected)
            continue
        if operation.kind == "store":
            if not set(selected) <= held or not set(selected) <= moved:
                return False
            held.difference_update(selected)
            continue

        destination_by_atom = dict(zip(operation.atoms, operation.destinations))
        phase_owners = []
        legs = []
        phase_positions = dict(positions)
        phase_positions.update({atom: physical[atom] for atom in held})
        for atom in selected:
            if atom not in held:
                return False
            start, end = physical[atom], destination_by_atom[atom]
            distance = math.dist(start, end)
            if distance > 1e-9:
                phase_owners.append(atom)
                legs.append((distance, *start, *end))
        vectors = [(leg[1], leg[3], leg[2], leg[4]) for leg in legs]
        for left in range(len(vectors)):
            for right in range(left + 1, len(vectors)):
                if not compatible_2d(vectors[left], vectors[right]):
                    return False
        moving = set(phase_owners)
        ghosts = [(atom, *point) for atom, point in phase_positions.items()
                  if atom not in moving]
        if ghost_hits(legs, ghosts):
            return False
        for atom in selected:
            physical[atom] = destination_by_atom[atom]
            moved.add(atom)
    if held:
        return False
    outside = {point for atom, point in positions.items() if atom not in subset}
    return (len(set(physical.values())) == len(physical)
            and not (set(physical.values()) & outside))


def _slm_points(architecture: Mapping[str, object]
                ) -> list[tuple[int, int, int, tuple[float, float]]]:
    result = []
    for zone_name in ("storage_zones", "entanglement_zones"):
        for zone in architecture.get(zone_name, []):
            for slm in zone.get("slms", []):
                separation = slm.get("site_seperation", slm.get("site_separation"))
                for row in range(int(slm["r"])):
                    for column in range(int(slm["c"])):
                        point = (
                            float(slm["location"][0]) + float(separation[0]) * column,
                            float(slm["location"][1]) + float(separation[1]) * row,
                        )
                        result.append((0 if zone_name == "storage_zones" else 1,
                                       row, column, point))
    return result


def _waypoint(atom: str, end: tuple[float, float],
              positions: Mapping[str, tuple[float, float]],
              candidates: Sequence[tuple[int, int, int, tuple[float, float]]]
              ) -> tuple[float, float] | None:
    start = positions[atom]
    occupied = {point for name, point in positions.items() if name != atom}
    ghosts = [(name, *point) for name, point in positions.items() if name != atom]
    ordered = sorted(candidates, key=lambda item: (
        item[0], math.dist(start, item[3]) + math.dist(item[3], end),
        item[3][0], item[3][1], item[1], item[2]))
    for _zone, _row, _column, point in ordered:
        if point in occupied or point in {start, end}:
            continue
        first = (math.dist(start, point), *start, *point)
        second = (math.dist(point, end), *point, *end)
        if not ghost_hits([first], ghosts) and not ghost_hits([second], ghosts):
            return point
    return None


def _block(kind: str, values: Iterable[str]) -> list[str]:
    rows = list(values)
    if not rows:
        return []
    if len(rows) == 1:
        return [f"@+ {kind} {rows[0]}"]
    return [f"@+ {kind} [", *(f"    {row}" for row in rows), "]"]


def _serialize_subset(batch: _MovementBatch, subset: set[str]) -> list[str]:
    output = []
    for operation in batch.operations:
        selected = [atom for atom in operation.atoms if atom in subset]
        if not selected:
            continue
        if operation.kind == "move":
            destination_by_atom = dict(zip(operation.atoms, operation.destinations))
            output.extend(_block("move", (
                f"({destination_by_atom[atom][0]:.6f}, "
                f"{destination_by_atom[atom][1]:.6f}) {atom}"
                for atom in selected)))
        else:
            output.extend(_block(operation.kind, selected))
    return output


def _serialize_waypoint(atom: str, waypoint: tuple[float, float],
                        end: tuple[float, float]) -> list[str]:
    return [
        f"@+ load {atom}",
        f"@+ move ({waypoint[0]:.6f}, {waypoint[1]:.6f}) {atom}",
        f"@+ move ({end[0]:.6f}, {end[1]:.6f}) {atom}",
        f"@+ store {atom}",
    ]


def _architecture_spec(
    architecture: Mapping[str, object] | str | Path,
) -> dict[str, object]:
    if isinstance(architecture, Mapping):
        return dict(architecture)
    value = json.loads(Path(architecture).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("NA physicalizer architecture root must be an object")
    return dict(value)


def _repair_batch(
    batch: _MovementBatch,
    positions: dict[str, tuple[float, float]],
    candidates: Sequence[tuple[int, int, int, tuple[float, float]]],
) -> tuple[list[str], dict[str, int]]:
    """Repair one complete batch using the shared endpoint-preserving policy."""

    output: list[str] = []
    final = _final_positions(batch, positions)
    remaining = list(batch.atoms)
    emitted = 0
    waypoint_atoms = 0
    while remaining:
        selected: list[str] = []
        changed = True
        while changed:
            changed = False
            for atom in remaining:
                if atom in selected:
                    continue
                candidate = set((*selected, atom))
                if _replay_subset(batch, candidate, positions):
                    selected.append(atom)
                    changed = True
        if selected:
            subset = set(selected)
            output.extend(_serialize_subset(batch, subset))
            for atom in selected:
                positions[atom] = final[atom]
                remaining.remove(atom)
            emitted += 1
            continue

        atom = remaining[0]
        point = _waypoint(atom, final[atom], positions, candidates)
        if point is None:
            raise ValueError(f"no ghost-safe NA waypoint for {atom}")
        output.extend(_serialize_waypoint(atom, point, final[atom]))
        positions[atom] = final[atom]
        remaining.remove(atom)
        emitted += 1
        waypoint_atoms += 1
    return output, {
        "repaired_move_batches": emitted,
        "ghost_splits": max(0, emitted - 1),
        "waypoint_atoms": waypoint_atoms,
    }


def physicalize_na(source: str | Path, architecture: Mapping[str, object] | str | Path
                   ) -> tuple[str, dict[str, int]]:
    """Return repaired NA text and an auditable repair counter dictionary."""
    text = Path(source).read_text(encoding="utf-8") if isinstance(source, Path) else str(source)
    spec = _architecture_spec(architecture)
    declarations, positions, segments = _parse(text)
    candidates = _slm_points(spec)
    output = list(declarations)
    raw_batches = repaired_batches = split_batches = waypoint_atoms = 0

    for segment in segments:
        if isinstance(segment, str):
            output.append(segment)
            continue
        if not isinstance(segment, _MovementBatch):
            raise TypeError(f"unknown NA segment {segment!r}")
        raw_batches += 1
        repaired, counters = _repair_batch(segment, positions, candidates)
        output.extend(repaired)
        repaired_batches += counters["repaired_move_batches"]
        split_batches += counters["ghost_splits"]
        waypoint_atoms += counters["waypoint_atoms"]

    return "\n".join(output) + "\n", {
        "raw_move_batches": raw_batches,
        "repaired_move_batches": repaired_batches,
        "ghost_splits": split_batches,
        "waypoint_atoms": waypoint_atoms,
    }


def _meaningful_lines(handle: TextIO) -> Iterator[tuple[int, str]]:
    for line_number, raw in enumerate(handle, 1):
        value = raw.strip()
        if value and not value.startswith(("#", "//")):
            yield line_number, value


def _stream_payload(
    line: str,
    line_number: int,
    match: re.Match[str],
    lines: Iterator[tuple[int, str]],
) -> list[str]:
    suffix = line[match.end():].strip()
    if suffix != "[":
        if not suffix:
            raise ValueError(f"empty NA operation at line {line_number}: {line!r}")
        return [suffix]
    values: list[str] = []
    for payload_line, value in lines:
        if value == "]":
            return values
        values.append(value)
    raise ValueError(f"unterminated NA block beginning at line {line_number}")


def _write_physicalized_lines(handle: TextIO, lines: Iterable[str]) -> None:
    for line in lines:
        handle.write(line)
        handle.write("\n")


def physicalize_na_streaming(
    source: str | Path,
    destination: str | Path,
    architecture: Mapping[str, object] | str | Path,
) -> dict[str, int]:
    """Bounded-memory form of :func:`physicalize_na`.

    Only the atom-position registry and one open movement batch are retained.
    Output bytes and the four repair counters are identical to the whole-file
    implementation for the same valid input and original ZAC architecture.
    The destination is atomically published only after the source closes at a
    complete batch boundary.
    """

    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"NA physicalizer source is missing: {source_path}")
    if source_path == destination_path:
        raise ValueError("streaming NA physicalizer source and destination must differ")
    if destination_path.exists():
        raise FileExistsError(
            f"streaming NA physicalizer destination exists: {destination_path}"
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(
        f".{destination_path.name}.physicalizing"
    )
    if temporary.exists():
        raise FileExistsError(f"stale NA physicalizer temporary file: {temporary}")

    spec = _architecture_spec(architecture)
    candidates = _slm_points(spec)
    positions: dict[str, tuple[float, float]] = {}
    raw_batches = repaired_batches = split_batches = waypoint_atoms = 0

    try:
        with source_path.open(encoding="utf-8") as source_handle, temporary.open(
            "x", encoding="utf-8"
        ) as output_handle:
            lines = iter(_meaningful_lines(source_handle))
            current: tuple[int, str] | None = None
            for line_number, line in lines:
                declaration = _ATOM_DECL.fullmatch(line)
                if declaration is None:
                    current = (line_number, line)
                    break
                name = declaration.group(3)
                if name in positions:
                    raise ValueError(f"duplicate NA atom declaration {name}")
                point = (float(declaration.group(1)), float(declaration.group(2)))
                if point in positions.values():
                    raise ValueError("NA declarations contain duplicate positions")
                positions[name] = point
                _write_physicalized_lines(output_handle, (line,))
            if not positions:
                raise ValueError("NA declarations are empty")

            known = set(positions)
            held: set[str] = set()
            operations: list[_Operation] = []
            batch_atoms: list[str] = []
            while current is not None:
                line_number, line = current
                match = _OPERATION.match(line)
                kind = match.group(1) if match is not None else ""
                if kind not in {"load", "move", "store"}:
                    if held:
                        raise ValueError(
                            "non-movement operation occurs inside NA batch: "
                            f"{line}"
                        )
                    if _ATOM_DECL.fullmatch(line) is not None:
                        raise ValueError(
                            f"late NA atom declaration at line {line_number}"
                        )
                    _write_physicalized_lines(output_handle, (line,))
                else:
                    assert match is not None
                    values = _stream_payload(line, line_number, match, lines)
                    if kind == "move":
                        atoms: list[str] = []
                        destinations: list[tuple[float, float]] = []
                        for value in values:
                            item = _MOVE_ITEM.fullmatch(value)
                            if item is None or item.group(3) not in known:
                                raise ValueError(f"invalid NA move item {value!r}")
                            atoms.append(item.group(3))
                            destinations.append(
                                (float(item.group(1)), float(item.group(2)))
                            )
                        if (
                            not atoms
                            or len(set(atoms)) != len(atoms)
                            or not set(atoms) <= held
                        ):
                            raise ValueError(
                                "NA move has duplicate, empty, or unheld atoms"
                            )
                        operations.append(
                            _Operation(kind, tuple(atoms), tuple(destinations))
                        )
                    else:
                        atoms = [_atom_name(value, known) for value in values]
                        if not atoms or len(set(atoms)) != len(atoms):
                            raise ValueError(f"invalid NA {kind} atom list")
                        if kind == "load":
                            if set(atoms) & held:
                                raise ValueError("NA loads an already-held atom")
                            held.update(atoms)
                            for atom in atoms:
                                if atom not in batch_atoms:
                                    batch_atoms.append(atom)
                        else:
                            if not set(atoms) <= held:
                                raise ValueError(
                                    "NA stores an atom that is not held"
                                )
                            held.difference_update(atoms)
                        operations.append(_Operation(kind, tuple(atoms)))
                        if not held:
                            batch = _MovementBatch(
                                tuple(operations), tuple(batch_atoms)
                            )
                            repaired, counters = _repair_batch(
                                batch, positions, candidates
                            )
                            _write_physicalized_lines(output_handle, repaired)
                            raw_batches += 1
                            repaired_batches += counters[
                                "repaired_move_batches"
                            ]
                            split_batches += counters["ghost_splits"]
                            waypoint_atoms += counters["waypoint_atoms"]
                            operations = []
                            batch_atoms = []
                try:
                    current = next(lines)
                except StopIteration:
                    current = None
            if held or operations:
                raise ValueError("NA ends inside an incomplete movement batch")
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(temporary, destination_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "raw_move_batches": raw_batches,
        "repaired_move_batches": repaired_batches,
        "ghost_splits": split_batches,
        "waypoint_atoms": waypoint_atoms,
    }


__all__ = ["physicalize_na", "physicalize_na_streaming"]
