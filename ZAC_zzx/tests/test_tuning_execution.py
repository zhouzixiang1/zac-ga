from __future__ import annotations

import copy
import json
import hashlib
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.tuning import (  # noqa: E402
    DEFAULT_CANDIDATE,
    TuningTrial,
    build_trial_schedule,
    candidate_id,
    generate_candidates,
    promoted_candidates,
    stage_leaderboard,
    TUNING_PROTOCOL_ID,
    TUNING_QUALITY_POLICY_ID,
    validate_trial_schedule,
)
from experiments_v2.tuning_runner import (  # noqa: E402
    _acquire_claim,
    _archive_immutable_file,
    _claim_path,
    _phase_seal_path,
    _phase_seal_payload,
    _phase_lock,
    _receipt_path,
    _replayed_promotion_report,
    _release_claim,
    _seal_completed_phase,
    _select_worker_trials,
    _self_hash,
    _validate_receipt_snapshot,
    _validate_schedule_chain,
    _validate_tuning_workspace,
    _write_immutable_json,
    build_candidate_config_pair,
    finalize_tuning,
    promote_tuning_phase,
    recover_tuning_claim,
    run_tuning_phase,
    seal_trial_result,
    tuning_status,
    validate_tuning_selection_for_plan,
    validate_trial_receipt,
)
from experiments_v2.contracts import (RunManifest, sha256_file,
                                      stable_sha256)  # noqa: E402
from experiments_v2.protocol import (  # noqa: E402
    ghost_policy_for_method,
    physicalization_policy_for_method,
    trace_protocol_for_method,
)
from zzx.algorithm_v2 import (  # noqa: E402
    FORMAL_NATIVE_TUNING_PROTOCOL_ID,
    SCHEMA2_PAIR_EXEMPT_KEYS,
    validate_schema2_pair,
)


class TuningScheduleTests(unittest.TestCase):
    def setUp(self):
        self.candidates = generate_candidates()
        self.ids = [row["candidate_id"] for row in self.candidates]
        self.default = candidate_id(DEFAULT_CANDIDATE)

    def test_three_preregistered_schedule_sizes_and_unique_ids(self):
        screen = build_trial_schedule(
            "screen", candidate_ids=self.ids,
            circuits=[f"screen-{i}" for i in range(9)], seeds=[0])
        rows = validate_trial_schedule(screen)
        self.assertEqual(len(rows), 324)
        self.assertEqual(len({row.trial_id for row in rows}), 324)

        halving = build_trial_schedule(
            "successive_halving", candidate_ids=self.ids[:6],
            circuits=[f"train-{i}" for i in range(18)], seeds=[0, 1],
            parent_sha256="a" * 64)
        self.assertEqual(len(validate_trial_schedule(halving)), 432)

        validation = build_trial_schedule(
            "validation", candidate_ids=[self.default, *self.ids[1:3]],
            circuits=[f"validation-{i}" for i in range(9)],
            seeds=[0, 1, 2, 3, 4], parent_sha256="b" * 64)
        self.assertEqual(len(validate_trial_schedule(validation)), 270)

    def test_schedule_hash_and_content_are_fail_closed(self):
        schedule = build_trial_schedule(
            "screen", candidate_ids=self.ids,
            circuits=[f"c{i}" for i in range(9)], seeds=[0])
        tampered = json.loads(json.dumps(schedule))
        tampered["trials"][0]["circuit"] = "wrong"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_trial_schedule(tampered)

    def test_screen_keeps_default_and_halving_adds_two_nondefaults(self):
        circuits = [f"c{i}" for i in range(9)]
        trials = []
        for rank, cid in enumerate(self.ids):
            for method in ("M3", "M4"):
                for circuit in circuits:
                    trials.append(TuningTrial(
                        cid, circuit, method, 0, "success", True, 0, False,
                        -10.0 + rank / 1000.0, 1000 - rank, 10.0, 2,
                        False, -10.0 + rank / 1000.0))
        leaderboard = stage_leaderboard(
            trials, candidate_ids=self.ids, default_id=self.default,
            expected_circuits=circuits, expected_seeds=[0])
        promoted = promoted_candidates(
            "screen", leaderboard, default_id=self.default)
        self.assertEqual(len(promoted), 6)
        self.assertEqual(promoted[0], self.default)

        # The next promotion always contains three unique configs, preserving
        # the explicitly preregistered 270 validation attempts.
        promoted_validation = promoted_candidates(
            "successive_halving", leaderboard, default_id=self.default)
        self.assertEqual(len(promoted_validation), 3)
        self.assertEqual(promoted_validation[0], self.default)
        self.assertEqual(len(set(promoted_validation)), 3)

    def test_stage_leaderboard_uses_exponential_for_complete_ood_cohort(self):
        candidate = "candidate"
        trials = []
        for cid in (self.default, candidate):
            for method in ("M3", "M4"):
                ood = cid == candidate and method == "M3"
                linear = None if ood else (
                    -1.0 if cid == self.default else -0.9)
                exponential = (
                    -2.1 if cid == candidate and method == "M3" else -2.0)
                trials.append(TuningTrial(
                    candidate_id=cid, circuit="toy", method=method, seed=0,
                    status="success", verifier_ok=True, ghost_hits=0,
                    fallback=False, log_fidelity=linear,
                    transition_decision_ns=100, move_time_us=10.0,
                    move_batches=1, fidelity_ood=ood,
                    exponential_sensitivity_log_fidelity=exponential))

        rows = stage_leaderboard(
            trials, candidate_ids=[self.default, candidate],
            default_id=self.default, expected_circuits=["toy"],
            expected_seeds=[0])

        by_id = {row["candidate_id"]: row for row in rows}
        self.assertAlmostEqual(
            by_id[candidate]["methods"]["M3"][
                "median_delta_log_fidelity"], -0.1)
        self.assertEqual(by_id[candidate]["quality_policy"],
                         TUNING_QUALITY_POLICY_ID)

    @mock.patch("experiments_v2.tuning_runner._read_json")
    @mock.patch("experiments_v2.tuning_runner._replayed_promotion_report")
    @mock.patch("experiments_v2.tuning_runner.load_schedule")
    def test_validation_chain_recursively_replays_both_promotions(
            self, load_schedule_mock, replay, read_json):
        schedules = {
            "screen": {
                "schedule_sha256": "1" * 64,
                "candidate_ids": ["all"], "circuits": ["s"],
                "seeds": [0], "parent_sha256": "split"},
            "successive_halving": {
                "schedule_sha256": "2" * 64,
                "candidate_ids": ["top6"], "circuits": ["t"],
                "seeds": [0, 1], "parent_sha256": "a" * 64},
            "validation": {
                "schedule_sha256": "3" * 64,
                "candidate_ids": ["top3"], "circuits": ["v"],
                "seeds": [0, 1, 2, 3, 4], "parent_sha256": "b" * 64},
        }
        load_schedule_mock.side_effect = (
            lambda _root, phase: (schedules[phase], []))
        reports = {
            "screen": {
                "schedule_sha256": "1" * 64,
                "promoted_candidate_ids": ["top6"],
                "report_sha256": "a" * 64},
            "successive_halving": {
                "schedule_sha256": "2" * 64,
                "promoted_candidate_ids": ["top3"],
                "report_sha256": "b" * 64},
        }
        replay.side_effect = lambda _root, phase: reports[phase]
        read_json.return_value = {
            "screen": ["s"], "train": ["t"], "validation": ["v"]}
        workspace = {"screen_schedule_sha256": "1" * 64}
        result, _ = _validate_schedule_chain(
            Path("/frozen/tuning"), "validation", workspace)
        self.assertEqual(result["schedule_sha256"], "3" * 64)
        self.assertEqual(
            [call.args[1] for call in replay.call_args_list],
            ["screen", "successive_halving"])

        schedules["validation"]["candidate_ids"] = ["forged"]
        with self.assertRaisesRegex(ValueError, "promotion chain drift"):
            _validate_schedule_chain(
                Path("/frozen/tuning"), "validation", workspace)

    @mock.patch("experiments_v2.tuning_runner.promoted_candidates")
    @mock.patch("experiments_v2.tuning_runner.stage_leaderboard")
    @mock.patch("experiments_v2.tuning_runner.load_phase_trials")
    @mock.patch("experiments_v2.tuning_runner.load_schedule")
    @mock.patch("experiments_v2.tuning_runner._load_published_phase_snapshot")
    def test_promotion_report_is_recomputed_not_only_self_hashed(
            self, published_snapshot, load_schedule, load_trials,
            leaderboard, promoted):
        schedule = {
            "schedule_sha256": "1" * 64,
            "candidate_ids": ["c0"], "circuits": ["toy"], "seeds": [0]}
        load_trials.return_value = (schedule, [])
        load_schedule.return_value = (schedule, [])
        published_snapshot.return_value = {"record_sha256": "2" * 64}
        leaderboard.return_value = [{"candidate_id": "c0", "valid": True}]
        promoted.return_value = ["c0"]
        expected = {
            "experiment_schema": 2,
            "protocol_id": TUNING_PROTOCOL_ID,
            "quality_policy": TUNING_QUALITY_POLICY_ID,
            "transition_timing_scope": "concurrent_observational_nonclaim",
            "promotion_tiebreak": "quality_move_metrics_candidate_id",
            "phase": "screen",
            "schedule_sha256": "1" * 64,
            "promoted_candidate_ids": ["c0"],
            "leaderboard": leaderboard.return_value,
        }
        expected["report_sha256"] = _self_hash(
            expected, "report_sha256")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "candidates.json").write_text(json.dumps({
                "default_candidate_id": "c0"}), encoding="utf-8")
            report = root / "leaderboard_screen.json"
            report.write_text(json.dumps(expected), encoding="utf-8")
            self.assertEqual(
                _replayed_promotion_report(root, "screen"), expected)

            forged = copy.deepcopy(expected)
            forged["leaderboard"] = [{"candidate_id": "forged"}]
            forged["report_sha256"] = _self_hash(
                forged, "report_sha256")
            report.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ledger replay"):
                _replayed_promotion_report(root, "screen")


