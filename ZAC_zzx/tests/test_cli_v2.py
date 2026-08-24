"""Executable smoke and fail-closed tests for the Schema-2 experiment CLI."""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.cli import (  # noqa: E402
    UnifiedEvaluationGate,
    _attempt_spec,
    _manifest_paths,
    _run_matrix,
    _resume_manifest_is_current,
    command_run_main,
    command_verify_run,
    main,
)
from experiments_v2.contracts import (  # noqa: E402
    CanonicalCircuitManifest, RunManifest, load_run_manifest, sha256_file)
from experiments_v2.plan import effective_zac_setting, load_experiment_plan  # noqa: E402
from experiments_v2.protocol import (  # noqa: E402
    FORMAL_QUALITY_SEEDS,
    FORMAL_TIMING_REPETITIONS,
    ghost_policy_for_method,
    physicalization_policy_for_method,
    trace_protocol_for_method,
)
from experiments_v2.runner import AttemptSpec, run_attempt  # noqa: E402
from streaming.large_contract import (  # noqa: E402
    LARGE_CIRCUITS, QASMBENCH_COMMIT)
from zzx.algorithm_v2 import decay_lookahead_spec  # noqa: E402


ARCHITECTURE = {
    "storage_zones": [{
        "zone_id": 0,
        "slms": [{"id": 0, "site_seperation": [2, 2], "r": 4, "c": 4,
                  "location": [0, 0]}],
    }],
    "entanglement_zones": [{
        "zone_id": 0,
        "slms": [
            {"id": 1, "site_seperation": [4, 4], "r": 2, "c": 2,
             "location": [10, 0]},
            {"id": 2, "site_seperation": [4, 4], "r": 2, "c": 2,
             "location": [12, 0]},
        ],
    }],
    "aods": [{"id": 0, "site_seperation": 2, "r": 4, "c": 4}],
    "arch_range": [[0, -2], [20, 10]],
    "rydberg_range": [[[9, -1], [17, 9]]],
}


def algorithm_config(method: str, horizon: int) -> dict:
    horizon_spec = decay_lookahead_spec(
        8 if method == "ours_lk" else 0)
    return {
        "experiment_schema": 2,
        "method_id": method,
        "objective": "physical_log_fidelity",
        "lookahead_horizon": horizon_spec,
        "alpha_lookahead": 0.1,
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
    }


def compiler_stats(method: str) -> dict:
    return {
        "trace_protocol": trace_protocol_for_method(method),
        "ghost_policy": ghost_policy_for_method(method),
        "physicalization": physicalization_policy_for_method(method),
        "ghost_repairs": 0,
        "ghost_splits": 0,
    }


class PlanFixture:
    def __init__(self, base: Path):
        self.base = base
        self.repo = base / "repo"
        self.repo.mkdir()
        self.output = base / "results"
        self.architecture = base / "architecture.json"
        self.model = base / "model.json"
        self.architecture.write_text(json.dumps(ARCHITECTURE), encoding="utf-8")
        self.model.write_text(json.dumps({
            "experiment_schema": 2,
            "model": {
                "one_qubit_fidelity": 0.9997,
                "two_qubit_fidelity": 0.995,
                "idle_excitation_fidelity": 0.9975,
                "transfer_fidelity": 0.999,
                "coherence_time_us": 1.5e6,
                "transfer_duration_us": 15.0,
                "rydberg_duration_us": 0.36,
                "one_qubit_duration_us": 52.0,
                "movement_acceleration": 0.00275,
            },
        }), encoding="utf-8")
        configs = {
            "M1": {"experiment_schema": 2, "method_id": "M1"},
            "M2": {
                "compiler": "RoutingAwareCompiler",
                "deepening_factor": 0.6,
                "deepening_value": 0.2,
                "experiment_schema": 2,
                "fallback": False,
                "lookahead_factor": 0.2,
                "max_nodes": 50_000_000,
                "method_id": "iccad_qmap_3_2_astar",
                "mqt_core_version": "3.1.0",
                "mqt_qmap_version": "3.2.0",
                "reuse_level": 5.0,
            },
            "M3": algorithm_config("ours_nl", 0),
            "M4": algorithm_config("ours_lk", 2),
        }
        self.config_paths = {}
        for method, config in configs.items():
            path = base / f"{method}.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.config_paths[method] = path

        self.datasets = {}
        for name, kind in (("zac", "main"), ("large", "large")):
            canonical_directory = base / "canonical" / name
            canonical_directory.mkdir(parents=True)
            circuit_names = ("toy",) if kind == "main" else LARGE_CIRCUITS
            manifests = []
            sources = []
            for circuit in circuit_names:
                qubits = 177 if circuit == "bwt_n177" else 1
                qasm = canonical_directory / f"{circuit}.qasm"
                qasm.write_text(
                    'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
                    f'qreg q[{qubits}];\n',
                    encoding="utf-8")
                source = base / f"{name}-{circuit}-source.qasm"
                source.write_text(
                    qasm.read_text(encoding="utf-8"), encoding="utf-8")
                sources.append(str(source))
                manifests.append(CanonicalCircuitManifest(
                    experiment_schema=2,
                    source_path=str(source),
                    canonical_path=str(qasm),
                    source_sha256=sha256_file(source),
                    canonical_sha256=sha256_file(qasm),
                    qiskit_version=("1.2.4" if kind == "main" else "not-used"),
                    basis_gates=["cz", "u1", "u2", "u3"],
                    optimization_level=3 if kind == "main" else 0,
                    seed_transpiler=0,
                    qubits=qubits,
                    gates_1q=0,
                    gates_2q=0,
                    depth=0,
                    canonical_profile=("main_qiskit_1_2_4_opt3" if kind == "main"
                                       else "large_qasmbench_expand_only"),
                    upstream_commit=("" if kind == "main" else QASMBENCH_COMMIT),
                    canonicalizer_version=("qiskit-transpile-v1" if kind == "main"
                                           else "qasm2-stream-expander-v1"),
                ))
            suite = canonical_directory / "suite.manifest.json"
            suite.write_text(
                json.dumps([manifest.to_dict() for manifest in manifests]),
                encoding="utf-8")
            self.datasets[name] = {
                "kind": kind,
                "sources": sources,
                "canonical_directory": str(canonical_directory),
            }
            if kind == "large":
                self.datasets[name]["upstream_commit"] = QASMBENCH_COMMIT
        self.plan_path = base / "plan.json"
        self.write_plan()

    def payload(self):
        return {
            "experiment_schema": 2,
            "repo_root": str(self.repo),
            "output_root": str(self.output),
            "architecture": str(self.architecture),
            "model": str(self.model),
            "python": sys.executable,
            "minimum_free_bytes": 0,
            "methods": {
                method: {"config": str(path)}
                for method, path in self.config_paths.items()
            },
            "datasets": self.datasets,
        }

    def write_plan(self, payload=None):
        self.plan_path.write_text(json.dumps(payload or self.payload()), encoding="utf-8")


