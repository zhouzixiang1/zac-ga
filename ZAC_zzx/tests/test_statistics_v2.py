"""Strict cohort and publication-gate tests for Schema-2 statistics."""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.contracts import RunManifest, RunStatus  # noqa: E402
from experiments_v2.statistics import (  # noqa: E402
    aggregate_experiment, paired_wilcoxon)


class ManifestFactory:
    def __init__(self, root: Path, dataset: str = "zac18",
                 experiment_id: str = "experiment-frozen-v2"):
        self.root = root
        self.dataset = dataset
        self.experiment_id = experiment_id
        self.counter = 0

    @staticmethod
    def _digest(label: str) -> str:
        import hashlib
        return hashlib.sha256(label.encode()).hexdigest()

    def add(self, circuit: str, method: str, run_kind: str, *, seed: int = 0,
            repetition: int = 0, status: str = RunStatus.SUCCESS.value,
            log_fidelity: float = -1.0, move_batches: int = 100,
            move_time_us: float = 1000.0,
            compiler_seconds: float = 1.0,
            experiment_id: str | None = None) -> Path:
        self.counter += 1
        success = status == RunStatus.SUCCESS.value
        run = RunManifest(
            run_id=f"run-{self.counter:05d}", dataset=self.dataset,
            circuit=circuit, method=method, seed=seed, repetition=repetition,
            run_kind=run_kind,
            experiment_id=experiment_id or self.experiment_id,
            status=status, git_commit="1" * 40, git_dirty=False,
            input_sha256=self._digest(f"input-{circuit}"),
            config_sha256=self._digest(f"config-{run_kind}-{method}-seed{seed}"),
            architecture_sha256="a" * 64, model_sha256="b" * 64,
            compiler_time_ns=int(compiler_seconds * 1e9) if success else None,
            log_fidelity=log_fidelity if success else None,
            fidelity=math.exp(log_fidelity) if success else None,
            fidelity_components={
                "log_one_qubit_gate": 0.0, "log_two_qubit_gate": log_fidelity,
                "log_idle_excitation": 0.0, "log_atom_transfer": 0.0,
                "log_coherence_linear": 0.0,
            } if success else {},
            duration_us=100.0 if success else None, qubits=20,
            expected_gates_1q=10 if success else None,
            expected_gates_2q=100 if success else None,
            observed_gates_1q=10 if success else None,
            observed_gates_2q=100 if success else None,
            expected_gate_ledger_sha256="c" * 64 if success else "",
            observed_gate_ledger_sha256="c" * 64 if success else "",
            move_batches=move_batches if success else None,
            move_time_us=move_time_us if success else None,
            ghost_hits=0 if success else None,
            verifier_ok=True if success else None,
        )
        path = self.root / f"{run.run_id}.json"
        run.write(path)
        return path


def add_quality(factory: ManifestFactory, circuit: str, *,
                omit_m4_seed: int | None = None) -> list[Path]:
    paths = []
    paths.append(factory.add(circuit, "M1", "main", log_fidelity=-1.0,
                             move_batches=100, move_time_us=1000))
    paths.append(factory.add(circuit, "M2", "main", log_fidelity=-0.9,
                             move_batches=90, move_time_us=900))
    for seed in range(5):
        paths.append(factory.add(
            circuit, "M3", "main", seed=seed, log_fidelity=-0.8,
            move_batches=85, move_time_us=850))
        if seed != omit_m4_seed:
            paths.append(factory.add(
                circuit, "M4", "main", seed=seed,
                log_fidelity=-0.8 + math.log(1.03),
                move_batches=80, move_time_us=800))
    return paths


