"""Synthetic evidence tests; never import a compiler or read active study runs."""
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import math
from pathlib import Path
import shutil
import statistics
import tempfile
import unittest
from unittest.mock import patch

import generate_horizon_extension_values as export


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.original = Path("/sealed-original/project")
        self.study = self.root / export.STUDY_RELATIVE
        self.protocol = {"protocol_id": export.PROTOCOL_ID, "horizons": list(export.HORIZONS),
            "seeds": list(export.SEEDS), "circuits": [], "quality_jobs": [], "reused_quality": [],
            "configs": {}, "source_reports": {}, "frozen_source_commit": "a" * 40}
        self.manifests = {}
        self.receipts = {}
        self.path_by_key = {}
        self.new_keys = []
        self.old_keys = []
        p = self.protocol
        for name in ("architecture", "model", "wheel", "predecessor", "immutable_helper", "execution_plan"):
            relative = f"dependencies/{name}.json"
            self.write(relative, {"fixture": name})
            p[name] = self.reference(relative)
        p["wheel"]["abi"] = 9
        p["native"] = {"paper_native_abi_version": "9"}
        for prefix in ("paper_native_python", "paper_native_extension"):
            relative = f"dependencies/{prefix}"
            self.write(relative, {"fixture": prefix})
            p["native"][prefix + "_path"] = self.absolute(relative)
            p["native"][prefix + "_sha256"] = export.sha256(self.root / relative)
        driver = "ZAC_zzx/experiments_v2/horizon_extension_v2.py"
        self.write(driver, {"fixture": "driver"})
        p["extension_script"] = self.absolute(driver)
        p["extension_script_sha256"] = export.sha256(self.root / driver)
        self.write("dependencies/report.json", {"fixture": "old report"})
        p["source_reports"]["ablation"] = self.reference("dependencies/report.json")
        source = "IEEE_conference_template/build/horizon_extension_v2/frozen_source"
        self.write(source + "/ZAC_zzx/frozen.py", {"fixture": "source"})
        p["frozen_source_root"] = self.absolute(source)
        p["frozen_source_files"] = {"ZAC_zzx/frozen.py": export.sha256(self.root / source / "ZAC_zzx/frozen.py")}
        p["frozen_source_sha256"] = export.stable_hash(p["frozen_source_files"])
        for h in export.HORIZONS:
            for s in export.SEEDS:
                relative = str(export.STUDY_RELATIVE / f"configs/h{h}-s{s}.json")
                self.write(relative, self.wrapper(h, s))
                p["configs"][f"{h}:{s}"] = self.reference(relative)
        for i, (d, c) in enumerate(export.COHORT):
            relative = f"inputs/{d}/{c}.qasm"
            self.write(relative, {"fixture": c})
            canonical = {"canonical_path": self.absolute(relative), "canonical_sha256": export.sha256(self.root / relative),
                "qubits": 5, "gates_1q": 3, "gates_2q": 4}
            p["circuits"].append({"dataset": d, "circuit": c, "canonical": canonical})
            for h in export.HORIZONS:
                for s in export.SEEDS:
                    key = (d, c, h, s)
                    old = h in (0, 8) or (h in (2, 4) and s == 0)
                    (self.old_keys if old else self.new_keys).append(key)
                    job_id = f"{d}-{c}-h{h}-s{s}"
                    relative = str(export.STUDY_RELATIVE / f"fixture_runs/{job_id}/manifest.json")
                    config = p["configs"][f"{h}:{s}"]
                    manifest = {"dataset": d, "circuit": c, "seed": s, "repetition": 0,
                        "run_id": job_id, "method": "M4", "run_kind": "ablation", "status": "success",
                        "input_sha256": canonical["canonical_sha256"],
                        "architecture_sha256": p["architecture"]["sha256"], "model_sha256": p["model"]["sha256"],
                        "config_sha256": config["sha256"],
                        "command": ["fixture-python", "--input", canonical["canonical_path"], "--config", config["path"]],
                        "package_versions": {"horizon_extension_protocol": export.PROTOCOL_ID,
                            "horizon_frozen_source_commit": p["frozen_source_commit"],
                            "horizon_frozen_source_sha256": p["frozen_source_sha256"]},
                        "native_abi_version": 9, "backend": "native", "python_fallback": False,
                        "native_wheel_sha256": p["wheel"]["sha256"], "verifier_ok": True, "ghost_hits": 0,
                        "qubits": 5, "expected_gates_1q": 3, "observed_gates_1q": 3,
                        "expected_gates_2q": 4, "observed_gates_2q": 4,
                        "compiler_and_flags": {"native_abi_version": 9, "native_wheel_sha256": p["wheel"]["sha256"],
                            "extension_sha256": p["native"]["paper_native_extension_sha256"], "wheel_registered": True},
                        "forecast_summary": {"configured_depth": h, "alpha_lookahead": .5, "rho": .7, "epsilon": .05},
                        "expected_gate_ledger_sha256": "g" * 64, "observed_gate_ledger_sha256": "g" * 64,
                        "canonical_input_layer_ledger_sha256": "l" * 64, "observed_transition_layer_ledger_sha256": "l" * 64,
                        "fidelity": math.exp(-.2 * (i + 1)) * (1 + .001 * h * (i + 1)) * (1, .98, 1.08)[s],
                        "fidelity_ood": False, "move_batches": 100 + i - h + (3, 0, 80)[s]}
                    self.manifests[key] = manifest
                    self.path_by_key[key] = relative
                    self.write(relative, manifest)
                    item = dict(zip(export.IDENTITY, key))
                    if old:
                        p["reused_quality"].append({**item, "manifest": self.absolute(relative),
                            "manifest_sha256": export.sha256(self.root / relative)})
                    else:
                        p["quality_jobs"].append({**item, "job_id": job_id, "repetition": 0})
                        self.receipts[key] = {**item, "job_id": job_id, "repetition": 0, "phase": "quality",
                            "status": "success", "manifest": self.absolute(relative),
                            "manifest_sha256": export.sha256(self.root / relative)}
        self.seal()

    def absolute(self, relative):
        return str(self.original / relative)

    def reference(self, relative):
        return {"path": self.absolute(relative), "sha256": export.sha256(self.root / relative)}

    def local(self, path):
        return self.root / Path(path).relative_to(self.original)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=True) + "\n")

    def wrapper(self, h, s):
        return {"base_method": "M4", "search_policy": "ga", "run_kind": "ablation",
            "controls": {"decision_policy": "optimize", "fitness_phase_mode": "phase", "lookahead_horizon": h,
                "routing_batcher": "coloring", "search_policy": "ga"}, "base_config": {"zac_setting": [{
                "seed": s, "dir": "unused/", "backend": "native", "native_abi_version": 9,
                "native_wheel_sha256": self.protocol["wheel"]["sha256"], "engine": "ga", "init_engine": "sa",
                "objective": "physical_log_fidelity", "max_unique_evaluations": 576,
                "return_candidate_limit": 6, "return_assignment_k": 4, "alpha_lookahead": .5,
                "lookahead_horizon": {"max_horizon": h, "rho": .7, "epsilon": .05,
                    "decay": "geometric", "mode": "decay", "policy": "physical_terminal_decay_v1"}}]}}

    def seal(self):
        self.write(export.STUDY_RELATIVE / "protocol.json", self.protocol)
        self.pin = export.sha256(self.study / "protocol.json")
        self.write(export.STUDY_RELATIVE / "protocol.sha256.json", {"sha256": self.pin})
        for key, receipt in self.receipts.items():
            receipt["protocol_sha256"] = self.pin
            self.write(export.STUDY_RELATIVE / "receipts/quality" / (receipt["job_id"] + ".json"), receipt)
        self.summary = export.runner_aggregate(self.records(), export.COHORT)
        self.summary.update(protocol_sha256=self.pin, new_jobs_planned=84, new_jobs_claimed=84,
            new_jobs_pending=0, reused_records=96, timing_started=False,
            receipts=[self.receipts[k] for k in self.new_keys], records=self.records(),
            new_status_counts=dict(Counter(r["status"] for r in self.receipts.values())),
            dataset_summaries={d: export.runner_aggregate(self.records(), tuple(x for x in export.COHORT if x[0] == d))
                for d in ("zac18", "qmap154")})
        self.write_summary()

    def records(self):
        items = self.protocol["reused_quality"] + [self.receipts[k] for k in self.new_keys]
        return [export.quality_record(self.manifests[export.identity(item)], item) for item in items if "manifest" in item]

    def write_summary(self):
        self.write(export.STUDY_RELATIVE / "quality_summary.json", self.summary)

    def change_manifest(self, key, **changes):
        self.manifests[key].update(changes)
        path = self.path_by_key[key]
        self.write(path, self.manifests[key])
        item = self.receipts[key] if key in self.receipts else next(r for r in self.protocol["reused_quality"]
            if export.identity(r) == key)
        item["manifest_sha256"] = export.sha256(self.root / path)
        if key in self.receipts:
            item["status"] = self.manifests[key]["status"]
        self.seal()

    def continuation(self):
        """Append-only fixture: preserve 23 raw receipts, add 61 and two sidecars."""
        cont = export.STUDY_RELATIVE / "continuation_v1"
        ood_keys = [("qmap154", "clip_206", 1, 0), ("qmap154", "hwb8_113", 1, 2)]
        prior_keys = [k for k in self.new_keys if k not in ood_keys][:21] + ood_keys
        pending_keys = [k for k in self.new_keys if k not in prior_keys]
        sidecars = {}
        for key in ood_keys:
            manifest = self.manifests[key]
            manifest.update(fidelity=None, log_fidelity=None, fidelity_ood=True,
                ghost_policy="strict_zero", physicalization_policy="in_method_ghost_safe",
                trace_protocol="ours_lk_ghost_safe_v2")
            self.write(self.path_by_key[key], manifest)
            receipt = self.receipts[key]
            receipt.pop("manifest")
            receipt.pop("manifest_sha256")
            receipt.update(status="runner_error", error="ValueError('nominal success failed independent quality-domain checks')")
            relative_receipt = export.STUDY_RELATIVE / "receipts/quality" / (receipt["job_id"] + ".json")
            self.write(relative_receipt, receipt)
            scorer_path = str(Path(self.path_by_key[key]).with_name("fidelity.json"))
            self.write(scorer_path, {"result": {"ood": True, "fidelity": None, "log_fidelity": None,
                "move_batches": manifest["move_batches"],
                "components": {"coherence_linear": {"fidelity": None, "log_fidelity": None}},
                "warnings": ["linear coherence model out of domain for atoms: synthetic"]},
                "ghost_policy": "strict_zero", "physicalization_policy": "in_method_ghost_safe",
                "trace_protocol": "ours_lk_ghost_safe_v2"})
            sidecar = {"job_id": receipt["job_id"], "original_receipt": {**self.reference(relative_receipt), "status": "runner_error"},
                "manifest": {**self.reference(self.path_by_key[key]), "status": "success"},
                "scorer": self.reference(scorer_path), "classification": "success_model_ood",
                "physical_checks": {"verifier_ok": True, "ghost_hits": 0}, "fidelity": None, "log_fidelity": None,
                "base_protocol_sha256": self.pin}
            for field, filename in (("trace_zair", "trace.zair.json.gz"), ("canonical_trace", "canonical_trace.jsonl.gz")):
                path = str(Path(self.path_by_key[key]).with_name(filename))
                self.write(path, {"fixture": filename})
                sidecar[field] = self.reference(path)
            relative = cont / "classifications" / (receipt["job_id"] + ".json")
            self.write(relative, sidecar)
            sidecars[key] = (sidecar, self.reference(relative))
        driver = "ZAC_zzx/experiments_v2/horizon_extension_v2_continuation.py"
        self.write(driver, {"fixture": "continuation driver"})
        cp = {"protocol_id": "horizon-shared12-v2-ood-continuation-v1", "timing_execution_enabled": False,
            "base_protocol": self.reference(export.STUDY_RELATIVE / "protocol.json"),
            "base_driver": {"path": self.protocol["extension_script"], "sha256": self.protocol["extension_script_sha256"]},
            "continuation_driver": self.reference(driver), "prior_receipts": [],
            "pending_job_ids": [self.receipts[k]["job_id"] for k in pending_keys],
            "ood_sidecars": [pin for _, pin in sidecars.values()]}
        for key in prior_keys:
            receipt = self.receipts[key]
            relative = export.STUDY_RELATIVE / "receipts/quality" / (receipt["job_id"] + ".json")
            claim = export.STUDY_RELATIVE / "claims/quality" / (receipt["job_id"] + ".json")
            self.write(claim, {"job_id": receipt["job_id"], "protocol_sha256": self.pin})
            cp["prior_receipts"].append({"job_id": receipt["job_id"], **self.reference(relative),
                "raw_status": receipt["status"], "claim": self.reference(claim)})
        self.write(cont / "protocol.json", cp)
        pin = export.sha256(self.root / cont / "protocol.json")
        self.write(cont / "protocol.sha256.json", {"sha256": pin})
        for key in pending_keys:
            receipt = self.receipts[key]
            receipt.update(protocol_sha256=pin, base_protocol_sha256=self.pin, quality_classification="valid_fidelity")
            scorer_path = str(Path(self.path_by_key[key]).with_name("fidelity.json"))
            self.write(scorer_path, {"result": {"ood": False, "fidelity": self.manifests[key]["fidelity"]}})
            receipt["scorer"] = self.reference(scorer_path)
            relative = Path("receipts/quality") / (receipt["job_id"] + ".json")
            self.write(cont / relative, receipt)
            (self.study / relative).unlink()
        summary_receipts, records = [], []
        for item in self.protocol["reused_quality"]:
            records.append(export.quality_record(self.manifests[export.identity(item)], item))
        for key in self.new_keys:
            receipt = deepcopy(self.receipts[key])
            relative = (export.STUDY_RELATIVE if key in prior_keys else cont) / "receipts/quality" / (receipt["job_id"] + ".json")
            receipt["receipt"] = self.reference(relative)
            item = deepcopy(receipt)
            if key in ood_keys:
                sidecar, sidecar_pin = sidecars[key]
                receipt.update(ood_sidecar=sidecar_pin,
                    resolved_manifest={k: sidecar["manifest"][k] for k in ("path", "sha256")},
                    quality_classification="model_out_of_domain")
                item.update(manifest=sidecar["manifest"]["path"], manifest_sha256=sidecar["manifest"]["sha256"])
            records.append(export.quality_record(self.manifests[key], item))
            summary_receipts.append(receipt)
        summary = export.runner_aggregate(records, export.COHORT)
        summary.update(protocol_sha256=self.pin, continuation_protocol=self.reference(cont / "protocol.json"),
            new_jobs_planned=84, new_jobs_claimed=84, new_jobs_pending=0, reused_records=96, timing_started=False,
            receipts=summary_receipts, records=records, new_status_counts=dict(Counter(r["status"] for r in self.receipts.values())),
            dataset_summaries={d: export.runner_aggregate(records, tuple(x for x in export.COHORT if x[0] == d))
                for d in ("zac18", "qmap154")})
        self.write(export.STUDY_RELATIVE / "combined_quality_summary.json", summary)
        return pin, cp, sidecars, summary


class HorizonExtensionValuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.fixture = Fixture(self.root)
        self.pin_patch = patch.object(export, "PROTOCOL_SHA256", self.fixture.pin)
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        self.paper = Path(self.temp.name) / "paper"

    def refresh_pin(self):
        export.PROTOCOL_SHA256 = self.fixture.pin

    def invoke(self, check=False, root=None):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = export.main(["--project-root", str(root or self.root), "--paper-root", str(self.paper)]
                + (["--check"] if check else []))
        return code, json.loads(stream.getvalue())

    def snapshot(self):
        return {str(p.relative_to(self.temp.name)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in Path(self.temp.name).rglob("*") if p.is_file()}

    def test_independent_median_and_geometric_mean_formulas(self):
        values, rows, outcomes, hashes = export.load_verified(self.root)
        self.assertEqual(values["HorizonExtCompleteN"], 12)
        self.assertEqual(len(outcomes), 180)
        self.assertEqual(Counter(r["phase"] for r in outcomes), {"new": 84, "reused": 96})
        self.assertGreater(len(hashes), 280)
        for h in export.HORIZONS:
            expected_f = math.exp(statistics.mean(math.log((1 + .001 * h * (i + 1)) / (1 + .008 * (i + 1)))
                for i in range(12)))
            expected_b = math.exp(statistics.mean(math.log((103 + i - h) / (95 + i)) for i in range(12)))
            self.assertAlmostEqual(values[f"HorizonExt{export.WORDS[h]}FidelityRatio"], expected_f)
            self.assertAlmostEqual(values[f"HorizonExt{export.WORDS[h]}BatchRatio"], expected_b)
        self.assertEqual(rows[0]["horizons"]["8"]["fidelity_ratio_vs_h8"], 1)

    def test_exports_are_isolated_and_check_is_read_only(self):
        self.assertEqual(self.invoke()[0], 0)
        before = self.snapshot()
        with patch.object(Path, "write_text", side_effect=AssertionError("check writes")), patch.object(
                Path, "mkdir", side_effect=AssertionError("check creates directory")):
            code, result = self.invoke(True)
        self.assertEqual(code, 0)
        self.assertTrue(result["read_only"])
        self.assertEqual(before, self.snapshot())
        tex = (self.paper / export.DERIVED_FILENAMES[0]).read_text()
        self.assertEqual(tex.count("\\newcommand"), 14)
        self.assertNotIn("Ablation", tex)
        self.assertNotIn("InitExt", tex)
        self.assertIn(r"\HorizonExtEightFidelityRatio}{1.0000}", tex)
        self.assertIn(r"\HorizonExtEightBatchRatio}{1.0000}", tex)
        provenance = json.loads((self.paper / export.DERIVED_FILENAMES[2]).read_text())
        self.assertTrue(all(not k.startswith("/sealed-original") for k in provenance["input_sha256"]))
        for name, digest in provenance["derived_file_sha256"].items():
            self.assertEqual(export.sha256(self.paper / name), digest)

    def test_missing_or_stale_outputs_are_not_repaired_in_check(self):
        before = self.snapshot()
        self.assertEqual(self.invoke(True)[0], 1)
        self.assertFalse(self.paper.exists())
        self.assertEqual(before, self.snapshot())
        self.invoke()
        for filename in export.DERIVED_FILENAMES:
            path = self.paper / filename
            old = path.read_bytes()
            path.write_bytes(old + b"stale")
            before = self.snapshot()
            code, result = self.invoke(True)
            self.assertEqual(code, 1)
            self.assertIn({"file": filename, "reason": "stale"}, result["stale_files"])
            self.assertEqual(before, self.snapshot())
            path.write_bytes(old)

    def test_relocated_clone_rebases_absolute_paths_without_original_checkout(self):
        clone = Path(self.temp.name) / "different-clone"
        shutil.copytree(self.root, clone)
        self.assertEqual(export.expected_exports(self.root), export.expected_exports(clone))

    def test_missing_final_summary_never_uses_session_snapshot(self):
        (self.fixture.study / "quality_summary.json").rename(self.fixture.study / "session_summary.json")
        before = self.snapshot()
        self.assertEqual(self.invoke()[0], 1)
        self.assertFalse(self.paper.exists())
        self.assertEqual(before, self.snapshot())

    def test_pending_summary_refuses_publication(self):
        self.fixture.summary["new_jobs_pending"] = 1
        self.fixture.write_summary()
        self.assertEqual(self.invoke()[0], 1)
        self.assertFalse(self.paper.exists())

    def test_missing_terminal_receipt_and_running_status_refuse_publication(self):
        key = self.fixture.new_keys[0]
        receipt = self.fixture.receipts[key]
        path = self.fixture.study / "receipts/quality" / (receipt["job_id"] + ".json")
        path.unlink()
        self.assertEqual(self.invoke()[0], 1)
        receipt["status"] = "running"
        self.fixture.seal()
        self.refresh_pin()
        self.assertEqual(self.invoke()[0], 1)

    def test_manifest_hash_drift_refuses_publication(self):
        path = self.root / self.fixture.path_by_key[self.fixture.old_keys[0]]
        path.write_bytes(path.read_bytes() + b"\n")
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("hash mismatch", report["error"])

    def test_null_manifest_hash_is_not_an_unpinned_read(self):
        self.fixture.receipts[self.fixture.new_keys[0]]["manifest_sha256"] = None
        self.fixture.seal()
        self.refresh_pin()
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("missing SHA-256", report["error"])

    def test_summary_metric_and_record_drift_are_detected(self):
        for mutation in (lambda s: s["fidelity_ratio_vs_h8"].update({"0": 42.0}),
                         lambda s: s["records"][0].update(fidelity=.999),
                         lambda s: s["records"].append(deepcopy(s["records"][0])),
                         lambda s: s["receipts"].append(deepcopy(s["receipts"][0]))):
            with self.subTest(mutation=mutation):
                self.fixture.seal()
                mutation(self.fixture.summary)
                self.fixture.write_summary()
                self.assertEqual(self.invoke()[0], 1)

    def test_fixed_cohort_and_double_counting_are_rejected_even_if_resealed(self):
        original = deepcopy(self.fixture.protocol)
        for field in ("circuits", "quality_jobs", "reused_quality"):
            self.fixture.protocol = deepcopy(original)
            self.fixture.protocol[field][-1] = deepcopy(self.fixture.protocol[field][0])
            self.fixture.write(export.STUDY_RELATIVE / "protocol.json", self.fixture.protocol)
            self.fixture.pin = export.sha256(self.fixture.study / "protocol.json")
            self.fixture.write(export.STUDY_RELATIVE / "protocol.sha256.json", {"sha256": self.fixture.pin})
            self.fixture.summary["protocol_sha256"] = self.fixture.pin
            self.fixture.write_summary()
            self.refresh_pin()
            self.assertEqual(self.invoke()[0], 1)

    def test_config_budget_and_other_settings_differ_even_with_new_hash(self):
        for field, value in (("max_unique_evaluations", 1152), ("extra_setting", "different")):
            relative = export.STUDY_RELATIVE / "configs/h1-s0.json"
            wrapper = self.fixture.wrapper(1, 0)
            wrapper["base_config"]["zac_setting"][0][field] = value
            self.fixture.write(relative, wrapper)
            self.fixture.protocol["configs"]["1:0"] = self.fixture.reference(relative)
            self.fixture.seal()
            self.refresh_pin()
            code, report = self.invoke()
            self.assertEqual(code, 1)
            self.assertTrue("config" in report["error"] or "compiler setting" in report["error"])

    def test_protocol_and_frozen_source_hashes_are_mandatory(self):
        source = self.fixture.local(self.fixture.protocol["frozen_source_root"]) / "ZAC_zzx/frozen.py"
        original = source.read_bytes()
        source.write_bytes(original + b"drift")
        self.assertEqual(self.invoke()[0], 1)
        source.write_bytes(original)
        self.fixture.write(export.STUDY_RELATIVE / "protocol.sha256.json", {"sha256": "0" * 64})
        self.assertEqual(self.invoke()[0], 1)

    def test_receipt_and_manifest_status_disagreement_is_not_summary_truth(self):
        key = self.fixture.new_keys[0]
        self.fixture.receipts[key]["status"] = "timeout"
        self.fixture.seal()
        self.refresh_pin()
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("status mismatch", report["error"])

    def test_budget_and_native_input_controls_cannot_change(self):
        key = self.fixture.new_keys[0]
        changes = ({"input_sha256": "0" * 64}, {"native_abi_version": 10},
            {"python_fallback": True}, {"verifier_ok": False}, {"ghost_hits": 1},
            {"forecast_summary": {"configured_depth": 8, "alpha_lookahead": .5, "rho": .7, "epsilon": .05}})
        original = deepcopy(self.fixture.manifests[key])
        for change in changes:
            self.fixture.manifests[key] = deepcopy(original)
            self.fixture.change_manifest(key, **change)
            self.refresh_pin()
            self.assertEqual(self.invoke()[0], 1)

    def test_timeout_and_ood_remain_in_coverage_not_primary_cohort(self):
        keys = [self.fixture.new_keys[0], next(k for k in self.fixture.new_keys if k[1] != self.fixture.new_keys[0][1])]
        self.fixture.change_manifest(keys[0], status="timeout", fidelity=None, move_batches=None)
        self.fixture.change_manifest(keys[1], fidelity=None, fidelity_ood=True)
        self.refresh_pin()
        values, rows, outcomes, _ = export.load_verified(self.root)
        self.assertEqual(values["HorizonExtCompleteN"], 10)
        self.assertEqual(len([r for r in rows if not r["complete"]]), 2)
        self.assertEqual(len(outcomes), 180)
        self.assertEqual(Counter(r["status"] for r in outcomes)["timeout"], 1)

    def test_terminal_runner_error_without_manifest_is_retained(self):
        key = self.fixture.new_keys[0]
        receipt = self.fixture.receipts[key]
        receipt.pop("manifest")
        receipt.pop("manifest_sha256")
        receipt.pop("phase")  # The continuation's genuine exception receipt may omit phase.
        receipt["status"] = "runner_error"
        self.fixture.seal()
        self.refresh_pin()
        values, _, outcomes, _ = export.load_verified(self.root)
        self.assertEqual(values["HorizonExtCompleteN"], 11)
        self.assertEqual(len(outcomes), 180)
        self.assertEqual(len(self.fixture.summary["records"]), 179)

    def test_zero_fidelity_or_nonfinite_batch_excludes_entire_circuit_for_both_metrics(self):
        for value, field in ((0.0, "fidelity"), (float("nan"), "fidelity"),
                             (float("inf"), "move_batches"), (-1, "move_batches")):
            with self.subTest(value=value, field=field):
                key = self.fixture.new_keys[0]
                old = deepcopy(self.fixture.manifests[key])
                self.fixture.change_manifest(key, **{field: value})
                self.refresh_pin()
                exports, values = export.expected_exports(self.root)
                self.assertEqual(values["HorizonExtCompleteN"], 11)
                data = json.loads(exports[export.DERIVED_FILENAMES[1]])
                self.assertEqual(len(data["primary_cohort"]), 11)
                self.fixture.manifests[key] = old
                self.fixture.change_manifest(key)
                self.refresh_pin()

    def test_zero_batches_are_defined_only_with_positive_reference_median(self):
        d, c = export.COHORT[0]
        for seed in export.SEEDS:
            self.fixture.change_manifest((d, c, 0, seed), move_batches=0)
        self.refresh_pin()
        values = export.load_verified(self.root)[0]
        self.assertEqual(values["HorizonExtZeroBatchRatio"], 0)
        self.assertEqual(values["HorizonExtCompleteN"], 12)
        for seed in export.SEEDS:
            self.fixture.change_manifest((d, c, 8, seed), move_batches=0)
        self.refresh_pin()
        self.assertEqual(export.load_verified(self.root)[0]["HorizonExtCompleteN"], 11)

    def test_no_complete_circuit_refuses_all_output(self):
        for d, c in export.COHORT:
            key = (d, c, 1, 0)
            self.fixture.change_manifest(key, fidelity=0.0)
        self.refresh_pin()
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("no complete valid", report["error"])
        self.assertFalse(self.paper.exists())

    def test_append_only_continuation_keeps_raw_errors_and_excludes_verified_ood(self):
        pin, _, _, _ = self.fixture.continuation()
        with patch.object(export, "CONTINUATION_SHA256", pin):
            before = self.snapshot()
            values, rows, outcomes, hashes = export.load_verified(self.root)
            self.assertEqual(before, self.snapshot())
        self.assertEqual(values["HorizonExtCompleteN"], 10)
        self.assertEqual(len(outcomes), 180)
        resolved = [r for r in outcomes if "raw_receipt_status" in r]
        self.assertEqual(len(resolved), 2)
        self.assertTrue(all(r["raw_receipt_status"] == "runner_error" and r["status"] == "success"
            and r["fidelity_ood"] is True and r["exclusions"] for r in resolved))
        self.assertTrue(any("combined_quality_summary" in path for path in hashes))

    def test_unpinned_or_incomplete_continuation_cannot_use_old_summary(self):
        pin, cp, _, _ = self.fixture.continuation()
        with patch.object(export, "CONTINUATION_SHA256", None):
            self.assertEqual(self.invoke()[0], 1)
        path = self.fixture.study / "continuation_v1/receipts/quality" / (cp["pending_job_ids"][0] + ".json")
        path.unlink()
        with patch.object(export, "CONTINUATION_SHA256", pin):
            self.assertEqual(self.invoke()[0], 1)

    def test_continuation_does_not_hide_original_receipt_or_manifest_drift(self):
        pin, cp, sidecars, _ = self.fixture.continuation()
        paths = [self.fixture.local(cp["prior_receipts"][0]["path"]),
            self.fixture.local(next(iter(sidecars.values()))[0]["manifest"]["path"]),
            self.fixture.local(next(iter(sidecars.values()))[0]["scorer"]["path"])]
        with patch.object(export, "CONTINUATION_SHA256", pin):
            for path in paths:
                old = path.read_bytes()
                path.write_bytes(old + b"\n")
                self.assertEqual(self.invoke()[0], 1)
                path.write_bytes(old)

    def test_combined_summary_cannot_relabel_original_runner_error(self):
        pin, _, _, summary = self.fixture.continuation()
        next(r for r in summary["receipts"] if "ood_sidecar" in r)["status"] = "success"
        self.fixture.write(export.STUDY_RELATIVE / "combined_quality_summary.json", summary)
        with patch.object(export, "CONTINUATION_SHA256", pin):
            self.assertEqual(self.invoke()[0], 1)

    def test_continuation_record_order_does_not_change_analysis(self):
        pin, _, _, summary = self.fixture.continuation()
        with patch.object(export, "CONTINUATION_SHA256", pin):
            expected = export.load_verified(self.root)[:3]
            summary["receipts"].reverse()
            summary["records"].reverse()
            summary.pop("dataset_summaries")  # Actual continuation reports omit this optional detail.
            self.fixture.write(export.STUDY_RELATIVE / "combined_quality_summary.json", summary)
            self.assertEqual(export.load_verified(self.root)[:3], expected)

    def test_continuation_trace_hashes_are_not_optional(self):
        pin, _, sidecars, _ = self.fixture.continuation()
        path = self.fixture.local(next(iter(sidecars.values()))[0]["trace_zair"]["path"])
        path.write_bytes(path.read_bytes() + b"drift")
        with patch.object(export, "CONTINUATION_SHA256", pin):
            self.assertEqual(self.invoke()[0], 1)


if __name__ == "__main__":
    unittest.main()
