"""Disk-backed exact ASAP schedule spool for the formal QMAP 3.2 Large path."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .qasm_sqlite import LayerStore


QMAP_SCHEDULE_FORMAT = "qmap-schedule-store-v1"


@dataclass(frozen=True)
class QmapScheduledOperation:
    seq: int
    layer: int
    kind: int
    operation: str
    q0: int
    q1: int | None
    statement: str


@dataclass(frozen=True)
class QmapScheduledLayer:
    layer: int
    single_qubit: tuple[QmapScheduledOperation, ...]
    two_qubit: tuple[QmapScheduledOperation, ...]
    has_two_qubit_layer: bool


def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        str(row["key"]): json.loads(row["value"])
        for row in connection.execute("SELECT key,value FROM metadata")
    }


def _schedule_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256(b"qmap-schedule-store-v1\0")
    for row in connection.execute(
            "SELECT seq,layer,kind,operation,q0,q1,statement "
            "FROM qmap_operations ORDER BY layer,kind,seq"):
        payload = [
            int(row["seq"]), int(row["layer"]), int(row["kind"]),
            str(row["operation"]), int(row["q0"]),
            None if row["q1"] is None else int(row["q1"]),
            str(row["statement"]),
        ]
        digest.update(json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


class QmapScheduleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.connection = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self._metadata = _metadata(self.connection)
        if self._metadata.get("format") != QMAP_SCHEDULE_FORMAT:
            raise ValueError("unsupported QMAP schedule-store format")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "QmapScheduleStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def metadata(self) -> Mapping[str, Any]:
        return dict(self._metadata)

    @property
    def scheduled_items(self) -> int:
        return int(self._metadata["scheduled_items"])

    def layer(self, layer: int) -> QmapScheduledLayer:
        layer = int(layer)
        if not 0 <= layer < self.scheduled_items:
            raise IndexError(f"QMAP scheduled layer out of range: {layer}")
        rows = self.connection.execute(
            "SELECT seq,layer,kind,operation,q0,q1,statement "
            "FROM qmap_operations WHERE layer=? ORDER BY kind,seq", (layer,))
        single: list[QmapScheduledOperation] = []
        two: list[QmapScheduledOperation] = []
        for row in rows:
            operation = QmapScheduledOperation(
                seq=int(row["seq"]),
                layer=int(row["layer"]),
                kind=int(row["kind"]),
                operation=str(row["operation"]),
                q0=int(row["q0"]),
                q1=None if row["q1"] is None else int(row["q1"]),
                statement=str(row["statement"]),
            )
            (single if operation.kind == 1 else two).append(operation)
        has_two = layer < int(self._metadata["two_qubit_layers"])
        if not has_two and two:
            raise ValueError("final QMAP scheduled item unexpectedly contains CZ gates")
        return QmapScheduledLayer(
            layer=layer,
            single_qubit=tuple(single),
            two_qubit=tuple(two),
            has_two_qubit_layer=has_two,
        )

    def iter_layers(self, start_layer: int = 0) -> Iterator[QmapScheduledLayer]:
        if start_layer < 0:
            raise ValueError("QMAP start layer must be non-negative")
        for layer in range(start_layer, self.scheduled_items):
            yield self.layer(layer)

    def verify(self) -> Mapping[str, Any]:
        metadata = self._metadata
        counts = self.connection.execute(
            "SELECT COUNT(*) AS events,"
            "SUM(CASE WHEN kind=1 THEN 1 ELSE 0 END) AS gates_1q,"
            "SUM(CASE WHEN kind=2 THEN 1 ELSE 0 END) AS gates_2q,"
            "MAX(CASE WHEN kind=2 THEN layer ELSE -1 END) AS max_2q_layer "
            "FROM qmap_operations").fetchone()
        actual = {
            "events": int(counts["events"]),
            "gates_1q": int(counts["gates_1q"] or 0),
            "gates_2q": int(counts["gates_2q"] or 0),
            "two_qubit_layers": int(counts["max_2q_layer"]) + 1,
        }
        for key, value in actual.items():
            if int(metadata[key]) != value:
                raise ValueError(
                    f"QMAP schedule metadata mismatch for {key}: "
                    f"{metadata[key]} != {value}")
        layer_counts = self.connection.execute(
            "SELECT COUNT(*) AS layers,COALESCE(SUM(gate_count),0) AS gates "
            "FROM two_qubit_counts").fetchone()
        if (int(layer_counts["layers"]) != actual["two_qubit_layers"] or
                int(layer_counts["gates"]) != actual["gates_2q"]):
            raise ValueError("QMAP two-qubit layer-count ledger mismatch")
        expected_items = (
            0 if actual["events"] == 0 else actual["two_qubit_layers"] + 1)
        if int(metadata["scheduled_items"]) != expected_items:
            raise ValueError("QMAP scheduled item count mismatch")
        digest = _schedule_digest(self.connection)
        if digest != metadata.get("schedule_sha256"):
            raise ValueError("QMAP schedule digest mismatch")
        return {**actual, "scheduled_items": expected_items,
                "schedule_sha256": digest}


def build_qmap_schedule_store(
    source_store_path: str | Path,
    destination_path: str | Path,
    *,
    max_two_qubit_gates_per_layer: int,
    commit_interval: int = 10_000,
    count_cache_size: int = 4_096,
) -> Mapping[str, Any]:
    """Persist QMAP 3.2 ``ASAPScheduler::scheduleOperation`` assignments.

    The C++ scheduler keeps a gate-count vector proportional to circuit depth.
    This first-pass equivalent stores those counts in SQLite and retains only
    per-qubit next-layer state plus a fixed LRU, so Python RSS is O(qubits).
    """
    if max_two_qubit_gates_per_layer <= 0:
        raise ValueError("QMAP 2Q layer capacity must be positive")
    if commit_interval <= 0 or count_cache_size <= 0:
        raise ValueError("QMAP schedule build limits must be positive")
    source_path = Path(source_store_path).resolve()
    destination = Path(destination_path).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        temporary.unlink()

    connection = sqlite3.connect(temporary)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            "PRAGMA journal_mode=OFF;"
            "PRAGMA synchronous=OFF;"
            "PRAGMA temp_store=FILE;"
            "CREATE TABLE qmap_operations("
            " seq INTEGER PRIMARY KEY,layer INTEGER NOT NULL,"
            " kind INTEGER NOT NULL CHECK(kind IN (1,2)),"
            " operation TEXT NOT NULL,q0 INTEGER NOT NULL,q1 INTEGER,"
            " statement TEXT NOT NULL);"
            "CREATE INDEX qmap_operations_by_layer "
            "ON qmap_operations(layer,kind,seq);"
            "CREATE TABLE two_qubit_counts("
            " layer INTEGER PRIMARY KEY,gate_count INTEGER NOT NULL);"
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        )
        with LayerStore(source_path) as source:
            source_metadata = source.metadata
            n_qubits = int(source_metadata["qubits"])
            next_layer = [0] * n_qubits
            layer_count_cache: OrderedDict[int, int] = OrderedDict()
            counts = {"events": 0, "gates_1q": 0, "gates_2q": 0}
            max_two_layer = -1

            def gate_count(layer: int) -> int:
                cached = layer_count_cache.get(layer)
                if cached is not None:
                    layer_count_cache.move_to_end(layer)
                    return cached
                row = connection.execute(
                    "SELECT gate_count FROM two_qubit_counts WHERE layer=?",
                    (layer,),
                ).fetchone()
                value = 0 if row is None else int(row["gate_count"])
                layer_count_cache[layer] = value
                layer_count_cache.move_to_end(layer)
                while len(layer_count_cache) > count_cache_size:
                    layer_count_cache.popitem(last=False)
                return value

            rows = source.connection.execute(
                "SELECT seq,operation,q0,q1,statement FROM events ORDER BY seq")
            for row in rows:
                seq = int(row["seq"])
                operation = str(row["operation"])
                q0 = int(row["q0"])
                q1 = None if row["q1"] is None else int(row["q1"])
                if q1 is None:
                    layer = next_layer[q0]
                    kind = 1
                    counts["gates_1q"] += 1
                else:
                    layer = max(next_layer[q0], next_layer[q1])
                    while gate_count(layer) >= max_two_qubit_gates_per_layer:
                        layer += 1
                    updated = gate_count(layer) + 1
                    connection.execute(
                        "INSERT INTO two_qubit_counts(layer,gate_count) "
                        "VALUES(?,?) ON CONFLICT(layer) DO UPDATE SET "
                        "gate_count=excluded.gate_count", (layer, updated))
                    layer_count_cache[layer] = updated
                    layer_count_cache.move_to_end(layer)
                    next_layer[q0] = layer + 1
                    next_layer[q1] = layer + 1
                    max_two_layer = max(max_two_layer, layer)
                    kind = 2
                    counts["gates_2q"] += 1
                connection.execute(
                    "INSERT INTO qmap_operations("
                    "seq,layer,kind,operation,q0,q1,statement) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (seq, layer, kind, operation, q0, q1, str(row["statement"])),
                )
                counts["events"] += 1
                if counts["events"] % commit_interval == 0:
                    connection.commit()

            two_layers = max_two_layer + 1
            scheduled_items = 0 if counts["events"] == 0 else two_layers + 1
            metadata = {
                "format": QMAP_SCHEDULE_FORMAT,
                "source_store_path": str(source_path),
                "source_statement_sha256": source_metadata.get(
                    "statement_sha256", ""),
                "qubits": n_qubits,
                **counts,
                "two_qubit_layers": two_layers,
                "scheduled_items": scheduled_items,
                "max_two_qubit_gates_per_layer": (
                    max_two_qubit_gates_per_layer),
            }
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                ((key, json.dumps(value, sort_keys=True))
                 for key, value in metadata.items()),
            )
            connection.commit()
            schedule_sha256 = _schedule_digest(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                ("schedule_sha256", json.dumps(schedule_sha256)),
            )
            connection.commit()
        connection.close()
        os.replace(temporary, destination)
    except BaseException:
        connection.close()
        if temporary.exists():
            temporary.unlink()
        raise

    with QmapScheduleStore(destination) as result:
        return dict(result.verify())


__all__ = [
    "QMAP_SCHEDULE_FORMAT",
    "QmapScheduleStore",
    "QmapScheduledLayer",
    "QmapScheduledOperation",
    "build_qmap_schedule_store",
]
