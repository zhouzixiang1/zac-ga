"""Mechanism and cohort tests for the isolated Schema-2 ablation track."""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.ablation import (  # noqa: E402
    ABLATION_VARIANTS, build_ablation_config, select_ablation_cohort,
    validate_ablation_config,
)
from experiments_v2.ablation_statistics import aggregate_ablation  # noqa: E402
from experiments_v2.contracts import (  # noqa: E402
    CanonicalCircuitManifest, RunManifest, stable_sha256)
from zac.ds.architecture import Architecture  # noqa: E402
from zzx.zcost import compatible_2d, greedy_phase_batches  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


def _base_config(method: str) -> dict:
    name = "ours_nl_v2.json" if method == "M3" else "ours_lk_v2.json"
    payload = json.loads((ROOT / "exp_setting" / name).read_text())
    payload["zac_setting"][0]["seed"] = 3
    return payload


def _manifest(name: str, gates_2q: int, digest_number: int
              ) -> CanonicalCircuitManifest:
    digest = f"{digest_number:064x}"
    return CanonicalCircuitManifest(
        experiment_schema=2,
        source_path=f"/source/{name}.qasm",
        canonical_path=f"/canonical/{name}.qasm",
        source_sha256=digest,
        canonical_sha256=digest,
        qiskit_version="1.2.4",
        basis_gates=["cz", "u1", "u2", "u3"],
        optimization_level=3,
        seed_transpiler=0,
        qubits=4,
        gates_1q=0,
        gates_2q=gates_2q,
        depth=gates_2q,
    )


class TestAblationConfig(unittest.TestCase):
    def test_registered_variants_cover_each_requested_mechanism(self):
        self.assertEqual(set(ABLATION_VARIANTS), {
            "h0", "h2_phase_coloring", "always_stay", "always_return",
            "adjacent_only", "lumped_greedy",
        })
        self.assertEqual(ABLATION_VARIANTS["h0"].base_method, "M3")
        self.assertEqual(
            ABLATION_VARIANTS["h2_phase_coloring"].routing_batcher,
            "coloring")
        self.assertEqual(
            ABLATION_VARIANTS["lumped_greedy"].fitness_phase_mode,
            "lumped_greedy")

    def test_ablation_wrapper_does_not_mutate_main_config(self):
        base = _base_config("M4")
        before = json.dumps(base, sort_keys=True)
        wrapper = build_ablation_config("always_stay", base)
        extracted, variant = validate_ablation_config(
            wrapper, expected_variant="always_stay", expected_method="M4")
        self.assertEqual(json.dumps(base, sort_keys=True), before)
        self.assertEqual(extracted, base)
        self.assertEqual(variant.decision_policy, "always_stay")
        self.assertNotIn("ablation_variant", base["zac_setting"][0])

    def test_mismatched_or_modified_ablation_controls_fail_closed(self):
        wrapper = build_ablation_config("adjacent_only", _base_config("M4"))
        wrapper["controls"]["decision_policy"] = "optimize"
        with self.assertRaisesRegex(ValueError, "controls differ"):
            validate_ablation_config(wrapper)

    def test_manifest_variant_is_required_and_track_isolated(self):
        identity = dict(
            run_id="r", dataset="zac18", circuit="c", method="M4",
            run_kind="ablation", experiment_id="frozen")
        with self.assertRaisesRegex(ValueError, "missing ablation_variant"):
            RunManifest(**identity).validate()
        RunManifest(
            **identity, ablation_variant="always_stay").validate()
        with self.assertRaisesRegex(ValueError, "forbidden"):
            RunManifest(
                **{**identity, "run_kind": "main"},
                ablation_variant="always_stay").validate()


