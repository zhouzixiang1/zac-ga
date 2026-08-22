"""Strict, bounded-memory logical ledger tests for the Large layer store."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.qasm_sqlite import (  # noqa: E402
    LOGICAL_LEDGER_FORMAT,
    ZAC_VIEW_FORMAT,
    LayerStore,
    LogicalLedgerHasher,
    build_layer_store,
)
from streaming.trace_pipeline import IncrementalTraceValidator  # noqa: E402
from evaluation import CanonicalTraceEvent, EventType, TraceValidationError  # noqa: E402
from zac.zac import ZAC  # noqa: E402


QASM = '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[4];
u1(0) q[0];
u2(0,0) q[0];
u3(0,0,0) q[2];
cz q[0],q[1];
cz q[1],q[2];
u1(0) q[0];
'''


def _zac_scheduler_qasm() -> str:
    lines = [
        "OPENQASM 2.0;",
        'include "qelib1.inc";',
        "qreg q[20];",
        "u1(0) q[0];",
        "u2(0,0) q[19];",
    ]
    for gate_index in range(10):
        q0, q1 = 2 * gate_index, 2 * gate_index + 1
        if gate_index == 3:
            q0, q1 = q1, q0  # Stock ZAC canonicalises each CZ pair.
        lines.append(f"cz q[{q0}],q[{q1}];")
        if gate_index == 0:
            lines.append("u1(0) q[0];")
        if gate_index == 5:
            lines.append("u2(0,0) q[10];")
    # Still parented by gate 0 despite appearing after all ten layer-0 CZs.
    lines.append("u3(0,0,0) q[1];")
    lines.extend((
        "cz q[0],q[2];",       # index 10, ASAP layer 1
        "u1(0) q[0];",         # trailing child of index 10
        "cz q[4],q[6];",       # index 11, ASAP layer 1
        "u2(0,0) q[6];",       # trailing child of index 11
        "u3(0,0,0) q[8];",     # late source event, still child of index 4
    ))
    return "\n".join(lines) + "\n"


def _stock_zac_schedule(source: Path, max_gates: int) -> ZAC:
    """Run the frozen Qiskit parser and stock Scheduler_mixin on one toy."""
    stock = ZAC()
    stock.resyn = False
    stock.architecture = SimpleNamespace(
        entanglement_zone=[("rydberg",)],
        dict_SLM={
            "rydberg": SimpleNamespace(n_r=1, n_c=max_gates),
        },
    )
    with redirect_stdout(io.StringIO()):
        stock.set_program(str(source))
        stock.scheduling()
    return stock


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=False)


def _reference_hash(sequences: list[list[str]]) -> tuple[str, list[str]]:
    """Independent implementation of the checkpointable v2 chain framing."""
    atom_hashes: list[str] = []
    atom_rows: list[tuple[int, int, int, int, bytes]] = []
    for qubit, markers in enumerate(sequences):
        raw_digest = hashlib.sha256(
            b"zac-logical-atom-sequence-v1\0" + _u64(qubit)).digest()
        for marker in markers:
            encoded = marker.encode("ascii")
            frame = len(encoded).to_bytes(4, "big", signed=False) + encoded
            raw_digest = hashlib.sha256(raw_digest + frame).digest()
        atom_hashes.append(raw_digest.hex())
        atom_rows.append((
            qubit,
            len(markers),
            sum(marker == "1q" for marker in markers),
            sum(marker.startswith("cz:") for marker in markers),
            raw_digest,
        ))

    total = hashlib.sha256()
    total.update(b"zac-logical-ledger-v1\0")
    total.update(_u64(len(sequences)))
    for qubit, operations, gates_1q, gates_2q, raw_digest in atom_rows:
        total.update(_u64(qubit))
        total.update(_u64(operations))
        total.update(_u64(gates_1q))
        total.update(_u64(gates_2q))
        total.update(raw_digest)
    return total.hexdigest(), atom_hashes


class TestLargeLogicalLedger(unittest.TestCase):
    def test_build_freezes_per_atom_sequences_and_total_hash(self):
        sequences = [
            ["1q", "1q", "cz:1", "1q"],
            ["cz:0", "cz:2"],
            ["1q", "cz:1"],
            [],
        ]
        expected_total, expected_atoms = _reference_hash(sequences)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, database = base / "toy.qasm", base / "toy.sqlite"
            source.write_text(QASM, encoding="utf-8")
            metadata = build_layer_store(source, database, commit_interval=2)

            self.assertEqual(metadata["logical_ledger_format"],
                             LOGICAL_LEDGER_FORMAT)
            self.assertEqual(metadata["logical_ledger_operations"], 8)
            self.assertEqual(metadata["logical_ledger_sha256"], expected_total)
            self.assertEqual(
                metadata["per_atom_logical_operation_sequence_sha256"],
                expected_total,
            )

            with LayerStore(database) as store:
                self.assertTrue(store.has_logical_ledger)
                self.assertEqual(store.logical_ledger_sha256, expected_total)
                records = list(store.iter_logical_atom_ledgers())
                self.assertEqual(
                    [(row.operation_count, row.gates_1q, row.gates_2q)
                     for row in records],
                    [(4, 3, 1), (2, 0, 2), (2, 1, 1), (0, 0, 0)],
                )
                self.assertEqual(
                    [row.sequence_sha256 for row in records], expected_atoms)
                self.assertEqual(store.verify_logical_ledger(), expected_total)

    def test_hasher_is_per_atom_ordered_not_globally_interleaved(self):
        first = LogicalLedgerHasher(3)
        first.record_one_qubit(0)
        first.record_one_qubit(2)
        first.record_cz(0, 1)

        reordered = LogicalLedgerHasher(3)
        reordered.record_one_qubit(2)
        reordered.record_one_qubit(0)
        reordered.record_cz(0, 1)
        self.assertEqual(first.hexdigest(), reordered.hexdigest())

        wrong_partner = LogicalLedgerHasher(3)
        wrong_partner.record_one_qubit(2)
        wrong_partner.record_one_qubit(0)
        wrong_partner.record_cz(0, 2)
        self.assertNotEqual(first.hexdigest(), wrong_partner.hexdigest())

        restored = LogicalLedgerHasher.from_state(first.state_dict())
        self.assertEqual(restored.hexdigest(), first.hexdigest())
        restored.record_one_qubit(1)
        first.record_one_qubit(1)
        self.assertEqual(restored.hexdigest(), first.hexdigest())

    def test_tampered_frozen_atom_digest_fails_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, database = base / "toy.qasm", base / "toy.sqlite"
            source.write_text(QASM, encoding="utf-8")
            build_layer_store(source, database)
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE logical_atom_ledger SET sequence_sha256=? WHERE qubit=0",
                ("0" * 64,),
            )
            connection.commit()
            connection.close()
            with LayerStore(database) as store:
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    store.verify_logical_ledger()

    def test_incremental_validator_rejects_same_counts_wrong_per_atom_sequence(self):
        expected = LogicalLedgerHasher(3)
        expected.record_cz(0, 1)
        validator = IncrementalTraceValidator(
            3,
            expected_one_qubit_gates=0,
            expected_two_qubit_gates=1,
            expected_logical_ledger_sha256=expected.hexdigest(),
        )
        validator.consume(CanonicalTraceEvent(
            EventType.INIT, 0, 0, atoms=(0, 1, 2),
            end_positions=((0, 0), (1, 0), (2, 0)),
            end_regions=("storage", "storage", "storage"),
        ))
        # Gate counts are unchanged, but q[2] has replaced q[1].  A count-only
        # Large verifier would accept this corrupted logical program.
        validator.consume(CanonicalTraceEvent(
            EventType.TWO_QUBIT_GATE, 0, 0.36, atoms=(0, 2),
            gate_pairs=((0, 2),), region_atoms=(0, 2), gate_names=("cz",),
        ))
        with self.assertRaisesRegex(
                TraceValidationError, "per-atom logical gate ledger"):
            validator.finalize()


class TestDependencyLayerStreaming(unittest.TestCase):
    def test_events_and_metadata_stream_once_by_dependency_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, database = base / "toy.qasm", base / "toy.sqlite"
            source.write_text(QASM, encoding="utf-8")
            build_layer_store(source, database)
            with LayerStore(database) as store:
                # The q[2] 1Q gate is later in source order but independent,
                # hence it joins dependency layer 0 before seq 1's layer 1.
                events = list(store.iter_dependency_events())
                self.assertEqual([event.seq for event in events],
                                 [0, 2, 1, 3, 4, 5])
                batches = list(store.iter_dependency_layers())
                self.assertEqual(
                    [batch.metadata.layer for batch in batches], [0, 1, 2, 3])
                self.assertEqual(
                    [[event.seq for event in batch.events] for batch in batches],
                    [[0, 2], [1], [3], [4, 5]],
                )
                self.assertEqual(
                    [(batch.metadata.event_count,
                      batch.metadata.gates_1q,
                      batch.metadata.gates_2q,
                      batch.metadata.first_seq,
                      batch.metadata.last_seq)
                     for batch in batches],
                    [(2, 2, 0, 0, 2), (1, 1, 0, 1, 1),
                     (1, 0, 1, 3, 3), (2, 1, 1, 4, 5)],
                )
                ranged = list(store.iter_dependency_layers(1, 2))
                self.assertEqual(
                    [[event.seq for event in batch.events] for batch in ranged],
                    [[1], [3]],
                )

    def test_new_streaming_api_falls_back_on_legacy_store(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE events("
                "seq INTEGER PRIMARY KEY,layer INTEGER NOT NULL,"
                "operation TEXT NOT NULL,q0 INTEGER NOT NULL,q1 INTEGER,"
                "statement TEXT NOT NULL);"
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "INSERT INTO events VALUES(0,0,'u1',0,NULL,'u1(0) q[0];');"
                "INSERT INTO events VALUES(1,1,'cz',0,1,'cz q[0],q[1];');"
                "INSERT INTO metadata VALUES('max_layer','1');"
            )
            connection.commit()
            connection.close()

            with LayerStore(database) as store:
                self.assertFalse(store.has_logical_ledger)
                self.assertIsNone(store.logical_ledger_sha256)
                batches = list(store.iter_dependency_layers())
                self.assertEqual(
                    [(batch.metadata.layer, batch.metadata.event_count)
                     for batch in batches], [(0, 1), (1, 1)])
                self.assertEqual(
                    [event.seq for batch in batches for event in batch.events],
                    [0, 1],
                )
                with self.assertRaisesRegex(ValueError, "rebuild"):
                    store.verify_logical_ledger()


class TestFrozenZacSchedulerView(unittest.TestCase):
    def _build(self, base: Path) -> tuple[Path, dict[str, object]]:
        source, database = base / "zac-view.qasm", base / "zac-view.sqlite"
        source.write_text(_zac_scheduler_qasm(), encoding="utf-8")
        return database, dict(build_layer_store(source, database))

    def test_parent_indices_asap_layers_and_metadata_are_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            database, metadata = self._build(Path(directory))
            self.assertEqual(metadata["zac_view_format"], ZAC_VIEW_FORMAT)
            self.assertEqual(metadata["zac_two_qubit_index_count"], 12)
            self.assertEqual(metadata["zac_leading_one_qubit_count"], 2)
            self.assertEqual(metadata["zac_parent_one_qubit_count"], 6)
            with LayerStore(database) as store:
                stock = _stock_zac_schedule(
                    database.with_name("zac-view.qasm"), 11)
                self.assertTrue(store.has_zac_view)
                self.assertEqual(store.verify_zac_view(), {
                    "gates_1q": 8,
                    "gates_2q": 12,
                    "leading_1q": 2,
                    "parent_1q": 6,
                })
                leading = list(store.iter_zac_leading_one_qubit())
                self.assertEqual(
                    [(event.operation, event.qubits[0],
                      event.parent_two_qubit_index)
                     for event in leading],
                    [("u1", 0, -1), ("u2", 19, -1)],
                )
                self.assertEqual(
                    [(event.operation, event.qubits[0]) for event in leading],
                    stock.dict_g_1q_parent[-1],
                )
                layers = list(store.iter_zac_asap_layers())
                self.assertEqual(
                    [[gate.two_qubit_index for gate in layer.gates]
                     for layer in layers],
                    [list(range(10)), [10, 11]],
                )
                self.assertEqual(
                    [gate.gate_pair for layer in layers for gate in layer.gates],
                    [tuple((2 * index, 2 * index + 1))
                     for index in range(10)] + [(0, 2), (4, 6)],
                )

                # A continuous gate range is parent-major, not source-major.
                children = list(store.iter_zac_one_qubit_for_gate_range(0, 5))
                self.assertEqual(
                    [(event.parent_two_qubit_index, event.operation,
                      event.qubits[0]) for event in children],
                    [(0, "u1", 0), (0, "u3", 1),
                     (4, "u3", 8), (5, "u2", 10)],
                )

                # The ordinary event view exposes the same frozen indices.
                source_events = sorted(
                    store.iter_dependency_events(), key=lambda event: event.seq)
                self.assertEqual(
                    [event.two_qubit_index for event in source_events
                     if len(event.qubits) == 2],
                    list(range(12)),
                )

    def test_balanced_stages_and_children_match_stock_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            database, _ = self._build(Path(directory))
            with LayerStore(database) as store:
                expected_stage_sizes = {
                    6: [5, 5, 2],   # balanced 10 -> 5+5, not 6+4
                    10: [10, 2],    # equality takes stock ZAC's split branch
                    11: [10, 2],    # strictly larger retains the layer
                }
                for capacity in (6, 10, 11):
                    stock = _stock_zac_schedule(
                        database.with_name("zac-view.qasm"), capacity)
                    stages = list(store.iter_zac_stages(capacity))
                    actual_indices = [
                        [gate.two_qubit_index for gate in stage.gates]
                        for stage in stages
                    ]
                    self.assertEqual(actual_indices, stock.gate_scheduling_idx)
                    self.assertEqual(
                        [len(stage.gates) for stage in stages],
                        expected_stage_sizes[capacity],
                    )
                    self.assertEqual(
                        [[gate.gate_pair for gate in stage.gates]
                         for stage in stages],
                        [[tuple(gate) for gate in layer]
                         for layer in stock.gate_scheduling],
                    )
                    actual_children = [
                        [(event.operation, event.qubits[0])
                         for event in store.iter_zac_one_qubit_for_stage(stage)]
                        for stage in stages
                    ]
                    self.assertEqual(actual_children,
                                     stock.gate_1q_scheduling)
                    self.assertEqual(
                        [stage.stage_index for stage in stages],
                        list(range(len(stages))),
                    )
                with self.assertRaisesRegex(ValueError, "positive"):
                    list(store.iter_zac_stages(0))

    def test_gapped_asap_stage_streams_only_its_parent_children(self):
        qasm = '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[5];
cz q[0],q[1];
cz q[0],q[2];
u1(0) q[0];
cz q[3],q[4];
u2(0,0) q[3];
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, database = base / "gapped.qasm", base / "gapped.sqlite"
            source.write_text(qasm, encoding="utf-8")
            build_layer_store(source, database)
            stock = _stock_zac_schedule(source, 5)
            with LayerStore(database) as store:
                stages = list(store.iter_zac_stages(5))
                self.assertEqual(
                    [[gate.two_qubit_index for gate in stage.gates]
                     for stage in stages],
                    stock.gate_scheduling_idx,
                )
                self.assertEqual(stock.gate_scheduling_idx, [[0, 2], [1]])
                self.assertEqual(
                    [[event.parent_two_qubit_index
                      for event in store.iter_zac_one_qubit_for_stage(stage)]
                     for stage in stages],
                    [[2], [1]],
                )
                self.assertEqual(
                    [[(event.operation, event.qubits[0])
                      for event in store.iter_zac_one_qubit_for_stage(stage)]
                     for stage in stages],
                    stock.gate_1q_scheduling,
                )

    def test_tampered_parent_fails_strict_view_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            database, _ = self._build(Path(directory))
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE events SET parent_two_qubit_index=1 "
                "WHERE seq=(SELECT MIN(seq) FROM events "
                "WHERE q1 IS NULL AND parent_two_qubit_index=0)")
            connection.commit()
            connection.close()
            with LayerStore(database) as store:
                with self.assertRaisesRegex(ValueError, "most recent CZ"):
                    store.verify_zac_view()

    def test_legacy_store_fails_closed_for_zac_view_only(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE events("
                "seq INTEGER PRIMARY KEY,layer INTEGER NOT NULL,"
                "operation TEXT NOT NULL,q0 INTEGER NOT NULL,q1 INTEGER,"
                "statement TEXT NOT NULL);"
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "INSERT INTO events VALUES(0,0,'cz',0,1,'cz q[0],q[1];');"
                "INSERT INTO metadata VALUES('max_layer','0');"
            )
            connection.commit()
            connection.close()
            with LayerStore(database) as store:
                self.assertFalse(store.has_zac_view)
                self.assertEqual([event.seq for event in store.layer(0)], [0])
                with self.assertRaisesRegex(ValueError, "rebuild"):
                    list(store.iter_zac_stages(2))


if __name__ == "__main__":
    unittest.main()
