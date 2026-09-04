"""Bounded-memory normalization of native MQT-QMAP NA code.

The reference :func:`evaluation.normalize_na` adapter first reads and filters
the complete NA program.  That is convenient for ordinary circuits, but a
multi-million-operation Large run must not retain the native text.  This
module implements the same decoder as a forward-only line state machine.  Its
persistent state is the current position/region of every atom plus the open
AOD batch; only the payload of the current ``[...]`` operation is buffered.

The emitted events intentionally use the same dependency order as the NA
program (which is also chronological because QMAP's NA timing model is fully
serial), the same filtered-line ``source_index`` values, and the same metadata
as the reference adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
import gzip
import math
from pathlib import Path
import re
from typing import Any, TextIO

from evaluation.adapters import (
    _ATOM_DECL,
    _ATOM_NAME,
    _MOVE_ITEM,
    _NA_OPERATION,
    _ArchitectureView,
    _infer_pairs,
    _load_architecture,
)
from evaluation.model import (
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    GatePair,
    Position,
    TraceValidationError,
    UnsupportedOperationError,
)


_FilteredLine = tuple[int, str]


def _looks_like_existing_path(source: str) -> Path | None:
    """Match the reference adapter's path-or-inline-text convention safely."""

    if "\n" in source:
        return None
    candidate = Path(source)
    try:
        return candidate if candidate.exists() else None
    except OSError:
        # Very long inline NA snippets without a newline can exceed the host's
        # maximum filename length.  They are text, not paths.
        return None


def _raw_lines(source: str | Path) -> Iterator[str]:
    """Yield source lines while keeping a path-backed source genuinely lazy."""

    path: Path | None
    if isinstance(source, Path):
        path = source
    else:
        path = _looks_like_existing_path(source)

    if path is None:
        # Scan inline text without ``splitlines`` or ``StringIO`` so accepting
        # an already-resident Large string does not create a second full-size
        # text buffer.  Each yielded slice is at most one physical line.
        text = str(source)
        begin = 0
        while begin < len(text):
            lf = text.find("\n", begin)
            cr = text.find("\r", begin)
            candidates = [index for index in (lf, cr) if index >= 0]
            if not candidates:
                yield text[begin:]
                break
            end = min(candidates)
            stop = end + 1
            if text[end] == "\r" and stop < len(text) and text[stop] == "\n":
                stop += 1
            yield text[begin:stop]
            begin = stop
        return

    handle: TextIO
    if path.suffix == ".gz":
        handle = gzip.open(path, "rt", encoding="utf-8")
    else:
        handle = path.open("rt", encoding="utf-8")
    with handle:
        yield from handle


class _FilteredLineReader:
    """One-item-lookahead reader over non-empty, non-comment NA lines."""

    def __init__(self, source: str | Path):
        self._raw = iter(_raw_lines(source))
        self._pushed: _FilteredLine | None = None
        self._next_index = 0

    def next(self) -> _FilteredLine | None:
        if self._pushed is not None:
            value = self._pushed
            self._pushed = None
            return value
        for raw in self._raw:
            line = raw.strip()
            if not line or line.lstrip().startswith(("#", "//")):
                continue
            value = (self._next_index, line)
            self._next_index += 1
            return value
        return None

    def push(self, value: _FilteredLine) -> None:
        if self._pushed is not None:
            raise RuntimeError("NA line reader supports only one item of lookahead")
        self._pushed = value


def _known_atoms(
    values: Iterable[str], name_to_id: Mapping[str, int], *, unique: bool = True
) -> list[str]:
    """Copy the reference adapter's deliberately strict atom extraction."""

    result: list[str] = []
    for value in values:
        tokens = re.findall(r"[A-Za-z_]\w*", value)
        matches = [token for token in tokens if token in name_to_id]
        if len(matches) != 1:
            raise TraceValidationError(
                f"expected exactly one atom in NA item: {value!r}"
            )
        result.append(matches[0])
    if unique and len(set(result)) != len(result):
        raise TraceValidationError(f"duplicate atom in NA operation: {result!r}")
    return result


