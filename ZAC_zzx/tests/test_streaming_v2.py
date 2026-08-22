"""Bounded layer-store and event-wise checkpoint/resume tests."""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.checkpoint import (  # noqa: E402
    Checkpoint, EventStreamWriter, load_checkpoint, save_checkpoint)
from streaming.controller import (  # noqa: E402
    CheckpointController, CheckpointPolicy)
from streaming.large_contract import (  # noqa: E402
    LARGE_CIRCUITS, LARGE_RSS_LIMIT_BYTES, LARGE_TIMEOUT_SECONDS,
    OFFICIAL_METADATA, QASMBENCH_COMMIT, STREAMING_COMPILER_INTEGRATED,
    LargeCircuitMetadata, LargeExperimentContract,
    create_large_suite_metadata, load_large_suite_metadata,
    write_large_suite_metadata)
from streaming.qasm_sqlite import LayerStore, build_layer_store  # noqa: E402
from streaming.rss_probe import run_synthetic_rss_probe  # noqa: E402
from streaming.trace_pipeline import (  # noqa: E402
    IncrementalTracePipeline, IncrementalTraceScorer,
    IncrementalTraceValidator)
from evaluation import (  # noqa: E402
    CanonicalTraceEvent, EventType, TraceValidationError, score_trace)


QASM = '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
u3(0,0,0) q[0];
cz q[0],q[1];
u1(0) q[2];
cz q[1],q[2];
'''

QASM_WITH_INTERLEAVED_1Q = '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
cz q[0],q[1];
u1(0) q[0];
u2(0,0) q[0];
u3(0,0,0) q[2];
cz q[0],q[2];
u1(0) q[1];
u2(0,0) q[1];
cz q[1],q[2];
cz q[0],q[1];
'''


