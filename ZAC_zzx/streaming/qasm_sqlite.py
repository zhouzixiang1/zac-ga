"""Two-pass, bounded-memory canonical OpenQASM 2 layer store.

This reader intentionally accepts only canonical files containing qreg plus
one- and two-qubit basis instructions.  It fails closed on measurements,
control flow, register-wide operands and user-defined gates.  Pass one writes
events, dependency layers, two-qubit layers, next-use links, the interaction
matrix and a strict per-atom logical-operation ledger directly to SQLite.  The
original dependency-layer API is retained for event replay.  Placement
look-ahead uses the independent CZ-layer API, so single-qubit gates cannot
silently consume the look-ahead horizon.  A separate frozen stock-ZAC view
retains original CZ indices, 1Q parent indices and capacity-balanced stages;
formal ZAC scheduling must consume that view rather than dependency layers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple


_QREG = re.compile(r"^qreg\s+([A-Za-z_]\w*)\[(\d+)\]$")
_CREG = re.compile(r"^creg\s+([A-Za-z_]\w*)\[(\d+)\]$")
_OP = re.compile(r"^([A-Za-z_]\w*)(?:\([^;]*\))?\s+(.+)$")
_REF = re.compile(r"^([A-Za-z_]\w*)\[(\d+)\]$")
_ALLOWED = frozenset(("cz", "u1", "u2", "u3"))

LOGICAL_LEDGER_FORMAT = "zac-per-atom-logical-sequence-v2"
ZAC_VIEW_FORMAT = "zac-scheduler-read-view-v1"
_LOGICAL_ATOM_DOMAIN = b"zac-logical-atom-sequence-v1\0"
_LOGICAL_LEDGER_DOMAIN = b"zac-logical-ledger-v1\0"


@dataclass(frozen=True)
class GateEvent:
    seq: int
    layer: int
    operation: str
    qubits: Tuple[int, ...]
    statement: str
    two_qubit_layer: Optional[int] = None
    two_qubit_index: Optional[int] = None
    parent_two_qubit_index: Optional[int] = None


@dataclass(frozen=True)
class LogicalAtomLedger:
    """Frozen digest and counts for one logical atom's operation sequence."""

    qubit: int
    operation_count: int
    gates_1q: int
    gates_2q: int
    sequence_sha256: str


@dataclass(frozen=True)
class DependencyLayerMetadata:
    """Small, streamable summary for one dependency layer."""

    layer: int
    event_count: int
    gates_1q: int
    gates_2q: int
    first_seq: int
    last_seq: int


@dataclass(frozen=True)
class DependencyLayerBatch:
    """All disjoint events in one dependency layer plus its summary.

    A dependency layer contains at most O(number-of-qubits) events, so a batch
    retains the bounded-memory property while avoiding a live nested SQLite
    cursor in the public API.
    """

    metadata: DependencyLayerMetadata
    events: Tuple[GateEvent, ...]


@dataclass(frozen=True)
class ZacGate:
    """One original CZ in the exact index space used by stock ZAC."""

    two_qubit_index: int
    gate_pair: Tuple[int, int]
    source_seq: int
    asap_layer: int


@dataclass(frozen=True)
class ZacAsapLayer:
    """One un-split ASAP layer produced from the original CZ stream."""

    layer: int
    gates: Tuple[ZacGate, ...]


@dataclass(frozen=True)
class ZacStage:
    """One capacity-safe stage after stock ZAC's balanced layer split."""

    stage_index: int
    asap_layer: int
    chunk_index: int
    chunk_count: int
    gates: Tuple[ZacGate, ...]


def _unsigned(value: int, width: int) -> bytes:
    if value < 0:
        raise ValueError("logical-ledger integers must be non-negative")
    return value.to_bytes(width, byteorder="big", signed=False)


def _marker_frame(marker: str) -> bytes:
    encoded = marker.encode("ascii")
    return _unsigned(len(encoded), 4) + encoded


def _initial_atom_digest(qubit: int) -> bytes:
    return hashlib.sha256(
        _LOGICAL_ATOM_DOMAIN + _unsigned(qubit, 8)).digest()


def _aggregate_logical_ledger_sha256(
    records: Iterable[LogicalAtomLedger], qubits: int,
) -> str:
    """Hash ordered per-atom sequence digests without materialising sequences."""
    digest = hashlib.sha256()
    digest.update(_LOGICAL_LEDGER_DOMAIN)
    digest.update(_unsigned(qubits, 8))
    expected_qubit = 0
    for record in records:
        if record.qubit != expected_qubit:
            raise ValueError(
                "logical atom ledger must contain contiguous sorted qubits: "
                f"expected {expected_qubit}, found {record.qubit}")
        try:
            sequence_digest = bytes.fromhex(record.sequence_sha256)
        except ValueError as exc:
            raise ValueError(
                f"invalid sequence SHA256 for qubit {record.qubit}") from exc
        if len(sequence_digest) != hashlib.sha256().digest_size:
            raise ValueError(
                f"invalid sequence SHA256 for qubit {record.qubit}")
        digest.update(_unsigned(record.qubit, 8))
        digest.update(_unsigned(record.operation_count, 8))
        digest.update(_unsigned(record.gates_1q, 8))
        digest.update(_unsigned(record.gates_2q, 8))
        digest.update(sequence_digest)
        expected_qubit += 1
    if expected_qubit != qubits:
        raise ValueError(
            "logical atom ledger has the wrong number of qubits: "
            f"expected {qubits}, found {expected_qubit}")
    return digest.hexdigest()