class IncrementalNANormalizer:
    """Normalize one NA source while retaining only bounded physical state.

    Instances are intentionally single-use.  The properties exposed after (or
    during) iteration make the bounded-state contract auditable without
    retaining previously emitted events.
    """

    def __init__(
        self,
        *,
        architecture: Mapping[str, Any] | str | Path | None = None,
        model: FidelityModel | None = None,
        gate_pairs: Iterable[Sequence[Sequence[int]]] | None = None,
    ):
        self.model = model or FidelityModel()
        self._view: _ArchitectureView | None = _load_architecture(architecture)
        self._explicit_pairs = iter(gate_pairs) if gate_pairs is not None else None
        self._used = False

        self._names: list[str] = []
        self._name_to_id: dict[str, int] = {}
        self._id_to_name: dict[int, str] = {}
        self._locations: dict[str, Position] = {}
        self._regions: dict[str, str] = {}
        self._held: set[str] = set()
        self._batch_origin: dict[str, str] = {}
        self._moved_in_batch: set[str] = set()
        self._batch_id: str | None = None
        self._batch_counter = 0
        self._cz_index = 0
        self._cursor = 0.0
        self._max_payload_items = 0

    @property
    def n_qubits(self) -> int:
        return len(self._names)

    @property
    def cursor_us(self) -> float:
        return self._cursor

    @property
    def cz_pulses(self) -> int:
        return self._cz_index

    @property
    def retained_atom_records(self) -> int:
        """Number of atom-keyed records retained across operation boundaries."""

        return (
            len(self._names)
            + len(self._name_to_id)
            + len(self._id_to_name)
            + len(self._locations)
            + len(self._regions)
            + len(self._held)
            + len(self._batch_origin)
            + len(self._moved_in_batch)
        )

    @property
    def max_payload_items(self) -> int:
        return self._max_payload_items

    def normalize(self, source: str | Path) -> Iterator[CanonicalTraceEvent]:
        if self._used:
            raise RuntimeError("IncrementalNANormalizer instances are single-use")
        self._used = True
        reader = _FilteredLineReader(source)

        first = reader.next()
        if first is None:
            raise TraceValidationError("NA source is empty")
        reader.push(first)
        self._read_declarations(reader)
        yield self._init_event()

        while True:
            item = reader.next()
            if item is None:
                break
            source_index, line = item
            operation_match = _NA_OPERATION.match(line)
            operation = (
                operation_match.group(1) if operation_match is not None else None
            )

            if operation == "load":
                assert operation_match is not None
                values = self._payload(reader, line, operation_match)
                yield self._load(values, source_index)
            elif operation == "move":
                assert operation_match is not None
                values = self._payload(reader, line, operation_match)
                yield self._move(values, source_index)
            elif operation == "store":
                assert operation_match is not None
                values = self._payload(reader, line, operation_match)
                yield self._store(values, source_index)
            elif operation == "cz":
                assert operation_match is not None
                yield self._cz(line, operation_match, source_index)
            elif operation in {"u", "rz", "ry", "sh"}:
                assert operation_match is not None
                values = self._payload(reader, line, operation_match)
                yield from self._one_qubit(
                    operation, values, source_index
                )
            elif operation is not None:
                raise UnsupportedOperationError(
                    f"unsupported NA operation {operation!r} at line "
                    f"{source_index + 1}"
                )
            elif line.startswith("atom "):
                raise TraceValidationError(
                    "NA atom declarations must precede all operations"
                )
            elif line == "]":
                raise TraceValidationError(
                    "unexpected closing bracket in NA source"
                )
            else:
                raise UnsupportedOperationError(
                    f"unsupported NA operation at line {source_index + 1}: "
                    f"{line!r}"
                )

        if self._held:
            raise TraceValidationError(
                f"NA trace ends with atoms still held: {sorted(self._held)}"
            )
        if self._explicit_pairs is not None:
            try:
                next(self._explicit_pairs)
            except StopIteration:
                pass
            else:
                raise TraceValidationError(
                    "NA gate-pair ledger has more entries than CZ pulses"
                )

    def _read_declarations(self, reader: _FilteredLineReader) -> None:
        declaration_names: list[str] = []
        while True:
            item = reader.next()
            if item is None:
                break
            _source_index, line = item
            match = _ATOM_DECL.match(line)
            if match is None:
                reader.push(item)
                break
            name = match.group(3)
            if name in self._locations:
                raise TraceValidationError(
                    f"duplicate NA atom declaration: {name}"
                )
            declaration_names.append(name)
            self._locations[name] = (
                float(match.group(1)),
                float(match.group(2)),
            )

        if not declaration_names:
            raise TraceValidationError(
                "NA source must start with atom declarations"
            )
        if len(set(self._locations.values())) != len(self._locations):
            raise TraceValidationError(
                "two NA atoms occupy the same initial position"
            )

        for name in declaration_names:
            atom_match = _ATOM_NAME.fullmatch(name)
            if atom_match is None:
                raise TraceValidationError(
                    "NA atom name must encode its logical id as atomN: "
                    f"{name!r}"
                )
            logical_id = int(atom_match.group(1))
            if logical_id in self._id_to_name:
                raise TraceValidationError(
                    f"duplicate NA logical atom id {logical_id}: "
                    f"{self._id_to_name[logical_id]!r}, {name!r}"
                )
            self._name_to_id[name] = logical_id
            self._id_to_name[logical_id] = name

        expected_ids = set(range(len(declaration_names)))
        if set(self._id_to_name) != expected_ids:
            raise TraceValidationError(
                "NA atom names must form a logical-id bijection over "
                f"0..{len(declaration_names) - 1}; got "
                f"{sorted(self._id_to_name)}"
            )
        self._names = [
            self._id_to_name[q] for q in range(len(declaration_names))
        ]
        if self._view is None:
            self._regions = {name: "storage" for name in self._names}
        else:
            self._regions = {
                name: self._view.coordinate_region(self._locations[name])
                for name in self._names
            }

    def _init_event(self) -> CanonicalTraceEvent:
        return CanonicalTraceEvent(
            EventType.INIT,
            0.0,
            0.0,
            atoms=tuple(range(len(self._names))),
            end_positions=tuple(self._locations[name] for name in self._names),
            end_regions=tuple(self._regions[name] for name in self._names),
            source_index=0,
            metadata={"source_format": "na", "atom_names": self._names},
        )

    def _payload(
        self,
        reader: _FilteredLineReader,
        line: str,
        operation_match: re.Match[str],
    ) -> list[str]:
        operation = operation_match.group(1)
        suffix = line[operation_match.end() :].strip()
        if suffix != "[":
            if not suffix:
                raise TraceValidationError(f"empty NA operation {line!r}")
            self._max_payload_items = max(self._max_payload_items, 1)
            return [suffix]

        values: list[str] = []
        while True:
            item = reader.next()
            if item is None:
                raise TraceValidationError(
                    f"unterminated NA block for {operation}"
                )
            _source_index, value = item
            if value == "]":
                break
            values.append(value)
        self._max_payload_items = max(
            self._max_payload_items, len(values)
        )
        return values

    def _load(
        self, values: Sequence[str], source_index: int
    ) -> CanonicalTraceEvent:
        atoms = _known_atoms(values, self._name_to_id)
        if any(name in self._held for name in atoms):
            raise TraceValidationError(
                "NA attempts to load an atom already held by the AOD"
            )
        if not self._held:
            self._batch_id = f"na:{self._batch_counter}"
            self._batch_counter += 1
            self._batch_origin = {}
            self._moved_in_batch = set()
        assert self._batch_id is not None
        self._batch_origin.update(
            {name: self._regions[name] for name in atoms}
        )
        begin = tuple(self._locations[name] for name in atoms)
        begin_regions = tuple(self._regions[name] for name in atoms)
        end = self._cursor + self.model.transfer_duration_us
        event = CanonicalTraceEvent(
            EventType.LOAD,
            self._cursor,
            end,
            atoms=tuple(self._name_to_id[name] for name in atoms),
            start_positions=begin,
            end_positions=begin,
            start_regions=begin_regions,
            end_regions=("aod",) * len(atoms),
            batch_id=self._batch_id,
            source_index=source_index,
            metadata={"source_format": "na", "atom_names": atoms},
        )
        self._held.update(atoms)
        for name in atoms:
            self._regions[name] = "aod"
        self._cursor = end
        return event

    def _move(
        self, values: Sequence[str], source_index: int
    ) -> CanonicalTraceEvent:
        if not self._held or self._batch_id is None:
            raise TraceValidationError(
                "NA move occurs outside a load/store batch"
            )
        move_names: list[str] = []
        destinations: list[Position] = []
        for value in values:
            match = _MOVE_ITEM.match(value)
            if match is None or match.group(3) not in self._name_to_id:
                raise TraceValidationError(f"invalid NA move item: {value!r}")
            name = match.group(3)
            if name not in self._held:
                raise TraceValidationError(
                    f"NA moves atom {name} without loading it"
                )
            move_names.append(name)
            destinations.append(
                (float(match.group(1)), float(match.group(2)))
            )
        if not move_names:
            raise TraceValidationError("NA move operation is empty")
        if len(set(move_names)) != len(move_names):
            raise TraceValidationError("duplicate atom in NA move")
        starts = tuple(self._locations[name] for name in move_names)
        max_distance = max(
            math.dist(start, destination)
            for start, destination in zip(starts, destinations)
        )
        move_duration = math.sqrt(
            max_distance / self.model.movement_acceleration
        )
        end = self._cursor + move_duration
        event = CanonicalTraceEvent(
            EventType.MOVE,
            self._cursor,
            end,
            atoms=tuple(self._name_to_id[name] for name in move_names),
            start_positions=starts,
            end_positions=tuple(destinations),
            start_regions=("aod",) * len(move_names),
            end_regions=("aod",) * len(move_names),
            batch_id=self._batch_id,
            source_index=source_index,
            metadata={
                "source_format": "na",
                "atom_names": move_names,
            },
        )
        for name, destination in zip(move_names, destinations):
            self._locations[name] = destination
        self._moved_in_batch.update(move_names)
        self._cursor = end
        return event

    def _store(
        self, values: Sequence[str], source_index: int
    ) -> CanonicalTraceEvent:
        if not self._held or self._batch_id is None:
            raise TraceValidationError(
                "NA store occurs outside a load/store batch"
            )
        atoms = _known_atoms(values, self._name_to_id)
        if any(name not in self._held for name in atoms):
            raise TraceValidationError("NA stores an atom not held by the AOD")
        if any(name not in self._moved_in_batch for name in atoms):
            raise TraceValidationError(
                "NA stores an atom that has not moved in this batch"
            )
        if self._view is None:
            destination_regions = tuple(
                "storage"
                if self._batch_origin[name] == "entanglement"
                else "entanglement"
                for name in atoms
            )
        else:
            destination_regions = tuple(
                self._view.coordinate_region(self._locations[name])
                for name in atoms
            )
        positions = tuple(self._locations[name] for name in atoms)
        end = self._cursor + self.model.transfer_duration_us
        event = CanonicalTraceEvent(
            EventType.STORE,
            self._cursor,
            end,
            atoms=tuple(self._name_to_id[name] for name in atoms),
            start_positions=positions,
            end_positions=positions,
            start_regions=("aod",) * len(atoms),
            end_regions=destination_regions,
            batch_id=self._batch_id,
            source_index=source_index,
            metadata={"source_format": "na", "atom_names": atoms},
        )
        for name, destination_region in zip(atoms, destination_regions):
            self._regions[name] = destination_region
            self._held.remove(name)
        self._cursor = end
        if not self._held:
            if len(set(self._locations.values())) != len(self._locations):
                raise TraceValidationError(
                    "two NA atoms occupy the same position after store"
                )
            self._batch_id = None
            self._batch_origin = {}
            self._moved_in_batch = set()
        return event

    def _cz(
        self,
        line: str,
        operation_match: re.Match[str],
        source_index: int,
    ) -> CanonicalTraceEvent:
        if self._held:
            raise TraceValidationError(
                "NA CZ occurs while atoms are still held by the AOD"
            )
        if self._view is None:
            raise TraceValidationError(
                "NA CZ normalization requires architecture pair topology"
            )
        zone_names = [
            name for name in self._names
            if self._regions[name] == "entanglement"
        ]
        physical_pairs = _infer_pairs(
            zone_names, self._locations, self._name_to_id, self._view
        )
        if self._explicit_pairs is None:
            pairs = physical_pairs
        else:
            try:
                raw_pairs = next(self._explicit_pairs)
            except StopIteration as exc:
                raise TraceValidationError(
                    "NA gate-pair ledger has fewer entries than CZ pulses"
                ) from exc
            parsed_pairs: list[GatePair] = []
            for pair in raw_pairs:
                if len(pair) != 2:
                    raise TraceValidationError(
                        "NA gate-pair ledger entry must have two atoms: "
                        f"{pair!r}"
                    )
                parsed_pairs.append((int(pair[0]), int(pair[1])))
            pairs = tuple(parsed_pairs)
        if not pairs:
            raise TraceValidationError(
                "NA CZ has no executable gate pair in the entanglement zone"
            )
        participants = tuple(q for pair in pairs for q in pair)
        region_atoms = tuple(
            self._name_to_id[name] for name in zone_names
        )
        if not set(participants).issubset(region_atoms):
            raise TraceValidationError(
                "NA gate-pair ledger references an atom outside the zone"
            )
        for left, right in pairs:
            if left not in self._id_to_name or right not in self._id_to_name:
                raise TraceValidationError(
                    "NA gate-pair ledger references an unknown atom: "
                    f"{(left, right)!r}"
                )
            if not self._view.is_legal_entanglement_pair(
                self._locations[self._id_to_name[left]],
                self._locations[self._id_to_name[right]],
            ):
                raise TraceValidationError(
                    f"NA CZ pair {(left, right)!r} is not a legal "
                    "architecture pair"
                )
        if self._explicit_pairs is not None:
            explicit_pair_set = {frozenset(pair) for pair in pairs}
            physical_pair_set = {
                frozenset(pair) for pair in physical_pairs
            }
            if explicit_pair_set != physical_pair_set:
                raise TraceValidationError(
                    "NA gate-pair ledger does not match the executable "
                    "architecture pairs occupied during the CZ pulse"
                )
        end = self._cursor + self.model.rydberg_duration_us
        event = CanonicalTraceEvent(
            EventType.TWO_QUBIT_GATE,
            self._cursor,
            end,
            atoms=participants,
            region=line[operation_match.end() :].strip()
            or "entanglement",
            gate_pairs=pairs,
            region_atoms=region_atoms,
            gate_names=("cz",) * len(pairs),
            source_index=source_index,
            metadata={
                "source_format": "na",
                "cz_index": self._cz_index,
            },
        )
        self._cz_index += 1
        self._cursor = end
        return event

    def _one_qubit(
        self,
        operation: str,
        values: Sequence[str],
        source_index: int,
    ) -> Iterator[CanonicalTraceEvent]:
        if self._held:
            raise TraceValidationError(
                "NA one-qubit gate occurs while an atom is held by the AOD"
            )
        atoms = _known_atoms(
            values, self._name_to_id, unique=False
        )
        for gate_index, name in enumerate(atoms):
            position = self._locations[name]
            atom_region = self._regions[name]
            end = self._cursor + self.model.one_qubit_duration_us
            yield CanonicalTraceEvent(
                EventType.ONE_QUBIT_GATE,
                self._cursor,
                end,
                atoms=(self._name_to_id[name],),
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
            self._cursor = end


def normalize_na_incrementally(
    source: str | Path,
    *,
    architecture: Mapping[str, Any] | str | Path | None = None,
    model: FidelityModel | None = None,
    gate_pairs: Iterable[Sequence[Sequence[int]]] | None = None,
) -> Iterator[CanonicalTraceEvent]:
    """Yield canonical NA events without materializing the native program."""

    normalizer = IncrementalNANormalizer(
        architecture=architecture,
        model=model,
        gate_pairs=gate_pairs,
    )
    yield from normalizer.normalize(source)


__all__ = ["IncrementalNANormalizer", "normalize_na_incrementally"]