class TestAblationCohort(unittest.TestCase):
    def test_hpca_uses_all_18(self):
        rows = [_manifest(f"h{i}", 10 + i, 100 - i) for i in range(18)]
        selected, report = select_ablation_cohort("zac18", rows)
        self.assertEqual(len(selected), 18)
        self.assertEqual(report["selection"], "all")
        self.assertEqual(
            [row.canonical_sha256 for row in selected],
            sorted(row.canonical_sha256 for row in rows))

    def test_qmap_selects_sha256_minimum_ten_per_2q_stratum(self):
        rows = []
        for i in range(52):
            rows.append(_manifest(f"small_{i}", 100, 1000 + (51 - i)))
        for i in range(51):
            rows.append(_manifest(f"medium_{i}", 800, 2000 + (50 - i)))
        for i in range(51):
            rows.append(_manifest(f"large_{i}", 2000, 3000 + (50 - i)))
        selected, report = select_ablation_cohort("qmap154", rows)
        self.assertEqual(len(selected), 30)
        self.assertEqual(report["selection"], "three_2q_strata_sha256_min10")
        for label, lower in (("le_300", 1000), ("301_1500", 2000),
                             ("gt_1500", 3000)):
            expected = [f"{value:064x}" for value in range(lower, lower + 10)]
            self.assertEqual(
                report["strata"][label]["canonical_sha256"], expected)


class TestAblationMechanisms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = json.loads(
            (ROOT / "hardware_spec" / "toy_architecture.json").read_text())
        cls.arch = Architecture(spec)
        cls.arch.preprocessing()

    def _run_policy(self, policy: str):
        initial = [(0, i, 0) for i in range(6)]
        schedule = [[[0, 1]], [[0, 2]], [[3, 4]]]
        placer = ResidentPlacer(
            initial, seed=0, experiment_schema=2,
            method_id="ours_lk", objective="physical_log_fidelity",
            lookahead_horizon=2, engine="ga", fitness_cache=True,
            population_size=6, iterations=2,
            neighbors_per_solution=2, neighbor_sample_size=6,
            ablation_policy=policy,
        )
        placer.run(self.arch, [initial], schedule, True,
                   [set() for _ in schedule])
        return placer

    def test_stay_return_and_adjacent_policies_change_real_decisions(self):
        stay = self._run_policy("always_stay").decision_log[0]
        adjacent = self._run_policy("adjacent_only").decision_log[0]
        always = self._run_policy("always_return").decision_log[0]
        self.assertEqual((stay["stay"], stay["return"]), (1, 0))
        # q0 participates in the immediate next layer and may remain only in the
        # adjacent-only arm; always-RETURN sends both q0 and q1 to storage.
        self.assertEqual((adjacent["eligible_decisions"], adjacent["return"]),
                         (1, 1))
        self.assertEqual((always["eligible_decisions"], always["return"]),
                         (2, 2))
        self.assertEqual(always["ablation_policy"], "always_return")

    def test_always_return_also_returns_the_terminal_residents(self):
        placer = self._run_policy("always_return")
        self.assertEqual(placer.decision_log[-1]["stay"], 0)
        self.assertGreater(placer.decision_log[-1]["return"], 0)

    def test_greedy_batcher_is_deterministic_and_physically_compatible(self):
        legs = [
            (3.0, 0.0, 0.0, 3.0, 0.0),
            (2.0, 1.0, 1.0, 2.0, 1.0),
            (1.0, 2.0, 2.0, 1.0, 2.0),
        ]
        first = greedy_phase_batches(legs)
        second = greedy_phase_batches(legs)
        self.assertEqual(first, second)
        self.assertEqual(first[2], "greedy-maximal")
        vectors = [(leg[1], leg[3], leg[2], leg[4]) for leg in legs]
        for batch in first[1]:
            for pos, i in enumerate(batch):
                for j in batch[pos + 1:]:
                    self.assertTrue(compatible_2d(vectors[i], vectors[j]))


