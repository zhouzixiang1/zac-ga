from __future__ import annotations

import copy
import json
import hashlib
import math
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
    validate_trial_schedule,
)
from experiments_v2.tuning_runner import (  # noqa: E402
    _replayed_promotion_report,
    _self_hash,
    _validate_schedule_chain,
    _validate_tuning_workspace,
    build_candidate_config_pair,
    seal_trial_result,
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
                        -10.0 + rank / 1000.0, 1000 - rank, 10.0, 2))
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
    def test_promotion_report_is_recomputed_not_only_self_hashed(
            self, load_trials, leaderboard, promoted):
        schedule = {
            "schedule_sha256": "1" * 64,
            "candidate_ids": ["c0"], "circuits": ["toy"], "seeds": [0]}
        load_trials.return_value = (schedule, [])
        leaderboard.return_value = [{"candidate_id": "c0", "valid": True}]
        promoted.return_value = ["c0"]
        expected = {
            "experiment_schema": 2,
            "protocol_id": "resident-ga-native-v1",
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
            self.assertEqual(setting["native_abi_version"], 3)
            self.assertEqual(len(setting["native_wheel_sha256"]), 64)
            self.assertEqual(
                setting["rng_version"], "python-random-mt19937-v1")
            self.assertEqual(setting["algorithm_revision"], "native-ga-v1")
            self.assertEqual(setting["tuning_protocol_id"],
                             "resident-ga-native-v1")


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
                "native_abi_version": 3,
                "native_wheel_sha256": "a" * 64,
                "rng_version": "python-random-mt19937-v1",
                "operator_profile": "tuned",
                "init_engine": "sa",
                "formal_native": True,
                "tuning_protocol_id": "resident-ga-native-v1",
            }
            workspace = {
                "experiment_schema": 2,
                "protocol_id": "resident-ga-native-v1",
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
                "protocol_id": "resident-ga-native-v1",
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
                native_abi_version=3, native_wheel_sha256="a" * 64,
                compiler_and_flags={
                    "cxx_standard": 17, "openmp": False,
                    "fast_math": False},
                tuning_protocol_id="resident-ga-native-v1",
                rng_version="python-random-mt19937-v1",
                config_sha256=sha256_file(config), input_sha256="b" * 64,
                architecture_sha256="d" * 64, model_sha256="e" * 64,
                compiler_time_ns=2000, transition_decision_ns=1234,
                log_fidelity=-1.25, fidelity=math.exp(-1.25),
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
                "protocol_id": "resident-ga-native-v1",
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

            candidates_path.write_text('{"tampered":true}\n',
                                       encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "workspace evidence drift"):
                _validate_tuning_workspace(plan, "qmap154", root)


class TuningFinalSelectionGateTests(unittest.TestCase):
    @mock.patch("experiments_v2.tuning_runner._native_identity")
    @mock.patch("experiments_v2.tuning_runner.repository_snapshot")
    @mock.patch("experiments_v2.tuning_runner._candidate_map")
    @mock.patch("experiments_v2.tuning_runner._validate_schedule_chain")
    @mock.patch("experiments_v2.tuning_runner.load_phase_trials")
    @mock.patch("experiments_v2.tuning_runner._ranked_validation_selection")
    def test_final_selection_binds_replayed_ledger_and_current_shared_configs(
            self, ranked, load_trials, validate_chain, candidate_map,
            repository_snapshot, native_identity):
        repository_snapshot.return_value = {
            "root": "/repo", "commit": "a" * 40,
            "branch": "codex/test", "dirty": False,
        }
        native_identity.return_value = {"native_abi_version": 3}
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
                "native_identity": {"native_abi_version": 3},
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
                "protocol_id": "resident-ga-native-v1",
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