class TestStrictStatistics(unittest.TestCase):
    def test_full_three_cohort_report_passes_all_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            circuits = [f"c{index:02d}" for index in range(10)]
            paths: list[Path] = []
            for circuit in circuits:
                for method in ("M1", "M2", "M3", "M4"):
                    paths.append(factory.add(circuit, method, "coverage"))
                paths.extend(add_quality(factory, circuit))
                for method in ("M1", "M2", "M3", "M4"):
                    for repetition in range(5):
                        paths.append(factory.add(
                            circuit, method, "timing", repetition=repetition,
                            compiler_seconds=1 + repetition / 10))
            report = aggregate_experiment(
                paths, dataset="zac18", frozen_circuits=circuits,
                bootstrap_iterations=200, bootstrap_seed=7)
            self.assertTrue(report["coverage"]["gate"]["passed"])
            self.assertEqual(report["main"]["valid"], 10)
            self.assertTrue(report["main"]["fidelity"]["gate"]["passed"])
            self.assertTrue(report["main"]["move"]["gate"]["passed"])
            self.assertTrue(report["timing"]["gate"]["passed"])
            self.assertTrue(report["claim_gate"]["passed"])
            comparison = report["main"]["fidelity"]["comparisons"]["M4_vs_M3"]
            self.assertGreaterEqual(comparison["ratio"], 1.02)
            self.assertIn("Bonferroni", comparison["simultaneous_method"])
            self.assertEqual((comparison["wins"], comparison["ties"],
                              comparison["losses"]), (10, 0, 0))
            quality = report["main"]["per_circuit_quality"]
            self.assertEqual(len(quality), 40)
            m4_c0 = next(row for row in quality
                         if row["circuit"] == "c00" and row["method"] == "M4")
            self.assertTrue(m4_c0["paired"])
            self.assertAlmostEqual(
                m4_c0["fidelity"], math.exp(-0.8 + math.log(1.03)))
            summary = report["main"]["paired_method_summary"]["M4"]
            self.assertEqual((summary["valid"], summary["N"]), (10, 10))
            self.assertAlmostEqual(
                summary["fidelity_geometric_mean"],
                math.exp(-0.8 + math.log(1.03)))
            self.assertEqual(summary["move_batches_median"], 80)
            self.assertEqual(
                set(report["main"]["stratum_method_summary"]), {"le32"})
            self.assertEqual(
                set(report["main"]["stratum_by_circuit"]), set(circuits))
            self.assertEqual(len(report["attempt_index"]), len(paths))
            self.assertEqual(
                {row["run_kind"] for row in report["attempt_index"]},
                {"coverage", "main", "timing"},
            )
            self.assertIn("log_idle_excitation", report["attempt_index"][0])
            self.assertIn("end_to_end_time_seconds", report["attempt_index"][0])
            self.assertIn("ghost_repairs", report["attempt_index"][0])

    def test_coverage_denominator_is_frozen_suite_not_observed_union(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory), dataset="qmap154")
            paths = [factory.add("present", method, "coverage")
                     for method in ("M1", "M2", "M3", "M4")]
            report = aggregate_experiment(
                paths, dataset="qmap154", frozen_circuits=["present", "missing"],
                bootstrap_iterations=10)
            self.assertEqual(report["coverage"]["methods"]["M4"]["N"], 2)
            self.assertEqual(report["coverage"]["methods"]["M4"]["valid"], 1)
            self.assertEqual(
                report["coverage"]["methods"]["M4"]["status_counts"]["missing"], 1)
            self.assertFalse(report["coverage"]["gate"]["passed"])

    def test_qmap_coverage_cannot_pass_with_a_missing_attempt_at_95_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory), dataset="qmap154")
            circuits = [f"c{index:02d}" for index in range(20)]
            paths = []
            for circuit in circuits:
                for method in ("M1", "M2", "M3", "M4"):
                    if circuit == circuits[-1] and method == "M4":
                        continue
                    paths.append(factory.add(circuit, method, "coverage"))
            report = aggregate_experiment(
                paths,
                dataset="qmap154",
                frozen_circuits=circuits,
                run_kind="coverage",
            )
            self.assertEqual(report["coverage"]["methods"]["M4"]["rate"], 0.95)
            self.assertTrue(
                report["coverage"]["methods"]["M4"]["meets_dataset_threshold"])
            self.assertFalse(
                report["coverage"]["gate"]
                ["all_attempts_have_one_explicit_terminal_record"])
            self.assertFalse(report["coverage"]["gate"]["passed"])

    def test_main_rejects_any_missing_paired_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths = add_quality(factory, "c0", omit_m4_seed=4)
            report = aggregate_experiment(
                paths, dataset="zac18", frozen_circuits=["c0"],
                bootstrap_iterations=10)
            self.assertEqual(report["main"]["valid"], 0)
            reason = report["main"]["methods"]["M4"]["invalid_reasons"]["c0"]
            self.assertIn("exactly five", reason)
            self.assertFalse(report["claim_gate"]["passed"])

    def test_bstar_and_move_best_baseline_identities_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths = add_quality(factory, "c0")
            report = aggregate_experiment(
                paths, dataset="zac18", frozen_circuits=["c0"],
                bootstrap_iterations=20)
            main = report["main"]
            self.assertEqual(main["fidelity"]["Bstar_method_by_circuit"]["c0"], "M2")
            self.assertEqual(
                main["move"]["metric_best_method_by_circuit"]["move_batches"]["c0"],
                "M2")
            self.assertIn("fidelity_Bstar_definition", main["move"])

    def test_move_claim_gate_uses_fidelity_bstar_not_metric_best(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths = [
                factory.add("c0", "M1", "main", log_fidelity=-1.0,
                            move_batches=50, move_time_us=500),
                factory.add("c0", "M2", "main", log_fidelity=-0.9,
                            move_batches=90, move_time_us=900),
            ]
            for seed in range(5):
                paths.append(factory.add(
                    "c0", "M3", "main", seed=seed, log_fidelity=-0.8,
                    move_batches=85, move_time_us=850))
                paths.append(factory.add(
                    "c0", "M4", "main", seed=seed,
                    log_fidelity=-0.8 + math.log(1.03),
                    move_batches=80, move_time_us=800))
            report = aggregate_experiment(
                paths, dataset="zac18", frozen_circuits=["c0"],
                bootstrap_iterations=20)
            move = report["main"]["move"]
            self.assertEqual(move["gate"]["baseline"],
                             "per-circuit fidelity B*")
            self.assertTrue(move["gate"]["passed"])
            self.assertGreater(
                move["metrics"]["move_batches"]
                ["vs_metric_best_baseline"]["ratio"], 1.0)

    def test_timing_par2_penalizes_failure_but_protocol_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths: list[Path] = []
            for method in ("M1", "M2", "M3", "M4"):
                for repetition in range(5):
                    status = (RunStatus.TIMEOUT.value
                              if method == "M1" and repetition == 4
                              else RunStatus.SUCCESS.value)
                    paths.append(factory.add(
                        "c0", method, "timing", repetition=repetition,
                        status=status, compiler_seconds=1))
            report = aggregate_experiment(
                paths, dataset="zac18", frozen_circuits=["c0"],
                bootstrap_iterations=10)
            self.assertTrue(report["timing"]["gate"]["passed"])
            self.assertAlmostEqual(
                report["timing"]["methods"]["M1"]["PAR2_seconds"],
                (4 + 1200) / 5)
            self.assertEqual(
                report["timing"]["methods"]["M1"]["status_counts"]["timeout"], 1)

    def test_timing_repeats_must_all_use_seed_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths: list[Path] = []
            for method in ("M1", "M2", "M3", "M4"):
                for repetition in range(5):
                    paths.append(factory.add(
                        "c0", method, "timing", repetition=repetition,
                        seed=1 if method == "M4" and repetition == 4 else 0,
                    ))
            report = aggregate_experiment(
                paths, dataset="zac18", frozen_circuits=["c0"],
                bootstrap_iterations=10)
            self.assertFalse(report["timing"]["gate"]["passed"])
            self.assertEqual(report["timing"]["methods"]["M4"]["valid"], 0)

    def test_timing_all_failures_cannot_pass_publication_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths = []
            for method in ("M1", "M2", "M3", "M4"):
                for repetition in range(5):
                    paths.append(factory.add(
                        "c0", method, "timing", repetition=repetition,
                        status=RunStatus.TIMEOUT.value))
            report = aggregate_experiment(
                paths, dataset="zac18", frozen_circuits=["c0"],
                bootstrap_iterations=10)
            self.assertFalse(report["timing"]["gate"]["passed"])
            self.assertFalse(
                report["timing"]["gate"]
                ["all_circuit_methods_have_successful_runtime"])
            self.assertEqual(report["timing"]["methods"]["M1"]["valid"], 0)

    def test_wilcoxon_is_scipy_and_never_a_sign_test_fallback(self):
        result = paired_wilcoxon([0.1, 0.2, 0.3])
        self.assertEqual(result["implementation"], "scipy.stats.wilcoxon")
        self.assertEqual(result["test"],
                         "Wilcoxon signed-rank, one-sided greater")
        self.assertNotIn("fallback", result["test"])

    def test_mixed_experiment_ids_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths = [factory.add("c0", "M1", "coverage"),
                     factory.add("c0", "M2", "coverage", experiment_id="other")]
            with self.assertRaisesRegex(ValueError, "exactly one non-empty experiment_id"):
                aggregate_experiment(paths, dataset="zac18", frozen_circuits=["c0"])

    def test_inferred_suite_is_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ManifestFactory(Path(directory))
            paths = [factory.add("c0", method, "coverage")
                     for method in ("M1", "M2", "M3", "M4")]
            report = aggregate_experiment(paths, dataset="zac18")
            self.assertFalse(report["frozen_suite"]["explicit"])
            self.assertFalse(report["claim_gate"]["passed"])


if __name__ == "__main__":
    unittest.main()