def _success_run(circuit: str, variant: str, seed: int, *,
                 run_id: str | None = None, ood: bool = False) -> RunManifest:
    method = ABLATION_VARIANTS[variant].base_method
    log_fidelity = -1.0 - (0.1 if circuit == "c1" else 0.0) \
        - (0.2 if variant == "h2_phase_coloring" else 0.0) - seed * 0.01
    components = {
        "log_one_qubit_gate": log_fidelity if not ood else -0.1,
        "log_two_qubit_gate": 0.0,
        "log_idle_excitation": 0.0,
        "log_atom_transfer": 0.0,
        "log_coherence_linear": None if ood else 0.0,
    }
    return RunManifest(
        run_id=run_id or f"{circuit}-{variant}-{seed}",
        dataset="zac18", circuit=circuit, method=method,
        seed=seed, repetition=0, run_kind="ablation",
        ablation_variant=variant, experiment_id="frozen-ablation",
        status="success", git_commit="a" * 40, git_dirty=False,
        machine={"hostname": "test-machine", "logical_cpus": 8},
        input_sha256=stable_sha256(circuit),
        config_sha256=stable_sha256([variant, seed]),
        architecture_sha256="b" * 64, model_sha256="c" * 64,
        compiler_time_ns=int((10 + seed) * 1e9),
        cpu_time_ns=int((9 + seed) * 1e9),
        log_fidelity=None if ood else log_fidelity,
        fidelity=None if ood else math.exp(log_fidelity),
        fidelity_components=components,
        fidelity_ood=ood,
        exponential_sensitivity_log_fidelity=-1.5 if ood else log_fidelity,
        exponential_sensitivity_fidelity=(
            math.exp(-1.5) if ood else math.exp(log_fidelity)),
        duration_us=100.0, qubits=4,
        expected_gates_1q=0, expected_gates_2q=2,
        observed_gates_1q=0, observed_gates_2q=2,
        expected_gate_ledger_sha256="d" * 64,
        observed_gate_ledger_sha256="d" * 64,
        move_batches=4 + seed, move_time_us=50.0 + seed,
        ghost_hits=0, verifier_ok=True,
        artifact_dir=f"/artifacts/{circuit}/{variant}/{seed}",
    )


