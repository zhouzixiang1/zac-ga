from __future__ import annotations

import gzip
import json
from pathlib import Path
import unittest

from evaluation import EventType, normalize_zair, score_trace
from zzx.scheduler_ledger import (
    ExpandedAODPhase,
    PureSchedulerLedger,
    SchedulerLedgerSnapshot,
    evaluate_exact_current_candidate,
)


class TestPureSchedulerLedger(unittest.TestCase):
    def test_independent_aod_and_cz_fill_a_global_one_qubit_tail(self):
        ledger = PureSchedulerLedger(3)
        ledger.schedule_init(0)
        prefix = ledger.schedule_one_qubit(1, (0,), dependencies=(0,))
        prefix_idle = ledger.idle_time_us

        movement = ledger.schedule_aod(
            2,
            (
                ExpandedAODPhase("load", 15.0, (1,)),
                ExpandedAODPhase("move", 10.0, (1,)),
                ExpandedAODPhase("store", 15.0, (1,)),
            ),
            dependencies=(0,),
        )
        pulse = ledger.schedule_cz(
            3, (1, 2), dependencies=(2,), zone_id=0)

        self.assertEqual((prefix.begin_us, prefix.end_us), (0.0, 52.0))
        self.assertEqual((movement.begin_us, movement.end_us), (0.0, 40.0))
        self.assertEqual((pulse.begin_us, pulse.end_us), (40.0, 40.36))
        self.assertEqual(ledger.trace_end_us, 52.0)
        for actual, expected in zip(
                ledger.active_union_us, (52.0, 30.36, 0.36)):
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertAlmostEqual(ledger.idle_time_us[0], 0.0, places=12)
        self.assertAlmostEqual(ledger.idle_time_us[1], 21.64, places=12)
        self.assertAlmostEqual(ledger.idle_time_us[2], 51.64, places=12)

        # Appending independent active work inside the existing 1Q tail reduces
        # an atom's absolute idle time.  This is the counterexample to the old
        # non-negative ``prior + delta`` coherence ledger.
        self.assertEqual(prefix_idle, (0.0, 52.0, 52.0))
        self.assertLess(ledger.idle_time_us[1], prefix_idle[1])

    def test_snapshot_json_round_trip_and_fork_are_deterministic(self):
        ledger = PureSchedulerLedger(3)
        ledger.schedule_init(0)
        ledger.schedule_one_qubit(1, (0,), dependencies=(0,))
        ledger.schedule_aod(
            2,
            (
                ExpandedAODPhase("load", 15.0, (1,)),
                ExpandedAODPhase("move", 7.5, (1,)),
                ExpandedAODPhase("store", 15.0, (1,)),
            ),
            dependencies=(0,),
        )

        encoded = json.loads(json.dumps(ledger.snapshot().to_dict()))
        decoded = SchedulerLedgerSnapshot.from_dict(encoded)
        restored = PureSchedulerLedger.from_snapshot(decoded)
        forked = ledger.fork()

        self.assertEqual(restored.snapshot(), ledger.snapshot())
        self.assertEqual(forked.snapshot(), ledger.snapshot())
        for value in (restored, forked):
            value.schedule_one_qubit(3, (2,), dependencies=(2,))
        self.assertEqual(restored.snapshot(), forked.snapshot())
        self.assertNotEqual(ledger.snapshot(), forked.snapshot())

    def test_site_dependency_mirrors_activation_minus_deactivation_offset(self):
        ledger = PureSchedulerLedger(4, n_aods=2)
        ledger.schedule_init(0)
        ledger.schedule_one_qubit(1, (0, 0), dependencies=(0,))
        prior = ledger.schedule_aod(
            2,
            (
                ExpandedAODPhase("load", 15.0, (1,)),
                ExpandedAODPhase("move", 10.0, (1,)),
                ExpandedAODPhase("store", 15.0, (1,)),
            ),
            dependencies=(1,),
            aod_id=0,
        )
        current = ledger.schedule_aod(
            3,
            (
                ExpandedAODPhase("load", 15.0, (2,)),
                ExpandedAODPhase("move", 10.0, (2,)),
                ExpandedAODPhase("store", 15.0, (2,)),
            ),
            dependencies=(0,),
            site_dependencies=(2,),
            aod_id=1,
        )

        self.assertEqual(prior.begin_us, 104.0)
        self.assertEqual(prior.activation_finish_us, 119.0)
        self.assertEqual(current.deactivation_offset_us, 25.0)
        self.assertEqual(current.begin_us, 94.0)
        self.assertEqual(current.begin_us + current.deactivation_offset_us,
                         prior.activation_finish_us)

    def test_exact_current_uses_absolute_idle_and_can_be_negative(self):
        ledger = PureSchedulerLedger(3, keep_events=False)
        ledger.schedule_init(0)
        ledger.schedule_one_qubit(1, (0,), dependencies=(0,))
        before = ledger.snapshot()
        candidate = ({
            "type": "rearrangeJob",
            "id": 2,
            "aod_id": -1,
            "aod_qubits": [[1]],
            "dependency": {"qubit": [0], "site": []},
            "insts": [
                {"type": "activate", "duration_us": 15.0,
                 "row_id": [0]},
                {"type": "move:big", "duration_us": 10.0},
                {"type": "deactivate", "duration_us": 15.0},
            ],
        },)
        result = evaluate_exact_current_candidate(before, candidate)
        self.assertLess(
            result.coherence_negative_log_fidelity, 0.0,
            "filling the existing 1Q tail must reduce absolute idle NLL")
        self.assertLess(result.idle_after_us[1], result.idle_before_us[1])

    def test_pruned_snapshot_restores_hash_and_continues_byte_exact(self):
        ledger = PureSchedulerLedger(3, keep_events=False)
        ledger.schedule_init(0)
        ledger.schedule_one_qubit(1, (0,), dependencies=(0,))
        ledger.prune_instructions((1,))
        restored = PureSchedulerLedger.from_snapshot(
            json.loads(json.dumps(ledger.snapshot().to_dict())),
            keep_events=False)
        for value in (ledger, restored):
            value.schedule_cz(2, (1, 2), dependencies=(1,))
        self.assertEqual(restored.snapshot(), ledger.snapshot())