class TestLayerStore(unittest.TestCase):
    def test_layers_next_use_and_interactions(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, database = base / "toy.qasm", base / "toy.sqlite"
            source.write_text(QASM, encoding="utf-8")
            metadata = build_layer_store(source, database)
            self.assertEqual(metadata["events"], 4)
            self.assertEqual(metadata["gates_1q"], 2)
            self.assertEqual(metadata["gates_2q"], 2)
            self.assertEqual(metadata["two_qubit_layers"], 2)
            with LayerStore(database) as store:
                events = [event for layer in range(metadata["layers"])
                          for event in store.layer(layer)]
                self.assertEqual(sorted(event.seq for event in events), [0, 1, 2, 3])
                self.assertEqual(store.next_use(0, 0), (1, 1))
                self.assertEqual(store.next_two_qubit_use(1, 1), (3, 1))
                self.assertEqual(list(store.interaction_matrix()), [(0, 1, 1), (1, 2, 1)])
                window = store.window(0, 1)
                self.assertTrue(all(event.layer <= 2 for event in window))

    def test_two_qubit_horizon_ignores_intervening_one_qubit_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, database = base / "interleaved.qasm", base / "interleaved.sqlite"
            source.write_text(QASM_WITH_INTERLEAVED_1Q, encoding="utf-8")
            metadata = build_layer_store(source, database)

            # Five 1Q instructions stretch the ordinary dependency DAG to six
            # layers, whereas the four dependent CZs form exactly four 2Q
            # layers.  Both notions remain available in the same store.
            self.assertEqual(metadata["layers"], 6)
            self.assertEqual(metadata["two_qubit_layers"], 4)
            with LayerStore(database) as store:
                all_event_h0 = store.window(0, 0)
                self.assertEqual([event.seq for event in all_event_h0], [0, 3, 1, 5])
                self.assertNotIn(4, [event.seq for event in all_event_h0])

                # NL (H=0) sees the current and next CZ layers even though
                # three dependency layers separate their CZ events.
                nl_window = store.two_qubit_window(0, 0)
                self.assertEqual([event.seq for event in nl_window], [0, 4])
                self.assertEqual(
                    [event.two_qubit_layer for event in nl_window], [0, 1])

                # LK (H=2) sees current, target, and the two extra forecast
                # layers L+2/L+3, independent of the intervening 1Q gates.
                lk_window = store.two_qubit_window(0, 2)
                self.assertEqual([event.seq for event in lk_window], [0, 4, 7, 8])
                self.assertEqual(
                    [event.two_qubit_layer for event in lk_window], [0, 1, 2, 3])
                self.assertTrue(all(event.operation == "cz" for event in lk_window))

                self.assertEqual(store.next_use(0, 0), (1, 1))
                self.assertEqual(store.next_two_qubit_use(0, 0), (4, 1))
                self.assertEqual(store.next_two_qubit_use(4, 0), (8, 3))
                self.assertEqual(store.next_two_qubit_use_after(0, 1), (8, 3))
                self.assertIsNone(store.next_two_qubit_use_after(2, 2))
                self.assertEqual(
                    [layer for layer, _ in store.iter_two_qubit_layers()],
                    [0, 1, 2, 3],
                )
                self.assertIsNone(store.layer(1)[0].two_qubit_layer)

    def test_empty_two_qubit_index_is_valid(self):
        qasm = '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
u1(0) q[0];
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, database = base / "oneq.qasm", base / "oneq.sqlite"
            source.write_text(qasm, encoding="utf-8")
            metadata = build_layer_store(source, database)
            self.assertEqual(metadata["two_qubit_layers"], 0)
            self.assertEqual(metadata["max_two_qubit_layer"], -1)
            with LayerStore(database) as store:
                self.assertEqual(store.two_qubit_window(0, 2), [])
                self.assertEqual(list(store.iter_two_qubit_layers()), [])

    def test_legacy_v1_store_keeps_old_read_apis(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE events("
                "seq INTEGER PRIMARY KEY, layer INTEGER NOT NULL, "
                "operation TEXT NOT NULL, q0 INTEGER NOT NULL, q1 INTEGER, "
                "statement TEXT NOT NULL);"
                "CREATE TABLE uses("
                "seq INTEGER NOT NULL, qubit INTEGER NOT NULL, "
                "layer INTEGER NOT NULL, next_seq INTEGER, next_layer INTEGER, "
                "PRIMARY KEY(seq,qubit));"
                "CREATE TABLE interactions("
                "q0 INTEGER NOT NULL, q1 INTEGER NOT NULL, weight INTEGER NOT NULL, "
                "PRIMARY KEY(q0,q1));"
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO events VALUES(0,0,'cz',0,1,'cz q[0],q[1];');"
                "INSERT INTO uses VALUES(0,0,0,NULL,NULL);"
                "INSERT INTO uses VALUES(0,1,0,NULL,NULL);"
                "INSERT INTO interactions VALUES(0,1,1);"
                "INSERT INTO metadata VALUES('max_layer','0');"
            )
            connection.commit()
            connection.close()

            with LayerStore(database) as store:
                event = store.layer(0)[0]
                self.assertEqual(event.qubits, (0, 1))
                self.assertIsNone(event.two_qubit_layer)
                self.assertIsNone(store.next_use(0, 0))
                self.assertEqual(list(store.interaction_matrix()), [(0, 1, 1)])
                with self.assertRaisesRegex(ValueError, "rebuild"):
                    store.two_qubit_window(0, 0)

    def test_noncanonical_operation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "bad.qasm"
            source.write_text(QASM.replace("u1(0)", "measure"), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_layer_store(source, base / "bad.sqlite")


class TestCheckpoint(unittest.TestCase):
    def test_resume_matches_uninterrupted_eventwise(self):
        events = [{"layer": index, "kind": "toy"} for index in range(20)]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            full, resumed = base / "full.jsonl.gz", base / "resumed.jsonl.gz"
            with EventStreamWriter(full) as writer:
                for event in events:
                    writer.write(event)
                full_hash = writer.event_hash
            with EventStreamWriter(resumed) as writer:
                for event in events[:7]:
                    writer.write(event)
                count, digest = writer.event_count, writer.event_hash
            with EventStreamWriter(resumed, append=True, event_count=count,
                                   event_hash=digest) as writer:
                for event in events[7:]:
                    writer.write(event)
                resumed_hash = writer.event_hash
            self.assertEqual(list(EventStreamWriter.read(full)), events)
            self.assertEqual(list(EventStreamWriter.read(resumed)), events)
            self.assertEqual(full_hash, resumed_hash)

    def test_checkpoint_restores_rng_and_validates_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            rng = random.Random(17)
            checkpoint = Checkpoint(current_layer=1000, input_sha256="a",
                                    config_sha256="b")
            checkpoint.capture_rng(rng)
            expected = [rng.random() for _ in range(5)]
            save_checkpoint(path, checkpoint)
            loaded = load_checkpoint(path, input_sha256="a", config_sha256="b")
            restored = random.Random()
            loaded.restore_rng(restored)
            self.assertEqual([restored.random() for _ in range(5)], expected)
            with self.assertRaisesRegex(ValueError, "input hash mismatch"):
                load_checkpoint(path, input_sha256="wrong")

    def test_controller_saves_on_layer_or_time_and_flushes_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            now = [0.0]
            actions = []
            controller = CheckpointController(
                path,
                input_sha256="input",
                config_sha256="config",
                policy=CheckpointPolicy(layer_interval=1000, time_interval_seconds=300),
                before_save=lambda: actions.append("flush"),
                clock=lambda: now[0],
            )

            def state(layer):
                return Checkpoint(
                    current_layer=layer,
                    input_sha256="input",
                    config_sha256="config",
                    event_count=layer,
                )

            skipped = controller.maybe_checkpoint(999, lambda: state(999))
            self.assertFalse(skipped.saved)
            layer_save = controller.maybe_checkpoint(1000, lambda: state(1000))
            self.assertEqual(layer_save.reasons, ("layer_interval",))
            self.assertEqual(actions, ["flush"])
            self.assertEqual(load_checkpoint(path).current_layer, 1000)

            now[0] = 301.0
            time_save = controller.maybe_checkpoint(1001, lambda: state(1001))
            self.assertEqual(time_save.reasons, ("time_interval",))
            self.assertEqual(actions, ["flush", "flush"])
            self.assertEqual(load_checkpoint(path).current_layer, 1001)


class TestIncrementalTracePipeline(unittest.TestCase):
    @staticmethod
    def events():
        positions = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))
        return [
            CanonicalTraceEvent(
                EventType.INIT, 0.0, 0.0, atoms=(0, 1, 2),
                end_positions=positions,
                end_regions=("storage", "storage", "storage"),
            ),
            CanonicalTraceEvent(
                EventType.ONE_QUBIT_GATE, 0.0, 52.0, atoms=(0,),
                start_positions=(positions[0],), end_positions=(positions[0],),
                start_regions=("storage",), end_regions=("storage",),
                gate_names=("u3",),
            ),
            CanonicalTraceEvent(
                EventType.LOAD, 52.0, 67.0, atoms=(0,), batch_id="b0",
                start_positions=(positions[0],), end_positions=(positions[0],),
                start_regions=("storage",), end_regions=("aod",),
            ),
            CanonicalTraceEvent(
                EventType.MOVE, 67.0, 69.0, atoms=(0,), batch_id="b0",
                start_positions=(positions[0],), end_positions=((0.5, 0.0),),
                start_regions=("aod",), end_regions=("aod",),
                metadata={"ghost_hits": 0},
            ),
            CanonicalTraceEvent(
                EventType.STORE, 69.0, 84.0, atoms=(0,), batch_id="b0",
                start_positions=((0.5, 0.0),), end_positions=((0.5, 0.0),),
                start_regions=("aod",), end_regions=("storage",),
            ),
            CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE, 84.0, 84.36, atoms=(0, 1),
                gate_pairs=((0, 1),), region_atoms=(0, 1, 2),
                gate_names=("cz",), metadata={"ghost_hits": 0},
            ),
        ]

    @staticmethod
    def staggered_events():
        return [
            CanonicalTraceEvent(
                EventType.INIT, 0.0, 0.0, atoms=(0, 1),
                end_positions=((0.0, 0.0), (0.0, 10.0)),
                end_regions=("storage", "storage"),
            ),
            CanonicalTraceEvent(
                EventType.LOAD, 0.0, 15.0, atoms=(0,), batch_id="staggered",
                start_positions=((0.0, 0.0),), end_positions=((0.0, 0.0),),
                start_regions=("storage",), end_regions=("aod",),
            ),
            CanonicalTraceEvent(
                EventType.MOVE, 15.0, 20.0, atoms=(0,), batch_id="staggered",
                start_positions=((0.0, 0.0),), end_positions=((2.0, 0.0),),
                start_regions=("aod",), end_regions=("aod",),
            ),
            CanonicalTraceEvent(
                EventType.LOAD, 20.0, 35.0, atoms=(1,), batch_id="staggered",
                start_positions=((0.0, 10.0),), end_positions=((0.0, 10.0),),
                start_regions=("storage",), end_regions=("aod",),
            ),
            CanonicalTraceEvent(
                EventType.MOVE, 35.0, 40.0, atoms=(0, 1), batch_id="staggered",
                start_positions=((2.0, 0.0), (0.0, 10.0)),
                end_positions=((4.0, 0.0), (2.0, 10.0)),
                start_regions=("aod", "aod"), end_regions=("aod", "aod"),
            ),
            CanonicalTraceEvent(
                EventType.STORE, 40.0, 55.0, atoms=(0, 1), batch_id="staggered",
                start_positions=((4.0, 0.0), (2.0, 10.0)),
                end_positions=((4.0, 0.0), (2.0, 10.0)),
                start_regions=("aod", "aod"),
                end_regions=("entanglement", "entanglement"),
            ),
        ]

    def test_incremental_score_matches_reference_and_writes_eventwise(self):
        events = self.events()
        reference = score_trace(events, n_qubits=3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.gz"
            with EventStreamWriter(path) as writer:
                pipeline = IncrementalTracePipeline(
                    IncrementalTraceValidator(
                        3, expected_one_qubit_gates=1,
                        expected_two_qubit_gates=1),
                    IncrementalTraceScorer(3),
                    writer,
                )
                for event in events:
                    pipeline.consume(event)
                result = pipeline.finalize()
            self.assertEqual(result["fidelity"], reference.to_dict())
            self.assertTrue(result["validation"]["ok"])
            self.assertEqual(result["validation"]["ghost_hits"], 0)
            self.assertEqual(len(list(EventStreamWriter.read(path))), len(events))

    def test_incremental_scorer_accepts_large_timestamp_roundoff(self):
        scorer = IncrementalTraceScorer(1)
        scorer.consume(CanonicalTraceEvent(
            EventType.INIT, 0.0, 0.0, atoms=(0,),
            end_positions=((0.0, 0.0),), end_regions=("storage",)))
        scorer.consume(CanonicalTraceEvent(
            EventType.ONE_QUBIT_GATE,
            10_000_000.0, 10_000_051.999999998,
            atoms=(0,), gate_names=("u3",)))
        result = scorer.finalize()
        self.assertEqual(result.one_qubit_gates, 1)
        self.assertTrue(result.ood)

    def test_incremental_scorer_rejects_large_timestamp_real_error(self):
        scorer = IncrementalTraceScorer(1)
        scorer.consume(CanonicalTraceEvent(
            EventType.INIT, 0.0, 0.0, atoms=(0,),
            end_positions=((0.0, 0.0),), end_regions=("storage",)))
        with self.assertRaisesRegex(TraceValidationError, "duration must be 52"):
            scorer.consume(CanonicalTraceEvent(
                EventType.ONE_QUBIT_GATE,
                10_000_000.0, 10_000_052.00001,
                atoms=(0,), gate_names=("u3",)))

    def test_incremental_validator_rejects_nonzero_ghost_metadata(self):
        validator = IncrementalTraceValidator(2)
        validator.consume(CanonicalTraceEvent(
            EventType.INIT, 0.0, 0.0, atoms=(0, 1),
            end_positions=((0.0, 0.0), (1.0, 0.0)),
            end_regions=("storage", "storage")))
        with self.assertRaisesRegex(TraceValidationError, "ghost hits"):
            validator.consume(CanonicalTraceEvent(
                EventType.TWO_QUBIT_GATE, 0.0, 0.36, atoms=(0, 1),
                gate_pairs=((0, 1),), region_atoms=(0, 1),
                gate_names=("cz",), metadata={"ghost_hits": 1},
            ))

    def test_incremental_validator_replays_ghost_geometry(self):
        validator = IncrementalTraceValidator(2)
        validator.consume(CanonicalTraceEvent(
            EventType.INIT, 0.0, 0.0, atoms=(0, 1),
            end_positions=((0.0, 0.0), (1.0, 1.0)),
            end_regions=("storage", "storage")))
        validator.consume(CanonicalTraceEvent(
            EventType.LOAD, 0.0, 15.0, atoms=(0,), batch_id="b0",
            start_positions=((0.0, 0.0),), end_positions=((0.0, 0.0),),
            start_regions=("storage",), end_regions=("aod",)))
        with self.assertRaisesRegex(TraceValidationError, "ghost collision"):
            validator.consume(CanonicalTraceEvent(
                EventType.MOVE, 15.0, 16.0, atoms=(0,), batch_id="b0",
                start_positions=((0.0, 0.0),), end_positions=((2.0, 2.0),),
                start_regions=("aod",), end_regions=("aod",),
                metadata={"ghost_hits": 0},
            ))

    def test_incremental_validator_treats_held_stationary_atom_as_ghost(self):
        validator = IncrementalTraceValidator(2)
        events = [
            CanonicalTraceEvent(
                EventType.INIT, 0, 0, atoms=(0, 1),
                end_positions=((0.0, 0.0), (5.0, 0.0))),
            CanonicalTraceEvent(
                EventType.LOAD, 0, 15, atoms=(0,), batch_id="staggered",
                start_positions=((0.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.MOVE, 15, 20, atoms=(0,), batch_id="staggered",
                start_positions=((0.0, 0.0),), end_positions=((1.0, 0.0),)),
            CanonicalTraceEvent(
                EventType.LOAD, 20, 35, atoms=(1,), batch_id="staggered",
                start_positions=((5.0, 0.0),)),
        ]
        for event in events:
            validator.consume(event)
        with self.assertRaisesRegex(TraceValidationError, "ghost collision"):
            validator.consume(CanonicalTraceEvent(
                EventType.MOVE, 35, 40, atoms=(0,), batch_id="staggered",
                start_positions=((1.0, 0.0),), end_positions=((10.0, 0.0),),
            ))

    def test_incremental_pipeline_checkpoint_resume_is_exact(self):
        events = self.events()
        uninterrupted = IncrementalTracePipeline(
            IncrementalTraceValidator(
                3, expected_one_qubit_gates=1,
                expected_two_qubit_gates=1),
            IncrementalTraceScorer(3),
        )
        for event in events:
            uninterrupted.consume(event)
        expected = uninterrupted.finalize()

        interrupted = IncrementalTracePipeline(
            IncrementalTraceValidator(
                3, expected_one_qubit_gates=1,
                expected_two_qubit_gates=1),
            IncrementalTraceScorer(3),
        )
        # Stop while the atom is still held, ensuring the open AOD batch and
        # physical location state are both present in the checkpoint.
        for event in events[:4]:
            interrupted.consume(event)
        serialized = json.loads(json.dumps(interrupted.state_dict(), sort_keys=True))
        resumed = IncrementalTracePipeline.from_state(serialized)
        for event in events[4:]:
            resumed.consume(event)
        self.assertEqual(resumed.finalize(), expected)

    def test_staggered_load_move_matches_reference_and_checkpoint_resume(self):
        events = self.staggered_events()
        reference = score_trace(events, n_qubits=2).to_dict()

        uninterrupted = IncrementalTracePipeline(
            IncrementalTraceValidator(2), IncrementalTraceScorer(2)
        )
        for event in events:
            uninterrupted.consume(event)
        expected = uninterrupted.finalize()
        self.assertEqual(expected["fidelity"], reference)
        self.assertEqual(expected["fidelity"]["move_batches"], 1)
        self.assertEqual(expected["fidelity"]["move_time_us"], 55.0)

        interrupted = IncrementalTracePipeline(
            IncrementalTraceValidator(2), IncrementalTraceScorer(2)
        )
        # Checkpoint after LOAD, MOVE, LOAD: moved=True and both atoms held is
        # precisely the state rejected by the old phase-only state machine.
        for event in events[:4]:
            interrupted.consume(event)
        serialized = json.loads(json.dumps(interrupted.state_dict(), sort_keys=True))
        resumed = IncrementalTracePipeline.from_state(serialized)
        for event in events[4:]:
            resumed.consume(event)
        self.assertEqual(resumed.finalize(), expected)


class TestLargeContractAndRSS(unittest.TestCase):
    def test_large_contract_freezes_commit_suite_and_equal_resource_caps(self):
        contract = LargeExperimentContract()
        contract.validate()
        self.assertEqual(contract.upstream_commit, QASMBENCH_COMMIT)
        self.assertEqual(tuple(contract.circuits), LARGE_CIRCUITS)
        self.assertFalse(STREAMING_COMPILER_INTEGRATED)
        self.assertFalse(contract.streaming_compiler_integrated)
        self.assertFalse(contract.to_dict()["streaming_compiler_integrated"])
        with self.assertRaisesRegex(RuntimeError, "streaming_compiler_integrated=false"):
            contract.require_streaming_execution()
        contract.validate_attempt_limits(
            rss_limit_bytes=LARGE_RSS_LIMIT_BYTES,
            timeout_seconds=LARGE_TIMEOUT_SECONDS,
        )
        with self.assertRaisesRegex(ValueError, "RSS limit mismatch"):
            contract.validate_attempt_limits(
                rss_limit_bytes=LARGE_RSS_LIMIT_BYTES - 1,
                timeout_seconds=LARGE_TIMEOUT_SECONDS,
            )
        with self.assertRaisesRegex(ValueError, "frozen plan"):
            replace(contract, upstream_commit="0" * 40).validate()

    def test_bwt177_metadata_distinguishes_official_counts_from_plan_depth(self):
        metadata = OFFICIAL_METADATA["bwt_n177"]
        self.assertEqual(metadata["qubits"], 177)
        self.assertEqual(metadata["total_gates"], 12_049_201)
        self.assertEqual(metadata["cnot_gates"], 4_641_200)
        self.assertIn(QASMBENCH_COMMIT, metadata["source_url"])
        self.assertEqual(metadata["depth_provenance"], "user_plan_approx")
        self.assertFalse(metadata["depth_is_acceptance_gate"])

    def test_large_suite_metadata_round_trip_and_contract_hash(self):
        record = LargeCircuitMetadata(
            circuit="bwt_n37",
            source_path="source.qasm",
            source_sha256="a" * 64,
            canonical_path="canonical.qasm",
            canonical_sha256="b" * 64,
            qubits=37,
            gates_1q=10,
            gates_2q=20,
            layers=12,
            statement_sha256="c" * 64,
        )
        metadata = create_large_suite_metadata([record], complete=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-suite.json"
            write_large_suite_metadata(path, metadata)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(payload["streaming_compiler_integrated"])
            self.assertEqual(load_large_suite_metadata(path), metadata)

    def test_small_rss_probe_consumes_bounded_window(self):
        # The explicit acceptance command is:
        # python -m streaming.rss_probe --million --workspace <scratch>
        report = run_synthetic_rss_probe(
            layer_count=1500,
            lookahead_horizon=2,
            sample_interval=500,
        )
        self.assertEqual(report["layers_consumed"], 1500)
        self.assertLessEqual(report["max_window_events"], 4)
        self.assertTrue(report["within_22_gib_contract"])
        self.assertLess(report["python_peak_bytes"], 16 * (1 << 20))


if __name__ == "__main__":
    unittest.main()
