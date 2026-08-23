"""Final three-sheet result contract tests."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.contracts import RunManifest, RunStatus  # noqa: E402
from experiments_v2.final_results import (  # noqa: E402
    OVERALL_LABEL,
    SHEET_NAMES,
    aggregate_final_results,
    write_final_results,
)
from experiments_v2.final_results_cli import (  # noqa: E402
    _record_hash,
    aggregate_from_indices,
    load_manifest_index,
    render_final_workbook,
    seal_manifest_index,
    seal_report_manifest_index,
)
from experiments_v2.protocol import (  # noqa: E402
    ghost_policy_for_method,
    physicalization_policy_for_method,
    trace_protocol_for_method,
)
from zzx.algorithm_v2 import (  # noqa: E402
    FORMAL_NATIVE_ABI_VERSION,
    FORMAL_NATIVE_ALGORITHM_REVISION,
    FORMAL_NATIVE_RNG_VERSION,
    FORMAL_NATIVE_TUNING_PROTOCOL_ID,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class FinalFixture:
    def __init__(self) -> None:
        self.suites = {"zac18": ["zac_toy"], "qmap154": ["qmap_toy"]}
        self.experiment_ids = {
            dataset: _digest(f"experiment-{dataset}") for dataset in self.suites
        }
        self.quality: list[RunManifest] = []
        self.timing: list[RunManifest] = []
        self._counter = 0
        self._build()

    def _manifest(self, dataset: str, circuit: str, method: str, run_kind: str,
                  *, seed: int = 0, repetition: int = 0,
                  log_fidelity: float = -1.0,
                  move_batches: int = 10, move_time_us: float = 20.0,
                  transition_ns: int | None = None,
                  full_compile_ns: int | None = None) -> RunManifest:
        self._counter += 1
        timed = run_kind == "timing"
        native = method in {"M3", "M4"}
        return RunManifest(
            run_id=f"formal-{self._counter:04d}", dataset=dataset,
            circuit=circuit, method=method, seed=seed, repetition=repetition,
            run_kind=run_kind, experiment_id=self.experiment_ids[dataset],
            status=RunStatus.SUCCESS.value,
            git_commit="1" * 40, git_dirty=False,
            input_sha256=_digest(f"input-{dataset}-{circuit}"),
            config_sha256=_digest(f"config-{method}-s{seed}"),
            architecture_sha256="a" * 64, model_sha256="b" * 64,
            algorithm_revision=(
                FORMAL_NATIVE_ALGORITHM_REVISION if native else ""),
            backend="native" if native else "",
            native_abi_version=FORMAL_NATIVE_ABI_VERSION if native else None,
            native_wheel_sha256="d" * 64 if native else "",
            compiler_and_flags=(
                {"cxx_standard": 17, "openmp": False, "fast_math": False}
                if native else {}),
            tuning_protocol_id=(
                FORMAL_NATIVE_TUNING_PROTOCOL_ID if native else ""),
            rng_version=FORMAL_NATIVE_RNG_VERSION if native else "",
            compiler_time_ns=full_compile_ns or 1_000_000_000,
            transition_decision_ns=transition_ns if timed else None,
            initial_placement_ns=100 if timed else None,
            routing_ns=200 if timed else None,
            full_compile_ns=full_compile_ns if timed else None,
            layer_ledger_sha256=_digest(f"ledger-{dataset}-{circuit}") if timed else "",
            transition_count=7 if timed else None,
            canonical_input_layer_ledger_sha256=(
                _digest(f"ledger-{dataset}-{circuit}") if timed else ""),
            canonical_input_transition_count=7 if timed else None,
            observed_transition_layer_ledger_sha256=(
                _digest(f"ledger-{dataset}-{circuit}") if timed else ""),
            observed_transition_count=7 if timed else None,
            observed_transition_layer_ledger_source=(
                ("normalized_qmap_placement_trace" if method == "M2"
                 else "compiler.gate_scheduling") if timed else ""),
            log_fidelity=log_fidelity, fidelity=math.exp(log_fidelity),
            fidelity_components={
                "log_one_qubit_gate": 0.0,
                "log_two_qubit_gate": log_fidelity,
                "log_idle_excitation": 0.0,
                "log_atom_transfer": 0.0,
                "log_coherence_linear": 0.0,
            },
            duration_us=100.0, qubits=20,
            expected_gates_1q=10, expected_gates_2q=100,
            observed_gates_1q=10, observed_gates_2q=100,
            expected_gate_ledger_sha256="c" * 64,
            observed_gate_ledger_sha256="c" * 64,
            move_batches=move_batches, move_time_us=move_time_us,
            ghost_repairs=0, ghost_splits=0, ghost_hits=0,
            trace_protocol=trace_protocol_for_method(method),
            ghost_policy=ghost_policy_for_method(method),
            physicalization_policy=physicalization_policy_for_method(method),
            verifier_ok=True,
        )

    def _build(self) -> None:
        for dataset, circuits in self.suites.items():
            circuit = circuits[0]
            self.quality.append(self._manifest(
                dataset, circuit, "M1", "main", log_fidelity=-1.0,
                move_batches=20, move_time_us=200.0))
            self.quality.append(self._manifest(
                dataset, circuit, "M2", "main", log_fidelity=-0.9,
                move_batches=18, move_time_us=180.0))
            for seed in range(5):
                self.quality.append(self._manifest(
                    dataset, circuit, "M3", "main", seed=seed,
                    log_fidelity=-0.84 + seed * 0.02,
                    move_batches=14 + seed, move_time_us=140.0 + seed * 10))
                self.quality.append(self._manifest(
                    dataset, circuit, "M4", "main", seed=seed,
                    log_fidelity=-0.74 + seed * 0.02,
                    move_batches=12 + seed, move_time_us=120.0 + seed * 10))
            base_seconds = {"M1": 1.0, "M2": 2.0, "M3": 1.0, "M4": 0.5}
            for method in ("M1", "M2", "M3", "M4"):
                for repetition in range(5):
                    seconds = base_seconds[method] + repetition
                    self.timing.append(self._manifest(
                        dataset, circuit, method, "timing",
                        repetition=repetition,
                        transition_ns=int(seconds * 1e9),
                        full_compile_ns=int((seconds + 10) * 1e9)))

    def aggregate(self, quality=None, timing=None):
        return aggregate_final_results(
            self.quality if quality is None else quality,
            self.timing if timing is None else timing,
            expected_experiment_ids=self.experiment_ids,
            frozen_suites=self.suites,
        )


class FormalPlanFixture:
    """Small current-plan projection for the formal final-evidence tests."""

    def __init__(self, root: Path, runs: FinalFixture) -> None:
        self.path = root / "plan.json"
        self.path.write_text('{"experiment_schema":2}\n', encoding="utf-8")
        self.repo_root = root / "repo"
        self.repo_root.mkdir()
        self.output_root = root / "artifacts"
        self.architecture_path = root / "architecture.json"
        self.model_path = root / "model.json"
        self.architecture_path.write_text("{}\n", encoding="utf-8")
        self.model_path.write_text("{}\n", encoding="utf-8")
        self.commit = "1" * 40
        self.datasets = {
            name: SimpleNamespace(name=name) for name in runs.suites
        }
        self._experiment_ids = dict(runs.experiment_ids)
        self._suites = {}
        for dataset, circuits in runs.suites.items():
            rows = []
            for circuit in circuits:
                canonical = root / "canonical" / dataset / f"{circuit}.qasm"
                canonical.parent.mkdir(parents=True, exist_ok=True)
                canonical.write_text(
                    "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[1];\n",
                    encoding="utf-8")
                rows.append(SimpleNamespace(
                    canonical_path=str(canonical.resolve()),
                    canonical_sha256=_file_digest(canonical)))
            self._suites[dataset] = rows

        native_setting = {
            "algorithm_revision": FORMAL_NATIVE_ALGORITHM_REVISION,
            "backend": "native",
            "native_abi_version": FORMAL_NATIVE_ABI_VERSION,
            "native_wheel_sha256": "d" * 64,
            "tuning_protocol_id": FORMAL_NATIVE_TUNING_PROTOCOL_ID,
            "rng_version": FORMAL_NATIVE_RNG_VERSION,
        }
        self.methods = {
            method: SimpleNamespace(payload=(
                {"zac_setting": [{**native_setting,
                                  "method_id": ("ours_nl" if method == "M3"
                                                else "ours_lk")}]}
                if method in {"M3", "M4"} else {}))
            for method in ("M1", "M2", "M3", "M4")
        }
        self._configs = {}
        for method in ("M1", "M2", "M3", "M4"):
            seeds = range(5) if method in {"M3", "M4"} else (0,)
            for seed in seeds:
                path = root / "resolved" / method / f"seed-{seed}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    "method": method, "seed": seed,
                    "formal_selection": "final",
                }, sort_keys=True) + "\n", encoding="utf-8")
                self._configs[(method, seed)] = path

        canonical_hashes = {
            (dataset, Path(row.canonical_path).stem): row.canonical_sha256
            for dataset, rows in self._suites.items() for row in rows
        }
        architecture = _file_digest(self.architecture_path)
        model = _file_digest(self.model_path)
        for manifest in [*runs.quality, *runs.timing]:
            manifest.git_commit = self.commit
            manifest.git_dirty = False
            manifest.input_sha256 = canonical_hashes[
                (manifest.dataset, manifest.circuit)]
            manifest.config_sha256 = _file_digest(
                self._configs[(manifest.method, manifest.seed)])
            manifest.architecture_sha256 = architecture
            manifest.model_sha256 = model

    def experiment_id(self, dataset):
        return self._experiment_ids[dataset.name]

    def load_suite(self, dataset):
        return list(self._suites[dataset.name])

    def resolved_config(self, method: str, seed: int) -> Path:
        return self._configs[(method, seed)]


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fixture_indices(root: Path, fixture: FinalFixture) -> tuple[Path, Path]:
    paths = {"main": [], "timing": []}
    for run_kind, manifests in (("main", fixture.quality),
                                ("timing", fixture.timing)):
        for index, manifest in enumerate(manifests):
            path = root / "runs" / run_kind / f"{index:04d}" / "manifest.json"
            manifest.write(path)
            paths[run_kind].append(path)
    quality = root / "quality-index.json"
    timing = root / "timing-index.json"
    seal_manifest_index(paths["main"], run_kind="main", output_path=quality)
    seal_manifest_index(paths["timing"], run_kind="timing", output_path=timing)
    return quality, timing


def _write_main_report(root: Path, fixture: FinalFixture,
                       selection: Mapping[str, object]) -> Path:
    rows = []
    for index, manifest in enumerate(fixture.quality):
        artifact = root / "report-runs" / f"{index:04d}"
        path = artifact / "manifest.json"
        manifest.artifact_dir = str(artifact.resolve())
        manifest.write(path)
        identity = [manifest.dataset, manifest.circuit, manifest.method,
                    manifest.seed, manifest.repetition]
        rows.append({
            "path": str(path.resolve()),
            "sha256": _file_digest(path),
            "run_id": manifest.run_id,
            "status": manifest.status,
            "identity": identity,
        })
    rows.sort(key=lambda row: tuple(row["identity"]))
    report = {
        "experiment_schema": 2,
        "phase": "main",
        "dry_run": False,
        "planned_jobs": len(rows),
        "cohort_manifests": rows,
        "formal_selection": dict(selection),
    }
    path = root / "run-main.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def _write_minimal_workbook(path: Path) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets>
  <sheet name="ZAC18" sheetId="1" r:id="rId1"/>
  <sheet name="QMAP154" sheetId="2" r:id="rId2"/>
  <sheet name="Runtime" sheetId="3" r:id="rId3"/>
 </sheets>
</workbook>"""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)


