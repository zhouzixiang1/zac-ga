"""Fixture tests for fail-closed paper-native baseline raw replay."""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REAL_PLAN = ROOT / "experiments_v2" / "experiment_plan_v2.json"
REAL_ARTIFACT_ROOT = ROOT.parents[1] / "artifacts"
REAL_BASELINE_ARCHIVE = (
    REAL_ARTIFACT_ROOT / "archive" / "legacy-20260823" / "runs"
)
REAL_M1_LINEAGE = (
    REAL_ARTIFACT_ROOT / "native-ga-v1" /
    "tuning-quality-v1-51583a5" / "workspace.json"
)

from experiments_v2.baseline_raw_replay import (  # noqa: E402
    audit_baseline_raw_replay,
    import_baseline_raw_replays,
)
from experiments_v2.cli import (  # noqa: E402
    UnifiedEvaluationGate, build_parser, command_verify_run)
from experiments_v2.contracts import (  # noqa: E402
    load_run_manifest, sha256_file, stable_sha256)
from experiments_v2.plan import load_experiment_plan  # noqa: E402
from tests.test_cli_v2 import PlanFixture  # noqa: E402


class BaselineRawReplayTests(unittest.TestCase):
    @staticmethod
    def _fixture(path: Path) -> PlanFixture:
        path.mkdir()
        return PlanFixture(path)

    def _source(self, root: Path, plan, method: str, *,
                name: str = "attempt", fallback: bool = False,
                repaired_m1: bool = False, corrupt_trace: bool = False,
                input_sha256: str | None = None,
                m2_trace: str = "atom (0, 0) atom0\n",
                source_status: str = "success",
                write_raw: bool = True) -> Path:
        canonical = plan.load_suite(plan.datasets["zac"])[0]
        directory = root / name
        directory.mkdir(parents=True)
        payload = {
            "experiment_schema": 2,
            "run_id": name,
            "dataset": "zac",
            "circuit": "toy",
            "method": method,
            "seed": 0,
            "repetition": 0,
            "run_kind": "coverage",
            "status": source_status,
            "input_sha256": (
                input_sha256 or sha256_file(Path(canonical.canonical_path))),
            "config_sha256": sha256_file(plan.resolved_config(method, 0)),
            "architecture_sha256": sha256_file(plan.architecture_path),
            "model_sha256": sha256_file(plan.model_path),
            "compiler_time_ns": 123456,
            "exit_code": 0,
            "warnings": [],
            "error": None,
        }
        (directory / "manifest.json").write_text(
            json.dumps(payload), encoding="utf-8")
        if method == "M1":
            stats = {
                "compiler_module": "zac.zac",
                "routing_strategy": "maximalis_sort",
                "physicalization": (
                    "common_expanded_phase_ghost_safe_repair"
                    if repaired_m1 else "paper_native_unmodified"),
                "ghost_repairs": 1 if repaired_m1 else 0,
                "ghost_splits": 1 if repaired_m1 else 0,
            }
            trace = ({"instructions": []} if corrupt_trace else {
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
        else:
            stats = {
                "compiler_class": "RoutingAwareCompiler",
                "fallback": fallback,
                "mqt_qmap_version": "3.2.0",
                "mqt_core_version": "3.1.0",
                # These counters belong to the ignored repaired trace.na.gz.
                "physicalization":
                    "common_ghost_safe_split_preserving_qmap_endpoints",
                "ghost_repairs": 7,
                "ghost_splits": 3,
            }
            if write_raw:
                with gzip.open(
                        directory / "trace.na.raw.gz", "wt",
                        encoding="utf-8") as handle:
                    handle.write(m2_trace)
            with gzip.open(
                    directory / "trace.na.gz", "wt",
                    encoding="utf-8") as handle:
                handle.write("this repaired derivative must never be read\n")
        with gzip.open(
                directory / "compiler_stats.json.gz", "wt",
                encoding="utf-8") as handle:
            json.dump(stats, handle)
        # Poison prior derived values.  A replay that opens either will fail or
        # propagate an impossible payload into the new attempt.
        (directory / "fidelity.json").write_text(
            '{"poisoned_old_score":true}\n', encoding="utf-8")
        with gzip.open(
                directory / "canonical_trace.jsonl.gz", "wt",
                encoding="utf-8") as handle:
            handle.write("poisoned old normalized trace\n")
        return directory

    @staticmethod
    def _lineage(source_root: Path, *, manifest: Path | None = None) -> Path:
        manifest = manifest or (source_root / "workspace.json")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "experiment_schema": 2,
            "protocol_id": "fixture-paper-native-m1-lineage",
            "root": str(source_root.resolve()),
        }
        payload["record_sha256"] = stable_sha256(payload)
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        return manifest

    def test_audit_requires_unique_exact_hash_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            self._source(source, plan, "M1", name="first")
            report = audit_baseline_raw_replay(
                plan, [source], dataset_names=["zac"], methods=["M1"])
            self.assertTrue(report["replay_ready"])
            self.assertEqual(report["state_counts"], {"ready": 1})

            self._source(source, plan, "M1", name="second")
            report = audit_baseline_raw_replay(
                plan, [source], dataset_names=["zac"], methods=["M1"])
            self.assertFalse(report["replay_ready"])
            self.assertEqual(report["state_counts"], {"ambiguous": 1})
            destination = base / "destination"
            lineage = self._lineage(source)
            with self.assertRaisesRegex(ValueError, "expected one eligible"):
                import_baseline_raw_replays(
                    plan, [source], dataset_names=["zac"], methods=["M1"],
                    output_root=destination,
                    m1_lineage_manifest=lineage)
            self.assertFalse(destination.exists())

    def test_audit_separates_hash_drift_and_source_policy_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            mismatched = base / "mismatched"
            self._source(
                mismatched, plan, "M1", input_sha256="0" * 64)
            report = audit_baseline_raw_replay(
                plan, [mismatched], dataset_names=["zac"], methods=["M1"])
            self.assertEqual(report["state_counts"], {"hash_mismatch": 1})

            repaired = base / "repaired"
            self._source(repaired, plan, "M1", repaired_m1=True)
            report = audit_baseline_raw_replay(
                plan, [repaired], dataset_names=["zac"], methods=["M1"])
            self.assertEqual(
                report["state_counts"], {"source_policy_incompatible": 1})
            reason = report["entries"][0]["rejected_exact_candidates"][0]["reason"]
            self.assertIn("not an unmodified paper-native trace", reason)
            self.assertEqual(len(report["rerun_required"]), 1)
            self.assertIn(
                "without ghost repair/split",
                report["rerun_required"][0]["action"])

    def test_m2_routing_agnostic_fallback_is_never_imported(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            self._source(source, plan, "M2", fallback=True)
            report = audit_baseline_raw_replay(
                plan, [source], dataset_names=["zac"], methods=["M2"])
            self.assertEqual(
                report["state_counts"], {"source_policy_incompatible": 1})
            reason = report["entries"][0]["rejected_exact_candidates"][0]["reason"]
            self.assertIn("without fallback", reason)

    def test_m2_identical_raw_reruns_form_one_sealed_equivalence_class(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            self._source(
                source, plan, "M2", name="z-second",
                source_status="verifier_fail")
            self._source(source, plan, "M2", name="a-first")
            self._source(
                source, plan, "M2", name=".abandoned.tmp",
                source_status="compiler_error")
            report = audit_baseline_raw_replay(
                plan, [source], dataset_names=["zac"], methods=["M2"])
            self.assertTrue(report["replay_ready"])
            entry = report["entries"][0]
            self.assertEqual(entry["equivalent_source_count"], 2)
            self.assertEqual(
                entry["equivalent_source_run_ids"], ["a-first", "z-second"])
            self.assertTrue(entry["selected_manifest"].endswith(
                "a-first/manifest.json"))

            result = import_baseline_raw_replays(
                plan, [source], dataset_names=["zac"], methods=["M2"],
                output_root=base / "destination")
            receipt = json.loads(Path(
                result["attempted"][0]["receipt"]).read_text())
            self.assertEqual(receipt["source"]["equivalent_source_count"], 2)
            self.assertEqual(
                receipt["source"]["equivalent_source_run_ids"],
                ["a-first", "z-second"])
            self.assertEqual(
                receipt["source"]["equivalent_source_statuses"],
                ["success", "verifier_fail"])
            self.assertEqual(len(receipt["source"]["equivalent_sources"]), 2)

    def test_m2_reruns_with_different_raw_bytes_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            self._source(source, plan, "M2", name="first")
            self._source(
                source, plan, "M2", name="different",
                m2_trace="atom (2, 0) atom0\n")
            report = audit_baseline_raw_replay(
                plan, [source], dataset_names=["zac"], methods=["M2"])
            self.assertFalse(report["replay_ready"])
            self.assertEqual(report["state_counts"], {"ambiguous": 1})
            self.assertEqual(report["rerun_required_by_method"], {"M2": 1})
            evidence = report["rerun_required"][0][
                "conflicting_raw_evidence"]
            self.assertEqual(len(evidence), 2)
            self.assertEqual(
                len({row["raw_payload_sha256"] for row in evidence}), 2)

    def test_m1_lineage_manifest_scopes_one_root_without_silent_choice(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            archive = base / "archive"
            selected_root = archive / "tuning-quality-v1-51583a5"
            other_root = archive / "older-lineage"
            self._source(selected_root, plan, "M1", name="selected")
            other = self._source(other_root, plan, "M1", name="other")
            with gzip.open(
                    other / "trace.zair.json.gz", "wt",
                    encoding="utf-8") as handle:
                json.dump({
                    "architecture_spec_path": str(plan.architecture_path),
                    "instructions": [{
                        "type": "init", "id": 0,
                        "init_locs": [[0, 0, 0, 1]],
                    }],
                }, handle)

            unfrozen = audit_baseline_raw_replay(
                plan, [archive], dataset_names=["zac"], methods=["M1"])
            self.assertEqual(unfrozen["state_counts"], {"ambiguous": 1})
            self.assertEqual(unfrozen["m1_lineage"], {"mode": "unfrozen"})
            self.assertEqual(len(unfrozen["entries"][0][
                "eligible_raw_evidence"]), 2)

            lineage = self._lineage(selected_root)
            frozen = audit_baseline_raw_replay(
                plan, [archive], dataset_names=["zac"], methods=["M1"],
                m1_lineage_manifest=lineage)
            self.assertEqual(frozen["state_counts"], {"ready": 1})
            entry = frozen["entries"][0]
            self.assertEqual(entry["source_root"], str(selected_root.resolve()))
            self.assertEqual(
                entry["lineage_sha256"],
                frozen["m1_lineage"]["lineage_sha256"])
            self.assertEqual(
                entry["exact_canonical_sha256"],
                entry["expected_hashes"]["input_sha256"])
            self.assertEqual(len(entry["raw_payload_sha256"]), 64)

    def test_m1_lineage_record_seal_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            self._source(source, plan, "M1")
            lineage = self._lineage(source)
            payload = json.loads(lineage.read_text())
            payload["protocol_id"] = "tampered"
            lineage.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "record seal differs"):
                audit_baseline_raw_replay(
                    plan, [base], dataset_names=["zac"], methods=["M1"],
                    m1_lineage_manifest=lineage)

    @unittest.skipUnless(
        REAL_PLAN.is_file() and REAL_ARTIFACT_ROOT.is_dir()
        and REAL_BASELINE_ARCHIVE.is_dir()
        and REAL_M1_LINEAGE.is_file(),
        "registered local baseline archive is unavailable",
    )
    def test_registered_archive_inventory_is_lineage_explicit(self):
        """Lock the real 172-circuit reuse inventory without writing it."""
        plan = load_experiment_plan(REAL_PLAN)

        class ReadOnlyPlan:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

            def resolved_config(self, method, seed):
                del seed
                return self._wrapped.methods[method].config_path

        plan = ReadOnlyPlan(plan)
        frozen = audit_baseline_raw_replay(
            plan, [REAL_ARTIFACT_ROOT], methods=["M1"],
            m1_lineage_manifest=REAL_M1_LINEAGE)
        self.assertEqual(frozen["expected_attempts"], 172)
        self.assertEqual(frozen["state_counts"], {
            "missing": 142,
            "ready": 30,
        })
        self.assertEqual(frozen["dataset_state_counts"], {
            "qmap154": {"missing": 136, "ready": 18},
            "zac18": {"missing": 6, "ready": 12},
        })

        unfrozen = audit_baseline_raw_replay(
            plan, [REAL_ARTIFACT_ROOT], methods=["M1"])
        self.assertEqual(unfrozen["state_counts"].get("ambiguous"), 30)
        self.assertEqual(unfrozen["ready"], 0)

        m2 = audit_baseline_raw_replay(
            plan, [REAL_BASELINE_ARCHIVE], methods=["M2"])
        self.assertEqual(m2["expected_attempts"], 172)
        self.assertEqual(m2["state_counts"], {"ready": 172})

    def test_replay_rebuilds_score_and_receipt_from_m1_raw_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            source_attempt = self._source(source, plan, "M1")
            lineage = self._lineage(source)
            result = import_baseline_raw_replays(
                plan, [source], dataset_names=["zac"], methods=["M1"],
                output_root=base / "destination",
                m1_lineage_manifest=lineage)
            self.assertEqual(result["status_counts"], {"success": 1})
            manifest_path = Path(result["attempted"][0]["manifest"])
            artifact = manifest_path.parent
            manifest = load_run_manifest(
                manifest_path, require_success_metrics=True)
            self.assertEqual(manifest.compiler_time_ns, 123456)
            self.assertEqual((manifest.ghost_repairs, manifest.ghost_splits), (0, 0))
            self.assertEqual(manifest.trace_protocol, "zac_paper_original_raw_v1")
            self.assertFalse((artifact / "compiler_stats.json.gz").exists())
            fresh = json.loads((artifact / "fidelity.json").read_text())
            self.assertIn("result", fresh)
            self.assertNotIn("poisoned_old_score", fresh)
            receipt = json.loads(
                (artifact / "replay_receipt.json").read_text())
            self.assertFalse(receipt["compilation_executed"])
            self.assertEqual(receipt["normalization_input"], "trace.zair.json.gz")
            self.assertFalse(
                receipt["baseline_postprocessing"]["ghost_repair"])
            self.assertIn("fidelity.json", receipt["ignored_source_artifacts"])
            self.assertEqual(
                receipt["source"]["source_root"], str(source.resolve()))
            self.assertEqual(
                receipt["source"]["lineage_sha256"],
                json.loads(lineage.read_text())["record_sha256"])
            self.assertEqual(
                receipt["exact_canonical_sha256"],
                receipt["exact_match_hashes"]["input_sha256"])
            self.assertEqual(
                json.loads((source_attempt / "fidelity.json").read_text()),
                {"poisoned_old_score": True})

    def test_m2_replay_uses_raw_not_repaired_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            self._source(source, plan, "M2")
            result = import_baseline_raw_replays(
                plan, [source], dataset_names=["zac"], methods=["M2"],
                output_root=base / "destination")
            self.assertEqual(result["status_counts"], {"success": 1})
            manifest_path = Path(result["attempted"][0]["manifest"])
            manifest = load_run_manifest(
                manifest_path, require_success_metrics=True)
            self.assertEqual((manifest.ghost_repairs, manifest.ghost_splits), (0, 0))
            artifact = manifest_path.parent
            self.assertTrue((artifact / "trace.na.raw.gz").is_file())
            self.assertFalse((artifact / "trace.na.gz").exists())
            self.assertTrue(command_verify_run(
                plan, manifest_path)["verified"])
            receipt = json.loads(
                (artifact / "replay_receipt.json").read_text())
            self.assertEqual(receipt["normalization_input"], "trace.na.raw.gz")
            self.assertIn("trace.na.gz", receipt["ignored_source_artifacts"])

    def test_incompatible_raw_trace_is_preserved_as_verifier_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = self._fixture(base / "fixture")
            plan = load_experiment_plan(fixture.plan_path)
            source = base / "source"
            self._source(source, plan, "M1", corrupt_trace=True)
            lineage = self._lineage(source)
            result = import_baseline_raw_replays(
                plan, [source], dataset_names=["zac"], methods=["M1"],
                output_root=base / "destination",
                m1_lineage_manifest=lineage)
            self.assertEqual(result["status_counts"], {"verifier_fail": 1})
            manifest = load_run_manifest(
                Path(result["attempted"][0]["manifest"]))
            self.assertFalse(manifest.verifier_ok)
            self.assertIn("verifier exception", manifest.error)
            self.assertEqual((manifest.ghost_repairs, manifest.ghost_splits), (0, 0))

    def test_gate_raw_override_and_cli_surface_are_narrow(self):
        parser = build_parser()
        args = parser.parse_args([
            "import-baseline-raw", "--plan", "plan.json",
            "--source-roots", "archive", "--audit-only",
            "--m1-lineage-manifest", "lineage.json",
        ])
        self.assertEqual(args.command, "import-baseline-raw")
        self.assertEqual(args.m1_lineage_manifest, Path("lineage.json"))
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PlanFixture(Path(temporary))
            plan = load_experiment_plan(fixture.plan_path)
            canonical = plan.load_suite(plan.datasets["zac"])[0]
            with self.assertRaisesRegex(ValueError, "cannot be evaluated"):
                UnifiedEvaluationGate(
                    plan, canonical, "M1", raw_trace_name="trace.na.raw")


if __name__ == "__main__":
    unittest.main()
