"""Tests for the shared SA-vs-GA initial-placement gate."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.initial_placement_benchmark import (  # noqa: E402
    InitialPlacementTrial,
    build_initial_placement_schedule,
    select_initial_placement_engine,
)
from experiments_v2.initial_placement_runner import (  # noqa: E402
    _config_path,
    _expected_experiment_id,
    _payload_hash,
    _receipt_path,
    _seal_receipt,
    _validate_initial_selection_evidence,
    _validate_receipt,
    apply_initial_selection_to_plan,
    build_initial_placement_config,
    initial_config_core_sha256,
)
from experiments_v2.contracts import (  # noqa: E402
    CanonicalCircuitManifest,
    RunManifest,
    RunStatus,
    sha256_file,
)
from zzx.algorithm_v2 import decay_lookahead_spec  # noqa: E402


class InitialPlacementProtocolTests(unittest.TestCase):
    circuits = tuple(f"circuit_{index}" for index in range(9))

    @staticmethod
    def _trials(circuits, *, ga_delta=0.02, ga_speedup=2.0):
        rows = []
        for circuit in circuits:
            for method in ("M3", "M4"):
                for engine in ("sa", "ga"):
                    for seed in range(5):
                        is_ga = engine == "ga"
                        rows.append(InitialPlacementTrial(
                            circuit=circuit,
                            method=method,
                            engine=engine,
                            seed=seed,
                            status="success",
                            verifier_ok=True,
                            ghost_hits=0,
                            fallback=False,
                            log_fidelity=-1.0 + (ga_delta if is_ga else 0.0),
                            initial_placement_ns=(
                                int(1_000_000 / ga_speedup)
                                if is_ga else 1_000_000),
                            full_compile_ns=2_000_000,
                        ))
        return rows

    def test_schedule_is_deterministic_and_contains_exactly_180_jobs(self):
        first = build_initial_placement_schedule(self.circuits, schedule_seed=7)
        second = build_initial_placement_schedule(self.circuits, schedule_seed=7)
        self.assertEqual(first, second)
        self.assertEqual(first["expected_trials"], 180)
        self.assertEqual(len(first["jobs"]), 180)
        identities = {
            (row["circuit"], row["method"], row["engine"], row["seed"])
            for row in first["jobs"]
        }
        self.assertEqual(len(identities), 180)

    def test_switches_only_when_both_methods_are_non_regressing_and_faster(self):
        report = select_initial_placement_engine(
            self._trials(self.circuits), expected_circuits=self.circuits)
        self.assertTrue(report["complete"])
        self.assertTrue(report["switch_to_ga"])
        self.assertEqual(report["selected_engine"], "ga")
        self.assertTrue(report["methods"]["M3"]["quality_non_regressing"])
        self.assertTrue(report["methods"]["M4"]["ga_faster"])

        rows = self._trials(self.circuits)
        rows = [copy.copy(row) for row in rows]
        # Degrade GA only for M4.  M3 winning is insufficient for a joint switch.
        for index, row in enumerate(rows):
            if row.method == "M4" and row.engine == "ga":
                rows[index] = InitialPlacementTrial(
                    **{**row.__dict__, "log_fidelity": -1.05})
        report = select_initial_placement_engine(
            rows, expected_circuits=self.circuits)
        self.assertFalse(report["switch_to_ga"])
        self.assertEqual(report["selected_engine"], "sa")

    def test_incomplete_or_non_verified_ledger_never_switches(self):
        rows = self._trials(self.circuits)
        with self.assertRaisesRegex(ValueError, "incomplete initial-placement ledger"):
            select_initial_placement_engine(
                rows[:-1], expected_circuits=self.circuits)

        bad = rows[0]
        rows[0] = InitialPlacementTrial(
            **{**bad.__dict__, "status": "verifier_fail", "verifier_ok": False,
               "log_fidelity": None, "initial_placement_ns": None,
               "full_compile_ns": None})
        report = select_initial_placement_engine(
            rows, expected_circuits=self.circuits)
        self.assertFalse(report["complete"])
        self.assertFalse(report["switch_to_ga"])
        self.assertEqual(report["selected_engine"], "sa")

    def test_materialized_config_changes_only_registered_initial_controls(self):
        setting = {
            "experiment_schema": 2,
            "method_id": "ours_nl",
            "objective": "physical_log_fidelity",
            "lookahead_horizon": decay_lookahead_spec(0),
            "population_size": 6,
            "iterations": 8,
            "neighbors_per_solution": 2,
            "neighbor_sample_size": 24,
            "seed": 0,
            "placer": "resident",
            "engine": "ga",
            "routing_strategy": "coloring",
            "resyn": False,
            "fitness_cache": True,
            "alpha_lookahead": 0.1,
            "init_engine": "sa",
            "dir": "old/output",
        }
        wrapped = {"zac_setting": [setting], "animation": False}
        materialized = build_initial_placement_config(
            wrapped, method="M3", engine="ga", seed=4)
        resolved = materialized["zac_setting"][0]
        self.assertEqual(resolved["init_engine"], "ga")
        self.assertEqual(resolved["seed"], 4)
        self.assertIn("initial-placement/m3/ga", resolved["dir"])
        unchanged = set(setting) - {"init_engine", "seed", "dir"}
        self.assertEqual(
            {key: resolved[key] for key in unchanged},
            {key: setting[key] for key in unchanged})
        self.assertEqual(
            materialized["initial_placement"]["protocol_id"],
            "sa-vs-ga-initial-v1")

    @mock.patch(
        "experiments_v2.initial_placement_runner."
        "_validate_initial_selection_evidence")
    def test_apply_changes_only_shared_init_engine(self, evidence):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection = root / "selected_engine.json"
            selection.write_text("{}\n", encoding="utf-8")
            methods = {}
            payloads = {}
            for method, config_name, method_id, horizon in (
                    ("M3", "ours_nl.json", "ours_nl", 0),
                    ("M4", "ours_lk.json", "ours_lk", 8)):
                setting = {
                    "experiment_schema": 2,
                    "method_id": method_id,
                    "objective": "physical_log_fidelity",
                    "lookahead_horizon": decay_lookahead_spec(horizon),
                    "population_size": 6,
                    "iterations": 8,
                    "neighbors_per_solution": 2,
                    "neighbor_sample_size": 24,
                    "seed": 0,
                    "placer": "resident",
                    "engine": "ga",
                    "routing_strategy": "coloring",
                    "resyn": False,
                    "fitness_cache": True,
                    "alpha_lookahead": 0.1,
                    "init_engine": "sa",
                    "dir": f"output/{method}",
                }
                payload = {"zac_setting": [setting], "animation": False}
                path = root / config_name
                path.write_text(json.dumps(payload), encoding="utf-8")
                payloads[method] = payload
                methods[method] = SimpleNamespace(
                    config_path=path, payload=payload)
            schedule = {
                "sha256": "a" * 64,
                "method_config_core_sha256": {
                    method: initial_config_core_sha256(payload)
                    for method, payload in payloads.items()},
            }
            report = {
                "record_sha256": "b" * 64,
                "selected_engine": "ga",
            }
            evidence.return_value = (root, schedule, report)
            plan_path = root / "plan.json"
            plan_path.write_text("{}\n", encoding="utf-8")
            plan = SimpleNamespace(path=plan_path, methods=methods)
            applied = apply_initial_selection_to_plan(plan, selection)
            self.assertEqual(applied["selected_engine"], "ga")
            for method in ("M3", "M4"):
                after = json.loads(
                    methods[method].config_path.read_text(encoding="utf-8"))
                self.assertEqual(after["zac_setting"][0]["init_engine"], "ga")
                before = copy.deepcopy(payloads[method])
                before["zac_setting"][0]["init_engine"] = "ga"
                self.assertEqual(after, before)

    def test_receipt_reloads_manifest_and_rejects_identity_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "experiment_schema": 2,
                "method_id": "ours_nl",
                "objective": "physical_log_fidelity",
                "lookahead_horizon": decay_lookahead_spec(0),
                "population_size": 6,
                "iterations": 8,
                "neighbors_per_solution": 2,
                "neighbor_sample_size": 24,
                "seed": 0,
                "placer": "resident",
                "engine": "ga",
                "routing_strategy": "coloring",
                "resyn": False,
                "fitness_cache": True,
                "alpha_lookahead": 0.1,
            }), encoding="utf-8")
            canonical = CanonicalCircuitManifest(
                experiment_schema=2,
                source_path="source.qasm",
                canonical_path=str(root / "toy.qasm"),
                source_sha256="1" * 64,
                canonical_sha256="2" * 64,
                qiskit_version="1.2.4",
                basis_gates=["cz", "u1", "u2", "u3"],
                optimization_level=3,
                seed_transpiler=0,
                qubits=2,
                gates_1q=0,
                gates_2q=1,
                depth=1,
            )
            job = {
                "circuit": "toy", "method": "M3", "engine": "sa",
                "seed": 0, "ordinal": 0, "job_id": "job-0",
            }
            schedule = {
                "dataset": "qmap154",
                "sha256": "3" * 64,
                "repository": {"commit": "4" * 40},
                "canonical_input_sha256": {"toy": "2" * 64},
            }
            manifest_path = root / "attempt" / "manifest.json"
            manifest = RunManifest(
                run_id="initial-test", dataset="qmap154", circuit="toy",
                method="M3", seed=0, repetition=0, run_kind="smoke",
                experiment_id=_expected_experiment_id(schedule, job),
                status=RunStatus.COMPILER_ERROR.value,
                git_commit="4" * 40, git_dirty=False,
                input_sha256="2" * 64,
                config_sha256=sha256_file(config),
                architecture_sha256="5" * 64,
                model_sha256="6" * 64,
                artifact_dir=str(manifest_path.parent),
            )
            manifest.write(manifest_path)
            receipt_path = root / "receipt.json"
            _seal_receipt(
                receipt_path, schedule=schedule, job=job,
                config_path=config, manifest_path=manifest_path,
                canonical=canonical)
            _validate_receipt(
                receipt_path, schedule=schedule, job=job,
                config_path=config, canonical=canonical)

            original_receipt = receipt_path.read_bytes()
            projected = json.loads(original_receipt)
            projected["log_fidelity"] = -123.0
            projected["record_sha256"] = _payload_hash(projected)
            receipt_path.write_text(json.dumps(projected), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "projection drift"):
                _validate_receipt(
                    receipt_path, schedule=schedule, job=job,
                    config_path=config, canonical=canonical)
            receipt_path.write_bytes(original_receipt)

            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
            tampered["circuit"] = "different"
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            payload["attempt_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()).hexdigest()
            payload["record_sha256"] = _payload_hash(payload)
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "attempt identity drift"):
                _validate_receipt(
                    receipt_path, schedule=schedule, job=job,
                    config_path=config, canonical=canonical)

    @mock.patch(
        "experiments_v2.initial_placement_runner._validate_receipt")
    @mock.patch(
        "experiments_v2.initial_placement_runner._load_schedule")
    def test_selection_is_replayed_and_rejects_self_hashed_tampering(
            self, load_schedule, validate_receipt):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schedule = build_initial_placement_schedule(self.circuits)
            schedule.update({"dataset": "qmap154"})
            for job in schedule["jobs"]:
                job["job_id"] = f"job-{job['ordinal']:03d}"
                config = _config_path(root, job)
                receipt = _receipt_path(root, job)
                config.parent.mkdir(parents=True, exist_ok=True)
                receipt.parent.mkdir(parents=True, exist_ok=True)
                config.write_text("{}\n", encoding="utf-8")
                receipt.write_text("{}\n", encoding="utf-8")
            load_schedule.return_value = schedule
            trial_by_key = {
                (row.circuit, row.method, row.engine, row.seed): row
                for row in self._trials(self.circuits)
            }

            def receipt_for(_path, *, schedule, job, config_path):
                row = trial_by_key[(
                    job["circuit"], job["method"], job["engine"],
                    job["seed"])]
                return {
                    "status": row.status,
                    "verifier_ok": row.verifier_ok,
                    "ghost_hits": row.ghost_hits,
                    "fallback": row.fallback,
                    "log_fidelity": row.log_fidelity,
                    "initial_placement_ns": row.initial_placement_ns,
                    "full_compile_ns": row.full_compile_ns,
                }

            validate_receipt.side_effect = receipt_for
            report = select_initial_placement_engine(
                list(trial_by_key.values()),
                expected_circuits=self.circuits)
            report.update({
                "schedule_sha256": schedule["sha256"],
                "dataset": schedule["dataset"],
                "selected_engine": "sa",
                "switch_to_ga": False,
                "reason": "retain_sa_until_both_methods_pass",
            })
            report["record_sha256"] = _payload_hash(report)
            selection = root / "selected_engine.json"
            selection.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "replayed evidence"):
                _validate_initial_selection_evidence(selection)


if __name__ == "__main__":
    unittest.main()