class TestFinalResults(unittest.TestCase):
    def test_exact_three_sheet_rows_and_medians(self):
        fixture = FinalFixture()
        result = fixture.aggregate()
        self.assertEqual(tuple(result["rows"]), SHEET_NAMES)
        self.assertEqual(
            result["workbook_contract"]["sheet_names"], list(SHEET_NAMES))
        self.assertFalse(result["workbook_contract"]["charts"])
        runtime_contract = result["workbook_contract"]["sheets"][2]
        self.assertEqual(runtime_contract["frozen_row_order"], [
            "zac18/zac_toy", f"zac18/{OVERALL_LABEL}",
            "qmap154/qmap_toy", f"qmap154/{OVERALL_LABEL}",
        ])

        zac = result["rows"]["ZAC18"][0]
        self.assertEqual(zac["circuit"], "zac_toy")
        self.assertAlmostEqual(zac["M3__fidelity"], math.exp(-0.80))
        self.assertEqual(zac["M3__move_batches"], 16)
        self.assertEqual(zac["M4__move_time_us"], 140.0)
        self.assertEqual(zac["M1__valid_over_N"], "1/1")
        self.assertEqual(zac["M4__valid_over_N"], "5/5")
        # Front-sheet stage time comes from the independent five-repeat timing run.
        self.assertEqual(zac["M2__transition_decision_s"], 4.0)

        zac_overall = result["rows"]["ZAC18"][1]
        self.assertEqual(zac_overall["circuit"], OVERALL_LABEL)
        self.assertAlmostEqual(zac_overall["M3__fidelity"], math.exp(-0.80))
        self.assertEqual(zac_overall["M3__valid_over_N"], "5/5")

        runtime = result["rows"]["Runtime"][0]
        self.assertEqual((runtime["dataset"], runtime["circuit"]),
                         ("zac18", "zac_toy"))
        self.assertEqual(runtime["M2__transition_decision_s_median"], 4.0)
        self.assertEqual(runtime["M2__transition_decision_s_q1"], 3.0)
        self.assertEqual(runtime["M2__transition_decision_s_q3"], 5.0)
        self.assertEqual(runtime["M2__transition_decision_s_iqr"], 2.0)
        self.assertEqual(runtime["M2__full_compile_s_median"], 14.0)
        self.assertAlmostEqual(runtime["M3__speedup_vs_M2"], 4.0 / 3.0)
        self.assertAlmostEqual(runtime["M4__speedup_vs_M2"], 4.0 / 2.5)
        self.assertTrue(runtime["layer_ledger_comparable"])
        self.assertEqual(runtime["layer_ledger_reason"], "comparable")

        runtime_overall = result["rows"]["Runtime"][1]
        self.assertEqual((runtime_overall["dataset"], runtime_overall["circuit"]),
                         ("zac18", OVERALL_LABEL))
        self.assertAlmostEqual(runtime_overall["M4__speedup_vs_M2"], 4.0 / 2.5)
        self.assertEqual(runtime_overall["M4__valid_over_N"], "5/5")
        self.assertTrue(runtime_overall["layer_ledger_comparable"])
        self.assertIsNone(runtime_overall["M4__transition_decision_s_q1"])
        self.assertIsNone(runtime_overall["M4__transition_decision_s_q3"])
        self.assertIsNone(runtime_overall["M4__transition_decision_s_iqr"])

    def test_failure_is_reported_but_disables_strict_speed_ratio(self):
        fixture = FinalFixture()
        timing = copy.deepcopy(fixture.timing)
        target = next(run for run in timing
                      if run.dataset == "zac18" and run.method == "M4"
                      and run.repetition == 4)
        target.status = RunStatus.TIMEOUT.value
        target.verifier_ok = None
        result = fixture.aggregate(timing=timing)
        runtime = result["rows"]["Runtime"][0]
        self.assertEqual(runtime["M4__valid_over_N"], "4/5")
        self.assertFalse(runtime["layer_ledger_comparable"])
        self.assertIn("M4", runtime["layer_ledger_reason"])
        self.assertIsNone(runtime["M3__speedup_vs_M2"])
        self.assertIsNone(runtime["M4__speedup_vs_M2"])
        overall = result["rows"]["Runtime"][1]
        self.assertIsNone(overall["M4__transition_decision_s_median"])
        self.assertEqual(overall["M4__valid_over_N"], "4/5")

    def test_qmap_internal_layer_order_drift_blanks_speedup_despite_same_input(self):
        fixture = FinalFixture()
        timing = copy.deepcopy(fixture.timing)
        qmap_runs = [
            run for run in timing
            if run.dataset == "zac18" and run.method == "M2"
        ]
        self.assertTrue(qmap_runs)
        canonical = {
            run.canonical_input_layer_ledger_sha256 for run in qmap_runs
        }
        self.assertEqual(len(canonical), 1)
        for run in qmap_runs:
            # Simulate a compiler-private layer sequence that differs from the
            # canonical QASM while leaving the input receipt untouched.
            run.observed_transition_layer_ledger_sha256 = _digest(
                "qmap-observed-different-layer-order")
        result = fixture.aggregate(timing=timing)
        runtime = result["rows"]["Runtime"][0]
        self.assertFalse(runtime["layer_ledger_comparable"])
        self.assertEqual(
            runtime["layer_ledger_reason"],
            "observed_transition_layer_ledger_sha256_mismatch")
        self.assertIsNone(runtime["M3__speedup_vs_M2"])
        self.assertIsNone(runtime["M4__speedup_vs_M2"])

    def test_incomplete_quality_seed_set_blanks_metrics_instead_of_hiding_failure(self):
        fixture = FinalFixture()
        quality = copy.deepcopy(fixture.quality)
        target = next(run for run in quality
                      if run.dataset == "zac18" and run.method == "M4"
                      and run.seed == 4)
        target.status = RunStatus.VERIFIER_FAIL.value
        target.verifier_ok = False
        result = fixture.aggregate(quality=quality)
        row = result["rows"]["ZAC18"][0]
        self.assertEqual(row["M4__valid_over_N"], "4/5")
        self.assertIsNone(row["M4__fidelity"])
        self.assertIsNone(row["M4__move_batches"])
        self.assertIsNone(row["M4__move_time_us"])
        overall = result["rows"]["ZAC18"][1]
        self.assertIsNone(overall["M4__fidelity"])
        self.assertEqual(overall["M4__valid_over_N"], "4/5")

    def test_one_ood_seed_blanks_fidelity_without_hiding_compile_coverage(self):
        fixture = FinalFixture()
        quality = copy.deepcopy(fixture.quality)
        target = next(run for run in quality
                      if run.dataset == "zac18" and run.method == "M4"
                      and run.seed == 4)
        target.fidelity_ood = True
        target.log_fidelity = None
        target.fidelity = None
        target.fidelity_components["log_coherence_linear"] = None
        target.exponential_sensitivity_log_fidelity = -2.0
        target.exponential_sensitivity_fidelity = math.exp(-2.0)
        result = fixture.aggregate(quality=quality)
        row = result["rows"]["ZAC18"][0]
        self.assertEqual(row["M4__valid_over_N"], "5/5")
        self.assertIsNone(row["M4__fidelity"])
        self.assertIsNotNone(row["M4__move_batches"])
        self.assertIsNone(result["rows"]["ZAC18"][1]["M4__fidelity"])

    def test_writes_only_csvs_and_workbook_contract(self):
        fixture = FinalFixture()
        result = fixture.aggregate()
        with tempfile.TemporaryDirectory() as directory:
            paths = write_final_results(result, directory)
            self.assertEqual(
                {path.name for path in paths.values()},
                {"zac18.csv", "qmap154.csv", "runtime_vs_iccad.csv",
                 "workbook_contract.json"})
            self.assertFalse(any(Path(directory).glob("*.xlsx")))
            with open(paths["ZAC18"], encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["circuit"], "zac_toy")
            self.assertEqual(rows[1]["circuit"], OVERALL_LABEL)
            with open(paths["workbook_contract"], encoding="utf-8") as handle:
                contract = json.load(handle)
            self.assertEqual(contract["exact_sheet_count"], 3)
            self.assertEqual(contract["sheet_names"], list(SHEET_NAMES))

    def test_rejects_legacy_schema_path(self):
        fixture = FinalFixture()
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy.json"
            legacy.write_text(json.dumps({"experiment_schema": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-Schema-2"):
                fixture.aggregate(quality=[legacy])

    def test_rejects_wrong_experiment_id(self):
        fixture = FinalFixture()
        quality = copy.deepcopy(fixture.quality)
        quality[0].experiment_id = _digest("wrong")
        with self.assertRaisesRegex(ValueError, "experiment_id mismatch"):
            fixture.aggregate(quality=quality)

    def test_rejects_duplicate_and_missing_attempts(self):
        fixture = FinalFixture()
        with self.assertRaisesRegex(ValueError, "duplicate run_id"):
            fixture.aggregate(quality=[*fixture.quality, fixture.quality[0]])
        with self.assertRaisesRegex(ValueError, "incomplete or mixed main cohort"):
            fixture.aggregate(quality=fixture.quality[:-1])

    def test_rejects_mixed_canonical_or_config_sets(self):
        fixture = FinalFixture()
        timing = copy.deepcopy(fixture.timing)
        timing[0].input_sha256 = _digest("different-input")
        with self.assertRaisesRegex(ValueError, "mix canonical inputs"):
            fixture.aggregate(timing=timing)

        timing = copy.deepcopy(fixture.timing)
        target = next(run for run in timing
                      if run.dataset == "zac18" and run.method == "M2"
                      and run.repetition == 3)
        target.config_sha256 = _digest("mixed-config")
        with self.assertRaisesRegex(ValueError, "mixed config hashes"):
            fixture.aggregate(timing=timing)

    def test_rejects_dirty_mixed_commit_or_native_provenance(self):
        fixture = FinalFixture()
        quality = copy.deepcopy(fixture.quality)
        quality[0].git_dirty = True
        with self.assertRaisesRegex(ValueError, "dirty runs"):
            fixture.aggregate(quality=quality)

        timing = copy.deepcopy(fixture.timing)
        timing[0].git_commit = "2" * 40
        with self.assertRaisesRegex(ValueError, "mix Git commits"):
            fixture.aggregate(timing=timing)

        quality = copy.deepcopy(fixture.quality)
        native = next(run for run in quality if run.method == "M4")
        native.algorithm_revision = "unregistered"
        with self.assertRaisesRegex(ValueError, "native provenance mismatch"):
            fixture.aggregate(quality=quality)

        quality = copy.deepcopy(fixture.quality)
        native = next(run for run in quality if run.method == "M4")
        native.native_wheel_sha256 = "e" * 64
        with self.assertRaisesRegex(ValueError, "mix native wheels"):
            fixture.aggregate(quality=quality)

    def test_explicit_manifest_index_is_hash_sealed_and_never_scans_directories(self):
        fixture = FinalFixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index, manifest in enumerate(fixture.quality):
                path = root / "runs" / f"{index:03d}" / "manifest.json"
                manifest.write(path)
                paths.append(path)
            index_path = root / "quality-index.json"
            seal_manifest_index(
                paths, run_kind="main", output_path=index_path)
            loaded = load_manifest_index(
                index_path, expected_run_kind="main")
            self.assertEqual(set(loaded), {path.resolve() for path in paths})
            with self.assertRaisesRegex(ValueError, "explicit manifest files"):
                seal_manifest_index(
                    [root / "runs"], run_kind="main",
                    output_path=root / "bad.json")

            payload = json.loads(index_path.read_text(encoding="utf-8"))
            payload["manifests"][0]["run_id"] = "tampered"
            index_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "contract drift"):
                load_manifest_index(
                    index_path, expected_run_kind="main")

    @mock.patch("experiments_v2.cli._assert_reproduction_gate")
    @mock.patch("experiments_v2.cli._assert_formal_selection_gates")
    @mock.patch("experiments_v2.final_results_cli.repository_snapshot")
    @mock.patch("experiments_v2.final_results_cli.load_experiment_plan")
    def test_seal_report_index_rejects_one_missing_registry_row(
            self, load_plan, repository_snapshot_mock, selection_gate,
            reproduction_gate):
        del reproduction_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = FinalFixture()
            plan = FormalPlanFixture(root, fixture)
            load_plan.return_value = plan
            repository_snapshot_mock.return_value = {
                "root": str(plan.repo_root), "commit": plan.commit,
                "branch": "codex/test", "dirty": False,
            }
            selection = {
                "initial_selection": str(root / "selected_engine.json"),
                "initial_selection_record_sha256": "4" * 64,
                "initial_placement_engine": "sa",
                "tuning_selection": str(root / "selected_config.json"),
                "tuning_selection_manifest_sha256": "5" * 64,
                "shared_candidate_id": "candidate-final",
            }
            selection_gate.return_value = selection
            report_path = _write_main_report(root, fixture, selection)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["cohort_manifests"].pop()
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cohort count"):
                seal_report_manifest_index(
                    plan_path=plan.path, report_path=report_path,
                    run_kind="main", output_path=root / "quality-index.json")

    @mock.patch("experiments_v2.cli._assert_reproduction_gate")
    @mock.patch("experiments_v2.cli._assert_formal_selection_gates")
    @mock.patch("experiments_v2.final_results_cli.repository_snapshot")
    @mock.patch("experiments_v2.final_results_cli.load_experiment_plan")
    def test_seal_report_index_refuses_replacing_index_with_old_path_swap(
            self, load_plan, repository_snapshot_mock, selection_gate,
            reproduction_gate):
        del reproduction_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = FinalFixture()
            plan = FormalPlanFixture(root, fixture)
            load_plan.return_value = plan
            repository_snapshot_mock.return_value = {
                "root": str(plan.repo_root), "commit": plan.commit,
                "branch": "codex/test", "dirty": False,
            }
            selection = {
                "initial_selection": str(root / "selected_engine.json"),
                "initial_selection_record_sha256": "4" * 64,
                "initial_placement_engine": "sa",
                "tuning_selection": str(root / "selected_config.json"),
                "tuning_selection_manifest_sha256": "5" * 64,
                "shared_candidate_id": "candidate-final",
            }
            selection_gate.return_value = selection
            report_path = _write_main_report(root, fixture, selection)
            index_path = root / "quality-index.json"
            seal_report_manifest_index(
                plan_path=plan.path, report_path=report_path,
                run_kind="main", output_path=index_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            replaced = copy.deepcopy(fixture.quality[0])
            replaced.run_id = "replacement-current-run"
            replacement_dir = root / "replacement-current-run"
            replaced.artifact_dir = str(replacement_dir.resolve())
            replacement_path = replacement_dir / "manifest.json"
            replaced.write(replacement_path)
            identity = [replaced.dataset, replaced.circuit, replaced.method,
                        replaced.seed, replaced.repetition]
            row = next(item for item in report["cohort_manifests"]
                       if item["identity"] == identity)
            row.update({
                "path": str(replacement_path.resolve()),
                "sha256": _file_digest(replacement_path),
                "run_id": replaced.run_id,
            })
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError,
                                        "refusing to replace final index"):
                seal_report_manifest_index(
                    plan_path=plan.path, report_path=report_path,
                    run_kind="main", output_path=index_path)

    @mock.patch("experiments_v2.cli._assert_reproduction_gate")
    @mock.patch("experiments_v2.cli._assert_formal_selection_gates")
    @mock.patch("experiments_v2.final_results_cli.repository_snapshot")
    @mock.patch("experiments_v2.final_results_cli.load_experiment_plan")
    def test_formal_aggregate_binds_all_gates_plan_selections_and_indices(
            self, load_plan, repository_snapshot_mock, selection_gate,
            reproduction_gate):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = FinalFixture()
            plan = FormalPlanFixture(root, fixture)
            load_plan.return_value = plan
            repository = {
                "root": str(plan.repo_root), "commit": plan.commit,
                "branch": "codex/test", "dirty": False,
            }
            repository_snapshot_mock.return_value = repository
            initial = root / "initial-placement" / "selected_engine.json"
            tuning = root / "tuning" / "selected_config_manifest.json"
            initial.parent.mkdir(parents=True)
            tuning.parent.mkdir(parents=True)
            initial.write_text('{"selected":"sa"}\n', encoding="utf-8")
            tuning.write_text('{"selected":"candidate"}\n', encoding="utf-8")
            selection_gate.return_value = {
                "initial_selection": str(initial),
                "initial_selection_record_sha256": "4" * 64,
                "initial_placement_engine": "sa",
                "tuning_selection": str(tuning),
                "tuning_selection_manifest_sha256": "5" * 64,
                "shared_candidate_id": "candidate-final",
            }
            quality, timing = _write_fixture_indices(root, fixture)
            output = root / "reports"
            paths = aggregate_from_indices(
                plan_path=plan.path, quality_index=quality,
                timing_index=timing, output_directory=output)

            selection_gate.assert_called_once_with(plan)
            reproduction_gate.assert_called_once_with(plan)
            provenance = json.loads(Path(
                paths["aggregation_provenance"]).read_text(encoding="utf-8"))
            self.assertEqual(provenance["record_sha256"],
                             _record_hash(provenance))
            self.assertEqual(provenance["plan"], {
                "path": str(plan.path.resolve()),
                "sha256": _file_digest(plan.path),
            })
            self.assertEqual(provenance["repository"], repository)
            self.assertEqual(provenance["experiment_ids"],
                             fixture.experiment_ids)
            self.assertEqual(
                provenance["formal_selection"]["initial"]["record_sha256"],
                "4" * 64)
            self.assertEqual(
                provenance["formal_selection"]["tuning"]["manifest_sha256"],
                "5" * 64)
            for label, index_path in (("quality_index", quality),
                                      ("timing_index", timing)):
                index = json.loads(index_path.read_text(encoding="utf-8"))
                self.assertEqual(provenance[label]["path"],
                                 str(index_path.resolve()))
                self.assertEqual(provenance[label]["sha256"],
                                 _file_digest(index_path))
                self.assertEqual(provenance[label]["record_sha256"],
                                 index["record_sha256"])
            contract = json.loads(Path(
                paths["workbook_contract"]).read_text(encoding="utf-8"))
            self.assertEqual(contract["aggregation_provenance"], provenance)
            self.assertEqual(contract["aggregation_provenance_file"],
                             "aggregation_provenance.json")

    @mock.patch("experiments_v2.cli._assert_reproduction_gate")
    @mock.patch("experiments_v2.cli._assert_formal_selection_gates")
    @mock.patch("experiments_v2.final_results_cli.repository_snapshot")
    @mock.patch("experiments_v2.final_results_cli.load_experiment_plan")
    def test_formal_aggregate_rejects_self_consistent_old_config_cohort(
            self, load_plan, repository_snapshot_mock, selection_gate,
            reproduction_gate):
        del reproduction_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = FinalFixture()
            plan = FormalPlanFixture(root, fixture)
            load_plan.return_value = plan
            repository_snapshot_mock.return_value = {
                "root": str(plan.repo_root), "commit": plan.commit,
                "branch": "codex/test", "dirty": False,
            }
            initial = root / "selected_engine.json"
            tuning = root / "selected_config_manifest.json"
            initial.write_text("{}\n", encoding="utf-8")
            tuning.write_text("{}\n", encoding="utf-8")
            selection_gate.return_value = {
                "initial_selection": str(initial),
                "initial_selection_record_sha256": "4" * 64,
                "initial_placement_engine": "sa",
                "tuning_selection": str(tuning),
                "tuning_selection_manifest_sha256": "5" * 64,
                "shared_candidate_id": "candidate-final",
            }
            # Re-signing the index does not make a pre-selection config current.
            stale = next(run for run in fixture.quality
                         if run.method == "M4" and run.seed == 3)
            stale.config_sha256 = _digest("provisional-old-config")
            quality, timing = _write_fixture_indices(root, fixture)
            with self.assertRaisesRegex(
                    ValueError, "current final resolved plan"):
                aggregate_from_indices(
                    plan_path=plan.path, quality_index=quality,
                    timing_index=timing,
                    output_directory=root / "reports")

    @mock.patch("experiments_v2.final_results_cli.subprocess.run")
    def test_render_is_immutable_but_identical_repeat_is_idempotent(
            self, run_mock):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("zac18.csv", "qmap154.csv", "runtime_vs_iccad.csv"):
                (root / name).write_text("circuit,M1\ntoy,1\n", encoding="utf-8")
            provenance = {
                "provenance_schema": 1,
                "experiment_schema": 2,
                "contract_id": "native-ga-v1-formal-aggregation-v1",
                "plan": {"path": "/formal/plan.json", "sha256": "1" * 64},
                "repository": {"commit": "2" * 40, "dirty": False},
                "experiment_ids": {"zac18": "3" * 64,
                                   "qmap154": "4" * 64},
                "formal_selection": {
                    "initial": {"record_sha256": "5" * 64},
                    "tuning": {"manifest_sha256": "6" * 64},
                },
                "quality_index": {"path": "/quality.json",
                                  "sha256": "7" * 64,
                                  "record_sha256": "8" * 64},
                "timing_index": {"path": "/timing.json",
                                 "sha256": "9" * 64,
                                 "record_sha256": "a" * 64},
            }
            provenance["record_sha256"] = _record_hash(provenance)
            provenance_path = root / "aggregation_provenance.json"
            provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
            contract = {
                "contract_id": "native-ga-v1-three-sheet-results-v1",
                "sheet_names": ["ZAC18", "QMAP154", "Runtime"],
                "exact_sheet_count": 3,
                "charts": False,
                "aggregation_provenance": provenance,
                "aggregation_provenance_file": provenance_path.name,
                "sheets": [
                    {"name": "ZAC18", "source_csv": "zac18.csv"},
                    {"name": "QMAP154", "source_csv": "qmap154.csv"},
                    {"name": "Runtime", "source_csv": "runtime_vs_iccad.csv"},
                ],
            }
            contract_path = root / "workbook_contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            node = root / "node"
            node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            node.chmod(0o755)
            modules = root / "modules"
            modules.mkdir()

            def render_side_effect(command, **_kwargs):
                xlsx = Path(command[3])
                qa = Path(command[4])
                qa.mkdir()
                _write_minimal_workbook(xlsx)
                sheets = []
                for name in ("ZAC18", "QMAP154", "Runtime"):
                    preview = qa / f"{name}.png"
                    preview.write_bytes(f"preview-{name}".encode())
                    sheets.append({"name": name, "preview": preview.name})
                (qa / "workbook_qa.json").write_text(json.dumps({
                    "sheets": sheets, "formula_errors": [],
                }), encoding="utf-8")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            run_mock.side_effect = render_side_effect
            output = root / "four_methods_results.xlsx"
            qa = root / "qa"
            first = render_final_workbook(
                contract_path=contract_path, output_path=output,
                qa_directory=qa, node_executable=node,
                node_modules=modules,
                aggregation_provenance_path=provenance_path)
            second = render_final_workbook(
                contract_path=contract_path, output_path=output,
                qa_directory=qa, node_executable=node,
                node_modules=modules)
            self.assertEqual(first, second)
            self.assertEqual(run_mock.call_count, 1)
            final_manifest = json.loads(
                (root / "final_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(final_manifest["record_sha256"],
                             _record_hash(final_manifest))
            self.assertEqual(final_manifest["aggregation_provenance"],
                             provenance)
            self.assertEqual(
                final_manifest["aggregation_provenance_record_sha256"],
                provenance["record_sha256"])

            # A self-consistent but different aggregate may not replace it.
            changed = copy.deepcopy(provenance)
            changed["plan"]["sha256"] = "b" * 64
            changed["record_sha256"] = _record_hash(changed)
            provenance_path.write_text(json.dumps(changed), encoding="utf-8")
            contract["aggregation_provenance"] = changed
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "different inputs"):
                render_final_workbook(
                    contract_path=contract_path, output_path=output,
                    qa_directory=qa, node_executable=node,
                    node_modules=modules)


if __name__ == "__main__":
    unittest.main()
