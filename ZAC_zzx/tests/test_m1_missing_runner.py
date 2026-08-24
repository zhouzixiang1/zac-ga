"""Tests for the independent paper-native M1 cohort runner."""

from __future__ import annotations

import copy
import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.contracts import (  # noqa: E402
    RunManifest, sha256_file, stable_sha256)
from experiments_v2.m1_missing_runner import (  # noqa: E402
    SINGLE_THREAD_ENVIRONMENT, build_m1_cohort, build_parser, run_m1_cohort)
from experiments_v2.plan import load_experiment_plan  # noqa: E402
from experiments_v2.protocol import (  # noqa: E402
    ghost_policy_for_method, physicalization_policy_for_method,
    trace_protocol_for_method)
from tests.test_cli_v2 import PlanFixture  # noqa: E402


class M1MissingRunnerTests(unittest.TestCase):
    @staticmethod
    def _fixture(path: Path) -> PlanFixture:
        path.mkdir()
        return PlanFixture(path)

    @staticmethod
    def _git_init(root: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.invalid"],
            cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Fixture"], cwd=root,
            check=True)
        (root / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"], cwd=root,
            check=True)
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

    @staticmethod
    def _lineage(source_root: Path) -> Path:
        source_root.mkdir(parents=True, exist_ok=True)
        path = source_root / "workspace.json"
        payload = {
            "experiment_schema": 2,
            "protocol_id": "fixture-paper-native-m1-lineage",
            "root": str(source_root.resolve()),
        }
        payload["record_sha256"] = stable_sha256(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @staticmethod
    def _ready_source(source_root: Path, plan, *, source_status: str) -> Path:
        canonical = plan.load_suite(plan.datasets["zac"])[0]
        directory = source_root / "attempt"
        directory.mkdir(parents=True)
        manifest = {
            "experiment_schema": 2,
            "run_id": "paper-source",
            "dataset": "zac",
            "circuit": "toy",
            "method": "M1",
            "seed": 0,
            "repetition": 0,
            "run_kind": "smoke",
            "status": source_status,
            "input_sha256": sha256_file(Path(canonical.canonical_path)),
            "config_sha256": sha256_file(plan.resolved_config("M1", 0)),
            "architecture_sha256": sha256_file(plan.architecture_path),
            "model_sha256": sha256_file(plan.model_path),
            "compiler_time_ns": 123,
            "exit_code": 0,
            "warnings": [],
            "error": None,
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        with gzip.open(
                directory / "compiler_stats.json.gz", "wt",
                encoding="utf-8") as handle:
            json.dump({
                "compiler_module": "zac.zac",
                "routing_strategy": "maximalis_sort",
                "physicalization": "paper_native_unmodified",
                "ghost_repairs": 0,
                "ghost_splits": 0,
            }, handle)
        trace = (
            {"instructions": []}
            if source_status == "verifier_fail" else {
                "architecture_spec_path": str(plan.architecture_path),
                "instructions": [{
                    "type": "init", "id": 0,
                    "init_locs": [[0, 0, 0, 0]],
                }],
            })
        with gzip.open(
                directory / "trace.zair.json.gz", "wt",
                encoding="utf-8") as handle:
            json.dump(trace, handle)
        return directory

    @staticmethod
    def _add_second_canonical(fixture: PlanFixture) -> None:
        canonical_root = Path(
            fixture.datasets["zac"]["canonical_directory"])
        suite_path = canonical_root / "suite.manifest.json"
        rows = json.loads(suite_path.read_text(encoding="utf-8"))
        row = copy.deepcopy(rows[0])
        qasm = canonical_root / "toy_second.qasm"
        qasm.write_text(
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\n',
            encoding="utf-8")
        source = fixture.base / "zac-toy_second-source.qasm"
        source.write_text(qasm.read_text(encoding="utf-8"), encoding="utf-8")
        row.update(
            source_path=str(source),
            canonical_path=str(qasm),
            source_sha256=sha256_file(source),
            canonical_sha256=sha256_file(qasm),
        )
        rows.append(row)
        suite_path.write_text(json.dumps(rows), encoding="utf-8")

    @staticmethod
    def _fake_terminal_attempt(spec, *, verifier, scorer):
        directory = spec.output_root / f"fake-{spec.circuit}"
        directory.mkdir(parents=True)
        manifest = RunManifest.with_provenance(
            spec.repo_root,
            run_id=f"fake-{spec.circuit}",
            dataset=spec.dataset,
            circuit=spec.circuit,
            method=spec.method,
            seed=spec.seed,
            repetition=spec.repetition,
            run_kind=spec.run_kind,
            experiment_id=spec.experiment_id,
            status="compiler_error",
            command=list(spec.command),
            timeout_seconds=spec.timeout_seconds,
            input_sha256=sha256_file(spec.input_path),
            config_sha256=sha256_file(spec.config_path),
            architecture_sha256=sha256_file(spec.architecture_path),
            model_sha256=sha256_file(spec.model_path),
            trace_protocol=trace_protocol_for_method("M1"),
            ghost_policy=ghost_policy_for_method("M1"),
            physicalization_policy=physicalization_policy_for_method("M1"),
            artifact_dir=str(directory),
        )
        manifest.write(directory / "manifest.json")
        return manifest

    def test_parser_registers_three_worker_dry_run_entry(self):
        args = build_parser().parse_args([
            "--plan", "plan.json", "--dry-run"])
        self.assertEqual(args.workers, 3)
        self.assertTrue(args.dry_run)

    def test_audit_queue_partitions_ready_without_fresh_recompile(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            lineage = self._lineage(source)
            self._ready_source(source, plan, source_status="success")
            _, audit, entries = build_m1_cohort(
                plan, lineage_manifest=lineage,
                enforce_registered_cohort=False)
            self.assertEqual(audit["state_counts"], {"ready": 1})
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].action, "raw_replay")
            self.assertEqual(entries[0].source_status, "success")

    def test_ready_native_verifier_failure_is_replayed_and_never_compiled(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            self._git_init(fixture.repo)
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            lineage = self._lineage(source)
            self._ready_source(
                source, plan, source_status="verifier_fail")
            with mock.patch(
                    "experiments_v2.m1_missing_runner.run_attempt") as compile_call:
                report = run_m1_cohort(
                    plan, lineage_manifest=lineage,
                    output_root=base / "cohort", workers=3,
                    enforce_registered_cohort=False)
            compile_call.assert_not_called()
            self.assertTrue(report["complete"])
            self.assertEqual(report["planned_by_action"], {"raw_replay": 1})
            self.assertEqual(report["status_counts"], {"verifier_fail": 1})
            self.assertEqual(report["entries"][0]["source_status"],
                             "verifier_fail")

    def test_ready_replay_stage_finishes_before_missing_compile_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            self._add_second_canonical(fixture)
            self._git_init(fixture.repo)
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            lineage = self._lineage(source)
            self._ready_source(source, plan, source_status="success")

            from experiments_v2 import m1_missing_runner as module
            real_replay = module.replay_one_baseline_raw
            stage_order = []

            def replay(*args, **kwargs):
                stage_order.append("replay")
                return real_replay(*args, **kwargs)

            def compile_missing(spec, *, verifier, scorer):
                self.assertEqual(stage_order, ["replay"])
                stage_order.append("compile")
                return self._fake_terminal_attempt(
                    spec, verifier=verifier, scorer=scorer)

            with mock.patch(
                    "experiments_v2.m1_missing_runner.replay_one_baseline_raw",
                    side_effect=replay), mock.patch(
                    "experiments_v2.m1_missing_runner.run_attempt",
                    side_effect=compile_missing):
                report = run_m1_cohort(
                    plan, lineage_manifest=lineage,
                    output_root=base / "cohort", workers=3,
                    enforce_registered_cohort=False)
            self.assertEqual(stage_order, ["replay", "compile"])
            self.assertTrue(report["complete"])
            self.assertEqual(
                report["planned_by_action"],
                {"fresh_compile": 1, "raw_replay": 1})

    def test_missing_target_uses_only_m1_attempt_spec_and_is_resumable(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            commit = self._git_init(fixture.repo)
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            lineage = self._lineage(source)
            observed_environments = []

            def fake(spec, *, verifier, scorer):
                self.assertEqual(spec.method, "M1")
                self.assertEqual(spec.run_kind, "main")
                self.assertTrue(spec.require_clean_git)
                observed_environments.append(dict(spec.environment))
                return self._fake_terminal_attempt(
                    spec, verifier=verifier, scorer=scorer)

            destination = base / "cohort"
            with mock.patch(
                    "experiments_v2.cli._assert_formal_selection_gates"
                    ) as selection_gate, mock.patch(
                    "experiments_v2.m1_missing_runner.run_attempt",
                    side_effect=fake) as compile_call:
                first = run_m1_cohort(
                    plan, lineage_manifest=lineage,
                    output_root=destination, workers=3,
                    enforce_registered_cohort=False)
            selection_gate.assert_not_called()
            self.assertEqual(compile_call.call_count, 1)
            self.assertTrue(first["complete"])
            self.assertEqual(first["planned_by_action"], {"fresh_compile": 1})
            self.assertEqual(first["status_counts"], {"compiler_error": 1})
            self.assertTrue(all(
                observed_environments[0][key] == value
                for key, value in SINGLE_THREAD_ENVIRONMENT.items()))
            manifest = json.loads(Path(
                first["entries"][0]["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["git_commit"], commit)
            self.assertFalse(manifest["git_dirty"])

            with mock.patch(
                    "experiments_v2.m1_missing_runner.run_attempt") as rerun:
                second = run_m1_cohort(
                    plan, lineage_manifest=lineage,
                    output_root=destination, workers=3, resume=True,
                    enforce_registered_cohort=False)
            rerun.assert_not_called()
            self.assertTrue(second["complete"])
            with self.assertRaisesRegex(FileExistsError, "use --resume"):
                run_m1_cohort(
                    plan, lineage_manifest=lineage,
                    output_root=destination, workers=3, resume=False,
                    enforce_registered_cohort=False)

    def test_low_disk_pauses_before_any_fresh_compiler(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            self._git_init(fixture.repo)
            payload = fixture.payload()
            payload["minimum_free_bytes"] = 5 * (1 << 30)
            fixture.write_plan(payload)
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            lineage = self._lineage(source)
            with mock.patch(
                    "experiments_v2.m1_missing_runner._disk_free",
                    return_value=1), mock.patch(
                    "experiments_v2.m1_missing_runner.run_attempt") as compile_call:
                report = run_m1_cohort(
                    plan, lineage_manifest=lineage,
                    output_root=base / "cohort", workers=3,
                    enforce_registered_cohort=False)
            compile_call.assert_not_called()
            self.assertTrue(report["paused"])
            self.assertFalse(report["complete"])
            self.assertEqual(report["completed"], 0)
            self.assertIn("disk below pause threshold", report["paused_reason"])

    def test_non_dry_runner_rejects_dirty_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            self._git_init(fixture.repo)
            (fixture.repo / "tracked.txt").write_text(
                "dirty\n", encoding="utf-8")
            plan = load_experiment_plan(fixture.plan_path)
            lineage = self._lineage(base / "source")
            with self.assertRaisesRegex(RuntimeError, "clean Git commit"):
                run_m1_cohort(
                    plan, lineage_manifest=lineage,
                    output_root=base / "cohort", workers=3,
                    enforce_registered_cohort=False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