class TestSchedulerLedgerRealTraceParity(unittest.TestCase):
    """Optional artifact-backed differential tests for the prototype.

    The checked-in unit suite stays hermetic.  When the native-ga artifact root
    is present, three existing successful traces exercise serial, parallel and
    multi-row expanded AOD schedules without copying large traces into Git.
    """

    _ARTIFACT_ROOT = (
        Path(__file__).resolve().parents[3]
        / "artifacts" / "native-ga-v1" / "runs" / "main" / "zac18")
    _CIRCUITS = ("seca_n11", "ghz_n23", "wstate_n27")

    @classmethod
    def _trace_path(cls, circuit: str) -> Path | None:
        matches = sorted(cls._ARTIFACT_ROOT.glob(
            f"zac18-{circuit}*/trace.zair.json.gz"))
        return matches[0] if matches else None

    def _assert_real_trace(self, path: Path) -> None:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            code = json.load(handle)
        events = tuple(normalize_zair(code))
        init = next(event for event in events
                    if event.event_type is EventType.INIT)
        n_atoms = len(init.atoms)
        n_aods = max(
            (int(instruction.get("aod_id", -1)) + 1
             for instruction in code["instructions"]
             if instruction.get("type") == "rearrangeJob"),
            default=1)
        n_aods = max(1, n_aods)
        n_zones = max(
            (int(instruction.get("zone_id", -1)) + 1
             for instruction in code["instructions"]
             if instruction.get("type") == "rydberg"),
            default=1)
        n_zones = max(1, n_zones)

        phase_atoms: dict[tuple[int, int], tuple[int, ...]] = {}
        canonical_by_key = {}
        for event in events:
            if event.event_type is EventType.INIT:
                continue
            native_id = int(event.metadata["native_id"])
            if event.event_type is EventType.ONE_QUBIT_GATE:
                phase = int(event.metadata["native_gate_index"])
            elif event.event_type in {EventType.LOAD, EventType.MOVE, EventType.STORE}:
                phase = int(event.metadata["native_phase"])
                phase_atoms[(event.source_index, phase)] = event.atoms
            else:
                phase = -1
            key = (native_id, event.event_type.value, phase)
            self.assertNotIn(key, canonical_by_key)
            canonical_by_key[key] = event

        ledger = PureSchedulerLedger(
            n_atoms, n_aods=n_aods, n_rydberg_zones=n_zones)
        for source_index, instruction in enumerate(code["instructions"]):
            atoms = {
                phase: values
                for (source, phase), values in phase_atoms.items()
                if source == source_index
            }
            timing = ledger.schedule_zair_instruction(
                instruction, phase_atoms=atoms)
            self.assertAlmostEqual(
                timing.begin_us, float(instruction["begin_time"]), places=7)
            self.assertAlmostEqual(
                timing.end_us, float(instruction["end_time"]), places=7)

        ledger_by_key = {
            (event.instruction_id, event.event_type, event.phase_index): event
            for event in ledger.events}
        self.assertEqual(set(ledger_by_key), set(canonical_by_key))
        for key, expected in canonical_by_key.items():
            actual = ledger_by_key[key]
            self.assertEqual(actual.atoms, expected.atoms)
            self.assertAlmostEqual(actual.start_us, expected.start_us, places=7)
            self.assertAlmostEqual(actual.end_us, expected.end_us, places=7)

        scored = score_trace(events, n_qubits=n_atoms)
        self.assertAlmostEqual(ledger.trace_end_us, scored.duration_us, places=7)
        for atom in range(n_atoms):
            self.assertAlmostEqual(
                ledger.idle_time_us[atom], scored.idle_time_us[atom], places=7)
            self.assertAlmostEqual(
                ledger.active_union_us[atom],
                scored.duration_us - scored.idle_time_us[atom], places=7)

    def test_three_existing_native_traces_match_event_by_event(self):
        paths = tuple(self._trace_path(circuit) for circuit in self._CIRCUITS)
        if any(path is None for path in paths):
            self.skipTest("native-ga real-trace artifacts are not available")
        for path in paths:
            with self.subTest(trace=path.parent.name):
                self._assert_real_trace(path)


if __name__ == "__main__":
    unittest.main()