class TuningConfigTests(unittest.TestCase):
    def test_shared_pair_only_differs_by_identity_dir_and_horizon(self):
        config_root = ROOT / "exp_setting" / "native_ga_v1"
        m3 = json.loads((config_root / "ours_nl_shared.json").read_text())
        m4 = json.loads((config_root / "ours_lk_shared.json").read_text())
        candidate = {**DEFAULT_CANDIDATE,
                     "candidate_id": candidate_id(DEFAULT_CANDIDATE)}
        pair = build_candidate_config_pair(m3, m4, candidate, seed=3)
        for method in ("M3", "M4"):
            self.assertEqual(pair[method]["tuning"]["protocol_id"],
                             TUNING_PROTOCOL_ID)
        left = pair["M3"]["zac_setting"][0]
        right = pair["M4"]["zac_setting"][0]
        validate_schema2_pair(left, right)
        differences = {key for key in set(left) | set(right)
                       if left.get(key) != right.get(key)}
        self.assertEqual(
            differences, SCHEMA2_PAIR_EXEMPT_KEYS | {"lookahead_horizon"})
        left_spec = dict(left["lookahead_horizon"])
        right_spec = dict(right["lookahead_horizon"])
        self.assertEqual(left_spec.pop("max_horizon"), 0)
        self.assertEqual(right_spec.pop("max_horizon"), 8)
        self.assertEqual(left_spec, right_spec)
        self.assertEqual(left_spec["rho"], DEFAULT_CANDIDATE["rho"])
        for setting in (left, right):
            self.assertEqual(setting["backend"], "native")
            self.assertIs(setting["native_fail_closed"], True)
            self.assertIs(setting["formal_native"], True)
            self.assertEqual(setting["native_abi_version"], 5)
            self.assertEqual(len(setting["native_wheel_sha256"]), 64)
            self.assertEqual(
                setting["rng_version"], "python-random-mt19937-v1")
            self.assertEqual(setting["algorithm_revision"], "native-ga-v1")
            self.assertEqual(setting["tuning_protocol_id"],
                             FORMAL_NATIVE_TUNING_PROTOCOL_ID)


