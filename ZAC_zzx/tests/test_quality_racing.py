from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from experiments_v2.quality_racing import (
    DEVELOPMENT_CIRCUITS,
    PROTOCOL_ID,
    RacingTrial,
    SEARCH_PROFILES,
    VALIDATION_CIRCUITS,
    VALIDATION_SEEDS,
    decision_candidates,
    lookahead_candidates,
    materialize_formal_setting,
    race_checkpoint,
    search_profile_candidates,
    search_space_manifest,
    select_top,
    shared_forward_pair,
    split_manifest,
    validate_shared_formal_settings,
)
from experiments_v2.plan import _validate_pair_payloads
from experiments_v2.plan import load_experiment_plan
from experiments_v2.quality_racing_runner import (
    _record_parallel_execution,
    _source_attempt_manifest,
    _strongest_original_scores,
    _valid_original_baselines,
    prepare_workspace, run_baselines, run_profiles,
    validate_quality_selection_for_plan,
)
from experiments_v2.cli import _assert_formal_selection_gates
from experiments_v2.contracts import stable_sha256
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
            workspace = prepare_workspace(plan, root)
            self.assertTrue(workspace["serial_execution"])
            baselines = run_baselines(plan, root, dry_run=True)
            self.assertEqual(60, baselines["planned"])
            self.assertEqual(60, len(baselines["outputs"]))
            self.assertEqual("bv_n14", baselines["outputs"][0]["identity"][
                "circuit_key"])
            profiles = run_profiles(plan, root, dry_run=True)
            self.assertEqual(75, len(profiles["M3"]["outputs"]))
            self.assertEqual(75, len(profiles["M4"]["outputs"]))
            self.assertEqual(10, len(list((root / "configs").glob(
                "*/seed-0.json"))))

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
            from experiments_v2.quality_racing_runner import _record_sha256
            validation = {
                "experiment_schema": 2,
                "protocol_id": PROTOCOL_ID,
                "selected_independent": selected_candidates,
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
