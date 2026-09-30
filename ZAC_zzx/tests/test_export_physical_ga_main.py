"""Bounded synthetic exporter tests. No compiler, experiment, or trace replay."""
from copy import deepcopy
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export = load("ga_main_export_test", REPO / "ZAC_zzx/experiments_v2/export_physical_ga_main.py")
sys.path.insert(0, str(REPO / "scripts/paper"))
try:
    baseline = load("ga_main_baseline_test", REPO / "scripts/paper/generate_default_initial_values.py")
finally:
    sys.path.remove(str(REPO / "scripts/paper"))
baseline.TERMINAL_FAILURES = baseline.TERMINAL_FAILURES | export.NEW_FAILURES


def record(dataset, circuit, digest, seed=0, method=None, status="success", ood=False, log_f=-1.):
    row = {"dataset": dataset, "circuit": circuit, "canonical_sha256": digest,
           "seed": seed, "status": status, "source_status": status, "fidelity_ood": ood,
           "metrics": {"log_fidelity": None if ood else log_f,
                       "fidelity": None if ood else math.exp(log_f),
                       "move_batches": 10, "move_time_us": 100, "transfers": 4,
                       "idle_exposures": 0, "log_atom_transfer": -.004,
                       "log_idle_excitation": 0., "log_coherence_linear": None if ood else -.1}}
    if method:
        row["method"] = method
    if status != "success":
        row["metrics"] = {}
    return row


def fixture():
    inventory = {"zac18": {}, "qmap154": {}}
    old, new = [], []
    for d, dataset in enumerate(inventory):
        for i in range(4):
            circuit, digest = f"c{d}_{i}", f"{d * 4 + i + 1:064x}"
            inventory[dataset][circuit] = {"canonical_sha256": digest, "qubits": 2, "gates_2q": 3}
            old.extend(record(dataset, circuit, digest, method=m) for m in ("M1", "M2"))
            new.extend(record(dataset, circuit, digest, seed=s, log_f=-.9 + .1 * i) for s in (0, 1, 2))
    return old, new, inventory


def matrix_fixture():
    inventory = {"zac18": {}, "qmap154": {}}
    circuits, jobs = [], []
    for i in range(169):
        dataset, name, sha = "zac18" if i < 18 else "qmap154", f"c{i}", hashlib.sha256(str(i).encode()).hexdigest()
        label = {"dataset": dataset, "circuit": name, "canonical_sha256": sha,
                 "qubits": 2, "gates_1q": 1, "gates_2q": 3}
        labels = [label]
        if 18 <= i < 21:
            labels.append({**label, "circuit": name + "_alias"})
        for item in labels:
            inventory[dataset][item["circuit"]] = {k: item[k] for k in
                ("canonical_sha256", "qubits", "gates_1q", "gates_2q")}
        circuits.append({"input_sha256": sha, "labels": labels})
        for seed in (0, 1, 2):
            jobs.append({"input_sha256": sha, "seed": seed, "job_id": f"{sha[:16]}-s{seed}-ga_h2",
                         "origin": "reused" if i < 9 else "fresh"})
    return {"protocol_id": export.PROTOCOL_ID, "arms": ["ga_h2"], "seeds": [0, 1, 2],
            "jobs": jobs, "circuits": circuits}, inventory


