"""Isolated refinement protocol regressions; no compiler matrix is launched."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments_v2 import refinement_study as study


def cohort_fixture():
    canonical = {"zac18": {"canonical_inputs": {}}, "qmap154": {"canonical_inputs": {}}}
    identities, payloads = [], {}
    for dataset, count in (("zac18", 18), ("qmap154", 30)):
        for index in range(count):
            name = f"{dataset}_{index}"
            payload_index = 28 if dataset == "qmap154" and index == 29 else index
            payload = f"// fixed fixture {dataset}:{payload_index}\n".encode()
            canonical[dataset]["canonical_inputs"][name] = hashlib.sha256(payload).hexdigest()
            identities.append([dataset, name])
            payloads[dataset, name] = payload
    strata = {key: {"circuits": [f"qmap154_{index}" for index in range(offset, offset + 10)]}
              for key, offset in (("le_300", 0), ("301_1500", 10), ("gt_1500", 20))}
    excluded = [["zac18", f"zac18_{i}"] for i in range(6)] + [
        ["qmap154", f"qmap154_{i}"] for i in (0, 1, 10, 11, 20, 21)]
    return {"canonical_suites": canonical, "parity_timing_cohort": {"identities": excluded},
            "ablation_cohort": {"identities": identities,
                                "datasets": {"qmap154": {"strata": strata}}}}, payloads


BASE = {"return_candidate_limit": 6, "return_assignment_k": 4,
        "native_wheel_sha256": "a" * 64, "native_abi_version": 9,
        "backend": "native", "formal_native": True, "native_fail_closed": True,
        "objective": "physical_log_fidelity", "seed": 0, "max_unique_evaluations": 576,
        "lookahead_horizon": {"max_horizon": 8, "rho": .7}}


class RefinementStudyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        self.freeze, payloads = cohort_fixture()
        for relative, data in ((study.FREEZE, self.freeze),
                               (study.CONFIG, {"base_config": {"zac_setting": [BASE]}}),
                               (study.ARCH, {"fixture": "architecture"}),
                               ("ZAC_zzx/results/paper_zh_v2/accepted.json", {"keep": True}),
                               ("ZAC_zzx/results/initial_lookahead_v1/accepted.json", {"keep": True})):
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        for (dataset, name), payload in payloads.items():
            path = self.repo / "fidelity-lookahead-v2/artifacts/canonical" / dataset / f"{name}.qasm"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.name = "unit-study"
        self.build = self.repo / "IEEE_conference_template/build/refinement_v1" / self.name
        self.output = self.repo / "ZAC_zzx/results/refinement_v1" / self.name
        self.protocol_path = self.output / "protocol.json"

    def fake_export(self, repo, destination):
        result = {}
        for relative in study.SOURCE_REQUIRED:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# frozen fixture\n")
            result[str(path)] = study.file_hash(path)
        return result

    def seal(self):
        native = {"native_abi_version": 9, "wheel_registered": True,
                  "native_wheel_sha256": BASE["native_wheel_sha256"],
                  "extension_sha256": "e" * 64, "extension_path": "/isolated/native.so"}
        probe = {"native": native, "model": {"coherence_time_us": 1500000.0},
                 "verifier_file": str(self.build / "source/verify_batches.py")}
        with patch.object(study, "export_source", side_effect=self.fake_export), \
                patch.object(study.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(probe))):
            result = study.freeze_study(self.repo, self.name)
        self.assertEqual(result["status"], "sealed")
        return study.read_protocol(self.protocol_path)

    def seal_phase(self, protocol, phase="development", selected=None):
        root = self.output / phase
        root.mkdir(exist_ok=True)
        document = {"phase": phase, "sealed_at": "fixed-test-time", "jobs": study.phase_jobs(protocol, phase, selected),
                    "protocol_sha256": protocol["seal_sha256"], "selected_variant": selected,
                    "development_decision_sha256": study.file_hash(self.output / "development/decision.json")
                    if phase == "validation" else None}
        document["phase_sha256"] = study.stable_hash(document)
        path = root / "phase_protocol.json"
        study.write_json(path, document)
        return path, document

    def make_result(self, protocol, document, job, *, gain=.002):
        phase_root = self.output / document["phase"]
        job_root = phase_root / "jobs" / job["job_id"]
        job_root.mkdir(parents=True)
        trace = job_root / "native_trace.json.gz"
        trace.write_bytes(b"immutable trace fixture")
        setting = deepcopy(protocol["variants"][job["variant"]])
        setting.update(seed=job["seed"], name=job["circuit"], dir=str(job_root) + "/",
                       arch_spec=protocol["architecture"], use_verifier=False, resyn=False)
        delta = 0 if job["variant"] == "reference" else gain
        result = {key: job[key] for key in ("job_id", "variant", "seed", "dataset", "circuit", "canonical_sha256")}
        result.update(status="success", protocol_sha256=protocol["seal_sha256"], phase_sha256=document["phase_sha256"],
                      setting=setting, native_runtime=protocol["native"], scoring_model=protocol["scoring_model"],
                      frozen_source_unchanged=True, zair_validation={"ok": True, "errors": {"ghost": []}},
                      validation={"ok": True, "ghost_hits": 0}, logical_validation={"ok": True},
                      native_trace_sha256=study.file_hash(trace), score={"log_fidelity": -1 + delta, "ood": False},
                      compile_cpu_ns=100, initial_mapping_sha256="0" * 64)
        result_path = job_root / "result.json"
        study.write_json(result_path, result)
        receipt_root = phase_root / "receipts"
        receipt_root.mkdir(exist_ok=True)
        receipt = {"job_id": job["job_id"], "status": "success", "result_sha256": study.file_hash(result_path),
                   "phase_protocol_sha256": study.file_hash(phase_root / "phase_protocol.json")}
        receipt_path = receipt_root / (job["job_id"] + ".json")
        study.write_json(receipt_path, receipt)
        return result_path, receipt_path

    def mutate_result(self, result_path, receipt_path, mutator):
        result = study.load(result_path)
        mutator(result)
        result_path.write_text(json.dumps(result))
        receipt = study.load(receipt_path)
        receipt["result_sha256"] = study.file_hash(result_path)
        receipt_path.write_text(json.dumps(receipt))

    def test_split_is_12_development_23_validation_with_aliases_grouped(self):
        split = study.derive_split(self.freeze)
        self.assertEqual([len(split[p]) for p in ("development", "validation")], [12, 23])
        development = {r["canonical_sha256"] for r in split["development"]}
        validation = {r["canonical_sha256"] for r in split["validation"]}
        self.assertFalse(development & validation)
        aliases = [r for p in ("development", "validation") for r in split[p] if len(r["aliases"]) == 2]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["aliases"], ["qmap154_28", "qmap154_29"])
        excluded = {self.freeze["canonical_suites"][ds]["canonical_inputs"][name] for ds, name in split["excluded"]}
        self.assertFalse((development | validation) & excluded)
        reversed_freeze = deepcopy(self.freeze)
        reversed_freeze["ablation_cohort"]["identities"].reverse()
        self.assertEqual(study.derive_split(reversed_freeze), split)

    def test_split_refuses_changed_input_counts(self):
        self.freeze["ablation_cohort"]["identities"].pop()
        with self.assertRaisesRegex(ValueError, "48/12"):
            study.derive_split(self.freeze)

    def test_variants_change_exactly_one_registered_domain_field(self):
        original = deepcopy(BASE)
        for variant, change in study.VARIANTS.items():
            setting = study.variant_setting(BASE, variant)
            actual = {key: value for key, value in setting.items() if value != BASE[key]}
            self.assertEqual(actual, change)
            setting["lookahead_horizon"]["rho"] = .1
            self.assertEqual(BASE, original)
        with self.assertRaises(ValueError):
            study.variant_setting(BASE, "invented")
        with self.assertRaises(ValueError):
            study.variant_setting({**BASE, "return_assignment_k": 5}, "reference")

    def test_export_includes_top_level_real_verifier_and_refuses_incomplete_closure(self):
        target = self.repo / "export"
        names = ["ZAC_zzx/" + name for name in study.SOURCE_REQUIRED]
        def git(arguments, **kwargs):
            if arguments[1] == "ls-tree":
                self.assertIn("ZAC_zzx/verify_batches.py", arguments)
                return "\n".join(names)
            return b"# committed content\n"
        with patch.object(study.subprocess, "check_output", side_effect=git):
            hashes = study.export_source(self.repo, target)
        self.assertIn(str(target / "verify_batches.py"), hashes)
        names.remove("ZAC_zzx/verify_batches.py")
        with patch.object(study.subprocess, "check_output", side_effect=git), \
                self.assertRaisesRegex(ValueError, "required compiler/verifier"):
            study.export_source(self.repo, self.repo / "incomplete")

    def test_new_seal_uses_isolated_build_and_preserves_existing_evidence(self):
        before = study.protected_hashes(self.repo)
        protocol = self.seal()
        self.assertEqual(study.protected_hashes(self.repo), before)
        self.assertEqual(protocol["native"]["native_abi_version"], 9)
        self.assertFalse((self.repo / "build").exists())
        self.assertEqual(Path(protocol["build"]), self.build)
        self.assertFalse(protocol["formal_paper_result"])
        self.assertFalse(protocol["automatic_promotion"])
        self.assertEqual(study.verify_protected(self.repo, protocol)["unchanged"], True)
        with self.assertRaises(FileExistsError):
            study.freeze_study(self.repo, self.name)

    def test_source_drift_or_extra_python_file_is_rejected(self):
        protocol = self.seal()
        extra = Path(protocol["source"]) / "unsealed.py"
        extra.write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "closure changed"):
            study.read_protocol(self.protocol_path)
        extra.unlink()
        verifier = Path(protocol["source"]) / "verify_batches.py"
        verifier.write_text("# modified\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            study.read_protocol(self.protocol_path)

    def test_settings_or_queue_changes_are_rejected_even_if_json_resealed(self):
        original = self.seal()
        changes = (lambda p: p["variants"]["A_domain8"].update(max_unique_evaluations=1000),
                   lambda p: p.update(workers=2), lambda p: p.update(seeds={"development": [1], "validation": [0, 1, 2]}),
                   lambda p: p["split"]["development"][0].update(aliases=["leaked_alias"]))
        for change in changes:
            with self.subTest(change=change):
                modified = deepcopy(original)
                change(modified)
                modified.pop("seal_sha256")
                modified["seal_sha256"] = study.stable_hash(modified)
                self.protocol_path.write_text(json.dumps(modified))
                with self.assertRaises(ValueError):
                    study.read_protocol(self.protocol_path)

    def test_canonical_and_reference_dependencies_are_hash_bound(self):
        protocol = self.seal()
        config = Path(protocol["reference_config"])
        config.write_text(config.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "source input changed"):
            study.read_protocol(self.protocol_path)

    def test_registered_job_counts_and_smoke_do_not_read_validation(self):
        protocol = self.seal()
        development = study.phase_jobs(protocol, "development")
        self.assertEqual(len(development), 36)
        self.assertEqual(len(study.phase_jobs(protocol, "validation", "A_domain8")), 138)
        self.assertEqual({j["variant"] for j in development}, set(study.VARIANTS))
        smoke = study.phase_jobs(protocol, "smoke")
        self.assertEqual(len(smoke), 1)
        self.assertEqual(smoke[0]["variant"], "reference")
        self.assertEqual(smoke[0]["canonical_sha256"], protocol["split"]["development"][0]["canonical_sha256"])
        self.assertEqual(smoke[0]["seed"], 0)
        with self.assertRaises(ValueError):
            study.phase_jobs(protocol, "validation")

    def test_phase_rejects_removed_or_reordered_jobs_after_resealing(self):
        protocol = self.seal()
        path, document = self.seal_phase(protocol)
        document["jobs"].reverse()
        document.pop("phase_sha256")
        document["phase_sha256"] = study.stable_hash(document)
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "registered queue"):
            study.read_phase_protocol(protocol, path)

    def test_incomplete_phase_cannot_produce_decision(self):
        protocol = self.seal()
        self.seal_phase(protocol)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            study.analyze_phase(self.protocol_path, "development")
        self.assertFalse((self.output / "development/decision.json").exists())

    def test_success_requires_real_gate_score_native_and_effective_configuration(self):
        protocol = self.seal()
        _, document = self.seal_phase(protocol, "smoke")
        job = document["jobs"][0]
        result_path, receipt_path = self.make_result(protocol, document, job)
        baseline = study.load(result_path)
        mutations = (
            lambda r: r.pop("zair_validation"),
            lambda r: r["zair_validation"].update(errors={"adjacency": ["invalid gate site"]}),
            lambda r: r["logical_validation"].update(ok=False),
            lambda r: r["validation"].update(ghost_hits=1),
            lambda r: r["score"].update(ood=True),
            lambda r: r["score"].update(log_fidelity=float("nan")),
            lambda r: r.update(compile_cpu_ns=0),
            lambda r: r["native_runtime"].update(native_wheel_sha256="different"),
            lambda r: r["setting"].update(max_unique_evaluations=2000),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result_path.write_text(json.dumps(baseline))
                self.mutate_result(result_path, receipt_path, mutation)
                with self.assertRaises(ValueError):
                    study.read_phase_records(self.output, "smoke", [job], protocol=protocol)

    def test_trace_bytes_and_receipt_identity_are_validated(self):
        protocol = self.seal()
        _, document = self.seal_phase(protocol, "smoke")
        job = document["jobs"][0]
        result, receipt = self.make_result(protocol, document, job)
        result.with_name("native_trace.json.gz").write_bytes(b"tampered trace")
        with self.assertRaisesRegex(ValueError, "native trace"):
            study.read_phase_records(self.output, "smoke", [job], protocol=protocol)
        data = study.load(receipt)
        data["job_id"] = "different job"
        receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "receipt identity"):
            study.read_phase_records(self.output, "smoke", [job], protocol=protocol)

    def test_development_selection_does_not_consume_validation_files(self):
        protocol = self.seal()
        _, document = self.seal_phase(protocol)
        for job in document["jobs"]:
            self.make_result(protocol, document, job, gain=.003 if job["variant"] == "A_domain8" else .002)
        (self.output / "validation").mkdir()
        (self.output / "validation/validation_report.json").write_text("DO NOT READ: hidden outcomes")
        report = study.analyze_phase(self.protocol_path, "development")
        self.assertEqual(report["selected_variant"], "A_domain8")
        self.assertFalse(report["formal_paper_result"])
        self.assertEqual((self.output / "validation/validation_report.json").read_text(), "DO NOT READ: hidden outcomes")
        self.assertEqual(study.analyze_phase(self.protocol_path, "development"), report)

    def test_smoke_is_diagnostic_and_never_selects_variant(self):
        protocol = self.seal()
        _, document = self.seal_phase(protocol, "smoke")
        self.make_result(protocol, document, document["jobs"][0])
        report = study.analyze_phase(self.protocol_path, "smoke")
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["acceptance_eligible"])
        self.assertNotIn("selected_variant", report)
        self.assertFalse((self.output / "development").exists())

    def test_missing_protected_evidence_or_modification_is_fail_closed(self):
        protocol = self.seal()
        (self.repo / "ZAC_zzx/results/paper_zh_v2/accepted.json").write_text("authoritative data changed")
        with self.assertRaisesRegex(RuntimeError, "existing evidence changed"):
            study.verify_protected(self.repo, protocol)
        with patch.object(study.subprocess, "Popen") as popen, self.assertRaises(RuntimeError):
            study.run_phase(self.protocol_path, "smoke")
        popen.assert_not_called()

    def test_timeout_is_retained_and_not_retried(self):
        self.seal()
        process = SimpleNamespace(pid=123456, returncode=None)
        process.poll = lambda: process.returncode
        def kill(p):
            p.returncode = -9
        with patch.object(study.subprocess, "Popen", return_value=process) as popen, \
                patch.object(study.time, "monotonic", side_effect=[0, 601, 602]), \
                patch.object(study, "kill_group", side_effect=kill):
            report = study.run_phase(self.protocol_path, "smoke")
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(report["status"], "failed")
        receipts = list((self.output / "smoke/receipts").glob("*.json"))
        completed = [p for p in receipts if not p.name.endswith(".started.json")]
        self.assertEqual(study.load(completed[0])["status"], "timeout")
        with patch.object(study.subprocess, "Popen") as popen:
            study.run_phase(self.protocol_path, "smoke")
        popen.assert_not_called()

    def comparison_fixture(self):
        groups, records = [], {}
        for index in range(12):
            dataset = "zac18" if index < 6 else "qmap154"
            group = {"canonical_sha256": f"{index:064x}", "circuit": f"test-{index}",
                     "dataset": dataset, "stratum": dataset}
            groups.append(group)
            for seed in (0, 1, 2):
                for variant in ("reference", "A_domain8"):
                    records[group["canonical_sha256"], seed, variant] = {
                        "status": "success", "score": {"log_fidelity": -1 if variant == "reference" else -.997},
                        "compile_cpu_ns": 100, "initial_mapping_sha256": "same"}
        return groups, records

    def test_unchanged_acceptance_gates_require_quality_cpu_coverage_and_validation_support(self):
        groups, records = self.comparison_fixture()
        accepted = study.compare_candidate(groups, [0, 1, 2], records, "A_domain8", validation=True)
        self.assertTrue(accepted["passed"])
        self.assertGreater(accepted["one_sided_95_lower_bound"], 1)
        key = (groups[0]["canonical_sha256"], 0, "A_domain8")
        records[key]["status"] = "timeout"
        self.assertFalse(study.compare_candidate(groups, [0, 1, 2], records, "A_domain8")["passed"])
        groups, records = self.comparison_fixture()
        for (_, _, variant), row in records.items():
            if variant == "A_domain8":
                row["compile_cpu_ns"] = 121
        self.assertFalse(study.compare_candidate(groups, [0, 1, 2], records, "A_domain8")["checks"]["cpu_overhead"])


if __name__ == "__main__":
    unittest.main()