class LogicalLedgerHasher:
    """O(qubits) rolling hasher for strict logical-operation verification.

    One-qubit gates are represented by ``1q``.  A CZ on ``q0, q1`` appends
    ``cz:q1`` to q0 and ``cz:q0`` to q1.  Markers are length framed and each
    atom has an independent SHA-256 state, so independent operations may be
    scheduled in a different global order without changing the total ledger.
    """

    def __init__(self, qubits: int = 0):
        self._digests: List[bytes] = []
        self._operations: List[int] = []
        self._gates_1q: List[int] = []
        self._gates_2q: List[int] = []
        self.extend_qubits(qubits)

    @property
    def qubits(self) -> int:
        return len(self._digests)

    def extend_qubits(self, count: int) -> None:
        if count < 0:
            raise ValueError("qubit count must be non-negative")
        for qubit in range(self.qubits, self.qubits + count):
            self._digests.append(_initial_atom_digest(qubit))
            self._operations.append(0)
            self._gates_1q.append(0)
            self._gates_2q.append(0)

    def _validate_qubit(self, qubit: int) -> None:
        if not 0 <= qubit < self.qubits:
            raise ValueError(f"logical qubit out of range: {qubit}")

    def _record(self, qubit: int, marker: str, *, one_qubit: bool) -> None:
        self._validate_qubit(qubit)
        # A chain hash is deliberately used instead of serialising CPython's
        # opaque hashlib state.  The current 32-byte digest is sufficient to
        # checkpoint and resume the per-atom ledger exactly.
        self._digests[qubit] = hashlib.sha256(
            self._digests[qubit] + _marker_frame(marker)).digest()
        self._operations[qubit] += 1
        if one_qubit:
            self._gates_1q[qubit] += 1
        else:
            self._gates_2q[qubit] += 1

    def record_one_qubit(self, qubit: int) -> None:
        self._record(qubit, "1q", one_qubit=True)

    def record_cz(self, q0: int, q1: int) -> None:
        self._validate_qubit(q0)
        self._validate_qubit(q1)
        self._record(q0, f"cz:{q1}", one_qubit=False)
        self._record(q1, f"cz:{q0}", one_qubit=False)

    def iter_atom_ledgers(self) -> Iterator[LogicalAtomLedger]:
        for qubit, digest in enumerate(self._digests):
            yield LogicalAtomLedger(
                qubit=qubit,
                operation_count=self._operations[qubit],
                gates_1q=self._gates_1q[qubit],
                gates_2q=self._gates_2q[qubit],
                sequence_sha256=digest.hex(),
            )

    def hexdigest(self) -> str:
        return _aggregate_logical_ledger_sha256(
            self.iter_atom_ledgers(), self.qubits)

    def state_dict(self) -> dict[str, Any]:
        """Return the exact O(qubits) rolling state for a compiler checkpoint."""

        return {
            "format": LOGICAL_LEDGER_FORMAT,
            "digests": [value.hex() for value in self._digests],
            "operations": list(self._operations),
            "gates_1q": list(self._gates_1q),
            "gates_2q": list(self._gates_2q),
        }

    @classmethod
    def from_state(cls, value: Mapping[str, Any]) -> "LogicalLedgerHasher":
        state = dict(value)
        if state.pop("format", None) != LOGICAL_LEDGER_FORMAT:
            raise ValueError("unsupported logical-ledger checkpoint format")
        unknown = set(state) - {"digests", "operations", "gates_1q", "gates_2q"}
        if unknown:
            raise ValueError(
                f"unknown logical-ledger checkpoint fields: {sorted(unknown)}")
        raw_digests = list(state["digests"])
        hasher = cls(len(raw_digests))
        try:
            hasher._digests = [bytes.fromhex(str(item)) for item in raw_digests]
        except ValueError as error:
            raise ValueError("invalid logical-ledger checkpoint digest") from error
        if any(len(item) != hashlib.sha256().digest_size
               for item in hasher._digests):
            raise ValueError("invalid logical-ledger checkpoint digest length")
        hasher._operations = [int(item) for item in state["operations"]]
        hasher._gates_1q = [int(item) for item in state["gates_1q"]]
        hasher._gates_2q = [int(item) for item in state["gates_2q"]]
        for name, sequence in (
            ("operations", hasher._operations),
            ("gates_1q", hasher._gates_1q),
            ("gates_2q", hasher._gates_2q),
        ):
            if len(sequence) != hasher.qubits or any(item < 0 for item in sequence):
                raise ValueError(f"invalid logical-ledger checkpoint {name}")
        if any(
            hasher._operations[q] != hasher._gates_1q[q] + hasher._gates_2q[q]
            for q in range(hasher.qubits)
        ):
            raise ValueError("logical-ledger checkpoint count mismatch")
        return hasher


def _statements(lines: Iterable[str]) -> Iterator[str]:
    """Yield semicolon-terminated statements without buffering the file."""
    pending = ""
    for raw in lines:
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        pending = f"{pending} {line}".strip()
        while ";" in pending:
            statement, pending = pending.split(";", 1)
            statement = statement.strip()
            if statement:
                yield statement
            pending = pending.strip()
    if pending:
        raise ValueError(f"unterminated QASM statement: {pending[:80]}")