class TestCliPlan(unittest.TestCase):
    @mock.patch("experiments_v2.cli._atomic_json")
    @mock.patch("experiments_v2.cli._run_matrix")
    @mock.patch("experiments_v2.cli._assert_formal_selection_gates")
    def test_non_dry_main_requires_and_records_both_selection_gates(
            self, selection_gate, run_matrix, atomic_json):
        selection_gate.return_value = {
            "initial_placement_engine": "sa",
            "shared_candidate_id": "candidate",
        }
        run_matrix.return_value = {"experiment_schema": 2, "attempted": []}
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            report = command_run_main(
                plan, ["zac"], FORMAL_QUALITY_SEEDS,
                dry_run=False, resume=False)
        selection_gate.assert_called_once_with(plan)
        self.assertEqual(report["formal_selection"],
                         selection_gate.return_value)
        atomic_json.assert_called_once()

    def test_formal_resume_requires_same_clean_commit(self):
        repository = {"commit": "a" * 40, "dirty": False}
        current = RunManifest(
            run_id="r", dataset="d", circuit="c", method="M1",
            git_commit="a" * 40, git_dirty=False)
        self.assertTrue(_resume_manifest_is_current(current, repository))
        current.git_commit = "b" * 40
        self.assertFalse(_resume_manifest_is_current(current, repository))
        current.git_commit = "a" * 40
        current.git_dirty = True
        self.assertFalse(_resume_manifest_is_current(current, repository))
        self.assertFalse(_resume_manifest_is_current(
            current, {"commit": "unknown", "dirty": True}))

    def test_registered_truth_tables_resolve_inside_the_versioned_checkout(self):
        plan = load_experiment_plan(
            ROOT / "experiments_v2" / "experiment_plan_v2.json")
        for key in ("zac_truth", "iccad_truth"):
            path = (plan.path.parent / plan.reproduction[key]).resolve()
            self.assertTrue(path.is_file())
            self.assertTrue(path.is_relative_to(ROOT.parent.resolve()))

    def test_plan_preserves_virtual_environment_executable_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            venv = Path(directory) / "venv" / "bin"
            venv.mkdir(parents=True)
            python_link = venv / "python"
            python_link.symlink_to(Path(sys.executable))
            qmap_venv = Path(directory) / "qmap-venv" / "bin"
            qmap_venv.mkdir(parents=True)
            qmap_link = qmap_venv / "python"
            qmap_link.symlink_to(Path(sys.executable))
            payload = fixture.payload()
            payload["python"] = os.path.relpath(
                python_link, fixture.plan_path.parent)
            payload["qmap_python"] = os.path.relpath(
                qmap_link, fixture.plan_path.parent)
            fixture.write_plan(payload)

            plan = load_experiment_plan(fixture.plan_path)

            expected_python = os.path.abspath(
                os.fspath(plan.path.parent / payload["python"]))
            expected_qmap = os.path.abspath(
                os.fspath(plan.path.parent / payload["qmap_python"]))
            self.assertEqual(plan.python, expected_python)
            self.assertEqual(plan.qmap_python, expected_qmap)
            self.assertEqual(plan.methods["M1"].python,
                             expected_python)
            self.assertEqual(plan.methods["M2"].python,
                             expected_qmap)
            self.assertNotEqual(plan.python, str(python_link.resolve()))

    def test_canonicalize_reexecutes_the_plan_python_once(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            marker = Path(directory) / "reexec.log"
            wrapper = Path(directory) / "frozen-python"
            wrapper.write_text(
                "#!/bin/sh\n"
                f"printf 'invoked\\n' >> {shlex.quote(str(marker))}\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            payload = fixture.payload()
            payload["python"] = str(wrapper)
            fixture.write_plan(payload)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT)
            completed = subprocess.run(
                [sys.executable, "-m", "experiments_v2", "canonicalize-suite",
                 "--plan", str(fixture.plan_path), "--datasets", "zac"],
                cwd=ROOT, env=environment, text=True, capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "invoked\n")
            report = json.loads(completed.stdout)
            self.assertTrue(report["datasets"][0]["reused_immutable_suite"])

    def test_experiment_id_is_bound_to_git_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            subprocess.run(["git", "init", "-q"], cwd=fixture.repo, check=True)
            subprocess.run(["git", "config", "user.name", "Schema Two"],
                           cwd=fixture.repo, check=True)
            subprocess.run(["git", "config", "user.email",
                            "schema2@example.test"], cwd=fixture.repo, check=True)
            marker = fixture.repo / "marker.txt"
            marker.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=fixture.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "one"],
                           cwd=fixture.repo, check=True)
            plan = load_experiment_plan(fixture.plan_path)
            first = plan.experiment_id(plan.datasets["zac"])
            marker.write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=fixture.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "two"],
                           cwd=fixture.repo, check=True)
            second = plan.experiment_id(plan.datasets["zac"])
            self.assertNotEqual(first, second)

    def test_legacy_pilot_inventory_is_hash_locked_and_excluded(self):
        inventory = json.loads(
            (ROOT / "experiments_v2" / "legacy_pilot_inventory.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(inventory["classification"], "legacy/pilot")
        self.assertFalse(inventory["accepted_by_schema2_aggregator"])
        repo = ROOT.parent
        for artifact in inventory["artifacts"]:
            path = repo / artifact["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(sha256_file(path), artifact["sha256"])

    def test_module_help_lists_all_required_commands(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        completed = subprocess.run(
            [sys.executable, "-m", "experiments_v2", "--help"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        for command in (
            "reproduce-baselines", "canonicalize-suite", "run-coverage",
            "run-main", "run-timing", "run-ablation", "run-large",
            "verify-run", "aggregate", "aggregate-ablation",
        ):
            self.assertIn(command, completed.stdout)

    def test_extra_m3_m4_difference_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            config = json.loads(fixture.config_paths["M4"].read_text())
            config["iterations"] = 9
            fixture.config_paths["M4"].write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "配置差异"):
                load_experiment_plan(fixture.plan_path)

    def test_method_cannot_bypass_the_frozen_python_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            payload = fixture.payload()
            payload["methods"]["M3"]["python"] = "/bin/false"
            fixture.write_plan(payload)
            with self.assertRaisesRegex(ValueError, "frozen python interpreter"):
                load_experiment_plan(fixture.plan_path)

    def test_run_main_dry_run_uses_one_input_and_paired_seed_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main([
                    "run-main", "--plan", str(fixture.plan_path),
                    "--datasets", "zac", "--seeds", "0,1,2", "--dry-run",
                ])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(len(report["commands"]), 8)
            self.assertEqual(report["workers"], 1)
            self.assertFalse(report["parallel_execution"])
            inputs = {
                row["command"][row["command"].index("--input") + 1]
                for row in report["commands"]
            }
            self.assertEqual(len(inputs), 1)
            plan = load_experiment_plan(fixture.plan_path)
            m3 = json.loads(plan.resolved_config("M3", 1).read_text())
            m4 = json.loads(plan.resolved_config("M4", 1).read_text())
            self.assertEqual(effective_zac_setting(m3)["seed"], 1)
            self.assertEqual(effective_zac_setting(m4)["seed"], 1)
            plan.validate_resolved_pair(1)

    def test_run_main_resume_skips_exact_frozen_attempt_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            canonical = plan.load_suite(plan.datasets["zac"])[0]
            spec = _attempt_spec(
                plan, plan.datasets["zac"], canonical, "M1", 0, 0, "main")
            artifact = (fixture.output / "runs" / "main" / "zac" /
                        "existing")
            artifact.mkdir(parents=True)
            manifest = RunManifest(
                run_id="existing", dataset="zac", circuit="toy", method="M1",
                seed=0, repetition=0, run_kind="main",
                experiment_id=spec.experiment_id, status="compiler_error",
                input_sha256=sha256_file(spec.input_path),
                config_sha256=sha256_file(spec.config_path),
                architecture_sha256=sha256_file(spec.architecture_path),
                model_sha256=sha256_file(spec.model_path),
                trace_protocol=trace_protocol_for_method("M1"),
                ghost_policy=ghost_policy_for_method("M1"),
                physicalization_policy=physicalization_policy_for_method("M1"),
                artifact_dir=str(artifact),
            )
            manifest.write(artifact / "manifest.json")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main([
                    "run-main", "--plan", str(fixture.plan_path),
                    "--datasets", "zac", "--seeds", "0,1,2",
                    "--resume", "--dry-run",
                ])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(len(report["skipped_existing"]), 1)
            self.assertEqual(len(report["commands"]), 7)

    @mock.patch("experiments_v2.cli.run_attempt")
    @mock.patch("experiments_v2.cli.repository_snapshot")
    @mock.patch("experiments_v2.cli._assert_reproduction_gate")
    def test_interrupted_main_resume_closes_one_complete_manifest_registry(
            self, reproduction_gate, repository_snapshot_mock, run_attempt_mock):
        del reproduction_gate
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            dataset = plan.datasets["zac"]
            canonical = plan.load_suite(dataset)[0]
            repository_snapshot_mock.return_value = {
                "commit": "a" * 40, "dirty": False,
            }

            interrupted_spec = _attempt_spec(
                plan, dataset, canonical, "M1", 0, 0, "main")
            interrupted_dir = (fixture.output / "runs" / "main" / "zac" /
                               "interrupted-complete")
            interrupted_dir.mkdir(parents=True)
            interrupted = RunManifest(
                run_id="interrupted", dataset="zac", circuit="toy",
                method="M1", seed=0, repetition=0, run_kind="main",
                experiment_id=interrupted_spec.experiment_id,
                status="compiler_error", git_commit="a" * 40,
                git_dirty=False,
                input_sha256=sha256_file(interrupted_spec.input_path),
                config_sha256=sha256_file(interrupted_spec.config_path),
                architecture_sha256=sha256_file(
                    interrupted_spec.architecture_path),
                model_sha256=sha256_file(interrupted_spec.model_path),
                trace_protocol=trace_protocol_for_method("M1"),
                ghost_policy=ghost_policy_for_method("M1"),
                physicalization_policy=physicalization_policy_for_method("M1"),
                artifact_dir=str(interrupted_dir),
            )
            interrupted.write(interrupted_dir / "manifest.json")

            counter = 0

            def complete(spec, **_kwargs):
                nonlocal counter
                counter += 1
                artifact = (spec.output_root /
                            f"resumed-{counter:02d}-{spec.method}-{spec.seed}")
                artifact.mkdir(parents=True)
                manifest = RunManifest(
                    run_id=f"resumed-{counter:02d}", dataset=spec.dataset,
                    circuit=spec.circuit, method=spec.method, seed=spec.seed,
                    repetition=spec.repetition, run_kind=spec.run_kind,
                    experiment_id=spec.experiment_id, status="compiler_error",
                    git_commit="a" * 40, git_dirty=False,
                    input_sha256=sha256_file(spec.input_path),
                    config_sha256=sha256_file(spec.config_path),
                    architecture_sha256=sha256_file(spec.architecture_path),
                    model_sha256=sha256_file(spec.model_path),
                    trace_protocol=trace_protocol_for_method(spec.method),
                    ghost_policy=ghost_policy_for_method(spec.method),
                    physicalization_policy=physicalization_policy_for_method(
                        spec.method),
                    artifact_dir=str(artifact),
                )
                manifest.write(artifact / "manifest.json")
                return manifest

            run_attempt_mock.side_effect = complete
            jobs = [("M1", 0, 0), ("M2", 0, 0)]
            jobs.extend((method, seed, 0)
                        for seed in FORMAL_QUALITY_SEEDS
                        for method in ("M3", "M4"))
            report = _run_matrix(
                plan, [dataset], jobs, phase="main", resume=True,
                dry_run=False)
            self.assertEqual(report["planned_jobs"], 8)
            self.assertEqual(len(report["cohort_manifests"]), 8)
            self.assertEqual(len(report["attempted"]), 7)
            self.assertEqual(len(report["skipped_existing"]), 1)
            self.assertEqual(
                report["skipped_existing"][0]["manifest"],
                str((interrupted_dir / "manifest.json").resolve()))
            self.assertEqual(
                [row["identity"] for row in report["cohort_manifests"]],
                sorted(row["identity"] for row in report["cohort_manifests"]))

    @mock.patch("experiments_v2.cli.run_attempt")
    @mock.patch("experiments_v2.cli._assert_reproduction_gate")
    def test_run_main_workers_overlap_isolated_attempts_and_keep_plan_order(
            self, reproduction_gate, run_attempt_mock):
        del reproduction_gate
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            dataset = plan.datasets["zac"]
            lock = threading.Lock()
            active = 0
            peak = 0

            def complete(spec, **_kwargs):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.03)
                    artifact = (
                        spec.output_root /
                        f"parallel-{spec.method}-s{spec.seed}-r{spec.repetition}")
                    artifact.mkdir(parents=True)
                    manifest = RunManifest(
                        run_id=artifact.name, dataset=spec.dataset,
                        circuit=spec.circuit, method=spec.method,
                        seed=spec.seed, repetition=spec.repetition,
                        run_kind=spec.run_kind,
                        experiment_id=spec.experiment_id,
                        status="compiler_error", git_commit="a" * 40,
                        git_dirty=False,
                        input_sha256=sha256_file(spec.input_path),
                        config_sha256=sha256_file(spec.config_path),
                        architecture_sha256=sha256_file(spec.architecture_path),
                        model_sha256=sha256_file(spec.model_path),
                        trace_protocol=trace_protocol_for_method(spec.method),
                        ghost_policy=ghost_policy_for_method(spec.method),
                        physicalization_policy=physicalization_policy_for_method(
                            spec.method),
                        artifact_dir=str(artifact),
                    )
                    manifest.write(artifact / "manifest.json")
                    return manifest
                finally:
                    with lock:
                        active -= 1

            run_attempt_mock.side_effect = complete
            jobs = [("M1", 0, 0), ("M2", 0, 0)]
            jobs.extend(
                (method, seed, 0)
                for seed in FORMAL_QUALITY_SEEDS
                for method in ("M3", "M4"))
            report = _run_matrix(
                plan, [dataset], jobs, phase="main", resume=False,
                dry_run=False, workers=3)

            self.assertGreaterEqual(peak, 2)
            self.assertEqual(report["workers"], 3)
            self.assertTrue(report["parallel_execution"])
            expected_order = [
                (method, seed, repetition)
                for method, seed, repetition in jobs
            ]
            self.assertEqual(
                [(row["method"], row["seed"], row["repetition"])
                 for row in report["attempted"]],
                expected_order)
            paths = [row["manifest"] for row in report["attempted"]]
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(
                [row["identity"] for row in report["cohort_manifests"]],
                sorted(row["identity"] for row in report["cohort_manifests"]))

    def test_run_main_rejects_non_positive_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            with self.assertRaisesRegex(ValueError, "workers must be a positive"):
                command_run_main(
                    plan, ["zac"], FORMAL_QUALITY_SEEDS,
                    dry_run=True, workers=0)

    def test_run_main_rejects_duplicate_formal_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            with self.assertRaisesRegex(ValueError, "paired seeds exactly 0,1,2"):
                command_run_main(
                    plan, ["zac"], [0, 1, 2, 2], dry_run=True)

    def test_timing_dry_run_has_excluded_warmups_and_three_repetitions(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main([
                    "run-timing", "--plan", str(fixture.plan_path),
                    "--datasets", "zac", "--dry-run",
                ])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertTrue(report["warmups"]["excluded_from_statistics"])
            self.assertEqual(len(report["warmups"]["attempts"]), 4)
            commands = report["timed"]["commands"]
            self.assertEqual(len(commands), 12)
            for method in ("M1", "M2", "M3", "M4"):
                self.assertEqual(
                    sorted(row["repetition"] for row in commands
                           if row["method"] == method),
                    list(range(FORMAL_TIMING_REPETITIONS)),
                )

    def test_timing_resume_preserves_remaining_frozen_schedule_order(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            first_stdout = io.StringIO()
            with contextlib.redirect_stdout(first_stdout):
                code = main([
                    "run-timing", "--plan", str(fixture.plan_path),
                    "--datasets", "zac", "--dry-run",
                ])
            self.assertEqual(code, 0)
            first = json.loads(first_stdout.getvalue())
            commands = first["timed"]["commands"]
            completed = commands[len(commands) // 2]

            plan = load_experiment_plan(fixture.plan_path)
            dataset = plan.datasets[completed["dataset"]]
            canonical = next(
                item for item in plan.load_suite(dataset)
                if Path(item.canonical_path).stem == completed["circuit"])
            spec = _attempt_spec(
                plan, dataset, canonical, completed["method"], 0,
                completed["repetition"], "timing")
            artifact = (fixture.output / "runs" / "timing" /
                        completed["dataset"] / "existing")
            artifact.mkdir(parents=True)
            manifest = RunManifest(
                run_id="existing", dataset=completed["dataset"],
                circuit=completed["circuit"], method=completed["method"],
                seed=0, repetition=completed["repetition"], run_kind="timing",
                experiment_id=spec.experiment_id, status="compiler_error",
                input_sha256=sha256_file(spec.input_path),
                config_sha256=sha256_file(spec.config_path),
                architecture_sha256=sha256_file(spec.architecture_path),
                model_sha256=sha256_file(spec.model_path),
                package_versions={
                    "timing_schedule_sha256": first["schedule_sha256"],
                    "timing_schedule_order": str(completed["schedule_order"]),
                },
                trace_protocol=trace_protocol_for_method(completed["method"]),
                ghost_policy=ghost_policy_for_method(completed["method"]),
                physicalization_policy=physicalization_policy_for_method(
                    completed["method"]),
                artifact_dir=str(artifact),
            )
            manifest.write(artifact / "manifest.json")

            resumed_stdout = io.StringIO()
            with contextlib.redirect_stdout(resumed_stdout):
                code = main([
                    "run-timing", "--plan", str(fixture.plan_path),
                    "--datasets", "zac", "--resume", "--dry-run",
                ])
            self.assertEqual(code, 0)
            resumed = json.loads(resumed_stdout.getvalue())
            remaining_orders = [
                row["schedule_order"]
                for row in resumed["timed"]["commands"]
            ]
            self.assertEqual(
                remaining_orders,
                [row["schedule_order"] for row in commands
                 if row["schedule_order"] != completed["schedule_order"]],
            )
            self.assertEqual(
                [row["schedule_order"]
                 for row in resumed["timed"]["skipped_existing"]],
                [completed["schedule_order"]],
            )
            skipped = resumed["timed"]["skipped_existing"][0]
            manifest_path = (artifact / "manifest.json").resolve()
            self.assertEqual(skipped["manifest"], str(manifest_path))
            self.assertEqual(
                skipped["manifest_sha256"], sha256_file(manifest_path))

    def test_timing_refuses_tampered_immutable_schedule(self):
        from experiments_v2.runtime_benchmark import build_balanced_schedule

        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            schedule = build_balanced_schedule(
                ["zac/toy"], repetitions=FORMAL_TIMING_REPETITIONS)
            schedule["jobs"][0]["repetition"] = FORMAL_TIMING_REPETITIONS
            schedule_path = fixture.output / "timing" / "randomized_schedule.json"
            schedule_path.parent.mkdir(parents=True)
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main([
                    "run-timing", "--plan", str(fixture.plan_path),
                    "--datasets", "zac", "--dry-run",
                ])
            self.assertEqual(code, 2)
            self.assertIn("schedule SHA256 seal mismatch", stderr.getvalue())

    def test_run_large_dry_run_is_an_explicit_non_executable_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main([
                    "run-large", "--plan", str(fixture.plan_path),
                    "--datasets", "large", "--methods", "all", "--dry-run",
                ])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertFalse(report["streaming_compiler_integrated"])
            self.assertFalse(report["support_claim_eligible"])
            self.assertEqual(report["execution_status"], "blocked_not_integrated")
            self.assertEqual(report["commands"], [])
            self.assertEqual(len(report["planned_attempts"]), 7 * 4)
            self.assertTrue(all(row["blocked"] for row in report["planned_attempts"]))
            self.assertTrue(all(row["execution_command"] is None
                                for row in report["planned_attempts"]))
            self.assertFalse(
                report["large_contract"]["streaming_compiler_integrated"])

    def test_run_large_formal_execution_fails_before_creating_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main([
                    "run-large", "--plan", str(fixture.plan_path),
                    "--datasets", "large", "--methods", "M4",
                ])
            self.assertEqual(code, 2)
            self.assertIn("streaming_compiler_integrated=false", stderr.getvalue())
            self.assertFalse((fixture.output / "runs" / "large").exists())

    def test_generic_method_driver_spec_cannot_be_used_for_large(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            dataset = plan.datasets["large"]
            canonical = plan.load_suite(dataset)[0]
            with self.assertRaisesRegex(RuntimeError, "forbidden for Large"):
                _attempt_spec(
                    plan, dataset, canonical, "M1", 0, 0, "large")

    def test_non_schema2_manifest_is_never_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "legacy"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "experiment_schema": 1,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-Schema-2"):
                _manifest_paths(root)


class TestUnifiedEvaluationGate(unittest.TestCase):
    def test_compiler_ghost_repairs_are_summed_across_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            with gzip.open(
                    artifact / "compiler_stats.json.gz", "wt",
                    encoding="utf-8") as handle:
                json.dump({
                    "decision_log": [
                        {"stay": 2, "return": 1, "ghost_fix": 1},
                        {"stay": 3, "return": 2, "ghost_fix": 4},
                    ],
                    "ghost_splits": 2,
                }, handle)
            counters = UnifiedEvaluationGate._compiler_counters(artifact)
            self.assertEqual(counters["ghost_repairs"], 5)
            self.assertEqual(counters["ghost_splits"], 2)

    def test_m2_native_ghost_is_recorded_without_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            canonical_path = Path(
                fixture.datasets["zac"]["canonical_directory"]
            ) / "toy.qasm"
            canonical_path.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n',
                encoding="utf-8",
            )
            suite_path = canonical_path.parent / "suite.manifest.json"
            suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
            suite_payload[0].update({
                "canonical_sha256": sha256_file(canonical_path),
                "qubits": 2,
            })
            suite_path.write_text(json.dumps(suite_payload), encoding="utf-8")
            plan = load_experiment_plan(fixture.plan_path)
            canonical = plan.load_suite(plan.datasets["zac"])[0]
            artifact = Path(directory) / "attempt"
            artifact.mkdir()
            native = (
                "atom (0, 0) atom0\n"
                "atom (2, 0) atom1\n"
                "@+ load atom0\n"
                "@+ move (4, 0) atom0\n"
                "@+ store atom0\n"
            )
            (artifact / "trace.na").write_text(native, encoding="utf-8")

            gate = UnifiedEvaluationGate(
                plan, canonical, "M2", write_artifacts=False)
            self.assertTrue(gate.verifier(artifact)["ok"])
            metrics = gate.scorer(artifact)
            self.assertEqual(metrics["ghost_hits"], 1)
            self.assertEqual(
                metrics["physicalization_policy"],
                physicalization_policy_for_method("M2"),
            )
            self.assertTrue(any(
                "recorded without baseline repair" in warning
                for warning in metrics["warnings"]
            ))
            self.assertEqual(
                (artifact / "trace.na").read_text(encoding="utf-8"), native)

    def test_m2_observed_transition_ledger_comes_from_native_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            canonical_path = Path(
                fixture.datasets["zac"]["canonical_directory"]
            ) / "toy.qasm"
            canonical_path.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
                'qreg q[2];\ncz q[0],q[1];\n',
                encoding="utf-8",
            )
            suite_path = canonical_path.parent / "suite.manifest.json"
            suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
            suite_payload[0].update({
                "canonical_sha256": sha256_file(canonical_path),
                "qubits": 2,
                "gates_2q": 1,
                "depth": 1,
            })
            suite_path.write_text(json.dumps(suite_payload), encoding="utf-8")
            plan = load_experiment_plan(fixture.plan_path)
            canonical = plan.load_suite(plan.datasets["zac"])[0]
            artifact = Path(directory) / "attempt-ledger"
            artifact.mkdir()
            (artifact / "trace.na").write_text(
                "atom (10, 0) atom0\n"
                "atom (12, 0) atom1\n"
                "@+ cz zone_cz0\n",
                encoding="utf-8",
            )
            gate = UnifiedEvaluationGate(
                plan, canonical, "M2", write_artifacts=False)
            self.assertTrue(gate.verifier(artifact)["ok"])
            metrics = gate.scorer(artifact)
            self.assertEqual(metrics["observed_transition_count"], 0)
            self.assertEqual(
                metrics["observed_transition_layer_ledger_source"],
                "normalized_qmap_placement_trace")
            self.assertEqual(
                len(metrics["observed_transition_layer_ledger_sha256"]), 64)

    def test_normalize_score_and_gate_ledger_are_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            canonical = plan.load_suite(plan.datasets["zac"])[0]
            artifact = Path(directory) / "attempt"
            artifact.mkdir()
            (artifact / "trace.zair.json").write_text(json.dumps({
                "architecture_spec_path": str(fixture.architecture),
                "instructions": [{
                    "type": "init", "id": 0,
                    "init_locs": [[0, 0, 0, 0]],
                }],
            }), encoding="utf-8")
            gate = UnifiedEvaluationGate(plan, canonical, "M3")
            self.assertTrue(gate.verifier(artifact)["ok"])
            metrics = gate.scorer(artifact)
            self.assertEqual(metrics["log_fidelity"], 0.0)
            self.assertEqual(metrics["move_batches"], 0)
            self.assertTrue((artifact / "canonical_trace.jsonl.gz").is_file())
            self.assertTrue((artifact / "fidelity.json").is_file())

            mismatched = CanonicalCircuitManifest(
                **{**canonical.to_dict(), "gates_1q": 1})
            with self.assertRaisesRegex(ValueError, "gate ledger mismatch"):
                UnifiedEvaluationGate(plan, mismatched, "M3",
                                      write_artifacts=False).verifier(artifact)

            # Matching aggregate counts are insufficient: the logical qubit
            # ledger must also be identical to the canonical circuit.
            logical_qasm = Path(directory) / "logical.qasm"
            logical_qasm.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n'
                'u3(0,0,0) q[0];\n', encoding="utf-8")
            logical_manifest = CanonicalCircuitManifest(
                **{
                    **canonical.to_dict(),
                    "canonical_path": str(logical_qasm),
                    "canonical_sha256": sha256_file(logical_qasm),
                    "qubits": 2,
                    "gates_1q": 1,
                })
            (artifact / "trace.zair.json").write_text(json.dumps({
                "instructions": [
                    {"type": "init", "id": 0,
                     "init_locs": [[0, 0, 0, 0], [1, 0, 0, 1]]},
                    {"type": "1qGate", "id": 1,
                     "begin_time": 0, "end_time": 52,
                     "gates": [{"name": "u3", "q": 1}]},
                ],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "logical gate ledger mismatch"):
                UnifiedEvaluationGate(
                    plan, logical_manifest, "M3",
                    write_artifacts=False).verifier(artifact)

            # Per-qubit gate counts and CZ partners can still match while a 1Q
            # gate crosses a CZ dependency.  The ledger must retain that order.
            ordered_qasm = Path(directory) / "ordered.qasm"
            ordered_qasm.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n'
                'u3(0,0,0) q[0];\ncz q[0],q[1];\n', encoding="utf-8")
            ordered_manifest = CanonicalCircuitManifest(
                **{
                    **canonical.to_dict(),
                    "canonical_path": str(ordered_qasm),
                    "canonical_sha256": sha256_file(ordered_qasm),
                    "qubits": 2,
                    "gates_1q": 1,
                    "gates_2q": 1,
                })
            (artifact / "trace.zair.json").write_text(json.dumps({
                "instructions": [
                    {"type": "init", "id": 0,
                     "init_locs": [[0, 1, 0, 0], [1, 2, 0, 0]]},
                    {"type": "rydberg", "id": 1,
                     "begin_time": 0, "end_time": 0.36,
                     "gates": [{"q0": 0, "q1": 1}]},
                    {"type": "1qGate", "id": 2,
                     "begin_time": 0.36, "end_time": 52.36,
                     "gates": [{"name": "u3", "q": 0}]},
                ],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "logical gate ledger mismatch"):
                UnifiedEvaluationGate(
                    plan, ordered_manifest, "M3",
                    write_artifacts=False).verifier(artifact)

    def test_runner_success_is_sealed_only_after_unified_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = load_experiment_plan(fixture.plan_path)
            canonical = plan.load_suite(plan.datasets["zac"])[0]
            trace = json.dumps({
                "architecture_spec_path": str(fixture.architecture),
                "instructions": [{
                    "type": "init", "id": 0,
                    "init_locs": [[0, 0, 0, 0]],
                }],
            })
            script = (
                "import json,os,pathlib;"
                "p=pathlib.Path(os.environ['ZAC_RUN_DIR']);"
                f"(p/'trace.zair.json').write_text({trace!r});"
                f"(p/'compiler_stats.json').write_text("
                f"{json.dumps(compiler_stats('M3'))!r});"
                "(p/'compiler_timing.json').write_text("
                "json.dumps({'compiler_time_ns':123}))"
            )
            config = plan.resolved_config("M3", 0)
            spec = AttemptSpec(
                dataset="zac", circuit="toy", method="M3", seed=0,
                repetition=0, command=[sys.executable, "-c", script],
                output_root=fixture.output / "runs" / "smoke" / "zac",
                repo_root=fixture.repo,
                input_path=Path(canonical.canonical_path),
                config_path=config,
                architecture_path=plan.architecture_path,
                model_path=plan.model_path,
                minimum_free_bytes=0,
            )
            gate = UnifiedEvaluationGate(plan, canonical, "M3")
            manifest = run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)
            self.assertEqual(manifest.status, "success")
            sealed = load_run_manifest(
                Path(manifest.artifact_dir) / "manifest.json",
                require_success_metrics=True,
            )
            self.assertTrue(sealed.verifier_ok)
            self.assertEqual(sealed.log_fidelity, 0.0)
            self.assertEqual(sealed.compiler_time_ns, 123)
            self.assertFalse(
                (Path(manifest.artifact_dir) / "trace.zair.json").exists())
            self.assertTrue(
                (Path(manifest.artifact_dir) / "trace.zair.json.gz").is_file())

    def test_verify_run_recomputes_ood_and_exponential_sensitivity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            canonical_path = Path(
                fixture.datasets["zac"]["canonical_directory"]
            ) / "toy.qasm"
            canonical_path.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\n'
                'u3(0,0,0) q[0];\n',
                encoding="utf-8",
            )
            suite_path = canonical_path.parent / "suite.manifest.json"
            suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
            suite_payload[0]["canonical_sha256"] = sha256_file(canonical_path)
            suite_payload[0]["gates_1q"] = 1
            suite_payload[0]["depth"] = 1
            suite_path.write_text(json.dumps(suite_payload), encoding="utf-8")

            plan = load_experiment_plan(fixture.plan_path)
            canonical = plan.load_suite(plan.datasets["zac"])[0]
            trace = json.dumps({
                "architecture_spec_path": str(fixture.architecture),
                "instructions": [
                    {"type": "init", "id": 0,
                     "init_locs": [[0, 0, 0, 0]]},
                    {"type": "1qGate", "id": 1,
                     "begin_time": 1_500_000.0,
                     "end_time": 1_500_052.0,
                     "gates": [{"name": "u3", "q": 0}],
                     "locs": [[0, 0, 0, 0]]},
                ],
            })
            script = (
                "import json,os,pathlib;"
                "p=pathlib.Path(os.environ['ZAC_RUN_DIR']);"
                f"(p/'trace.zair.json').write_text({trace!r});"
                f"(p/'compiler_stats.json').write_text("
                f"{json.dumps(compiler_stats('M3'))!r});"
                "(p/'compiler_timing.json').write_text("
                "json.dumps({'compiler_time_ns':123}))"
            )
            spec = AttemptSpec(
                dataset="zac", circuit="toy", method="M3", seed=0,
                repetition=0, command=[sys.executable, "-c", script],
                output_root=fixture.output / "runs" / "smoke" / "zac",
                repo_root=fixture.repo,
                input_path=canonical_path,
                config_path=plan.resolved_config("M3", 0),
                architecture_path=plan.architecture_path,
                model_path=plan.model_path,
                minimum_free_bytes=0,
            )
            gate = UnifiedEvaluationGate(plan, canonical, "M3")
            manifest = run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)
            self.assertEqual(manifest.status, "success")
            self.assertTrue(manifest.fidelity_ood)
            self.assertIsNone(manifest.log_fidelity)
            self.assertIsNone(manifest.fidelity)
            self.assertIsNotNone(manifest.exponential_sensitivity_log_fidelity)
            self.assertIn("log_coherence_linear", manifest.fidelity_components)
            self.assertIsNone(
                manifest.fidelity_components["log_coherence_linear"])

            verified = command_verify_run(
                plan, Path(manifest.artifact_dir) / "manifest.json")
            self.assertTrue(verified["verified"])
            self.assertTrue(verified["fidelity_ood"])


if __name__ == "__main__":
    unittest.main()