class TestAblationStatistics(unittest.TestCase):
    VARIANTS = ("h0", "h2_phase_coloring")

    def _write_matrix(self, base: Path, *, ood_key=None):
        paths = []
        for circuit in ("c0", "c1"):
            for variant in self.VARIANTS:
                for seed in range(5):
                    run = _success_run(
                        circuit, variant, seed,
                        ood=(ood_key == (circuit, variant, seed)))
                    path = base / f"{run.run_id}.json"
                    run.write(path)
                    paths.append(path)
        return paths

    def test_five_seed_medians_and_diagnostic_only_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_matrix(Path(directory))
            report = aggregate_ablation(
                paths, dataset="zac18", frozen_circuits=["c0", "c1"],
                experiment_id="frozen-ablation",
                expected_variants=self.VARIANTS)
        self.assertTrue(report["integrity"]["passed"])
        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["eligible_for_main_claim_gate"])
        self.assertNotIn("claim_gate", report)
        h0 = report["variants"]["h0"]
        self.assertEqual(h0["success"], {"valid": 2, "N": 2, "rate": 1.0})
        c0 = h0["circuits"][0]
        self.assertAlmostEqual(c0["seed_median"]["log_fidelity"], -1.02)
        self.assertEqual(c0["seed_median"]["move_batches"], 6.0)
        self.assertEqual(c0["seed_median"]["move_time_us"], 52.0)
        self.assertEqual(c0["seed_median"]["compiler_time_seconds"], 12.0)
        self.assertEqual(report["fully_paired_success"]["valid"], 2)
        comparison = report["comparisons"]["metrics"]["fidelity"][
            "h0_vs_h2_phase_coloring"]
        self.assertEqual(comparison["n"], 2)
        self.assertAlmostEqual(comparison["ratio"], math.exp(0.2))
        self.assertEqual(comparison["implementation"],
                         "scipy.stats.wilcoxon")
        self.assertIn("holm_p_value", comparison)
        self.assertTrue(report["comparisons"]["exploratory_only"])

    def test_missing_duplicate_and_failure_are_explicit_invalid_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = self._write_matrix(base)
            # Missing h0/c0 seed4.
            paths = [path for path in paths if path.name != "c0-h0-4.json"]
            # Duplicate h0/c0 seed0.
            duplicate = _success_run("c0", "h0", 0, run_id="duplicate-h0-c0-s0")
            duplicate_path = base / "duplicate.json"
            duplicate.write(duplicate_path)
            paths.append(duplicate_path)
            # Replace h2/c1 seed3 by a terminal failure.
            paths = [path for path in paths
                     if path.name != "c1-h2_phase_coloring-3.json"]
            failure = RunManifest(
                run_id="failed", dataset="zac18", circuit="c1", method="M4",
                seed=3, repetition=0, run_kind="ablation",
                ablation_variant="h2_phase_coloring",
                experiment_id="frozen-ablation", status="timeout",
                git_commit="a" * 40, git_dirty=False,
                machine={"hostname": "test-machine", "logical_cpus": 8},
                input_sha256=stable_sha256("c1"),
                config_sha256=stable_sha256(["h2_phase_coloring", 3]),
                architecture_sha256="b" * 64, model_sha256="c" * 64)
            failure_path = base / "failure.json"
            failure.write(failure_path)
            paths.append(failure_path)
            report = aggregate_ablation(
                paths, dataset="zac18", frozen_circuits=["c0", "c1"],
                experiment_id="frozen-ablation",
                expected_variants=self.VARIANTS)
        h0_c0 = report["variants"]["h0"]["circuits"][0]
        self.assertFalse(h0_c0["success_valid"])
        self.assertTrue(any("missing" in reason for reason in h0_c0["invalid_reasons"]))
        self.assertTrue(any("duplicate" in reason for reason in h0_c0["invalid_reasons"]))
        h2_c1 = report["variants"]["h2_phase_coloring"]["circuits"][1]
        self.assertFalse(h2_c1["success_valid"])
        self.assertIn("seed_3:status=timeout", h2_c1["invalid_reasons"])
        self.assertEqual(report["fully_paired_success"]["valid"], 0)

    def test_ood_success_remains_success_but_not_fidelity_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_matrix(
                Path(directory), ood_key=("c0", "h0", 2))
            report = aggregate_ablation(
                paths, dataset="zac18", frozen_circuits=["c0", "c1"],
                experiment_id="frozen-ablation",
                expected_variants=self.VARIANTS)
        h0 = report["variants"]["h0"]
        self.assertEqual(h0["success"]["valid"], 2)
        self.assertEqual(h0["fidelity"]["valid"], 1)
        self.assertFalse(h0["circuits"][0]["fidelity_valid"])

    def test_mixed_run_kind_and_experiment_id_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run = _success_run("c0", "h0", 0)
            path = base / "ablation.json"
            run.write(path)
            wrong_kind = _success_run("c0", "h0", 1)
            wrong_kind.run_kind = "main"
            wrong_kind.ablation_variant = ""
            kind_path = base / "main.json"
            wrong_kind.write(kind_path)
            with self.assertRaisesRegex(ValueError, "only run_kind=ablation"):
                aggregate_ablation(
                    [path, kind_path], dataset="zac18",
                    frozen_circuits=["c0"], experiment_id="frozen-ablation",
                    expected_variants=["h0"])
            wrong_id = _success_run("c0", "h0", 2)
            wrong_id.experiment_id = "other"
            id_path = base / "wrong-id.json"
            wrong_id.write(id_path)
            with self.assertRaisesRegex(ValueError, "experiment_id mismatch"):
                aggregate_ablation(
                    [path, id_path], dataset="zac18",
                    frozen_circuits=["c0"], experiment_id="frozen-ablation",
                    expected_variants=["h0"])


if __name__ == "__main__":
    unittest.main()