class LayerStore:
    def __init__(self, path: str | Path, *, read_only: bool = True):
        self.path = Path(path).resolve()
        if read_only:
            uri = f"file:{self.path}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True)
        else:
            self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        event_columns = self.connection.execute(
            "PRAGMA table_info(events)").fetchall()
        self._event_columns = {str(row["name"]) for row in event_columns}
        self._has_two_qubit_layers = "two_qubit_layer" in self._event_columns
        self._has_zac_view = {
            "two_qubit_layer",
            "two_qubit_index",
            "parent_two_qubit_index",
        }.issubset(self._event_columns)
        self._zac_gate_count = -1
        if self._has_zac_view:
            row = self.connection.execute(
                "SELECT value FROM metadata "
                "WHERE key='zac_two_qubit_index_count'").fetchone()
            if row is not None:
                self._zac_gate_count = int(json.loads(row["value"]))
        tables = {
            str(row["name"])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        self._has_dependency_layer_metadata = "dependency_layers" in tables
        self._has_logical_atom_ledger = "logical_atom_ledger" in tables

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LayerStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def metadata(self) -> Dict[str, object]:
        rows = self.connection.execute("SELECT key, value FROM metadata")
        result: Dict[str, object] = {}
        for row in rows:
            result[row["key"]] = json.loads(row["value"])
        return result

    def _event_projection(self) -> str:
        two_qubit_layer = (
            "two_qubit_layer" if self._has_two_qubit_layers
            else "NULL AS two_qubit_layer")
        two_qubit_index = (
            "two_qubit_index" if "two_qubit_index" in self._event_columns
            else "NULL AS two_qubit_index")
        parent = (
            "parent_two_qubit_index"
            if "parent_two_qubit_index" in self._event_columns
            else "NULL AS parent_two_qubit_index")
        return (
            f"seq,layer,{two_qubit_layer},{two_qubit_index},{parent},"
            "operation,q0,q1,statement")

    def iter_layers(self, start_layer: int = 0, lookahead_horizon: int = 0
                    ) -> Iterator[Tuple[int, List[GateEvent]]]:
        if start_layer < 0 or lookahead_horizon < 0:
            raise ValueError("layer and horizon must be non-negative")
        max_layer = int(self.metadata.get("max_layer", -1))
        for layer in range(start_layer, max_layer + 1):
            yield layer, self.window(layer, lookahead_horizon)

    @staticmethod
    def _validate_layer_range(
        start_layer: int, stop_layer: Optional[int],
    ) -> None:
        if start_layer < 0:
            raise ValueError("start layer must be non-negative")
        if stop_layer is not None and stop_layer < start_layer:
            raise ValueError("stop layer must be at least the start layer")

    def iter_dependency_events(
        self, start_layer: int = 0, stop_layer: Optional[int] = None,
    ) -> Iterator[GateEvent]:
        """Stream every event exactly once in ``(dependency layer, seq)`` order.

        ``stop_layer`` is inclusive.  Unlike :meth:`iter_layers`, this API has
        no look-ahead overlap and does not materialise a window, making it the
        event-wise full-pass API for Large compilation and verification.
        """
        self._validate_layer_range(start_layer, stop_layer)
        query = f"SELECT {self._event_projection()} FROM events WHERE layer>=?"
        parameters: Tuple[int, ...] = (start_layer,)
        if stop_layer is not None:
            query += " AND layer<=?"
            parameters = (start_layer, stop_layer)
        query += " ORDER BY layer, seq"
        for row in self.connection.execute(query, parameters):
            yield self._event_from_row(row)

    def iter_dependency_layer_metadata(
        self, start_layer: int = 0, stop_layer: Optional[int] = None,
    ) -> Iterator[DependencyLayerMetadata]:
        """Stream summaries for all populated dependency layers."""
        self._validate_layer_range(start_layer, stop_layer)
        if self._has_dependency_layer_metadata:
            query = (
                "SELECT layer,event_count,gates_1q,gates_2q,first_seq,last_seq "
                "FROM dependency_layers WHERE layer>=?")
        else:
            # Additive API remains useful on old v1/v2 stores.
            query = (
                "SELECT layer,COUNT(*) AS event_count,"
                "SUM(CASE WHEN q1 IS NULL THEN 1 ELSE 0 END) AS gates_1q,"
                "SUM(CASE WHEN q1 IS NULL THEN 0 ELSE 1 END) AS gates_2q,"
                "MIN(seq) AS first_seq,MAX(seq) AS last_seq "
                "FROM events WHERE layer>=?")
        parameters: Tuple[int, ...] = (start_layer,)
        if stop_layer is not None:
            query += " AND layer<=?"
            parameters = (start_layer, stop_layer)
        if not self._has_dependency_layer_metadata:
            query += " GROUP BY layer"
        query += " ORDER BY layer"
        for row in self.connection.execute(query, parameters):
            yield DependencyLayerMetadata(
                layer=int(row["layer"]),
                event_count=int(row["event_count"]),
                gates_1q=int(row["gates_1q"]),
                gates_2q=int(row["gates_2q"]),
                first_seq=int(row["first_seq"]),
                last_seq=int(row["last_seq"]),
            )

    def iter_dependency_layers(
        self, start_layer: int = 0, stop_layer: Optional[int] = None,
    ) -> Iterator[DependencyLayerBatch]:
        """Stream non-overlapping dependency-layer batches.

        Each event appears in exactly one returned batch.  Only one layer is
        buffered, whose width is bounded by the number of logical qubits.
        """
        events = iter(self.iter_dependency_events(start_layer, stop_layer))
        event = next(events, None)
        for metadata in self.iter_dependency_layer_metadata(
                start_layer, stop_layer):
            batch: List[GateEvent] = []
            while event is not None and event.layer == metadata.layer:
                batch.append(event)
                event = next(events, None)
            if len(batch) != metadata.event_count:
                raise ValueError(
                    "dependency-layer metadata/event mismatch for layer "
                    f"{metadata.layer}: expected {metadata.event_count}, "
                    f"found {len(batch)}")
            yield DependencyLayerBatch(metadata=metadata, events=tuple(batch))
        if event is not None:
            raise ValueError(
                "dependency-layer metadata ended before the event stream")

    def window(self, current_layer: int, lookahead_horizon: int) -> List[GateEvent]:
        """Return the legacy all-event dependency-layer window.

        As before, ``H=0`` includes the current layer and its transition target
        (``current_layer + 1``).  This method intentionally does not use CZ
        layers; callers implementing residency look-ahead must use
        :meth:`two_qubit_window`.
        """
        if current_layer < 0 or lookahead_horizon < 0:
            raise ValueError("layer and horizon must be non-negative")
        upper = current_layer + lookahead_horizon + 1
        rows = self.connection.execute(
            f"SELECT {self._event_projection()} FROM events "
            "WHERE layer >= ? AND layer <= ? ORDER BY layer, seq",
            (current_layer, upper),
        )
        return [self._event_from_row(row) for row in rows]

    def layer(self, layer: int) -> List[GateEvent]:
        return [event for event in self.window(layer, 0) if event.layer == layer]

    def next_use(self, event_seq: int, qubit: int) -> Optional[Tuple[int, int]]:
        """Return the next use in the legacy all-event dependency layers."""
        row = self.connection.execute(
            "SELECT next_seq, next_layer FROM uses WHERE seq=? AND qubit=?",
            (event_seq, qubit),
        ).fetchone()
        if row is None or row["next_seq"] is None:
            return None
        return int(row["next_seq"]), int(row["next_layer"])

    def iter_two_qubit_layers(
        self, start_layer: int = 0, lookahead_horizon: int = 0,
    ) -> Iterator[Tuple[int, List[GateEvent]]]:
        """Iterate CZ layers with a residency transition/look-ahead window."""
        self._require_two_qubit_index()
        if start_layer < 0 or lookahead_horizon < 0:
            raise ValueError("layer and horizon must be non-negative")
        max_layer = int(self.metadata.get("max_two_qubit_layer", -1))
        for layer in range(start_layer, max_layer + 1):
            yield layer, self.two_qubit_window(layer, lookahead_horizon)

    def two_qubit_window(
        self, current_layer: int, lookahead_horizon: int,
    ) -> List[GateEvent]:
        """Return CZs from ``L`` through ``L + H + 1`` inclusive.

        ``H=0`` therefore exposes exactly the current-to-next CZ transition.
        ``H=2`` additionally exposes CZ layers ``L+2`` and ``L+3``.  Any
        number of intervening one-qubit dependency layers has no effect.
        """
        self._require_two_qubit_index()
        if current_layer < 0 or lookahead_horizon < 0:
            raise ValueError("layer and horizon must be non-negative")
        upper = current_layer + lookahead_horizon + 1
        rows = self.connection.execute(
            f"SELECT {self._event_projection()} "
            "FROM events WHERE two_qubit_layer >= ? AND two_qubit_layer <= ? "
            "ORDER BY two_qubit_layer, seq",
            (current_layer, upper),
        )
        return [self._event_from_row(row) for row in rows]

    def two_qubit_layer(self, layer: int) -> List[GateEvent]:
        """Return all CZ events assigned to one independent two-qubit layer."""
        if layer < 0:
            raise ValueError("layer must be non-negative")
        return [event for event in self.two_qubit_window(layer, 0)
                if event.two_qubit_layer == layer]

    def next_two_qubit_use(
        self, event_seq: int, qubit: int,
    ) -> Optional[Tuple[int, int]]:
        """Return ``(seq, CZ-layer)`` for the next CZ use after a CZ event."""
        self._require_two_qubit_index()
        row = self.connection.execute(
            "SELECT next_seq, next_two_qubit_layer FROM two_qubit_uses "
            "WHERE seq=? AND qubit=?", (event_seq, qubit),
        ).fetchone()
        if row is None or row["next_seq"] is None:
            return None
        return int(row["next_seq"]), int(row["next_two_qubit_layer"])

    def next_two_qubit_use_after(
        self, qubit: int, after_layer: int,
    ) -> Optional[Tuple[int, int]]:
        """Return the first CZ use of ``qubit`` strictly after a CZ layer."""
        self._require_two_qubit_index()
        if qubit < 0 or after_layer < -1:
            raise ValueError("qubit must be non-negative and layer must be >= -1")
        row = self.connection.execute(
            "SELECT seq, two_qubit_layer FROM two_qubit_uses "
            "WHERE qubit=? AND two_qubit_layer>? "
            "ORDER BY two_qubit_layer, seq LIMIT 1", (qubit, after_layer),
        ).fetchone()
        if row is None:
            return None
        return int(row["seq"]), int(row["two_qubit_layer"])

    def _require_two_qubit_index(self) -> None:
        if not self._has_two_qubit_layers:
            raise ValueError(
                "layer store has no independent 2Q index; rebuild it with "
                "build_layer_store")

    @property
    def has_zac_view(self) -> bool:
        """Whether the store freezes stock ZAC's CZ/parent index spaces."""
        return self._has_zac_view and self._zac_gate_count >= 0

    def _require_zac_view(self) -> None:
        if not self.has_zac_view:
            raise ValueError(
                "layer store has no frozen ZAC scheduler view; rebuild it "
                "with build_layer_store")

    def iter_zac_leading_one_qubit(self) -> Iterator[GateEvent]:
        """Stream stock ZAC's parent ``-1`` 1Q prefix in source order."""
        self._require_zac_view()
        rows = self.connection.execute(
            f"SELECT {self._event_projection()} FROM events "
            "WHERE q1 IS NULL AND parent_two_qubit_index=-1 ORDER BY seq")
        for row in rows:
            yield self._event_from_row(row)

    def _validate_zac_gate_range(
        self, start_gate_index: int, stop_gate_index: int,
    ) -> None:
        self._require_zac_view()
        count = self._zac_gate_count
        if (start_gate_index < 0 or stop_gate_index < start_gate_index or
                stop_gate_index >= count):
            raise ValueError(
                "ZAC gate-index range is outside the frozen CZ stream: "
                f"[{start_gate_index}, {stop_gate_index}] for {count} gates")

    def iter_zac_one_qubit_for_gate_range(
        self, start_gate_index: int, stop_gate_index: Optional[int] = None,
    ) -> Iterator[GateEvent]:
        """Stream 1Q children for an inclusive continuous parent-index range.

        Results match ``Scheduler_mixin`` ordering: parent gate index first,
        then original QASM sequence within one parent.  No 1Q list is built.
        """
        stop = start_gate_index if stop_gate_index is None else stop_gate_index
        self._validate_zac_gate_range(start_gate_index, stop)
        rows = self.connection.execute(
            f"SELECT {self._event_projection()} FROM events "
            "WHERE q1 IS NULL AND parent_two_qubit_index>=? "
            "AND parent_two_qubit_index<=? "
            "ORDER BY parent_two_qubit_index,seq",
            (start_gate_index, stop),
        )
        for row in rows:
            yield self._event_from_row(row)

    def iter_zac_one_qubit_for_gate_indices(
        self, gate_indices: Iterable[int],
    ) -> Iterator[GateEvent]:
        """Stream 1Q children for sorted distinct (possibly gapped) parents.

        This is the exact primitive needed by a split ASAP stage.  At most 500
        parent indices are bound in each SQLite query; the potentially much
        longer child stream is never materialised.
        """
        self._require_zac_view()
        indices = tuple(int(value) for value in gate_indices)
        if not indices:
            return
        if tuple(sorted(set(indices))) != indices:
            raise ValueError("ZAC gate indices must be sorted and distinct")
        self._validate_zac_gate_range(indices[0], indices[-1])
        for offset in range(0, len(indices), 500):
            chunk = indices[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT {self._event_projection()} FROM events "
                "WHERE q1 IS NULL AND parent_two_qubit_index IN "
                f"({placeholders}) ORDER BY parent_two_qubit_index,seq",
                chunk,
            )
            for row in rows:
                yield self._event_from_row(row)

    def iter_zac_one_qubit_for_stage(
        self, stage: ZacStage,
    ) -> Iterator[GateEvent]:
        """Stream exactly the 1Q children attached to one ZAC stage."""
        indices = tuple(gate.two_qubit_index for gate in stage.gates)
        yield from self.iter_zac_one_qubit_for_gate_indices(indices)

    def iter_zac_asap_layers(
        self, start_layer: int = 0, stop_layer: Optional[int] = None,
    ) -> Iterator[ZacAsapLayer]:
        """Stream the original CZ-only ASAP layers used by stock ZAC."""
        self._require_zac_view()
        self._validate_layer_range(start_layer, stop_layer)
        query = (
            "SELECT seq,two_qubit_layer,two_qubit_index,q0,q1 FROM events "
            "WHERE q1 IS NOT NULL AND two_qubit_layer>=?")
        parameters: Tuple[int, ...] = (start_layer,)
        if stop_layer is not None:
            query += " AND two_qubit_layer<=?"
            parameters = (start_layer, stop_layer)
        query += " ORDER BY two_qubit_layer,two_qubit_index"

        active_layer: Optional[int] = None
        gates: List[ZacGate] = []
        for row in self.connection.execute(query, parameters):
            layer = int(row["two_qubit_layer"])
            if active_layer is not None and layer != active_layer:
                yield ZacAsapLayer(layer=active_layer, gates=tuple(gates))
                gates = []
            active_layer = layer
            q0, q1 = sorted((int(row["q0"]), int(row["q1"])))
            gates.append(ZacGate(
                two_qubit_index=int(row["two_qubit_index"]),
                gate_pair=(q0, q1),
                source_seq=int(row["seq"]),
                asap_layer=layer,
            ))
        if active_layer is not None:
            yield ZacAsapLayer(layer=active_layer, gates=tuple(gates))

    def iter_zac_stages(self, max_gates: int) -> Iterator[ZacStage]:
        """Stream stock ZAC's exact capacity-balanced CZ stage sequence.

        This intentionally mirrors ``scheduler.py`` lines 20--35: a layer
        smaller than capacity is retained; otherwise it is divided into
        ``ceil(width/capacity)`` stages, each targeting
        ``ceil(width/number_of_stages)`` consecutive gates.
        """
        self._require_zac_view()
        if max_gates <= 0:
            raise ValueError("ZAC stage capacity must be positive")
        stage_index = 0
        for layer in self.iter_zac_asap_layers():
            width = len(layer.gates)
            if width < max_gates:
                chunk_count = 1
                gates_per_chunk = width
            else:
                chunk_count = (width + max_gates - 1) // max_gates
                gates_per_chunk = (width + chunk_count - 1) // chunk_count
            chunks = tuple(
                layer.gates[offset:offset + gates_per_chunk]
                for offset in range(0, width, gates_per_chunk)
            )
            if len(chunks) != chunk_count:
                raise ValueError("internal ZAC balanced-stage split mismatch")
            for chunk_index, gates in enumerate(chunks):
                yield ZacStage(
                    stage_index=stage_index,
                    asap_layer=layer.layer,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                    gates=gates,
                )
                stage_index += 1

    def verify_zac_view(self) -> Mapping[str, int]:
        """Strictly replay and verify the frozen stock-ZAC scheduling view."""
        self._require_zac_view()
        metadata = self.metadata
        if metadata.get("zac_view_format") != ZAC_VIEW_FORMAT:
            raise ValueError(
                "unsupported frozen ZAC scheduler view format: "
                f"{metadata.get('zac_view_format')!r}")
        qubits = int(metadata.get("qubits", -1))
        if qubits < 0:
            raise ValueError("frozen ZAC scheduler view has invalid qubit count")
        last_cz = [-1] * qubits
        next_asap_layer = [0] * qubits
        counts = {"gates_1q": 0, "gates_2q": 0,
                  "leading_1q": 0, "parent_1q": 0}
        max_asap_layer = -1
        rows = self.connection.execute(
            "SELECT seq,two_qubit_layer,two_qubit_index,"
            "parent_two_qubit_index,q0,q1 FROM events ORDER BY seq")
        for row in rows:
            q0 = int(row["q0"])
            if not 0 <= q0 < qubits:
                raise ValueError("frozen ZAC scheduler view qubit out of range")
            if row["q1"] is None:
                counts["gates_1q"] += 1
                if (row["two_qubit_index"] is not None or
                        row["two_qubit_layer"] is not None):
                    raise ValueError("1Q event has frozen CZ scheduling fields")
                parent = row["parent_two_qubit_index"]
                if parent is None or int(parent) != last_cz[q0]:
                    raise ValueError(
                        "frozen ZAC 1Q parent does not match the most recent CZ")
                key = "leading_1q" if int(parent) == -1 else "parent_1q"
                counts[key] += 1
                continue

            q1 = int(row["q1"])
            if not 0 <= q1 < qubits:
                raise ValueError("frozen ZAC scheduler view qubit out of range")
            if row["parent_two_qubit_index"] is not None:
                raise ValueError("CZ event has a frozen 1Q parent")
            gate_index = row["two_qubit_index"]
            if gate_index is None or int(gate_index) != counts["gates_2q"]:
                raise ValueError("frozen ZAC CZ indices are not contiguous")
            expected_layer = max(next_asap_layer[q0], next_asap_layer[q1])
            if (row["two_qubit_layer"] is None or
                    int(row["two_qubit_layer"]) != expected_layer):
                raise ValueError("frozen ZAC CZ ASAP layer mismatch")
            max_asap_layer = max(max_asap_layer, expected_layer)
            next_asap_layer[q0] = expected_layer + 1
            next_asap_layer[q1] = expected_layer + 1
            last_cz[q0] = int(gate_index)
            last_cz[q1] = int(gate_index)
            counts["gates_2q"] += 1

        expected = {
            "gates_1q": int(metadata.get("gates_1q", -1)),
            "gates_2q": int(metadata.get("zac_two_qubit_index_count", -1)),
            "leading_1q": int(metadata.get("zac_leading_one_qubit_count", -1)),
            "parent_1q": int(metadata.get("zac_parent_one_qubit_count", -1)),
        }
        if counts != expected:
            raise ValueError(
                f"frozen ZAC scheduler view count mismatch: "
                f"expected {expected}, replayed {counts}")
        if counts["gates_2q"] != int(metadata.get("gates_2q", -1)):
            raise ValueError("frozen ZAC scheduler view CZ count mismatch")
        if counts["gates_1q"] != int(
                metadata.get("zac_one_qubit_count", -1)):
            raise ValueError("frozen ZAC scheduler view 1Q coverage mismatch")
        if counts["gates_1q"] + counts["gates_2q"] != int(
                metadata.get("events", -1)):
            raise ValueError("frozen ZAC scheduler view event coverage mismatch")
        if max_asap_layer + 1 != int(metadata.get("two_qubit_layers", -1)):
            raise ValueError("frozen ZAC scheduler view ASAP layer count mismatch")
        return counts

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> GateEvent:
        raw_two_qubit_layer = row["two_qubit_layer"]
        raw_two_qubit_index = row["two_qubit_index"]
        raw_parent = row["parent_two_qubit_index"]
        return GateEvent(
            seq=int(row["seq"]),
            layer=int(row["layer"]),
            operation=str(row["operation"]),
            qubits=((int(row["q0"]),) if row["q1"] is None else
                    (int(row["q0"]), int(row["q1"]))),
            statement=str(row["statement"]),
            two_qubit_layer=(None if raw_two_qubit_layer is None else
                             int(raw_two_qubit_layer)),
            two_qubit_index=(None if raw_two_qubit_index is None else
                             int(raw_two_qubit_index)),
            parent_two_qubit_index=(None if raw_parent is None else
                                    int(raw_parent)),
        )

    def interaction_matrix(self) -> Iterator[Tuple[int, int, int]]:
        rows = self.connection.execute(
            "SELECT q0, q1, weight FROM interactions ORDER BY q0, q1")
        for row in rows:
            yield int(row["q0"]), int(row["q1"]), int(row["weight"])

    @property
    def has_logical_ledger(self) -> bool:
        return self._has_logical_atom_ledger

    @property
    def logical_ledger_sha256(self) -> Optional[str]:
        """Return the frozen total per-atom sequence hash, if available."""
        metadata = self.metadata
        primary = metadata.get("per_atom_logical_operation_sequence_sha256")
        alias = metadata.get("logical_ledger_sha256")
        if primary is not None and alias is not None and primary != alias:
            raise ValueError("logical ledger metadata hashes disagree")
        value = primary if primary is not None else alias
        return None if value is None else str(value)

    def iter_logical_atom_ledgers(self) -> Iterator[LogicalAtomLedger]:
        """Stream the frozen digest/count record for every logical atom."""
        if not self._has_logical_atom_ledger:
            raise ValueError(
                "layer store has no frozen logical ledger; rebuild it with "
                "build_layer_store")
        rows = self.connection.execute(
            "SELECT qubit,operation_count,gates_1q,gates_2q,sequence_sha256 "
            "FROM logical_atom_ledger ORDER BY qubit")
        for row in rows:
            yield LogicalAtomLedger(
                qubit=int(row["qubit"]),
                operation_count=int(row["operation_count"]),
                gates_1q=int(row["gates_1q"]),
                gates_2q=int(row["gates_2q"]),
                sequence_sha256=str(row["sequence_sha256"]),
            )

    def verify_logical_ledger(self) -> str:
        """Recompute the total from frozen atom records and verify metadata."""
        metadata = self.metadata
        expected = self.logical_ledger_sha256
        if expected is None or not self._has_logical_atom_ledger:
            raise ValueError(
                "layer store has no frozen logical ledger; rebuild it with "
                "build_layer_store")
        if metadata.get("logical_ledger_format") != LOGICAL_LEDGER_FORMAT:
            raise ValueError(
                "unsupported frozen logical ledger format: "
                f"{metadata.get('logical_ledger_format')!r}")
        qubits = int(metadata.get("qubits", -1))
        records = tuple(self.iter_logical_atom_ledgers())
        if sum(row.gates_1q for row in records) != int(
                metadata.get("gates_1q", -1)):
            raise ValueError("frozen logical ledger 1Q count mismatch")
        if sum(row.gates_2q for row in records) != 2 * int(
                metadata.get("gates_2q", -1)):
            raise ValueError("frozen logical ledger CZ endpoint count mismatch")
        if any(row.operation_count != row.gates_1q + row.gates_2q
               for row in records):
            raise ValueError("frozen logical ledger operation count mismatch")
        if sum(row.operation_count for row in records) != int(
                metadata.get("logical_ledger_operations", -1)):
            raise ValueError("frozen logical ledger total operation mismatch")
        actual = _aggregate_logical_ledger_sha256(records, qubits)
        if actual != expected:
            raise ValueError(
                "frozen logical ledger hash mismatch: "
                f"expected {expected}, recomputed {actual}")
        return actual


def build_layer_store(qasm_path: str | Path, sqlite_path: str | Path,
                      *, commit_interval: int = 10_000) -> Mapping[str, object]:
    """Build a fresh SQLite layer store with O(number-of-qubits) Python state."""
    source = Path(qasm_path).resolve()
    destination = Path(sqlite_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        temporary.unlink()

    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            "PRAGMA journal_mode=OFF;"
            "PRAGMA synchronous=OFF;"
            "PRAGMA temp_store=FILE;"
            "CREATE TABLE events("
            " seq INTEGER PRIMARY KEY, layer INTEGER NOT NULL,"
            " two_qubit_layer INTEGER, two_qubit_index INTEGER,"
            " parent_two_qubit_index INTEGER,"
            " operation TEXT NOT NULL, q0 INTEGER NOT NULL, q1 INTEGER,"
            " statement TEXT NOT NULL);"
            "CREATE INDEX events_by_layer ON events(layer, seq);"
            "CREATE INDEX events_by_two_qubit_layer "
            "ON events(two_qubit_layer, seq);"
            "CREATE UNIQUE INDEX events_by_two_qubit_index "
            "ON events(two_qubit_index) WHERE two_qubit_index IS NOT NULL;"
            "CREATE INDEX events_by_parent_two_qubit_index "
            "ON events(parent_two_qubit_index,seq) "
            "WHERE parent_two_qubit_index IS NOT NULL;"
            "CREATE TABLE uses("
            " seq INTEGER NOT NULL, qubit INTEGER NOT NULL, layer INTEGER NOT NULL,"
            " next_seq INTEGER, next_layer INTEGER, PRIMARY KEY(seq, qubit));"
            "CREATE INDEX uses_by_qubit ON uses(qubit, seq);"
            "CREATE TABLE two_qubit_uses("
            " seq INTEGER NOT NULL, qubit INTEGER NOT NULL,"
            " two_qubit_layer INTEGER NOT NULL, next_seq INTEGER,"
            " next_two_qubit_layer INTEGER, PRIMARY KEY(seq, qubit));"
            "CREATE INDEX two_qubit_uses_by_qubit "
            "ON two_qubit_uses(qubit, two_qubit_layer, seq);"
            "CREATE TABLE interactions("
            " q0 INTEGER NOT NULL, q1 INTEGER NOT NULL, weight INTEGER NOT NULL,"
            " PRIMARY KEY(q0, q1));"
            "CREATE TABLE dependency_layers("
            " layer INTEGER PRIMARY KEY, event_count INTEGER NOT NULL,"
            " gates_1q INTEGER NOT NULL, gates_2q INTEGER NOT NULL,"
            " first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL);"
            "CREATE TABLE logical_atom_ledger("
            " qubit INTEGER PRIMARY KEY, operation_count INTEGER NOT NULL,"
            " gates_1q INTEGER NOT NULL, gates_2q INTEGER NOT NULL,"
            " sequence_sha256 TEXT NOT NULL);"
            "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        registers: Dict[str, Tuple[int, int]] = {}
        qubit_count = 0
        next_layer: List[int] = []
        last_use: List[Optional[Tuple[int, int]]] = []
        next_two_qubit_layer: List[int] = []
        last_two_qubit_use: List[Optional[Tuple[int, int]]] = []
        last_two_qubit_index: List[int] = []
        counts = {"gates_1q": 0, "gates_2q": 0}
        zac_one_qubit_counts = {"leading": 0, "parent": 0}
        logical_ledger = LogicalLedgerHasher()
        seq = 0
        max_layer = -1
        max_two_qubit_layer = -1
        digest = hashlib.sha256()

        with open(source, "r", encoding="utf-8") as handle:
            for statement in _statements(handle):
                digest.update(statement.encode("utf-8"))
                digest.update(b";\n")
                if statement.startswith("OPENQASM") or statement.startswith("include"):
                    continue
                qreg = _QREG.match(statement)
                if qreg:
                    name, raw_size = qreg.groups()
                    if name in registers:
                        raise ValueError(f"duplicate qreg: {name}")
                    size = int(raw_size)
                    registers[name] = (qubit_count, size)
                    qubit_count += size
                    next_layer.extend([0] * size)
                    last_use.extend([None] * size)
                    next_two_qubit_layer.extend([0] * size)
                    last_two_qubit_use.extend([None] * size)
                    last_two_qubit_index.extend([-1] * size)
                    logical_ledger.extend_qubits(size)
                    continue
                if _CREG.match(statement):
                    continue
                match = _OP.match(statement)
                if not match:
                    raise ValueError(f"unsupported canonical QASM statement: {statement}")
                operation, raw_operands = match.groups()
                if operation not in _ALLOWED:
                    raise ValueError(f"non-canonical operation {operation}: {statement}")
                refs = [item.strip() for item in raw_operands.split(",")]
                expected = 2 if operation == "cz" else 1
                if len(refs) != expected:
                    raise ValueError(f"wrong arity for {operation}: {statement}")
                qubits: List[int] = []
                for ref in refs:
                    ref_match = _REF.match(ref)
                    if not ref_match:
                        raise ValueError(f"register-wide/invalid operand: {ref}")
                    register, raw_index = ref_match.groups()
                    if register not in registers:
                        raise ValueError(f"unknown qreg: {register}")
                    offset, size = registers[register]
                    index = int(raw_index)
                    if not 0 <= index < size:
                        raise ValueError(f"qreg index out of range: {ref}")
                    qubits.append(offset + index)

                layer = max(next_layer[qubit] for qubit in qubits)
                max_layer = max(max_layer, layer)
                q1 = qubits[1] if len(qubits) == 2 else None
                two_qubit_layer = None
                two_qubit_index = None
                parent_two_qubit_index = None
                if q1 is not None:
                    two_qubit_layer = max(
                        next_two_qubit_layer[qubit] for qubit in qubits)
                    two_qubit_index = counts["gates_2q"]
                    max_two_qubit_layer = max(
                        max_two_qubit_layer, two_qubit_layer)
                else:
                    parent_two_qubit_index = last_two_qubit_index[qubits[0]]
                connection.execute(
                    "INSERT INTO events("
                    "seq,layer,two_qubit_layer,two_qubit_index,"
                    "parent_two_qubit_index,operation,q0,q1,statement) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (seq, layer, two_qubit_layer, two_qubit_index,
                     parent_two_qubit_index, operation, qubits[0], q1,
                     statement + ";"),
                )
                connection.execute(
                    "INSERT INTO dependency_layers("
                    "layer,event_count,gates_1q,gates_2q,first_seq,last_seq) "
                    "VALUES(?,1,?,?,?,?) ON CONFLICT(layer) DO UPDATE SET "
                    "event_count=event_count+1,"
                    "gates_1q=gates_1q+excluded.gates_1q,"
                    "gates_2q=gates_2q+excluded.gates_2q,"
                    "first_seq=MIN(first_seq,excluded.first_seq),"
                    "last_seq=MAX(last_seq,excluded.last_seq)",
                    (layer, int(q1 is None), int(q1 is not None), seq, seq),
                )
                for qubit in qubits:
                    previous = last_use[qubit]
                    if previous is not None:
                        connection.execute(
                            "UPDATE uses SET next_seq=?, next_layer=? "
                            "WHERE seq=? AND qubit=?",
                            (seq, layer, previous[0], qubit),
                        )
                    connection.execute(
                        "INSERT INTO uses(seq,qubit,layer,next_seq,next_layer) "
                        "VALUES(?,?,?,NULL,NULL)", (seq, qubit, layer))
                    last_use[qubit] = (seq, layer)
                    next_layer[qubit] = layer + 1
                if q1 is None:
                    counts["gates_1q"] += 1
                    if parent_two_qubit_index == -1:
                        zac_one_qubit_counts["leading"] += 1
                    else:
                        zac_one_qubit_counts["parent"] += 1
                    logical_ledger.record_one_qubit(qubits[0])
                else:
                    counts["gates_2q"] += 1
                    logical_ledger.record_cz(qubits[0], q1)
                    assert two_qubit_layer is not None
                    assert two_qubit_index is not None
                    for qubit in qubits:
                        previous = last_two_qubit_use[qubit]
                        if previous is not None:
                            connection.execute(
                                "UPDATE two_qubit_uses SET next_seq=?, "
                                "next_two_qubit_layer=? WHERE seq=? AND qubit=?",
                                (seq, two_qubit_layer, previous[0], qubit),
                            )
                        connection.execute(
                            "INSERT INTO two_qubit_uses("
                            "seq,qubit,two_qubit_layer,next_seq,"
                            "next_two_qubit_layer) VALUES(?,?,?,NULL,NULL)",
                            (seq, qubit, two_qubit_layer),
                        )
                        last_two_qubit_use[qubit] = (seq, two_qubit_layer)
                        next_two_qubit_layer[qubit] = two_qubit_layer + 1
                        last_two_qubit_index[qubit] = two_qubit_index
                    lo, hi = sorted(qubits)
                    connection.execute(
                        "INSERT INTO interactions(q0,q1,weight) VALUES(?,?,1) "
                        "ON CONFLICT(q0,q1) DO UPDATE SET weight=weight+1", (lo, hi))
                seq += 1
                if seq % commit_interval == 0:
                    connection.commit()

        if not registers:
            raise ValueError("canonical QASM has no qreg")
        atom_ledgers = tuple(logical_ledger.iter_atom_ledgers())
        logical_ledger_sha256 = _aggregate_logical_ledger_sha256(
            atom_ledgers, qubit_count)
        connection.executemany(
            "INSERT INTO logical_atom_ledger("
            "qubit,operation_count,gates_1q,gates_2q,sequence_sha256) "
            "VALUES(?,?,?,?,?)",
            ((record.qubit, record.operation_count, record.gates_1q,
              record.gates_2q, record.sequence_sha256)
             for record in atom_ledgers),
        )
        metadata: Dict[str, object] = {
            "format": "zac-layer-store-v2",
            "source_path": str(source),
            "statement_sha256": digest.hexdigest(),
            "qubits": qubit_count,
            "events": seq,
            "gates_1q": counts["gates_1q"],
            "gates_2q": counts["gates_2q"],
            "layers": max_layer + 1,
            "max_layer": max_layer,
            "two_qubit_layers": max_two_qubit_layer + 1,
            "max_two_qubit_layer": max_two_qubit_layer,
            "logical_ledger_format": LOGICAL_LEDGER_FORMAT,
            "logical_ledger_operations": (
                counts["gates_1q"] + 2 * counts["gates_2q"]),
            "logical_ledger_sha256": logical_ledger_sha256,
            "per_atom_logical_operation_sequence_sha256":
                logical_ledger_sha256,
            "zac_view_format": ZAC_VIEW_FORMAT,
            "zac_two_qubit_index_count": counts["gates_2q"],
            "zac_one_qubit_count": counts["gates_1q"],
            "zac_leading_one_qubit_count": zac_one_qubit_counts["leading"],
            "zac_parent_one_qubit_count": zac_one_qubit_counts["parent"],
        }
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            ((key, json.dumps(value, sort_keys=True)) for key, value in metadata.items()),
        )
        connection.commit()
    except BaseException:
        connection.close()
        if temporary.exists():
            temporary.unlink()
        raise
    else:
        connection.close()
        os.replace(temporary, destination)
        return metadata


__all__ = [
    "DependencyLayerBatch",
    "DependencyLayerMetadata",
    "GateEvent",
    "LOGICAL_LEDGER_FORMAT",
    "LayerStore",
    "LogicalAtomLedger",
    "LogicalLedgerHasher",
    "ZAC_VIEW_FORMAT",
    "ZacAsapLayer",
    "ZacGate",
    "ZacStage",
    "build_layer_store",
]