class TuningParallelExecutionTests(unittest.TestCase):
    def _screen(self):
        candidates = generate_candidates()
        return build_trial_schedule(
            "screen",
            candidate_ids=[row["candidate_id"] for row in candidates],
            circuits=[f"c{i}" for i in range(9)], seeds=[0])

    def test_four_static_shards_cover_schedule_once_and_limit_after_shard(self):
        rows = validate_trial_schedule(self._screen())
        shards = [
            _select_worker_trials(
                rows, "screen", worker_count=4, worker_index=index)
            for index in range(4)
        ]
        ids = [{trial.trial_id for trial in shard} for shard in shards]
        self.assertEqual([len(shard) for shard in shards], [81] * 4)
        self.assertEqual(set.union(*ids), {trial.trial_id for trial in rows})
        self.assertFalse(any(ids[left] & ids[right]
                             for left in range(4)
                             for right in range(left + 1, 4)))
        limited = _select_worker_trials(
            rows, "screen", worker_count=4, worker_index=2, limit=3)
        self.assertEqual(limited, shards[2][:3])
        with self.assertRaisesRegex(ValueError, "validation.*serially"):
            _select_worker_trials(
                rows, "validation", worker_count=4, worker_index=0)

    def test_claim_competition_and_explicit_dead_owner_recovery(self):
        schedule = self._screen()
        trial = validate_trial_schedule(schedule)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schedules = root / "schedules"
            schedules.mkdir()
            (schedules / "screen.json").write_text(
                json.dumps(schedule), encoding="utf-8")
            claim = _acquire_claim(
                root, schedule, trial, worker_count=4,
                worker_index=trial.ordinal % 4)
            with self.assertRaisesRegex(RuntimeError, "already claimed"):
                _acquire_claim(
                    root, schedule, trial, worker_count=4,
                    worker_index=trial.ordinal % 4)
            with self.assertRaisesRegex(RuntimeError, "live tuning claim"):
                recover_tuning_claim(root, "screen", trial.trial_id)
            _release_claim(root, schedule, trial, claim)
            self.assertFalse(_claim_path(root, trial).exists())

            with mock.patch(
                    "experiments_v2.tuning_runner.os.getpid",
                    return_value=99_999_999):
                stale = _acquire_claim(
                    root, schedule, trial, worker_count=4,
                    worker_index=trial.ordinal % 4)
            # Simulate recovery being killed after the durable history link but
            # before it removed the active claim alias.  Recovery must resume
            # idempotently instead of leaving the trial permanently blocked.
            history = (root / "claim_history" / "screen" / trial.trial_id /
                       f"{stale['record_sha256']}.json")
            history.parent.mkdir(parents=True)
            os.link(_claim_path(root, trial), history)
            recovered = recover_tuning_claim(root, "screen", trial.trial_id)
            self.assertEqual(
                recovered["claim_record_sha256"], stale["record_sha256"])
            self.assertFalse(_claim_path(root, trial).exists())
            self.assertTrue(Path(recovered["recovered_claim"]).is_file())

    def test_immutable_publication_and_history_are_idempotent_no_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            immutable = root / "immutable.json"
            _write_immutable_json(immutable, {"value": 1})
            _write_immutable_json(immutable, {"value": 1})
            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                _write_immutable_json(immutable, {"value": 2})
            self.assertEqual(json.loads(immutable.read_text()), {"value": 1})

            source = root / "receipt.json"
            history = root / "history" / "receipt.json"
            source.write_text('{"sealed":true}\n', encoding="utf-8")
            history.parent.mkdir()
            os.link(source, history)
            digest = _archive_immutable_file(source, history)
            self.assertFalse(source.exists())
            self.assertEqual(digest, sha256_file(history))

    def test_exclusive_phase_lock_waits_for_worker_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "phase_locks" / "screen.lock"
            code = (
                "import fcntl,sys; "
                "h=open(sys.argv[1],'a+b'); "
                "fcntl.flock(h.fileno(),fcntl.LOCK_EX); print('exclusive',flush=True)"
            )
            with _phase_lock(root, "screen", exclusive=False):
                process = subprocess.Popen(
                    [sys.executable, "-c", code, str(lock_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.2)
            stdout, stderr = process.communicate(timeout=3.0)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "exclusive")

    @mock.patch("experiments_v2.tuning_runner.load_phase_trials")
    def test_phase_seal_freezes_and_revalidates_receipt_snapshot(
            self, load_trials):
        schedule = self._screen()
        trial = validate_trial_schedule(schedule)[0]
        load_trials.return_value = (schedule, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = _receipt_path(root, trial)
            receipt.parent.mkdir(parents=True)
            receipt.write_text('{"receipt":1}\n', encoding="utf-8")
            _, _, snapshot = _seal_completed_phase(
                root, schedule, [trial], phase="screen",
                purpose="promotion")
            _validate_receipt_snapshot(
                root, schedule, [trial], snapshot, purpose="promotion")
            receipt.write_text('{"receipt":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "snapshot changed"):
                _validate_receipt_snapshot(
                    root, schedule, [trial], snapshot, purpose="promotion")

    @mock.patch("experiments_v2.tuning_runner._validate_schedule_chain")
    @mock.patch("experiments_v2.tuning_runner._validate_tuning_workspace")
    def test_sealed_phase_rejects_resume_retry_before_any_attempt(
            self, validate_workspace, validate_chain):
        schedule = self._screen()
        trial = validate_trial_schedule(schedule)[0]
        validate_workspace.return_value = {}
        validate_chain.return_value = (schedule, [trial])
        dataset = SimpleNamespace(name="qmap154")
        plan = SimpleNamespace(datasets={"qmap154": dataset})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_immutable_json(
                _phase_seal_path(root, "screen"),
                _phase_seal_payload(schedule, "screen", "promotion"))
            with self.assertRaisesRegex(RuntimeError, "sealed.*no retry"):
                run_tuning_phase(
                    plan, "qmap154", root, "screen", resume=True,
                    retry_failed=True, worker_count=4, worker_index=0)

    def test_status_does_not_call_claimed_complete(self):
        schedule = self._screen()
        trials = validate_trial_schedule(schedule)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schedules = root / "schedules"
            schedules.mkdir()
            (schedules / "screen.json").write_text(
                json.dumps(schedule), encoding="utf-8")
            for trial in trials:
                receipt = _receipt_path(root, trial)
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text("{}\n", encoding="utf-8")
            claimed = trials[0]
            _acquire_claim(
                root, schedule, claimed, worker_count=4,
                worker_index=claimed.ordinal % 4)
            status = tuning_status(root)["phases"]["screen"]
            self.assertEqual(status["receipts"], status["expected"])
            self.assertEqual(status["claims"], 1)
            self.assertFalse(status["validated"])
            self.assertFalse(status["complete"])

    @mock.patch("experiments_v2.tuning_runner.promoted_candidates")
    @mock.patch("experiments_v2.tuning_runner.stage_leaderboard")
    @mock.patch("experiments_v2.tuning_runner.load_phase_trials")
    @mock.patch("experiments_v2.tuning_runner._validate_schedule_chain")
    @mock.patch("experiments_v2.tuning_runner._validate_tuning_workspace")
    @mock.patch("experiments_v2.tuning_runner.load_experiment_plan")
    def test_unpromotable_phase_never_publishes_seal(
            self, load_plan, validate_workspace, validate_chain, load_trials,
            leaderboard, promoted):
        schedule = self._screen()
        scheduled = validate_trial_schedule(schedule)
        load_plan.return_value = SimpleNamespace()
        validate_workspace.return_value = {}
        validate_chain.return_value = (schedule, scheduled)
        load_trials.return_value = (schedule, [])
        leaderboard.return_value = []
        promoted.side_effect = RuntimeError("insufficient valid candidates")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workspace_manifest.json").write_text(
                json.dumps({"plan_path": "/plan", "dataset": "qmap154"}),
                encoding="utf-8")
            (root / "candidates.json").write_text(json.dumps({
                "default_candidate_id": "default"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "insufficient"):
                promote_tuning_phase(root, "screen")
            self.assertFalse(_phase_seal_path(root, "screen").exists())

    @mock.patch("experiments_v2.tuning_runner._ranked_validation_selection")
    @mock.patch("experiments_v2.tuning_runner.load_phase_trials")
    @mock.patch("experiments_v2.tuning_runner._candidate_map")
    @mock.patch("experiments_v2.tuning_runner._validate_schedule_chain")
    @mock.patch("experiments_v2.tuning_runner._validate_tuning_workspace")
    def test_unselectable_validation_never_publishes_seal(
            self, validate_workspace, validate_chain, candidate_map,
            load_trials, ranked):
        schedule = {"schedule_sha256": "9" * 64,
                    "circuits": ["toy"], "seeds": [0, 1, 2, 3, 4]}
        validate_workspace.return_value = {}
        validate_chain.return_value = (schedule, [])
        candidate_map.return_value = {"default": {}}
        load_trials.return_value = (schedule, [])
        ranked.return_value = ({"shared_selected": None}, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(exist_ok=True)
            (root / "workspace_manifest.json").write_text(
                json.dumps({"dataset": "qmap154"}), encoding="utf-8")
            (root / "candidates.json").write_text(json.dumps({
                "default_candidate_id": "default"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no valid shared"):
                finalize_tuning(SimpleNamespace(), root, root / "configs")
            self.assertFalse(_phase_seal_path(root, "validation").exists())

    @mock.patch("experiments_v2.tuning_runner._attempt_spec")
    @mock.patch("experiments_v2.tuning_runner.validate_trial_receipt")
    @mock.patch("experiments_v2.tuning_runner.materialize_candidate_configs")
    @mock.patch("experiments_v2.tuning_runner._candidate_map")
    @mock.patch("experiments_v2.tuning_runner._validate_schedule_chain")
    @mock.patch("experiments_v2.tuning_runner._validate_tuning_workspace")
    def test_resume_skips_sealed_shard_without_new_attempt_or_claim(
            self, validate_workspace, validate_chain, candidate_map,
            materialize, validate_receipt, attempt_spec):
        schedule = self._screen()
        trial = validate_trial_schedule(schedule)[0]
        validate_workspace.return_value = {}
        validate_chain.return_value = (schedule, [trial])
        candidate_map.return_value = {trial.candidate_id: {}}
        validate_receipt.return_value = {"status": "success"}
        canonicals = [
            SimpleNamespace(canonical_path=f"/{name}.qasm")
            for name in schedule["circuits"]]
        dataset = SimpleNamespace(name="qmap154")
        plan = SimpleNamespace(
            datasets={"qmap154": dataset},
            load_suite=lambda _dataset: canonicals)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "ledger" / "screen" / f"{trial.trial_id}.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}\n", encoding="utf-8")
            config = root / "config.json"
            config.write_text("{}\n", encoding="utf-8")
            materialize.return_value = {trial.method: config}
            result = run_tuning_phase(
                plan, "qmap154", root, "screen", resume=True,
                worker_count=1, worker_index=0)
            self.assertEqual(len(result["skipped"]), 1)
            self.assertEqual(result["attempted"], [])
            self.assertFalse(_claim_path(root, trial).exists())
            attempt_spec.assert_not_called()

    def test_stage_promotion_order_ignores_concurrent_transition_time(self):
        candidate_ids = ["default", "alpha", "beta"]

        def trials(times):
            return [
                TuningTrial(
                    candidate_id=cid, circuit="toy", method=method, seed=0,
                    status="success", verifier_ok=True, ghost_hits=0,
                    fallback=False, log_fidelity=-1.0,
                    transition_decision_ns=times[cid], move_time_us=10.0,
                    move_batches=1, fidelity_ood=False,
                    exponential_sensitivity_log_fidelity=-1.0)
                for cid in candidate_ids for method in ("M3", "M4")
            ]

        first = stage_leaderboard(
            trials({"default": 100, "alpha": 1000, "beta": 1}),
            candidate_ids=candidate_ids, default_id="default",
            expected_circuits=["toy"], expected_seeds=[0])
        second = stage_leaderboard(
            trials({"default": 100, "alpha": 1, "beta": 1000}),
            candidate_ids=candidate_ids, default_id="default",
            expected_circuits=["toy"], expected_seeds=[0])
        self.assertEqual(
            [row["candidate_id"] for row in first],
            [row["candidate_id"] for row in second])


class TuningReceiptTests(unittest.TestCase):
    def test_receipt_binds_schedule_config_and_unique_attempt_manifest(self):
        candidates = generate_candidates()
        schedule = build_trial_schedule(
            "screen", candidate_ids=[row["candidate_id"] for row in candidates],
            circuits=[f"c{i}" for i in range(9)], seeds=[0])
        trial = validate_trial_schedule(schedule)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text('{"backend":"native"}\n', encoding="utf-8")
            attempt = root / "trials" / trial.trial_id / "attempts" / "uuid-one"
            attempt.mkdir(parents=True)
            native_identity = {
                "algorithm_revision": "native-ga-v1",
                "backend": "native",
                "native_fail_closed": True,
                "native_abi_version": 5,
                "native_wheel_sha256": "a" * 64,
                "rng_version": "python-random-mt19937-v1",
                "operator_profile": "tuned",
                "init_engine": "sa",
                "formal_native": True,
                "tuning_protocol_id": FORMAL_NATIVE_TUNING_PROTOCOL_ID,
            }
            workspace = {
                "experiment_schema": 2,
                "protocol_id": TUNING_PROTOCOL_ID,
                "quality_policy": TUNING_QUALITY_POLICY_ID,
                "dataset": "qmap154",
                "repository": {"commit": "f" * 40, "dirty": False},
                "canonical_inputs": {
                    trial.circuit: {
                        "path": "/immutable/toy.qasm",
                        "canonical_sha256": "b" * 64,
                        "source_sha256": "c" * 64,
                    },
                },
                "native_identity": native_identity,
                "architecture_sha256": "d" * 64,
                "model_sha256": "e" * 64,
            }
            unsigned = json.dumps(
                workspace, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True).encode()
            workspace["manifest_sha256"] = hashlib.sha256(unsigned).hexdigest()
            (root / "workspace_manifest.json").write_text(
                json.dumps(workspace), encoding="utf-8")
            experiment_id = hashlib.sha256(json.dumps({
                "protocol_id": TUNING_PROTOCOL_ID,
                "schedule_sha256": schedule["schedule_sha256"],
                "trial_id": trial.trial_id,
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            manifest_path = attempt / "manifest.json"
            manifest = RunManifest(
                run_id="tuning-attempt", dataset="qmap154",
                circuit=trial.circuit, method=trial.method,
                seed=trial.seed, repetition=0, run_kind="smoke",
                experiment_id=experiment_id, status="success",
                git_commit="f" * 40, git_dirty=False,
                algorithm_revision="native-ga-v1", backend="native",
                native_abi_version=5, native_wheel_sha256="a" * 64,
                compiler_and_flags={
                    "cxx_standard": 17, "openmp": False,
                    "fast_math": False},
                tuning_protocol_id=FORMAL_NATIVE_TUNING_PROTOCOL_ID,
                rng_version="python-random-mt19937-v1",
                config_sha256=sha256_file(config), input_sha256="b" * 64,
                architecture_sha256="d" * 64, model_sha256="e" * 64,
                compiler_time_ns=2000, transition_decision_ns=1234,
                log_fidelity=-1.25, fidelity=math.exp(-1.25),
                exponential_sensitivity_log_fidelity=-1.25,
                exponential_sensitivity_fidelity=math.exp(-1.25),
                fidelity_components={
                    "log_one_qubit_gate": 0.0,
                    "log_two_qubit_gate": -1.25,
                    "log_idle_excitation": 0.0,
                    "log_atom_transfer": 0.0,
                    "log_coherence_linear": 0.0,
                },
                duration_us=100.0, qubits=2,
                expected_gates_1q=0, expected_gates_2q=1,
                observed_gates_1q=0, observed_gates_2q=1,
                expected_gate_ledger_sha256="9" * 64,
                observed_gate_ledger_sha256="9" * 64,
                move_time_us=42.0, move_batches=3,
                ghost_repairs=0, ghost_splits=0, ghost_hits=0,
                trace_protocol=trace_protocol_for_method(trial.method),
                ghost_policy=ghost_policy_for_method(trial.method),
                physicalization_policy=physicalization_policy_for_method(
                    trial.method),
                verifier_ok=True, artifact_dir=str(attempt.resolve()),
            )
            manifest.write(manifest_path)
            (attempt / "compiler_stats.json").write_text(json.dumps({
                "backend": "native", "fallback": False,
                "transition_decision_ns": 1234,
            }), encoding="utf-8")
            result = seal_trial_result(
                root, schedule, trial, config, manifest_path)
            receipt = (root / "ledger" / "screen" /
                       f"{trial.trial_id}.json")
            checked = validate_trial_receipt(
                receipt, schedule, trial, config)
            self.assertEqual(checked["status"], "success")
            self.assertEqual(result["attempt_manifest"],
                             str(manifest_path.resolve()))

            original_manifest = manifest_path.read_bytes()
            original_receipt = receipt.read_bytes()
            metric_tamper = json.loads(original_receipt)
            metric_tamper["log_fidelity"] = -0.01
            metric_tamper["record_sha256"] = _self_hash(
                metric_tamper, "record_sha256")
            receipt.write_text(json.dumps(metric_tamper), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "projection drift"):
                validate_trial_receipt(receipt, schedule, trial, config)
            receipt.write_bytes(original_receipt)

            for label, mutate in (
                    ("run-kind", lambda value: value.update(run_kind="main")),
                    ("repetition", lambda value: value.update(repetition=1)),
                    ("architecture", lambda value: value.update(
                        architecture_sha256="0" * 64)),
                    ("native-flags", lambda value: value[
                        "compiler_and_flags"].update(fast_math=True))):
                tampered = json.loads(original_manifest)
                mutate(tampered)
                manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
                receipt_payload = json.loads(original_receipt)
                receipt_payload["attempt_manifest_sha256"] = sha256_file(
                    manifest_path)
                receipt_payload["record_sha256"] = _self_hash(
                    receipt_payload, "record_sha256")
                receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
                with self.subTest(label=label), self.assertRaises(ValueError):
                    validate_trial_receipt(
                        receipt, schedule, trial, config)
                manifest_path.write_bytes(original_manifest)
                receipt.write_bytes(original_receipt)

            ood_manifest = json.loads(original_manifest)
            ood_manifest.update({
                "fidelity_ood": True,
                "log_fidelity": None,
                "fidelity": None,
                "exponential_sensitivity_log_fidelity": -2.0,
                "exponential_sensitivity_fidelity": math.exp(-2.0),
            })
            ood_manifest["fidelity_components"]["log_coherence_linear"] = None
            manifest_path.write_text(json.dumps(ood_manifest), encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                seal_trial_result(
                    root, schedule, trial, config, manifest_path)
            receipt.unlink()
            ood_receipt = seal_trial_result(
                root, schedule, trial, config, manifest_path)
            self.assertEqual(ood_receipt["status"], "success")
            self.assertTrue(ood_receipt["fidelity_ood"])
            self.assertIsNone(ood_receipt["log_fidelity"])
            self.assertEqual(
                ood_receipt["exponential_sensitivity_log_fidelity"], -2.0)
            validate_trial_receipt(receipt, schedule, trial, config)
            manifest_path.write_bytes(original_manifest)
            receipt.write_bytes(original_receipt)

            config.write_text('{"backend":"reference"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "config drift"):
                validate_trial_receipt(receipt, schedule, trial, config)


class TuningWorkspaceGateTests(unittest.TestCase):
    @mock.patch(
        "experiments_v2.tuning_runner.validate_initial_selection_for_plan")
    @mock.patch("experiments_v2.tuning_runner.repository_snapshot")
    def test_workspace_revalidates_initializer_and_detects_input_tamper(
            self, repository_snapshot, validate_initial):
        repository_snapshot.return_value = {
            "root": "/repo", "commit": "f" * 40,
            "branch": "codex/test", "dirty": False,
        }
        validate_initial.return_value = {
            "record_sha256": "d" * 64,
            "selected_engine": "sa",
        }
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tuning"
            root.mkdir()
            initial = base / "initial-placement" / "selected_engine.json"
            initial.parent.mkdir()
            initial.write_text("{}\n", encoding="utf-8")
            plan_path = base / "plan.json"
            suite_path = base / "suite.json"
            architecture_path = base / "architecture.json"
            model_path = base / "model.json"
            plan_path.write_text("{}\n", encoding="utf-8")
            suite_path.write_text("[]\n", encoding="utf-8")
            architecture_path.write_text("{}\n", encoding="utf-8")
            model_path.write_text("{}\n", encoding="utf-8")
            split_path = root / "split_manifest.json"
            space_path = root / "search_space.json"
            candidates_path = root / "candidates.json"
            for path, value in (
                    (split_path, {"split": 1}),
                    (space_path, {"space": 1}),
                    (candidates_path, {"candidates": 1})):
                path.write_text(json.dumps(value), encoding="utf-8")

            circuits = [f"c{i}" for i in range(9)]
            candidates = generate_candidates()
            screen = build_trial_schedule(
                "screen",
                candidate_ids=[row["candidate_id"] for row in candidates],
                circuits=circuits, seeds=[0])
            schedule_path = root / "schedules" / "screen.json"
            schedule_path.parent.mkdir()
            schedule_path.write_text(json.dumps(screen), encoding="utf-8")

            suite = [SimpleNamespace(
                canonical_path=str(base / f"{name}.qasm"),
                canonical_sha256=(f"{index:x}" * 64)[:64],
                source_sha256=(f"{index + 1:x}" * 64)[:64],
            ) for index, name in enumerate(circuits)]
            config_root = ROOT / "exp_setting" / "native_ga_v1"
            methods = {}
            for method, name in (
                    ("M3", "ours_nl_shared.json"),
                    ("M4", "ours_lk_shared.json")):
                path = config_root / name
                methods[method] = SimpleNamespace(
                    config_path=path,
                    payload=json.loads(path.read_text(encoding="utf-8")))
            dataset = SimpleNamespace(suite_manifest=suite_path)
            plan = SimpleNamespace(
                path=plan_path, repo_root=base / "repo",
                architecture_path=architecture_path,
                model_path=model_path,
                datasets={"qmap154": dataset}, methods=methods,
                load_suite=lambda _dataset: suite,
                experiment_id=lambda _dataset: "e" * 64,
            )
            native = {
                key: methods["M3"].payload["zac_setting"][0].get(key)
                for key in (
                    "algorithm_revision", "backend", "native_fail_closed",
                    "native_abi_version", "native_wheel_sha256",
                    "rng_version", "operator_profile", "init_engine",
                    "formal_native", "tuning_protocol_id")
            }
            canonical = {
                name: {
                    "path": str((base / f"{name}.qasm").resolve()),
                    "canonical_sha256": suite[index].canonical_sha256,
                    "source_sha256": suite[index].source_sha256,
                }
                for index, name in enumerate(circuits)
            }
            workspace = {
                "experiment_schema": 2,
                "protocol_id": TUNING_PROTOCOL_ID,
                "quality_policy": TUNING_QUALITY_POLICY_ID,
                "dataset": "qmap154",
                "repository": repository_snapshot.return_value,
                "plan_path": str(plan_path.resolve()),
                "plan_sha256": sha256_file(plan_path),
                "dataset_suite_path": str(suite_path.resolve()),
                "dataset_suite_sha256": sha256_file(suite_path),
                "experiment_id": "e" * 64,
                "canonical_inputs": canonical,
                "method_config_sha256": {
                    method: sha256_file(value.config_path)
                    for method, value in methods.items()},
                "native_identity": native,
                "architecture_sha256": sha256_file(architecture_path),
                "model_sha256": sha256_file(model_path),
                "initial_selection_path": str(initial.resolve()),
                "initial_selection_sha256": sha256_file(initial),
                "initial_selection_record_sha256": "d" * 64,
                "initial_placement_engine": "sa",
                "split_sha256": sha256_file(split_path),
                "space_sha256": sha256_file(space_path),
                "candidates_sha256": sha256_file(candidates_path),
                "screen_schedule_sha256": screen["schedule_sha256"],
            }
            workspace["manifest_sha256"] = _self_hash(
                workspace, "manifest_sha256")
            (root / "workspace_manifest.json").write_text(
                json.dumps(workspace), encoding="utf-8")

            checked = _validate_tuning_workspace(
                plan, "qmap154", root)
            self.assertEqual(checked["manifest_sha256"],
                             workspace["manifest_sha256"])
            self.assertTrue(validate_initial.call_args.kwargs[
                "enforce_pre_tuning_core"])

            old_workspace = json.loads(json.dumps(workspace))
            old_workspace["protocol_id"] = "resident-ga-native-v1"
            old_workspace["manifest_sha256"] = _self_hash(
                old_workspace, "manifest_sha256")
            (root / "workspace_manifest.json").write_text(
                json.dumps(old_workspace), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "protocol/dataset mismatch"):
                _validate_tuning_workspace(plan, "qmap154", root)
            (root / "workspace_manifest.json").write_text(
                json.dumps(workspace), encoding="utf-8")

            candidates_path.write_text('{"tampered":true}\n',
                                       encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "workspace evidence drift"):
                _validate_tuning_workspace(plan, "qmap154", root)


class TuningFinalSelectionGateTests(unittest.TestCase):
    @mock.patch("experiments_v2.tuning_runner._candidate_map")
    @mock.patch("experiments_v2.tuning_runner._ranked_validation_selection")
    @mock.patch("experiments_v2.tuning_runner.load_phase_trials")
    @mock.patch("experiments_v2.tuning_runner._validate_receipt_snapshot")
    @mock.patch("experiments_v2.tuning_runner._seal_completed_phase")
    @mock.patch("experiments_v2.tuning_runner._validate_schedule_chain")
    @mock.patch("experiments_v2.tuning_runner._validate_tuning_workspace")
    def test_finalize_separates_outer_v2_from_unchanged_native_v1(
            self, validate_workspace, validate_chain, seal_phase,
            validate_snapshot, load_trials, ranked, candidate_map):
        candidate = {**DEFAULT_CANDIDATE}
        candidate["candidate_id"] = candidate_id(candidate)
        selection = {
            "protocol_id": TUNING_PROTOCOL_ID,
            "quality_policy": TUNING_QUALITY_POLICY_ID,
            "default_candidate_id": candidate["candidate_id"],
            "shared_selected": candidate["candidate_id"],
            "independent_selected": {
                "M3": candidate["candidate_id"],
                "M4": candidate["candidate_id"],
            },
            "summaries": [],
        }
        schedule = {"schedule_sha256": "9" * 64,
                    "circuits": ["toy"], "seeds": [0, 1, 2, 3, 4]}
        validate_chain.return_value = (schedule, [])
        load_trials.return_value = (schedule, [])
        seal_phase.return_value = ({}, [], {"record_sha256": "8" * 64})
        ranked.return_value = (selection, [])
        candidate_map.return_value = {candidate["candidate_id"]: candidate}

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tuning"
            output = base / "configs"
            root.mkdir()
            (root / "workspace_manifest.json").write_text(
                json.dumps({"dataset": "qmap154"}), encoding="utf-8")
            (root / "candidates.json").write_text(json.dumps({
                "default_candidate_id": candidate["candidate_id"]}),
                encoding="utf-8")
            config_root = ROOT / "exp_setting" / "native_ga_v1"
            methods = {
                method: SimpleNamespace(payload=json.loads(
                    (config_root / name).read_text(encoding="utf-8")))
                for method, name in (
                    ("M3", "ours_nl_shared.json"),
                    ("M4", "ours_lk_shared.json"))
            }
            plan = SimpleNamespace(methods=methods)
            workspace = {
                "manifest_sha256": "1" * 64,
                "repository": {"commit": "a" * 40, "dirty": False},
                "plan_path": "/plan.json", "plan_sha256": "2" * 64,
                "dataset": "qmap154", "dataset_suite_sha256": "3" * 64,
                "experiment_id": "4" * 64,
                "native_identity": {
                    "tuning_protocol_id": FORMAL_NATIVE_TUNING_PROTOCOL_ID},
                "initial_selection_record_sha256": "5" * 64,
            }
            validate_workspace.return_value = workspace

            manifest = finalize_tuning(plan, root, output)

            self.assertEqual(manifest["protocol_id"], TUNING_PROTOCOL_ID)
            self.assertEqual(manifest["quality_policy"],
                             TUNING_QUALITY_POLICY_ID)
            generated = json.loads(
                (output / "ours_nl_shared.json").read_text(encoding="utf-8"))
            self.assertEqual(generated["tuning"]["protocol_id"],
                             TUNING_PROTOCOL_ID)
            self.assertEqual(
                generated["zac_setting"][0]["tuning_protocol_id"],
                FORMAL_NATIVE_TUNING_PROTOCOL_ID)

    @mock.patch("experiments_v2.tuning_runner._native_identity")
    @mock.patch("experiments_v2.tuning_runner.repository_snapshot")
    @mock.patch("experiments_v2.tuning_runner._candidate_map")
    @mock.patch("experiments_v2.tuning_runner._validate_schedule_chain")
    @mock.patch("experiments_v2.tuning_runner.load_phase_trials")
    @mock.patch("experiments_v2.tuning_runner.load_schedule")
    @mock.patch("experiments_v2.tuning_runner._load_published_phase_snapshot")
    @mock.patch("experiments_v2.tuning_runner._ranked_validation_selection")
    def test_final_selection_binds_replayed_ledger_and_current_shared_configs(
            self, ranked, published_snapshot, load_schedule, load_trials,
            validate_chain, candidate_map, repository_snapshot,
            native_identity):
        repository_snapshot.return_value = {
            "root": "/repo", "commit": "a" * 40,
            "branch": "codex/test", "dirty": False,
        }
        native_identity.return_value = {"native_abi_version": 5}
        selection = {
            "shared_selected": "shared-candidate",
            "independent_selected": {"M3": "m3-independent", "M4": None},
            "summaries": [],
        }
        ranked.return_value = (selection, [])
        schedule = {
            "schedule_sha256": "9" * 64,
            "circuits": ["toy"],
            "seeds": [0, 1, 2, 3, 4],
        }
        load_trials.return_value = (schedule, [])
        load_schedule.return_value = (schedule, [])
        published_snapshot.return_value = {"record_sha256": "8" * 64}
        validate_chain.return_value = (schedule, [])
        candidate_map.return_value = {"shared-candidate": {}}

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tuning"
            selected = root / "selected"
            config_root = base / "configs"
            selected.mkdir(parents=True)
            config_root.mkdir()
            plan_path = base / "plan.json"
            suite_path = base / "suite.json"
            architecture_path = base / "architecture.json"
            model_path = base / "model.json"
            plan_path.write_text("{}\n", encoding="utf-8")
            suite_path.write_text("[]\n", encoding="utf-8")
            architecture_path.write_text("{}\n", encoding="utf-8")
            model_path.write_text("{}\n", encoding="utf-8")
            payloads = {
                "M3": {
                    "tuning": {"candidate_id": "shared-candidate",
                               "track": "shared"}},
                "M4": {
                    "tuning": {"candidate_id": "shared-candidate",
                               "track": "shared"}},
            }
            names = {"M3": "ours_nl_shared.json",
                     "M4": "ours_lk_shared.json"}
            hashes = {}
            methods = {}
            for method, name in names.items():
                current = config_root / name
                current.write_text(json.dumps(payloads[method]), encoding="utf-8")
                (selected / name).write_bytes(current.read_bytes())
                hashes[name] = sha256_file(current)
                methods[method] = SimpleNamespace(
                    config_path=current, payload=payloads[method])
            for name in ("ours_nl_independent.json",
                         "ours_lk_independent.json"):
                path = config_root / name
                path.write_text('{}\n', encoding="utf-8")
                hashes[name] = sha256_file(path)

            workspace = {
                "manifest_sha256": "",
                "repository": {"commit": "b" * 40, "dirty": False},
                "plan_path": str(plan_path.resolve()),
                "plan_sha256": sha256_file(plan_path),
                "dataset": "qmap154",
                "dataset_suite_path": str(suite_path.resolve()),
                "dataset_suite_sha256": sha256_file(suite_path),
                "canonical_inputs": {},
                "architecture_sha256": sha256_file(architecture_path),
                "model_sha256": sha256_file(model_path),
                "experiment_id": "3" * 64,
                "native_identity": {"native_abi_version": 5},
                "initial_selection_record_sha256": "4" * 64,
            }
            workspace["manifest_sha256"] = _self_hash(
                workspace, "manifest_sha256")
            (root / "workspace_manifest.json").write_text(
                json.dumps(workspace), encoding="utf-8")
            (root / "candidates.json").write_text(json.dumps({
                "default_candidate_id": "default-candidate"}), encoding="utf-8")
            expected_leaderboard = {
                **selection,
                "validation_schedule_sha256": schedule["schedule_sha256"],
            }
            (root / "leaderboard.json").write_text(
                json.dumps(expected_leaderboard), encoding="utf-8")
            manifest = {
                "experiment_schema": 2,
                "protocol_id": TUNING_PROTOCOL_ID,
                "quality_policy": TUNING_QUALITY_POLICY_ID,
                "algorithm_revision": "native-ga-v1",
                "selection_status": "frozen_validation_selection",
                "workspace_manifest_sha256": workspace["manifest_sha256"],
                "repository": workspace["repository"],
                "plan_path": workspace["plan_path"],
                "plan_sha256": workspace["plan_sha256"],
                "dataset": workspace["dataset"],
                "dataset_suite_sha256": workspace["dataset_suite_sha256"],
                "experiment_id": workspace["experiment_id"],
                "native_identity": workspace["native_identity"],
                "initial_selection_record_sha256":
                    workspace["initial_selection_record_sha256"],
                "validation_schedule_sha256": schedule["schedule_sha256"],
                "default_candidate_id": "default-candidate",
                "shared_candidate_id": "shared-candidate",
                "independent_candidate_ids": {
                    "M3": "m3-independent", "M4": "default-candidate"},
                "config_sha256": hashes,
                "selection_sha256": stable_sha256(selection),
            }
            manifest["manifest_sha256"] = _self_hash(
                manifest, "manifest_sha256")
            artifact_manifest = root / "selected_config_manifest.json"
            tracked_manifest = config_root / "selected_config_manifest.json"
            artifact_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            tracked_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            plan = SimpleNamespace(
                path=plan_path, repo_root=base / "repo", methods=methods,
                datasets={"qmap154": SimpleNamespace(
                    suite_manifest=suite_path)},
                architecture_path=architecture_path,
                model_path=model_path,
                load_suite=lambda _dataset: [])

            checked = validate_tuning_selection_for_plan(
                plan, artifact_manifest)
            self.assertEqual(checked["shared_candidate_id"],
                             "shared-candidate")
            suite_bytes = suite_path.read_bytes()
            suite_path.write_text('[{"tampered": true}]\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "current frozen inputs"):
                validate_tuning_selection_for_plan(plan, artifact_manifest)
            suite_path.write_bytes(suite_bytes)
            current = methods["M4"].config_path
            current.write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen shared selection"):
                validate_tuning_selection_for_plan(plan, artifact_manifest)


if __name__ == "__main__":
    unittest.main()
