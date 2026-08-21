"""Two-pass, bounded-memory canonical OpenQASM 2 layer store.

This reader intentionally accepts only canonical files containing qreg plus
one- and two-qubit basis instructions.  It fails closed on measurements,
control flow, register-wide operands and user-defined gates.  Pass one writes
events, dependency layers, two-qubit layers, next-use links and the interaction
matrix directly to SQLite.  The original dependency-layer API is retained for
event replay.  Placement look-ahead uses the independent CZ-layer API, so
single-qubit gates cannot silently consume the look-ahead horizon.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


_QREG = re.compile(r"^qreg\s+([A-Za-z_]\w*)\[(\d+)\]$")
_CREG = re.compile(r"^creg\s+([A-Za-z_]\w*)\[(\d+)\]$")
_OP = re.compile(r"^([A-Za-z_]\w*)(?:\([^;]*\))?\s+(.+)$")
_REF = re.compile(r"^([A-Za-z_]\w*)\[(\d+)\]$")
_ALLOWED = frozenset(("cz", "u1", "u2", "u3"))


@dataclass(frozen=True)
class GateEvent:
    seq: int
    layer: int
    operation: str
    qubits: Tuple[int, ...]
    statement: str
    two_qubit_layer: Optional[int] = None


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
        self._has_two_qubit_layers = any(
            str(row["name"]) == "two_qubit_layer" for row in event_columns)

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

    def iter_layers(self, start_layer: int = 0, lookahead_horizon: int = 0
                    ) -> Iterator[Tuple[int, List[GateEvent]]]:
        if start_layer < 0 or lookahead_horizon < 0:
            raise ValueError("layer and horizon must be non-negative")
        max_layer = int(self.metadata.get("max_layer", -1))
        for layer in range(start_layer, max_layer + 1):
            yield layer, self.window(layer, lookahead_horizon)

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
        two_qubit_select = (
            "two_qubit_layer" if self._has_two_qubit_layers
            else "NULL AS two_qubit_layer")
        rows = self.connection.execute(
            f"SELECT seq, layer, {two_qubit_select}, operation, q0, q1, "
            "statement FROM events "
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
            "SELECT seq, layer, two_qubit_layer, operation, q0, q1, statement "
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

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> GateEvent:
        raw_two_qubit_layer = row["two_qubit_layer"]
        return GateEvent(
            seq=int(row["seq"]),
            layer=int(row["layer"]),
            operation=str(row["operation"]),
            qubits=((int(row["q0"]),) if row["q1"] is None else
                    (int(row["q0"]), int(row["q1"]))),
            statement=str(row["statement"]),
            two_qubit_layer=(None if raw_two_qubit_layer is None else
                             int(raw_two_qubit_layer)),
        )

    def interaction_matrix(self) -> Iterator[Tuple[int, int, int]]:
        rows = self.connection.execute(
            "SELECT q0, q1, weight FROM interactions ORDER BY q0, q1")
        for row in rows:
            yield int(row["q0"]), int(row["q1"]), int(row["weight"])


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
            " two_qubit_layer INTEGER,"
            " operation TEXT NOT NULL, q0 INTEGER NOT NULL, q1 INTEGER,"
            " statement TEXT NOT NULL);"
            "CREATE INDEX events_by_layer ON events(layer, seq);"
            "CREATE INDEX events_by_two_qubit_layer "
            "ON events(two_qubit_layer, seq);"
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
            "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        registers: Dict[str, Tuple[int, int]] = {}
        qubit_count = 0
        next_layer: List[int] = []
        last_use: List[Optional[Tuple[int, int]]] = []
        next_two_qubit_layer: List[int] = []
        last_two_qubit_use: List[Optional[Tuple[int, int]]] = []
        counts = {"gates_1q": 0, "gates_2q": 0}
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
                if q1 is not None:
                    two_qubit_layer = max(
                        next_two_qubit_layer[qubit] for qubit in qubits)
                    max_two_qubit_layer = max(
                        max_two_qubit_layer, two_qubit_layer)
                connection.execute(
                    "INSERT INTO events("
                    "seq,layer,two_qubit_layer,operation,q0,q1,statement) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (seq, layer, two_qubit_layer, operation, qubits[0], q1,
                     statement + ";"),
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
                else:
                    counts["gates_2q"] += 1
                    assert two_qubit_layer is not None
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
                    lo, hi = sorted(qubits)
                    connection.execute(
                        "INSERT INTO interactions(q0,q1,weight) VALUES(?,?,1) "
                        "ON CONFLICT(q0,q1) DO UPDATE SET weight=weight+1", (lo, hi))
                seq += 1
                if seq % commit_interval == 0:
                    connection.commit()

        if not registers:
            raise ValueError("canonical QASM has no qreg")
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


__all__ = ["GateEvent", "LayerStore", "build_layer_store"]
