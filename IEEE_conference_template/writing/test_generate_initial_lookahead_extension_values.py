from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import math
from pathlib import Path
import statistics
import tempfile
import unittest
from unittest.mock import patch

import generate_initial_lookahead_extension_values as export


def fixture(root):
    """Entirely synthetic fixed-size evidence; never reads active experiments."""
    root = Path(root).resolve()
    study, pilot_root = root / export.STUDY_RELATIVE, root / export.PILOT_RELATIVE
    new = [f"new{i:02}" for i in range(32)]
    reused = [f"old{i:02}" for i in range(7)]
    extra = ["cm82a_208", "ex1_226"]
    target = new + reused
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(export.json_text(value))
    def entry(name):
        return {"circuit": name, "dataset": "fixture", "input": f"/fixture/{name}.qasm",
            "input_sha256": export.stable_hash(name), "qubits": 4, "gates_1q": 2, "gates_2q": 3}
    def protocol(names, identifier, source):
        return {"protocol_id": identifier, "formal_paper_result": False,
            "circuits": [entry(n) for n in names], "seeds": [0, 1, 2],
            "horizons": [0, 2], "candidates": 4, "rho": .7, "rollout_evaluations": 32,
            "dynamic_horizon": 8, "timeout_per_paired_job_s": 600,
            "repository": {"source_sha256": source},
            "job_order": [{"circuit": n, "seed": s, "job_id": f"{n}-s{s}"}
                for n in names for s in export.SEEDS]}
    p = protocol(new, "initial-physical-prefix-dynamic39-missing32-v1", "a" * 64)
    p.update(workers=3, pilot_root=str(pilot_root),
        cohort={"target": target, "new": new, "reused": reused, "pilot_outside_target": extra},
        sa_seed_semantics="SA seed fixed; trial seeds perturb candidates and dynamic search.")
    old = protocol(reused + extra, "initial-physical-prefix-nine-family-seed3-v1", "b" * 64)
    write(study / "protocol.json", p)
    write(pilot_root / "protocol.json", old)
    records = {}
    audits = {}
    for phase, directory, document, names in (
        ("parallel_extension", study, p, new), ("serial_pilot", pilot_root, old, reused)):
        evidence = {}
        completed_arms = 0
        for name in names:
            index = target.index(name)
            for seed in export.SEEDS:
                job_id = f"{name}-s{seed}"
                job = directory / "jobs" / job_id
                failed = (name, seed) in (("new00", 2), ("old00", 1))
                status = "timeout" if failed else "success"
                receipt_path = directory / "receipts" / f"{job_id}.json"
                write(receipt_path, {"circuit": name, "seed": seed,
                    "job_id": job_id, "status": status})
                evidence[str(receipt_path)] = export.sha256(receipt_path)
                dynamic = {"seed": seed, "name": name, "horizon": 8}
                write(job / "protocol.json", {"dynamic_setting": dynamic,
                    "input_sha256": entry(name)["input_sha256"],
                    "repository": document["repository"], "selection_uses_full_compile_results": False,
                    "initial_configs": [{"candidates": 4, "horizon": h, "rho": .7,
                        "rollout_evaluations": 32, "seed": seed} for h in (0, 2)]})
                if not failed:
                    write(job / "summary.json", {"source_stable": True})
                record = {"seed": seed, "status": status, "accepted": not failed, "results": {}}
                for arm in (export.ARMS[:1] if failed else export.ARMS):
                    effect = {"sa_reference": 0, "h0": .0001, "h2": .0003}[arm] * (index + 1)
                    batches = 100 + index * 7 + seed - {"sa_reference": 0, "h0": 1, "h2": 3}[arm]
                    value = {"variant": arm, "status": "success",
                        "horizon": None if arm == "sa_reference" else int(arm[1:]),
                        "dynamic_setting_sha256": export.stable_hash(dynamic),
                        "n_qubits": 4, "selected_candidate": 0,
                        "validation": {"ok": True, "ghost_hits": 0},
                        "score": {"ood": False, "log_fidelity": -.1 * (index + 1) - .005 * seed + effect,
                            "move_batches": batches, "move_time_us": float(batches * 20),
                            "counts": {"one_qubit_gates": 2, "two_qubit_gates": 3}}}
                    path = job / f"{arm}_result.json"
                    write(path, value)
                    evidence[str(path)] = export.sha256(path)
                    record["results"][arm] = value
                    completed_arms += 1
                records[name, seed] = record
        audits[phase] = {"jobs": len(names) * 3, "completed_arms": completed_arms,
            "physically_valid_arms": completed_arms, "logical_sequences_valid": completed_arms,
            "ood_arms": 0, "evidence_hashes": evidence}
    roots = {"parallel_extension": study, "serial_pilot": pilot_root}
    rows = [export.circuit_row(entry(n), [records[n, s] for s in export.SEEDS],
        "parallel_extension" if n in new else "serial_pilot",
        study if n in new else pilot_root) for n in target]
    nr, pr = rows[:32], rows[32:]
    raw = {"source_stable": True, "protocol_sha256": export.sha256(study / "protocol.json"),
        "overall": export.aggregate(nr),
        "job_statuses": dict(Counter(records[n, s]["status"] for n in new for s in export.SEEDS))}
    old_raw = {"source_stable": True, "per_circuit": pr}
    write(study / "summary.json", raw)
    write(pilot_root / "summary.json", old_raw)
    for filename in ("analyze_outputs.py", "test_analyze_outputs.py"):
        (study / filename).write_text("# synthetic fixture, not an executable experiment\n")
    sources = {"extension_protocol_sha256": export.sha256(study / "protocol.json"),
        "extension_summary_sha256": export.sha256(study / "summary.json"),
        "pilot_protocol_sha256": export.sha256(pilot_root / "protocol.json"),
        "pilot_summary_sha256": export.sha256(pilot_root / "summary.json"),
        "analysis_source_sha256": export.sha256(study / "analyze_outputs.py"),
        "analysis_test_sha256": export.sha256(study / "test_analyze_outputs.py")}
    linked = {"protocol_id": "linked-target39-quality-analysis-v1", "formal_paper_result": False,
        "per_circuit": rows, "overall": export.aggregate(rows), "sources": sources,
        "job_statuses": dict(Counter(r["status"] for r in records.values()))}
    new_report = {"per_circuit": nr, "overall": export.aggregate(nr), "sources": sources,
        "job_statuses": raw["job_statuses"]}
    provenance = {"sources": sources, "new_phase_audit": audits["parallel_extension"],
        "linked_pilot7_audit": audits["serial_pilot"]}
    write(study / "linked39_analysis.json", linked)
    write(study / "new32_analysis.json", new_report)
    write(study / "postrun_analysis_provenance.json", provenance)
    pins = {key: export.sha256(path) for key, path in (
        ("PROTOCOL_SHA256", study / "protocol.json"),
        ("RAW_SUMMARY_SHA256", study / "summary.json"),
        ("SUMMARY_SHA256", study / "linked39_analysis.json"),
        ("NEW32_SUMMARY_SHA256", study / "new32_analysis.json"),
        ("ANALYSIS_PROVENANCE_SHA256", study / "postrun_analysis_provenance.json"),
        ("PILOT_PROTOCOL_SHA256", pilot_root / "protocol.json"),
        ("PILOT_SUMMARY_SHA256", pilot_root / "summary.json"))}
    return p, old, linked, new_report, records, roots, pins


class InitialLookaheadExtensionValuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.p, self.old, self.linked, self.new, self.records,
            self.roots, pins) = fixture(self.root)
        patcher = patch.multiple(export, **pins)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paper = self.root / "paper"

    def validate(self, p=None, linked=None, records=None, new=None):
        return export.validate_documents(p or self.p, self.old, linked or self.linked,
            new or self.new, records or self.records, self.roots)

    def invoke(self, check=False):
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = export.main(["--project-root", str(self.root), "--paper-root", str(self.paper)]
                + (["--check"] if check else []))
        return status, json.loads(stream.getvalue())

    def snapshot(self, path=None):
        path = path or self.root
        return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in path.rglob("*") if p.is_file()}

    def test_independent_metric_formula_and_all_planned_jobs(self):
        values, rows, phases, overall = self.validate()
        complete = [i for i in range(39) if i not in (0, 32)]
        self.assertEqual(values["InitExtTargetN"], 39)
        self.assertEqual(values["InitExtCompleteN"], 37)
        self.assertEqual(values["InitExtTimeouts"], 2)
        self.assertAlmostEqual(values["InitExtFidelityGain"],
            100 * math.expm1(statistics.mean(.0002 * (i + 1) for i in complete)), places=12)
        self.assertAlmostEqual(values["InitExtVsSAGain"],
            100 * math.expm1(statistics.mean(.0003 * (i + 1) for i in complete)), places=12)
        h0_mean = statistics.mean(100 + i * 7 for i in complete)
        self.assertAlmostEqual(values["InitExtBatchReduction"], 100 * 2 / h0_mean, places=12)
        self.assertEqual([len(phases[x]["circuits"]) for x in phases], [32, 7])
        self.assertEqual(rows[0]["common_valid_paired_seeds"], [0, 1])
        self.assertTrue(rows[0]["common_seed_comparisons"]["h2_vs_h0"]["descriptive_only"])

    def test_successful_export_and_check_are_deterministic(self):
        self.assertEqual(self.invoke()[0], 0)
        before = self.snapshot()
        with patch.object(Path, "write_text", side_effect=AssertionError("write in check")), patch.object(
                Path, "mkdir", side_effect=AssertionError("mkdir in check")):
            status, receipt = self.invoke(True)
        self.assertEqual(status, 0)
        self.assertTrue(receipt["read_only"])
        self.assertEqual(before, self.snapshot())
        payload = json.loads((self.paper / export.DERIVED_FILENAMES[1]).read_text())
        self.assertEqual(len(payload["job_outcomes"]), 117)
        self.assertEqual(len(payload["incomplete_circuits"]), 2)
        tex = (self.paper / export.DERIVED_FILENAMES[0]).read_text()
        self.assertIn(r"\InitExtTargetN}{39}", tex)
        self.assertIn(r"\InitExtCompleteN}{37}", tex)
        self.assertIn(r"\InitExtTimeouts}{2}", tex)
        for key in export.PERCENT_FIELDS:
            self.assertIn(f"\\{key}Rounded}}{{{payload['values'][key]:.2f}\\%}}", tex)
        prov = json.loads((self.paper / export.DERIVED_FILENAMES[2]).read_text())
        for name, digest in prov["derived_file_sha256"].items():
            self.assertEqual(export.sha256(self.paper / name), digest)

    def test_check_missing_directory_never_creates_it(self):
        before = self.snapshot()
        status, receipt = self.invoke(True)
        self.assertEqual(status, 1)
        self.assertEqual(len(receipt["stale_files"]), 3)
        self.assertFalse(self.paper.exists())
        self.assertEqual(before, self.snapshot())

    def test_check_rejects_each_stale_export_without_repair(self):
        self.invoke()
        for filename in export.DERIVED_FILENAMES:
            path = self.paper / filename
            original = path.read_bytes()
            path.write_bytes(original + b"stale\n")
            before = self.snapshot()
            status, receipt = self.invoke(True)
            self.assertEqual(status, 1)
            self.assertIn({"file": filename, "reason": "stale"}, receipt["stale_files"])
            self.assertEqual(before, self.snapshot())
            path.write_bytes(original)

    def test_wrong_seed_controls_rejected(self):
        for seeds in ([0, 1, 3], [0, 1], [0, 1, 1], [False, True, 2]):
            p = deepcopy(self.p)
            p["seeds"] = seeds
            with self.assertRaises(ValueError):
                self.validate(p=p)

    def test_missing_or_duplicate_job_in_protocol_rejected(self):
        for replacement in (None, self.p["job_order"][0]):
            p = deepcopy(self.p)
            if replacement is None:
                p["job_order"].pop()
            else:
                p["job_order"][-1] = deepcopy(replacement)
            with self.assertRaises(ValueError):
                self.validate(p=p)

    def test_missing_seed_record_rejected(self):
        records = deepcopy(self.records)
        del records["new01", 1]
        with self.assertRaises(ValueError):
            self.validate(records=records)

    def test_wrong_seed_identity_in_record_rejected(self):
        records = deepcopy(self.records)
        records["new01", 1]["seed"] = 2
        with self.assertRaises(ValueError):
            self.validate(records=records)

    def test_missing_or_substituted_circuit_rejected(self):
        for mutation in ("omit", "duplicate"):
            linked = deepcopy(self.linked)
            if mutation == "omit":
                linked["per_circuit"].pop()
            else:
                linked["per_circuit"][-1] = deepcopy(linked["per_circuit"][0])
            with self.assertRaises(ValueError):
                self.validate(linked=linked)

    def test_partial_seed_circuit_cannot_enter_primary_cohort(self):
        linked = deepcopy(self.linked)
        linked["per_circuit"][0]["complete"] = True
        with self.assertRaises(ValueError):
            self.validate(linked=linked)

    def test_cross_phase_timeout_cannot_be_removed(self):
        linked = deepcopy(self.linked)
        linked["job_statuses"]["timeout"] = 1
        with self.assertRaises(ValueError):
            self.validate(linked=linked)

    def test_wrong_within_circuit_aggregation_rejected(self):
        linked = deepcopy(self.linked)
        linked["per_circuit"][1]["arms"]["h2"]["mean_log_fidelity"] += .1
        with self.assertRaises(ValueError):
            self.validate(linked=linked)

    def test_wrong_geometric_or_batch_aggregation_rejected(self):
        for key in ("fidelity_relative_gain_percent", "move_batches_reduction_percent"):
            linked = deepcopy(self.linked)
            linked["overall"]["comparisons"]["h2_vs_h0"][key] += 1
            with self.assertRaises(ValueError):
                self.validate(linked=linked)

    def test_new32_stage_aggregation_verified_separately(self):
        new = deepcopy(self.new)
        new["overall"]["complete_circuits"] -= 1
        with self.assertRaises(ValueError):
            self.validate(new=new)

    def test_cross_phase_wall_time_pooling_rejected(self):
        linked = deepcopy(self.linked)
        linked["overall"]["comparisons"]["h2_vs_h0"]["end_to_end_time_ratio"] = 1.0
        with self.assertRaises(ValueError):
            self.validate(linked=linked)

    def test_missing_final_pin_fails_without_outputs(self):
        before = self.snapshot()
        with patch.object(export, "SUMMARY_SHA256", None):
            status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("not yet sealed", receipt["error"])
        self.assertEqual(before, self.snapshot())

    def test_hash_mismatch_fails_before_generation_without_writes(self):
        self.invoke()
        path = self.roots["parallel_extension"] / "linked39_analysis.json"
        path.write_text(path.read_text() + " ")
        before = self.snapshot()
        for check in (False, True):
            status, receipt = self.invoke(check)
            self.assertEqual(status, 1)
            self.assertIn("hash mismatch", receipt["error"])
            self.assertEqual(before, self.snapshot())

    def test_raw_seed_evidence_hash_mismatch_rejected(self):
        path = self.roots["parallel_extension"] / "jobs/new01-s0/h2_result.json"
        path.write_text(path.read_text() + " ")
        before = self.snapshot()
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("seed evidence hash mismatch", receipt["error"])
        self.assertEqual(before, self.snapshot())

    def test_missing_successful_result_fails_without_outputs(self):
        path = self.roots["parallel_extension"] / "jobs/new01-s0/h2_result.json"
        path.unlink()
        before = self.snapshot()
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("missing completed seed result", receipt["error"])
        self.assertEqual(before, self.snapshot())

    def test_nonfinite_raw_result_cannot_be_accepted(self):
        records = deepcopy(self.records)
        records["new01", 0]["results"]["h2"]["score"]["log_fidelity"] = float("nan")
        with self.assertRaises(ValueError):
            self.validate(records=records)

    def test_failure_before_export_does_not_modify_existing_files(self):
        self.invoke()
        path = self.roots["parallel_extension"] / "jobs/new01-s0/protocol.json"
        p = json.loads(path.read_text())
        p["dynamic_setting"]["seed"] = 2
        path.write_text(export.json_text(p))
        before = self.snapshot()
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("job seed/input/source identity mismatch", receipt["error"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