class ExportTests(unittest.TestCase):
    def test_full_matrix_requires_every_pilot_outcome_and_alias(self):
        p, inventory = matrix_fixture()
        self.assertEqual(len(export.validate_inventory(p, inventory)), 169)
        for change in ("drop", "relabel"):
            bad = deepcopy(p)
            if change == "drop":
                del bad["jobs"][0]
            else:
                bad["jobs"][0]["origin"] = "fresh"
            with self.assertRaisesRegex(ValueError, "480 fresh"):
                export.validate_inventory(bad, inventory)

    def test_schema_and_alias_aggregation_exactly_match_pinned_loader(self):
        old, new, inventory = fixture()
        original = inventory["qmap154"]["c1_0"]
        inventory["qmap154"]["alias"] = deepcopy(original)
        for rows in (old, new):
            rows.extend({**deepcopy(r), "circuit": "alias"} for r in list(rows) if r["circuit"] == "c1_0")
        for r in new:
            if r["circuit"] == "alias":
                r["metrics"].update(log_fidelity=-.5, fidelity=math.exp(-.5))
        new[0] = record("zac18", "c0_0", new[0]["canonical_sha256"], status="program_error")
        old_outputs = baseline.render_exports(old, new, inventory, {})
        timing = {"circuit_N": 12, "post_initial_median_seconds": 21.3076912085,
                  "search_share_mean": .8830283402766648}
        budget_timing = {"circuit_N": 12, "budget_post_initial_time_ratio": .7571918522303143,
                         "return_post_initial_time_ratio": .7517310167676802,
                         "timing_context": {"run_kinds": {"ablation": 36}, "concurrency_limits": {"4": 36}}}
        outputs = export.render_exports(baseline, old, new, inventory, {"historical_fixed_layout_timing": timing,
            "historical_fixed_mapping_budget_timing": budget_timing})
        for name in ("main_rows.csv", "analysis_units.csv", "mechanism.csv"):
            self.assertEqual(outputs[name], old_outputs[name])
        data = json.loads(outputs["ga_main_values.json"])
        qmap = data["datasets"]["qmap154"]
        self.assertEqual((qmap["common_file_N"], qmap["common_canonical_N"]), (5, 4))
        self.assertEqual(qmap["comparisons"]["M1"]["wins"], 4)
        self.assertEqual(qmap["comparisons"]["M2"]["losses"], 0)
        self.assertEqual(data["datasets"]["zac18"]["common_canonical_N"], 3)
        self.assertTrue(all(k.startswith("GAMain") for k in data["macros"]))
        self.assertEqual(data["macros"]["GAMainPostInitialMedianSeconds"], "21.31")
        self.assertEqual(data["macros"]["GAMainSearchSharePercent"], "88.30")
        for macro, value in (("GAMainBudgetTimingN", "12"), ("GAMainBudgetPostInitialTimeRatio", "0.7572"),
                             ("GAMainReturnPostInitialTimeRatio", "0.7517")):
            self.assertEqual(data["macros"][macro], value)
            self.assertIn(rf"\newcommand{{\{macro}}}{{{value}}}", outputs["ga_main_values.tex"])
        self.assertEqual(data["historical_fixed_mapping_budget_timing"], budget_timing)
        self.assertEqual(data["historical_fixed_layout_timing"], timing)
        self.assertIn(r"\GAMainRepresentativeCircuitRows", outputs["representative_cases.tex"])
        self.assertNotIn("default_initial_values.json", outputs)

    def test_win_tie_loss_macros_copy_existing_comparisons(self):
        old, new, inventory = fixture()
        for row in old:
            if row["method"] == "M2":
                row["metrics"].update(log_fidelity=-.5, fidelity=math.exp(-.5))
        for row in new:
            value = (-1., -2., -.8, -.8)[int(row["circuit"][-1])]
            row["metrics"].update(log_fidelity=value, fidelity=math.exp(value))
        outputs = export.render_exports(baseline, old, new, inventory, {})
        data = json.loads(outputs["ga_main_values.json"])
        for dataset, name in (("zac18", "ZAC"), ("qmap154", "QMAP")):
            for method, label, expected in (("M1", "ZAC", (2, 1, 1)), ("M2", "ICCAD", (0, 0, 4))):
                for outcome, count in zip(("wins", "ties", "losses"), expected):
                    macro = f"GAMain{name}Vs{label}{outcome.capitalize()}"
                    self.assertEqual(data["datasets"][dataset]["comparisons"][method][outcome], count)
                    self.assertEqual(data["macros"][macro], str(count))
                    self.assertIn(rf"\newcommand{{\{macro}}}{{{count}}}", outputs["ga_main_values.tex"])

    def test_new_failures_keep_source_status_and_reuse_identity(self):
        job = {"dataset": "qmap154", "circuit": "c", "input_sha256": "a" * 64,
               "job_id": "a-s0-ga_h2", "seed": 0, "origin": "reused"}
        circuit = {"qubits": 2, "gates_1q": 0, "gates_2q": 0}
        for status in export.NEW_FAILURES | {"timeout", "memory_limit", "interrupted"}:
            row = export.normalize_observation(baseline, {"status": status}, job, circuit,
                {}, {"path": "pilot/protocol.json", "sha256": "b" * 64}, {"path": "pilot/receipt.json"})
            self.assertEqual(row["source_status"], status)
            self.assertEqual(row["status"], status)
            self.assertEqual(row["metrics"], {})
            self.assertEqual((row["main_origin"], row["source_origin"]), ("reused", "fresh"))
            self.assertFalse(row["fidelity_valid"])

    def test_failure_table_preserves_timeout_but_ood_stays_completed(self):
        rows = [record("qmap154", "c", "a" * 64, seed=s, status="timeout") for s in (0, 1, 2)]
        rows += [record("qmap154", "empty", "b" * 64, status="program_error"),
                 record("qmap154", "ood", "c" * 64, ood=True)]
        failed = list(csv.DictReader(io.StringIO(export.outcome_csv(baseline, rows))))
        ood = list(csv.DictReader(io.StringIO(export.outcome_csv(baseline, rows, ood=True))))
        self.assertEqual([r["source_status"] for r in failed], ["timeout"] * 3 + ["program_error"])
        self.assertEqual([r["source_status"] for r in ood], ["success"])
        self.assertIn("source_status", export.outcome_csv(baseline, [], ood=True))

    def test_diagnostic_requires_zero_gates_and_specific_attribute_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {"job_id": "x", "input_sha256": "a" * 64, "seed": 0, "arm": "ga_h2", "status": "program_error"}
            failure = {**row, "exception_type": "AttributeError", "message": "no zzx_initial_lookahead_report"}
            (root / "failure.json").write_text(export.canonical(failure))
            sources = export.Sources(root)
            (root / "receipt.json").write_text(export.canonical({"failure": sources.file(root / "failure.json")}))
            receipt = sources.file(root / "receipt.json")
            result = export.failure_evidence(row, {"gates_1q": 0, "gates_2q": 0}, receipt, sources)
            self.assertEqual(result["diagnostic_note"], "空门序列导致初始化报告字段缺失")
            self.assertEqual(result["failure_source_kind"], "failure_artifact")
            self.assertEqual(export.failure_evidence(row, {"gates_1q": 1, "gates_2q": 0}, receipt, sources)["diagnostic_note"], "")

    def test_alias_expansion_keeps_failed_canonical_observation(self):
        observation = {"canonical_sha256": "a" * 64, "seed": 2, "status": "timeout", "main_origin": "reused"}
        expanded = export.expand_aliases([observation], {"a" * 64: {"labels": [
            {"dataset": "qmap154", "circuit": "a"}, {"dataset": "qmap154", "circuit": "b"}]}})
        self.assertEqual([r["status"] for r in expanded], ["timeout", "timeout"])
        self.assertTrue(all(r["main_origin"] == "reused" for r in expanded))

    def test_check_never_writes_and_export_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            protocol = Path(directory) / "new_study/protocol.json"
            destination = protocol.parent / "paper_exports"
            outputs = {"values.json": "{}\n"}
            export.write_or_check(outputs, destination, protocol_path=protocol)
            with mock.patch.object(Path, "mkdir", side_effect=AssertionError("check attempted mkdir")):
                self.assertEqual(export.write_or_check(outputs, destination, protocol_path=protocol, check=True)["status"], "pass")
                self.assertEqual(export.write_or_check({"missing.csv": "x"}, destination, protocol_path=protocol, check=True)["status"], "fail")
            with self.assertRaises(FileExistsError):
                export.write_or_check(outputs, destination, protocol_path=protocol)
            with self.assertRaisesRegex(ValueError, "output must"):
                export.write_or_check(outputs, protocol.parent.parent / "old_data", protocol_path=protocol)

    def test_incomplete_matrix_stops_before_loading_computational_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.json"
            path.write_text(export.canonical({"jobs": [{"job_id": "missing", "origin": "fresh"}]}))
            digest = export.sha256(path)
            path.with_name("protocol.sha256.json").write_text(export.canonical({"path": str(path), "sha256": digest}))
            with mock.patch.object(export, "PROTOCOL_SHA256", digest), mock.patch.object(export, "frozen_modules") as modules:
                with self.assertRaisesRegex(ValueError, "matrix is not closed"):
                    export.compute_exports(path, repo=directory)
                modules.assert_not_called()

    def test_hash_closure_detects_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text("{}")
            sources = export.Sources(directory)
            ref = sources.file(path)
            with self.assertRaisesRegex(ValueError, "SHA256 drift"):
                sources.file({**ref, "sha256": "0" * 64})
            path.write_text("{\"changed\": true}")
            with self.assertRaisesRegex(ValueError, "source changed"):
                sources.finish()


if __name__ == "__main__":
    unittest.main()
