from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from experiments_v2.quality_racing import (
    DEVELOPMENT_CIRCUITS,
    DEVELOPMENT_COVERAGE_ONLY,
    DEVELOPMENT_TUNING_CIRCUITS,
    FORMAL_CANDIDATE_KEYS,
    INCUMBENT_NON_DEGRADATION_TOLERANCE,
    PROTOCOL_ID,
    RacingTrial,
    SEARCH_PROFILES,
    VALIDATION_CIRCUITS,
    VALIDATION_SEEDS,
    decision_candidates,
    incumbent_candidate,
    lookahead_candidates,
    materialize_formal_setting,
    race_checkpoint,
    search_profile_candidates,
    search_space_manifest,
    select_non_degrading,
    select_top,
    shared_forward_pair,
    split_manifest,
    validate_shared_formal_settings,
)
from experiments_v2.plan import (_validate_pair_payloads,
                                 effective_zac_setting,
                                 load_experiment_plan)
from experiments_v2.quality_racing_runner import (
    _assert_validation_non_degradation,
    _attempt_artifact_sha256,
    _attempt_identity_lock,
    _baseline_split_inventory,
    _native_runtime_identity_payload,
    _record_parallel_execution,
    _record_sha256,
    _retryable_native_environment_failure,
    _seal,
    _setting_native_identity,
    _source_attempt_manifest,
    _strongest_original_scores,
    _validate_attempt_receipt,
    _validate_source_baseline_receipt,
    _valid_original_baselines,
    prepare_workspace, run_baselines, run_profiles,
    validate_quality_selection_for_plan,
)
from experiments_v2.cli import _assert_formal_selection_gates
from experiments_v2.contracts import RunManifest, sha256_file, stable_sha256
from zzx.algorithm_v2 import FORMAL_NATIVE_TUNING_PROTOCOL_ID


ROOT = Path(__file__).resolve().parents[1]


def trial(candidate: str, method: str, circuit: str, seed: int, *,
          delta: float, move_time: float = 10.0,
          move_batches: int = 2, runtime: int = 100) -> RacingTrial:
    baseline = -2.0
    return RacingTrial(
        candidate_id=candidate,
        method=method,
        dataset="ZAC18" if circuit.startswith("z") else "QMAP154",
        circuit=circuit,
        seed=seed,
        status="success",
        verifier_ok=True,
        ghost_hits=0,
        fallback=False,
        log_fidelity=baseline + delta,
        baseline_log_fidelity=baseline,
        move_time_us=move_time,
        move_batches=move_batches,
        transition_decision_ns=runtime,
    )


class QualityRacingTests(unittest.TestCase):
    @staticmethod
    def _clean_repository(commit: str = "1" * 40):
        return {
            "root": str(ROOT.parent),
            "commit": commit,
            "branch": "codex/test",
            "dirty": False,
        }

    @staticmethod
    def _native_runtime_for_plan(plan):
        setting = effective_zac_setting(plan.methods["M3"].payload)
        return {
            "native_abi_version": setting["native_abi_version"],
            "native_wheel_sha256": setting["native_wheel_sha256"],
            "rng_version": setting["rng_version"],
            "extension_sha256": "e" * 64,
            "native_version": "test",
            "build_type": "Release",
            "compiler_id": "test",
            "compiler_version": "test",
            "cxx_standard": 17,
            "openmp": False,
            "fast_math": False,
        }

    def test_archived_baseline_manifest_is_rerooted_through_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            expected = source / "attempts" / "baselines" / "manifest.json"
            receipt = {
                "attempt_manifest":
                    "/former/workspace/attempts/baselines/manifest.json",
            }
            self.assertEqual(
                expected, _source_attempt_manifest(source, receipt))
            with self.assertRaisesRegex(ValueError, "lacks an attempts"):
                _source_attempt_manifest(source, {
                    "attempt_manifest": "/former/workspace/manifest.json",
                })

    def test_baseline_inventory_ignores_tuning_only_split_amendment(self):
        original = split_manifest()
        amended = json.loads(json.dumps(original))
        amended["protocol_id"] = "older-protocol"
        amended.pop("development_tuning")
        amended.pop("development_coverage_only")
        amended.pop("coverage_only_rule")
        self.assertEqual(
            _baseline_split_inventory(original),
            _baseline_split_inventory(amended))
        amended["development"]["QMAP154"] = amended[
            "development"]["QMAP154"][:-1]
        self.assertNotEqual(
            _baseline_split_inventory(original),
            _baseline_split_inventory(amended))

    def test_v1_baseline_receipt_remains_hash_valid_under_v3(self):
        source = _seal({
            "experiment_schema": 2,
            "protocol_id": PROTOCOL_ID,
            "identity": {"stage": "baselines"},
        })
        source["protocol_id"] = "resident-ga-quality-racing-v1"
        source["record_sha256"] = _record_sha256(source)
        _validate_source_baseline_receipt(source)
        source["identity"] = {"stage": "tampered"}
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            _validate_source_baseline_receipt(source)

    def test_parallel_quality_amendment_is_fail_closed_and_excludes_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _record_parallel_execution(root, 3)
            self.assertEqual(3, payload["workers"])
            self.assertEqual("spawned-process", payload["worker_isolation"])
            self.assertTrue(payload["receipt_identity_isolation"])
            self.assertFalse(payload["timing_benchmark_parallel"])
            self.assertEqual(
                payload,
                json.loads((root / "execution" /
                            "parallel-workers-3.json").read_text()))

    def test_strongest_baseline_ignores_but_preserves_invalid_original(self):
        invalid = SimpleNamespace(
            status="verifier_fail", verifier_ok=False,
            fidelity_ood=False,
            log_fidelity=None,
            exponential_sensitivity_log_fidelity=None)
        valid = SimpleNamespace(
            status="success", verifier_ok=True,
            fidelity_ood=False,
            log_fidelity=-1.0,
            exponential_sensitivity_log_fidelity=-0.9)
        valid_ood = SimpleNamespace(
            status="success", verifier_ok=True,
            fidelity_ood=True,
            log_fidelity=None,
            exponential_sensitivity_log_fidelity=-3.0)
        self.assertEqual(
            (valid,), _valid_original_baselines((invalid, valid)))
        self.assertEqual(
            (valid_ood,), _valid_original_baselines((invalid, valid_ood)))
        self.assertEqual(
            (None, -3.0),
            _strongest_original_scores((invalid, valid_ood)))
        self.assertEqual((), _valid_original_baselines((invalid, invalid)))

    def test_protocol_and_fixed_split_are_disjoint(self):
        self.assertEqual(FORMAL_NATIVE_TUNING_PROTOCOL_ID, PROTOCOL_ID)
        manifest = split_manifest()
        self.assertEqual(PROTOCOL_ID, manifest["protocol_id"])
        self.assertEqual(15, len(DEVELOPMENT_CIRCUITS))
        self.assertEqual(12, len(DEVELOPMENT_TUNING_CIRCUITS))
        self.assertEqual(
            ("dist_223", "hwb8_113", "hwb9_119"),
            DEVELOPMENT_COVERAGE_ONLY)
        self.assertNotIn("dist_223", DEVELOPMENT_TUNING_CIRCUITS)
        self.assertEqual(
            {"QMAP154": ["dist_223", "hwb8_113", "hwb9_119"],
             "ZAC18": []},
            manifest["development_coverage_only"])
        self.assertEqual(15, len(VALIDATION_CIRCUITS))
        self.assertFalse(set(DEVELOPMENT_CIRCUITS) & set(VALIDATION_CIRCUITS))
        config_root = ROOT / "exp_setting" / "native_ga_v1"
        self.assertEqual(
            manifest,
            json.loads((config_root / "split_manifest.json").read_text()))
        self.assertEqual(
            search_space_manifest(),
            json.loads((config_root / "tuning_space.json").read_text()))

    def test_five_profiles_and_sequential_expansions(self):
        m3 = search_profile_candidates("M3")
        m4 = search_profile_candidates("M4")
        self.assertEqual(list(SEARCH_PROFILES),
                         [row["search_profile"] for row in m3])
        self.assertEqual(5, len(m3))
        self.assertTrue(all(row["max_horizon"] == 0 for row in m3))
        self.assertTrue(all(row["max_horizon"] == 8 for row in m4))
        self.assertTrue(all(
            row["forecast_gate_candidate_budget"] == 1 for row in m4))
        decision = decision_candidates("M3", m3[:2])
        self.assertEqual(14, len(decision))
        self.assertEqual(14, len({row["candidate_id"] for row in decision}))
        decay = lookahead_candidates(decision_candidates("M4", m4[:2])[:2])
        self.assertEqual(36, len(decay))
        self.assertEqual({.1, .2, .35},
                         {row["alpha_lookahead"] for row in decay})
        self.assertEqual({.5, .7}, {row["rho"] for row in decay})
        self.assertEqual(
            {1, 2, 4},
            {row["forecast_gate_candidate_budget"] for row in decay})
        self.assertEqual(
            "sequential-profile-then-one-factor-then-m4-decay",
            search_space_manifest()["design"])

    def test_race_eliminates_quality_loser_unless_move_is_better(self):
        circuits = tuple(f"c{index}" for index in range(5))
        ids = ("leader", "loser", "move-winner")
        rows = []
        for circuit in circuits:
            rows.extend((
                trial("leader", "M3", circuit, 0, delta=.02,
                      move_time=10),
                trial("loser", "M3", circuit, 0, delta=.01,
                      move_time=12),
                trial("move-winner", "M3", circuit, 0, delta=.01,
                      move_time=8),
            ))
        result = race_checkpoint(
            rows, active_candidate_ids=ids, method="M3",
            completed_circuits=circuits)
        self.assertEqual("leader", result["leader"])
        self.assertEqual({"leader", "move-winner"}, set(result["retained"]))
        self.assertEqual("loser", result["eliminated"][0]["candidate_id"])

    def test_race_accepts_final_partial_block(self):
        circuits = tuple(f"c{index}" for index in range(4))
        rows = [
            trial(candidate, "M3", circuit, 0, delta=.01)
            for circuit in circuits for candidate in ("a", "b")
        ]
        result = race_checkpoint(
            rows, active_candidate_ids=("a", "b"), method="M3",
            completed_circuits=circuits)
        self.assertEqual(set(("a", "b")), set(result["retained"]))

    def test_validation_selection_prefers_runtime_inside_quality_band(self):
        circuits = ("a", "b")
        ids = ("best", "near-fast", "weak")
        rows = []
        for circuit in circuits:
            for seed in VALIDATION_SEEDS:
                rows.extend((
                    trial("best", "M4", circuit, seed, delta=.010,
                          runtime=100),
                    trial("near-fast", "M4", circuit, seed, delta=.009,
                          runtime=40),
                    trial("weak", "M4", circuit, seed, delta=.001,
                          runtime=1),
                ))
        selected = select_top(
            rows, candidate_ids=ids, method="M4", circuits=circuits,
            seeds=VALIDATION_SEEDS, count=2)
        self.assertEqual(["near-fast", "best"], selected["selected"])

    def test_incumbent_gate_forbids_purchasing_quality_regression(self):
        circuits = ("a", "b")
        rows = []
        for circuit in circuits:
            for seed in VALIDATION_SEEDS:
                rows.extend((
                    trial("incumbent", "M4", circuit, seed, delta=.010,
                          runtime=100),
                    trial("near-fast", "M4", circuit, seed, delta=.009,
                          runtime=1),
                    trial("better", "M4", circuit, seed, delta=.011,
                          runtime=40),
                ))
        selected = select_non_degrading(
            rows, candidate_ids=("incumbent", "near-fast", "better"),
            incumbent_candidate_id="incumbent", method="M4",
            circuits=circuits, seeds=VALIDATION_SEEDS)
        self.assertEqual(["better"], selected["selected"])
        self.assertNotIn("near-fast", selected["eligible_candidate_ids"])
        self.assertTrue(selected["non_degradation_passed"])
        with self.assertRaisesRegex(ValueError, "does not contain"):
            select_non_degrading(
                rows, candidate_ids=("near-fast", "better"),
                incumbent_candidate_id="incumbent", method="M4",
                circuits=circuits, seeds=VALIDATION_SEEDS)

    def test_incumbent_gate_forbids_cross_dataset_subsidy(self):
        zac = tuple(f"z{index}" for index in range(6))
        qmap = tuple(f"q{index}" for index in range(9))
        circuits = zac + qmap
        rows = []
        for circuit in circuits:
            for seed in VALIDATION_SEEDS:
                rows.extend((
                    trial("incumbent", "M4", circuit, seed, delta=0.0,
                          runtime=100),
                    trial(
                        "cross-subsidy", "M4", circuit, seed,
                        delta=(-0.01 if circuit in zac else 0.01),
                        runtime=1),
                ))
        selected = select_non_degrading(
            rows, candidate_ids=("incumbent", "cross-subsidy"),
            incumbent_candidate_id="incumbent", method="M4",
            circuits=circuits, seeds=VALIDATION_SEEDS)
        self.assertEqual(["incumbent"], selected["selected"])
        self.assertNotIn(
            "cross-subsidy", selected["eligible_candidate_ids"])
        self.assertEqual(
            {"ZAC18", "QMAP154"}, set(selected["dataset_summaries"]))
        self.assertLess(
            next(row for row in selected["dataset_summaries"]["ZAC18"]
                 if row["candidate_id"] == "cross-subsidy")[
                     "median_delta_log_fidelity"],
            selected["incumbent_median_delta_log_fidelity_by_dataset"][
                "ZAC18"])

    def test_incumbent_is_actual_tracked_config_not_cheap_profile(self):
        config_root = ROOT / "exp_setting" / "native_ga_v1"
        for method, name in (("M3", "ours_nl_independent.json"),
                             ("M4", "ours_lk_independent.json")):
            payload = json.loads((config_root / name).read_text())
            setting = payload["zac_setting"][0]
            candidate = incumbent_candidate(method, setting)
            for key in FORMAL_CANDIDATE_KEYS:
                self.assertEqual(setting[key], candidate[key])
            self.assertEqual(0 if method == "M3" else 8,
                             candidate["max_horizon"])
            self.assertNotEqual(
                search_profile_candidates(method)[0]["candidate_id"],
                candidate["candidate_id"])

    def test_shared_forward_pair_materializes_horizon_only_configs(self):
        m4 = search_profile_candidates("M4")[0]
        shared_m3, shared_m4 = shared_forward_pair(m4)
        config_root = ROOT / "exp_setting" / "native_ga_v1"
        nl_payload = json.loads(
            (config_root / "ours_nl_shared.json").read_text())
        lk_payload = json.loads(
            (config_root / "ours_lk_shared.json").read_text())
        nl = materialize_formal_setting(
            nl_payload["zac_setting"][0], shared_m3, seed=0,
            output_dir="results/shared-check/m3/")
        lk = materialize_formal_setting(
            lk_payload["zac_setting"][0], shared_m4, seed=0,
            output_dir="results/shared-check/m4/")
        validate_shared_formal_settings(nl, lk)
        self.assertEqual(0, nl["lookahead_horizon"]["max_horizon"])
        self.assertEqual(8, lk["lookahead_horizon"]["max_horizon"])
        self.assertEqual(1, nl["forecast_gate_candidate_budget"])
        self.assertEqual(1, lk["forecast_gate_candidate_budget"])

    def test_independent_main_track_accepts_independently_selected_knobs(self):
        config_root = ROOT / "exp_setting" / "native_ga_v1"
        nl = json.loads(
            (config_root / "ours_nl_independent.json").read_text())
        lk = json.loads(
            (config_root / "ours_lk_independent.json").read_text())
        lk["zac_setting"][0]["population_size"] = 8
        lk["zac_setting"][0]["max_unique_evaluations"] = 576
        lk["tuning"]["candidate_id"] = "independent-m4"
        _validate_pair_payloads(nl, lk)

    def test_serial_runner_prepare_and_dry_run_use_fixed_schedule(self):
        plan = load_experiment_plan(
            ROOT / "experiments_v2" / "experiment_plan_v2.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                    "experiments_v2.quality_racing_runner."
                    "repository_snapshot",
                    return_value=self._clean_repository()), mock.patch(
                        "experiments_v2.quality_racing_runner."
                        "_native_runtime_identity_payload",
                        return_value=self._native_runtime_for_plan(plan)):
                workspace = prepare_workspace(plan, root)
                self.assertTrue(workspace["serial_execution"])
                baselines = run_baselines(plan, root, dry_run=True)
                self.assertEqual(60, baselines["planned"])
                self.assertEqual(60, len(baselines["outputs"]))
                self.assertEqual("bv_n14", baselines["outputs"][0]["identity"][
                    "circuit_key"])
                profiles = run_profiles(plan, root, dry_run=True)
                self.assertEqual(60, len(profiles["M3"]["outputs"]))
                self.assertEqual(60, len(profiles["M4"]["outputs"]))
                self.assertEqual(
                    list(DEVELOPMENT_TUNING_CIRCUITS),
                    profiles["M3"]["ranking_circuits"])
                self.assertEqual(
                    ["dist_223", "hwb8_113", "hwb9_119"],
                    profiles["M3"]["coverage_only_circuits"])
                self.assertFalse(any(
                    row["identity"]["circuit_key"]
                    in DEVELOPMENT_COVERAGE_ONLY
                    for row in profiles["M3"]["outputs"]))
                self.assertEqual(10, len(list((root / "configs").glob(
                    "*/seed-0.json"))))
            execution = json.loads((root / "protocol" /
                                    "tuning_execution_identity.json").read_text())
            incumbents = json.loads((root / "protocol" /
                                     "incumbent_configs.json").read_text())
            self.assertEqual("1" * 40,
                             execution["repository"]["commit"])
            self.assertEqual(execution["record_sha256"], incumbents[
                "execution_identity_record_sha256"])
            for method in ("M3", "M4"):
                candidate = incumbents["methods"][method]["candidate"]
                setting = effective_zac_setting(plan.methods[method].payload)
                for key in FORMAL_CANDIDATE_KEYS:
                    self.assertEqual(setting[key], candidate[key])

    def test_runner_rejects_execution_identity_drift_after_prepare(self):
        plan = load_experiment_plan(
            ROOT / "experiments_v2" / "experiment_plan_v2.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                    "experiments_v2.quality_racing_runner."
                    "repository_snapshot",
                    return_value=self._clean_repository("1" * 40)), mock.patch(
                        "experiments_v2.quality_racing_runner."
                        "_native_runtime_identity_payload",
                        return_value=self._native_runtime_for_plan(plan)):
                prepare_workspace(plan, root)
            with mock.patch(
                    "experiments_v2.quality_racing_runner."
                    "repository_snapshot",
                    return_value=self._clean_repository("2" * 40)), mock.patch(
                        "experiments_v2.quality_racing_runner."
                        "_native_runtime_identity_payload",
                        return_value=self._native_runtime_for_plan(plan)):
                with self.assertRaisesRegex(ValueError,
                                            "execution identity changed"):
                    run_profiles(plan, root, dry_run=True)

    def test_prepare_runtime_preflight_rejects_loaded_wheel_drift(self):
        expected = {
            "native_abi_version": 8,
            "native_wheel_sha256": "a" * 64,
            "rng_version": "python-random-mt19937-v1",
        }
        loaded = {
            "native_abi_version": 8,
            "native_wheel_sha256": "b" * 64,
            "rng_version": "python-random-mt19937-v1",
            "extension_sha256": "e" * 64,
            "wheel_registered": True,
            "version": "test",
            "build_type": "Release",
            "compiler_id": "test",
            "compiler_version": "test",
            "cxx_standard": 17,
            "openmp": False,
            "fast_math": False,
        }
        with mock.patch("zzx.native_backend.build_info", return_value=loaded):
            with self.assertRaisesRegex(ValueError, "loaded native runtime drift"):
                _native_runtime_identity_payload(expected)

    def test_candidate_receipt_is_bound_to_config_commit_abi_and_wheel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "candidate.json"
            config.write_text('{"candidate": 1}\n')
            config_hash = sha256_file(config)
            execution = {
                "execution_identity_record_sha256": "a" * 64,
                "git_commit": "1" * 40,
                "git_dirty": False,
                "config_sha256": config_hash,
                "algorithm_revision": "native-ga-v1",
                "backend": "native",
                "formal_native": True,
                "native_abi_version": 8,
                "native_wheel_sha256": "b" * 64,
                "tuning_protocol_id": PROTOCOL_ID,
                "rng_version": "python-random-mt19937-v1",
            }
            identity = {
                "stage": "profiles", "dataset": "ZAC18",
                "circuit": "toy", "circuit_key": "toy", "method": "M3",
                "candidate_id": "candidate", "seed": 0,
                "execution": execution,
            }
            artifact = root / "attempt"
            manifest_path = artifact / "manifest.json"
            manifest = RunManifest(
                run_id="run", dataset="ZAC18", circuit="toy", method="M3",
                run_kind="smoke", status="compiler_error",
                git_commit=execution["git_commit"], git_dirty=False,
                algorithm_revision=execution["algorithm_revision"],
                backend="native", native_abi_version=8,
                native_wheel_sha256=execution["native_wheel_sha256"],
                compiler_and_flags={
                    "cxx_standard": 17, "openmp": False,
                    "fast_math": False,
                },
                tuning_protocol_id=PROTOCOL_ID,
                rng_version=execution["rng_version"],
                config_sha256=config_hash, artifact_dir=str(artifact),
            )
            manifest.write(manifest_path)

            receipt_path = root / "receipt.json"

            def write_receipt():
                receipt = _seal({
                    "experiment_schema": 2,
                    "protocol_id": PROTOCOL_ID,
                    "identity": identity,
                    "config": str(config),
                    "config_sha256": config_hash,
                    "status": manifest.status,
                    "attempt_manifest": str(manifest_path),
                    "attempt_manifest_sha256": sha256_file(manifest_path),
                })
                receipt_path.write_text(json.dumps(receipt))

            write_receipt()
            _validate_attempt_receipt(receipt_path, identity)

            config.write_text('{"candidate": 2}\n')
            with self.assertRaisesRegex(ValueError, "candidate config drift"):
                _validate_attempt_receipt(receipt_path, identity)
            config.write_text('{"candidate": 1}\n')

            manifest.git_commit = "2" * 40
            manifest.write(manifest_path)
            write_receipt()
            with self.assertRaisesRegex(ValueError, "candidate Git drift"):
                _validate_attempt_receipt(receipt_path, identity)

            manifest.git_commit = execution["git_commit"]
            manifest.native_abi_version = 9
            manifest.write(manifest_path)
            write_receipt()
            with self.assertRaisesRegex(ValueError,
                                        "candidate native_abi_version drift"):
                _validate_attempt_receipt(receipt_path, identity)

            manifest.native_abi_version = 8
            manifest.native_wheel_sha256 = "c" * 64
            manifest.write(manifest_path)
            write_receipt()
            with self.assertRaisesRegex(ValueError,
                                        "candidate native_wheel_sha256 drift"):
                _validate_attempt_receipt(receipt_path, identity)

    def test_unproven_native_compiler_error_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "attempt"
            manifest_path = artifact / "manifest.json"
            execution = {
                "algorithm_revision": "native-ga-v1",
                "backend": "native",
                "native_abi_version": 8,
                "native_wheel_sha256": "b" * 64,
                "tuning_protocol_id": PROTOCOL_ID,
                "rng_version": "python-random-mt19937-v1",
            }
            identity = {"execution": execution}
            manifest = RunManifest(
                run_id="run", dataset="ZAC18", circuit="toy", method="M3",
                run_kind="smoke", status="compiler_error",
                artifact_dir=str(artifact))
            manifest.write(manifest_path)
            payload = {
                "identity": identity,
                "status": "compiler_error",
                "attempt_manifest": str(manifest_path),
            }
            self.assertTrue(_retryable_native_environment_failure(payload))

            manifest.algorithm_revision = execution["algorithm_revision"]
            manifest.backend = "native"
            manifest.native_abi_version = 8
            manifest.native_wheel_sha256 = execution["native_wheel_sha256"]
            manifest.compiler_and_flags = {
                "cxx_standard": 17, "openmp": False, "fast_math": False,
            }
            manifest.tuning_protocol_id = PROTOCOL_ID
            manifest.rng_version = execution["rng_version"]
            manifest.write(manifest_path)
            self.assertFalse(_retryable_native_environment_failure(payload))

    def test_attempt_identity_lock_rejects_duplicate_live_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipts" / "attempt.json"
            identity = {"candidate_id": "candidate", "seed": 0}
            with _attempt_identity_lock(receipt, identity):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with _attempt_identity_lock(receipt, identity):
                        self.fail("duplicate identity lock unexpectedly acquired")

    def test_success_receipt_hashes_every_attempt_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "candidate.json"
            config.write_text('{"candidate": 1}\n')
            config_hash = sha256_file(config)
            artifact = root / "attempt"
            artifact.mkdir()
            raw = artifact / "trace.zair.json.gz"
            raw.write_bytes(b"frozen-trace")
            timing = artifact / "compiler_timing.json"
            timing.write_text('{"compiler_time_ns": 1}\n')
            manifest_path = artifact / "manifest.json"
            execution = {
                "execution_identity_record_sha256": "a" * 64,
                "git_commit": "1" * 40,
                "git_dirty": False,
                "config_sha256": config_hash,
                "algorithm_revision": "native-ga-v1",
                "backend": "native",
                "formal_native": True,
                "native_abi_version": 8,
                "native_wheel_sha256": "b" * 64,
                "tuning_protocol_id": PROTOCOL_ID,
                "rng_version": "python-random-mt19937-v1",
            }
            identity = {
                "stage": "profiles", "dataset": "ZAC18",
                "circuit": "toy", "circuit_key": "toy", "method": "M3",
                "candidate_id": "candidate", "seed": 0,
                "execution": execution,
            }
            manifest = RunManifest(
                run_id="run", dataset="ZAC18", circuit="toy", method="M3",
                run_kind="smoke", status="success",
                git_commit=execution["git_commit"], git_dirty=False,
                algorithm_revision=execution["algorithm_revision"],
                backend="native", native_abi_version=8,
                native_wheel_sha256=execution["native_wheel_sha256"],
                compiler_and_flags={
                    "cxx_standard": 17, "openmp": False, "fast_math": False,
                },
                tuning_protocol_id=PROTOCOL_ID,
                rng_version=execution["rng_version"],
                config_sha256=config_hash, artifact_dir=str(artifact),
            )
            manifest.write(manifest_path)
            hashes = _attempt_artifact_sha256(manifest_path)
            self.assertEqual(
                {"compiler_timing.json", "trace.zair.json.gz"}, set(hashes))
            receipt_path = root / "receipt.json"
            receipt = _seal({
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "identity": identity,
                "config": str(config),
                "config_sha256": config_hash,
                "status": "success",
                "attempt_manifest": str(manifest_path),
                "attempt_manifest_sha256": sha256_file(manifest_path),
                "attempt_artifact_sha256": hashes,
            })
            receipt_path.write_text(json.dumps(receipt))
            _validate_attempt_receipt(receipt_path, identity)
            raw.write_bytes(b"changed-trace")
            with self.assertRaisesRegex(ValueError, "success artifact drift"):
                _validate_attempt_receipt(receipt_path, identity)

    @mock.patch(
        "experiments_v2.initial_placement_runner."
        "validate_initial_selection_for_plan")
    @mock.patch(
        "experiments_v2.quality_racing_runner."
        "validate_quality_selection_for_plan")
    def test_formal_gate_uses_quality_racing_selection(
            self, quality_validator, initial_validator):
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            plan = SimpleNamespace(output_root=output_root)
            initial_validator.return_value = {
                "record_sha256": "a" * 64,
                "selected_engine": "sa",
            }
            quality_validator.return_value = {
                "initial_selection_record_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "shared_candidate_id": "shared-forward-candidate",
            }
            result = _assert_formal_selection_gates(plan)
            expected = (output_root / "tuning-quality-v1" /
                        "selected_config_manifest.json")
            quality_validator.assert_called_once_with(plan, expected)
            self.assertEqual(str(expected), result["tuning_selection"])
            self.assertEqual("shared-forward-candidate",
                             result["shared_candidate_id"])

    def test_quality_selection_rejects_tracked_config_drift(self):
        plan = load_experiment_plan(
            ROOT / "experiments_v2" / "experiment_plan_v2.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_root = root / "selected_configs"
            selected_root.mkdir(parents=True)
            names = (
                "ours_nl_independent.json", "ours_lk_independent.json",
                "ours_nl_shared.json", "ours_lk_shared.json",
            )
            hashes = {}
            selected_candidates = {}
            shared_candidates = {}
            for name in names:
                payload = json.loads((
                    ROOT / "exp_setting" / "native_ga_v1" / name
                ).read_text())
                (selected_root / name).write_text(json.dumps(payload))
                hashes[name] = stable_sha256(payload)
                method = ("M3" if "_nl_" in name else "M4")
                candidate = {
                    "candidate_id": payload["tuning"]["candidate_id"],
                    "method": method,
                }
                if "independent" in name:
                    selected_candidates[method] = candidate
                else:
                    shared_candidates[method] = candidate
            selections = root / "selections"
            selections.mkdir()
            protocol = root / "protocol"
            protocol.mkdir()
            native_identity = {
                method: {
                    "config": f"/frozen/{method}.json",
                    "config_sha256": ("a" if method == "M3" else "b") * 64,
                    "native": _setting_native_identity(
                        json.loads((selected_root / name).read_text())[
                            "zac_setting"][0])
                }
                for method, name in (
                    ("M3", "ours_nl_independent.json"),
                    ("M4", "ours_lk_independent.json"),
                )
            }
            execution = _seal({
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "kind": "quality-racing-execution-identity",
                "repository": self._clean_repository(),
                "methods": native_identity,
                "native_runtime": self._native_runtime_for_plan(plan),
            })
            (protocol / "tuning_execution_identity.json").write_text(
                json.dumps(execution))
            incumbent_candidates = {
                method: {
                    "source_config": native_identity[method]["config"],
                    "source_config_sha256":
                        native_identity[method]["config_sha256"],
                    "candidate": {
                        "candidate_id": f"inc-{method}", "method": method,
                    },
                } for method in ("M3", "M4")
            }
            incumbents = _seal({
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "kind": "tracked-pre-tuning-incumbents",
                "execution_identity_record_sha256":
                    execution["record_sha256"],
                "methods": incumbent_candidates,
            })
            (protocol / "incumbent_configs.json").write_text(
                json.dumps(incumbents))
            selection_evidence = {}
            for method in ("M3", "M4"):
                winner_id = selected_candidates[method]["candidate_id"]
                incumbent_id = incumbent_candidates[method]["candidate"][
                    "candidate_id"]
                paired_summaries = [
                    {
                        "candidate_id": incumbent_id,
                        "valid": True,
                        "median_delta_log_fidelity": 0.0,
                    },
                    {
                        "candidate_id": winner_id,
                        "valid": True,
                        "median_delta_log_fidelity": 0.1,
                    },
                ]
                selection_evidence[method] = {
                    "selected": [winner_id],
                    "incumbent_candidate_id": incumbent_id,
                    "non_degradation_passed": True,
                    "non_degradation_tolerance":
                        INCUMBENT_NON_DEGRADATION_TOLERANCE,
                    "incumbent_median_delta_log_fidelity": 0.0,
                    "winner_median_delta_log_fidelity": 0.1,
                    "incumbent_median_delta_log_fidelity_by_dataset": {
                        "ZAC18": 0.0, "QMAP154": 0.0,
                    },
                    "winner_median_delta_log_fidelity_by_dataset": {
                        "ZAC18": 0.1, "QMAP154": 0.1,
                    },
                    "dataset_summaries": {
                        "ZAC18": paired_summaries,
                        "QMAP154": paired_summaries,
                    },
                    "summaries": paired_summaries,
                }
            validation = {
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "selected_independent": selected_candidates,
                "selections": selection_evidence,
                "incumbent_config_record_sha256": incumbents[
                    "record_sha256"],
            }
            validation["record_sha256"] = _record_sha256(validation)
            (selections / "validation.json").write_text(
                json.dumps(validation))
            shared = {
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "candidates": shared_candidates,
                "m4_forward_better": False,
            }
            shared["record_sha256"] = _record_sha256(shared)
            (selections / "shared_forward_check.json").write_text(
                json.dumps(shared))
            manifest = {
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "selected_independent": selected_candidates,
                "execution_identity_record_sha256":
                    execution["record_sha256"],
                "incumbent_config_record_sha256":
                    incumbents["record_sha256"],
                "validation_non_degradation":
                    _assert_validation_non_degradation(
                        validation, incumbents),
                "validation_selection_record_sha256":
                    validation["record_sha256"],
                "shared_forward_check_record_sha256":
                    shared["record_sha256"],
                "initial_selection_record_sha256": "3" * 64,
                "config_sha256": hashes,
            }
            manifest["record_sha256"] = _record_sha256(manifest)
            path = root / "selected_config_manifest.json"
            path.write_text(json.dumps(manifest))
            drift = json.loads((selected_root / names[0]).read_text())
            drift["tuning"]["candidate_id"] = "drift"
            (selected_root / names[0]).write_text(json.dumps(drift))
            with mock.patch(
                    "experiments_v2.quality_racing_runner.default_root",
                    return_value=root), mock.patch(
                    "experiments_v2.initial_placement_runner."
                    "validate_initial_selection_for_plan",
                    return_value={"record_sha256": "3" * 64}):
                with self.assertRaisesRegex(ValueError,
                                            "selected config hash drift"):
                    validate_quality_selection_for_plan(plan, path)


if __name__ == "__main__":
    unittest.main()
