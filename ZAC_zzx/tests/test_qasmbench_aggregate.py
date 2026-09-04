"""Focused contracts for QASMBench four-method aggregation."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.qasmbench_aggregate import (  # noqa: E402
    AGGREGATE_SCHEMA,
    CANONICAL_PROFILE,
    EXPECTED_DIRECTORY_COUNTS,
    FINAL_PAYLOAD_SCHEMA,
    EVENT_HASH_PROTOCOL,
    MINIMUM_FREE_BYTES,
    QASMBENCH_COMMIT,
    QASMBENCH_REPOSITORY,
    RSS_LIMIT_BYTES,
    SOURCE_MANIFEST_SCHEMA,
    aggregate_qasmbench,
    load_qasmbench_run_manifests,
    validate_qasmbench_source_manifest,
    write_qasmbench_aggregation,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _entry(scale: str, directory: str, *, status: str = "success") -> dict:
    selected = status != "no_qasm_source"
    canonical = status == "success"
    return {
        "benchmark_scale": scale,
        "benchmark_directory": directory,
        "upstream_path": (
            f"{scale}/{directory}/{directory}_transpiled.qasm"
            if selected else f"{scale}/{directory}"),
        "upstream_git_blob": "a" * 40 if selected else "",
        "source_sha256": _digest(f"source-{scale}-{directory}") if selected else "",
        "selection_reason": "preferred_transpiled" if selected else "no_qasm_source",
        "canonical_profile": CANONICAL_PROFILE,
        "status": status,
        "canonical_path": f"/canonical/{scale}/{directory}.qasm" if canonical else "",
        "canonical_sha256": _digest(f"canonical-{scale}-{directory}") if canonical else "",
        "qubits": 8 if canonical else None,
        "gates_1q": 10 if canonical else None,
        "gates_2q": 20 if canonical else None,
        "error": None if canonical else status,
    }


def _source(entries: list[dict]) -> dict:
    statuses = {name: sum(entry["status"] == name for entry in entries)
                for name in ("success", "canonical_error", "no_qasm_source")}
    directories = {
        scale: sum(entry["benchmark_scale"] == scale for entry in entries)
        for scale in ("small", "medium", "large")
    }
    return {
        "manifest_schema": SOURCE_MANIFEST_SCHEMA,
        "upstream_repository": QASMBENCH_REPOSITORY,
        "upstream_commit": QASMBENCH_COMMIT,
        "canonical_profile": CANONICAL_PROFILE,
        "trace_retained": False,
        "counts": {
            "directories": directories,
            "selected_qasm": len(entries) - statuses["no_qasm_source"],
            **statuses,
        },
        "entries": entries,
    }


def _run(entry: dict, method: str, *, seed: int = 0,
         status: str = "success", log_fidelity: float | None = -1.0,
         fidelity: float | None = None, fidelity_ood: bool = False,
         transfers: float | None = 10.0, move_batches: float | None = 5.0,
         move_time_us: float | None = 100.0,
         algorithm_time_s: float | None = 1.0,
         ledger: str | None = None) -> dict:
    native = method in {"M3", "M4"}
    if fidelity is None and log_fidelity is not None:
        fidelity = math.exp(log_fidelity) if log_fidelity > -746 else 0.0
    return {
        "benchmark_scale": entry["benchmark_scale"],
        "benchmark_directory": entry["benchmark_directory"],
        "dataset": f"qasmbench_{entry['benchmark_scale']}",
        "circuit": entry["benchmark_directory"],
        "method": method,
        "seed": seed,
        "repetition": 0,
        "run_kind": "qasmbench",
        "status": status,
        "upstream_git_blob": entry["upstream_git_blob"],
        "input_selection_reason": entry["selection_reason"],
        "canonical_profile": CANONICAL_PROFILE,
        "trace_retained": False,
        "implementation_status": "formal_exact_qasmbench_v1",
        "rss_limit_bytes": RSS_LIMIT_BYTES,
        "minimum_free_bytes": MINIMUM_FREE_BYTES,
        "concurrency_limit": {"small": 4, "medium": 2, "large": 1}[
            entry["benchmark_scale"]],
        "event_stream_sha256": _digest(
            f"event-{entry['benchmark_scale']}-{entry['benchmark_directory']}-"
            f"{method}-{seed}"),
        "event_stream_hash_protocol": EVENT_HASH_PROTOCOL,
        "input_sha256": entry["canonical_sha256"],
        "expected_gates_1q": entry["gates_1q"],
        "expected_gates_2q": entry["gates_2q"],
        "observed_gates_1q": entry["gates_1q"],
        "observed_gates_2q": entry["gates_2q"],
        "expected_gate_ledger_sha256": ledger or _digest(
            f"ledger-{entry['benchmark_scale']}-{entry['benchmark_directory']}"),
        "observed_gate_ledger_sha256": ledger or _digest(
            f"ledger-{entry['benchmark_scale']}-{entry['benchmark_directory']}"),
        "verifier_ok": status == "success",
        "backend": "native" if native else "paper-original",
        "native_abi_version": 8 if native else None,
        "ghost_hits": 0 if native else None,
        "python_fallback": False,
        "trace_protocol": "native_exact_v1" if native else "baseline_raw_v1",
        "log_fidelity": log_fidelity,
        "fidelity": fidelity,
        "fidelity_ood": fidelity_ood,
        "fidelity_components": {"transfers": transfers},
        "move_batches": move_batches,
        "move_time_us": move_time_us,
        "compiler_time_ns": (
            None if algorithm_time_s is None else int(algorithm_time_s * 1e9)),
        "initial_placement_ns": 10_000_000 if native else None,
        "problem_preparation_ns": 20_000_000 if native else None,
        "search_kernel_ns": 30_000_000 if native else None,
        "native_search_wall_ns": 25_000_000 if native else None,
        "return_match_ns": 4_000_000 if native else None,
        "forecast_ns": 5_000_000 if native else None,
        "result_commit_ns": 6_000_000 if native else None,
        "routing_ns": 7_000_000 if native else None,
    }


def _four_runs(entry: dict, **overrides) -> list[dict]:
    return [_run(entry, method, **overrides) for method in ("M1", "M2", "M3", "M4")]


class TestQasmbenchAggregation(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = [
            _entry("small", "alpha"),
            _entry("small", "zero"),
            _entry("medium", "ood"),
            _entry("medium", "none"),
            _entry("large", "failed"),
            _entry("large", "nosrc", status="no_qasm_source"),
        ]
        by_name = {entry["benchmark_directory"]: entry for entry in self.entries}
        self.runs: list[dict] = []

        alpha = by_name["alpha"]
        self.runs.extend((
            _run(alpha, "M1", log_fidelity=-2.0, transfers=10,
                 move_batches=10, move_time_us=1_000, algorithm_time_s=1),
            _run(alpha, "M2", log_fidelity=-1.5, transfers=12,
                 move_batches=8, move_time_us=800, algorithm_time_s=2),
        ))
        for seed, log_fidelity, transfers in (
                (0, -1.4, 6), (1, -1.2, 4), (2, -1.0, 5)):
            self.runs.append(_run(
                alpha, "M3", seed=seed, log_fidelity=log_fidelity,
                transfers=transfers, move_batches=6 - seed,
                move_time_us=600 - seed * 100, algorithm_time_s=3 - seed))
        self.runs.append(_run(
            alpha, "M4", log_fidelity=-1.6, transfers=7,
            move_batches=7, move_time_us=700, algorithm_time_s=4))

        zero = by_name["zero"]
        for method in ("M1", "M2", "M3", "M4"):
            self.runs.append(_run(
                zero, method, log_fidelity=-1_000.0, fidelity=0.0,
                transfers=0.0, move_batches=0.0, move_time_us=0.0,
                algorithm_time_s=0.0))

        ood = by_name["ood"]
        self.runs.extend(_four_runs(ood, log_fidelity=-2.0, transfers=8))
        for run in self.runs:
            if run["benchmark_directory"] == "ood" and run["method"] == "M3":
                run["fidelity_ood"] = True
                run["log_fidelity"] = None
                run["fidelity"] = None

        none = by_name["none"]
        self.runs.extend(_four_runs(none, log_fidelity=-1.0, transfers=9))
        for run in self.runs:
            if run["benchmark_directory"] == "none" and run["method"] == "M3":
                run["fidelity_components"]["transfers"] = None

        failed = by_name["failed"]
        self.runs.extend((
            _run(failed, "M1"),
            _run(failed, "M2"),
            _run(failed, "M3", status="timeout", log_fidelity=None,
                 fidelity=None),
            _run(failed, "M4", status="oom", log_fidelity=None,
                 fidelity=None),
        ))

    def aggregate(self, runs=None) -> dict:
        return aggregate_qasmbench(
            _source(self.entries), self.runs if runs is None else runs,
            require_complete_source=False)

    def test_separate_scales_strict_pairing_seed_medians_and_zero_values(self):
        payload = self.aggregate()
        self.assertEqual(payload["aggregate_schema"], AGGREGATE_SCHEMA)
        self.assertEqual(payload["sheet_names"], ["Summary", "Small", "Medium", "Large"])
        self.assertEqual([len(payload["sheets"][name])
                          for name in ("Small", "Medium", "Large")], [2, 2, 2])

        alpha = payload["sheets"]["Small"][0]
        self.assertTrue(alpha["strict_paired"])
        self.assertTrue(alpha["fidelity_paired"])
        self.assertEqual(alpha["M3__seed_policy"], "median_seed0_1_2")
        self.assertEqual(alpha["M3__valid_over_N"], "3/3")
        self.assertAlmostEqual(alpha["M3__log_fidelity"], -1.2)
        self.assertEqual(alpha["M3__transfers"], 5.0)
        self.assertEqual(alpha["M3__move_batches"], 5.0)
        self.assertAlmostEqual(alpha["M3__move_time_ms"], 0.5)
        self.assertAlmostEqual(alpha["M3__native_search_s"], 0.025)
        self.assertAlmostEqual(alpha["M3__search_kernel_s"], 0.03)
        self.assertEqual(alpha["M4__seed_policy"], "interim_seed0")

        zero = payload["sheets"]["Small"][1]
        self.assertTrue(zero["fidelity_paired"])
        self.assertEqual(zero["M3__fidelity"], 0.0)
        self.assertEqual(zero["M3__transfers"], 0.0)
        self.assertEqual(zero["M3__move_batches"], 0.0)
        self.assertEqual(zero["M3__move_time_ms"], 0.0)

        small_m3 = payload["summary"]["small"]["methods"]["M3"]
        fidelity = small_m3["fidelity_vs_Bstar"]
        self.assertEqual(fidelity["paired_count"], 2)
        self.assertAlmostEqual(fidelity["geometric_log_ratio"], 0.15)
        self.assertAlmostEqual(fidelity["geometric_ratio"], math.exp(0.15))
        self.assertEqual((fidelity["wins"], fidelity["ties"], fidelity["losses"]),
                         (1, 1, 0))
        transfers = small_m3["transfers_vs_Bmin"]
        self.assertEqual(transfers["paired_count"], 2)
        self.assertEqual(transfers["baseline_mean"], 5.0)
        self.assertEqual(transfers["method_mean"], 2.5)
        self.assertEqual(transfers["reduction_ratio"], 0.5)
        m2_means = payload["summary"]["small"]["methods"]["M2"]["paired_means"]
        self.assertEqual(m2_means["fidelity_count"], 2)
        self.assertAlmostEqual(m2_means["fidelity_log_geometric_mean"], -500.75)
        summary_m2 = next(
            row for row in payload["sheets"]["Summary"]
            if row["benchmark_scale"] == "small" and row["method"] == "M2")
        self.assertEqual(summary_m2["paired_move_batches_mean"], 4.0)

    def test_ood_and_none_only_remove_the_affected_metric(self):
        payload = self.aggregate()
        medium = payload["sheets"]["Medium"]
        ood = next(row for row in medium if row["benchmark_directory"] == "ood")
        none = next(row for row in medium if row["benchmark_directory"] == "none")
        self.assertTrue(ood["strict_paired"])
        self.assertFalse(ood["fidelity_paired"])
        self.assertTrue(ood["M3__fidelity_ood"])
        self.assertTrue(none["strict_paired"])
        self.assertTrue(none["fidelity_paired"])
        self.assertIsNone(none["M3__transfers"])

        summary = payload["summary"]["medium"]
        self.assertEqual(summary["strict_paired_count"], 2)
        self.assertEqual(summary["fidelity_paired_count"], 1)
        self.assertEqual(
            summary["methods"]["M3"]["transfers_vs_Bmin"]["paired_count"], 1)
        self.assertEqual(
            summary["methods"]["M3"]["move_batches_vs_Bmin"]["paired_count"], 2)

    def test_failures_no_source_and_terminal_coverage_remain_visible(self):
        payload = self.aggregate()
        rows = payload["sheets"]["Large"]
        failed = next(row for row in rows if row["benchmark_directory"] == "failed")
        no_source = next(row for row in rows if row["benchmark_directory"] == "nosrc")
        self.assertFalse(failed["strict_paired"])
        self.assertEqual(failed["M3__status"], "timeout")
        self.assertEqual(failed["M4__status"], "oom")
        self.assertEqual(no_source["canonical_status"], "no_qasm_source")
        self.assertEqual(no_source["M1__status"], "no_qasm_source")

        m3 = payload["summary"]["large"]["methods"]["M3"]["coverage"]
        m4 = payload["summary"]["large"]["methods"]["M4"]["coverage"]
        self.assertEqual(m3["timeout"], 1)
        self.assertEqual(m4["oom"], 1)
        self.assertEqual(m3["no_qasm_source"], 1)
        self.assertEqual(payload["summary"]["large"]["strict_paired_count"], 0)

    def test_cross_method_ledger_mismatch_breaks_strict_pair_not_success_coverage(self):
        runs = [dict(run) for run in self.runs]
        for run in runs:
            if (run["benchmark_directory"] == "alpha"
                    and run["method"] == "M4"):
                run["expected_gate_ledger_sha256"] = "f" * 64
                run["observed_gate_ledger_sha256"] = "f" * 64
        payload = self.aggregate(runs)
        alpha = payload["sheets"]["Small"][0]
        self.assertFalse(alpha["strict_paired"])
        self.assertEqual(alpha["pairing_reason"], "cross_method_gate_ledger_mismatch")
        self.assertEqual(alpha["M4__status"], "success")

    def test_development_proxy_and_python_fallback_never_enter_paired_rows(self):
        runs = [dict(run) for run in self.runs]
        for run in runs:
            if (run["benchmark_directory"] == "alpha"
                    and run["method"] == "M3" and run["seed"] == 0):
                run["implementation_status"] = "development_streaming_proxy_v1"
            if (run["benchmark_directory"] == "none"
                    and run["method"] == "M4"):
                run["python_fallback"] = True
        payload = self.aggregate(runs)
        alpha = payload["sheets"]["Small"][0]
        none = next(row for row in payload["sheets"]["Medium"]
                    if row["benchmark_directory"] == "none")
        self.assertFalse(alpha["strict_paired"])
        self.assertEqual(alpha["M3__valid_over_N"], "2/3")
        self.assertFalse(none["strict_paired"])
        self.assertEqual(none["M4__valid_over_N"], "0/1")

    def test_missing_python_fallback_is_not_a_strict_native_success(self):
        runs = [dict(run) for run in self.runs]
        for run in runs:
            if (run["benchmark_directory"] == "none"
                    and run["method"] == "M4"):
                del run["python_fallback"]
        payload = self.aggregate(runs)
        none = next(row for row in payload["sheets"]["Medium"]
                    if row["benchmark_directory"] == "none")
        self.assertFalse(none["strict_paired"])
        self.assertEqual(none["pairing_reason"], "invalid_or_missing:M4")
        self.assertEqual(none["M4__status"], "success")
        self.assertEqual(none["M4__valid_over_N"], "0/1")

    def test_missing_log_fidelity_is_not_imputed_from_zero(self):
        runs = [dict(run) for run in self.runs]
        for run in runs:
            if (run["benchmark_directory"] == "none"
                    and run["method"] == "M4"):
                run["log_fidelity"] = None
                run["fidelity"] = 0.0
        payload = self.aggregate(runs)
        none = next(row for row in payload["sheets"]["Medium"]
                    if row["benchmark_directory"] == "none")
        self.assertTrue(none["strict_paired"])
        self.assertFalse(none["fidelity_paired"])
        self.assertIsNone(none["M4__log_fidelity"])

    def test_native_search_falls_back_to_outer_kernel_only_when_counter_missing(self):
        runs = [dict(run) for run in self.runs]
        for run in runs:
            if (run["benchmark_directory"] == "alpha"
                    and run["method"] == "M4"):
                run["native_search_wall_ns"] = None
        payload = self.aggregate(runs)
        alpha = payload["sheets"]["Small"][0]
        self.assertEqual(alpha["M4__native_search_s"],
                         alpha["M4__search_kernel_s"])

    def test_writer_emits_only_json_csv_and_hashes_every_evidence_file(self):
        payload = self.aggregate()
        for sheet_name in ("Summary", "Small", "Medium", "Large"):
            expected_columns = set(payload["columns"][sheet_name])
            self.assertTrue(payload["sheets"][sheet_name])
            self.assertTrue(all(set(row) == expected_columns
                                for row in payload["sheets"][sheet_name]))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "delivery"
            final_payload = write_qasmbench_aggregation(payload, output)
            self.assertEqual(final_payload["manifest_schema"], FINAL_PAYLOAD_SCHEMA)
            self.assertEqual(final_payload["state"], "xlsx_pending")
            self.assertEqual(final_payload["row_counts"], {
                "small": 2, "medium": 2, "large": 2})
            expected = {
                "small.csv", "medium.csv", "large.csv", "summary.csv",
                "source_manifest.json", "aggregate_summary.json",
                "final_manifest_payload.json",
            }
            self.assertEqual({path.name for path in output.iterdir()}, expected)
            self.assertFalse(any(path.suffix == ".xlsx" for path in output.iterdir()))
            self.assertTrue(all(len(value) == 64
                                for value in final_payload["files"].values()))
            with (output / "small.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["benchmark_directory"], "alpha")
            strict_json = json.loads((output / "aggregate_summary.json").read_text())
            self.assertEqual(strict_json["exact_sheet_count"], 4)

    def test_source_manifest_path_resolves_and_copies_existing_canonical_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "canonical" / "small" / "toy.qasm"
            canonical.parent.mkdir(parents=True)
            canonical_text = (
                "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[8];\n"
                "cz q[0],q[1];\n")
            canonical.write_text(canonical_text, encoding="utf-8")
            entry = _entry("small", "toy")
            entry["canonical_path"] = "canonical/small/toy.qasm"
            entry["canonical_sha256"] = _digest(canonical_text)
            source_path = root / "source_manifest.json"
            source_path.write_text(
                json.dumps(_source([entry]), sort_keys=True) + "\n",
                encoding="utf-8")
            payload = aggregate_qasmbench(
                source_path, _four_runs(entry), require_complete_source=False)

            resolved = Path(
                payload["source_manifest"]["entries"][0]["canonical_path"])
            self.assertTrue(resolved.is_absolute())
            self.assertTrue(resolved.is_file())
            self.assertEqual(resolved, canonical.resolve())

            output = root / "delivery"
            write_qasmbench_aggregation(payload, output)
            copied = json.loads(
                (output / "source_manifest.json").read_text(encoding="utf-8"))
            copied_path = Path(copied["entries"][0]["canonical_path"])
            self.assertTrue(copied_path.is_absolute())
            self.assertTrue(copied_path.is_file())
            self.assertEqual(copied_path, canonical.resolve())

    def test_loader_recurses_only_manifest_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "small" / "alpha" / "manifest.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(self.runs[0]), encoding="utf-8")
            (root / "ignore.json").write_text(json.dumps(self.runs[1]), encoding="utf-8")
            hidden = root / ".interrupted.tmp" / "manifest.json"
            hidden.parent.mkdir()
            hidden.write_text(json.dumps(self.runs[1]), encoding="utf-8")
            loaded = load_qasmbench_run_manifests(root)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["method"], "M1")


class TestQasmbenchSourceContract(unittest.TestCase):
    def test_complete_inventory_requires_42_25_70_and_six_no_source(self):
        entries: list[dict] = []
        no_source_remaining = 6
        for scale, count in EXPECTED_DIRECTORY_COUNTS.items():
            for index in range(count):
                no_source = scale == "large" and no_source_remaining > 0
                entries.append(_entry(
                    scale, f"circuit_{index:03d}",
                    status="no_qasm_source" if no_source else "success"))
                if no_source:
                    no_source_remaining -= 1
        source = _source(entries)
        payload = validate_qasmbench_source_manifest(source)
        self.assertEqual(len(payload["entries"]), 137)
        self.assertEqual(payload["counts"]["selected_qasm"], 131)
        self.assertEqual(payload["counts"]["no_qasm_source"], 6)
        aggregate = aggregate_qasmbench(source, [])
        self.assertEqual(
            {name: len(aggregate["sheets"][name])
             for name in ("Small", "Medium", "Large")},
            {"Small": 42, "Medium": 25, "Large": 70},
        )
        self.assertEqual(
            sum(row["canonical_status"] == "no_qasm_source"
                for row in aggregate["sheets"]["Large"]), 6)

    def test_tiny_inventory_requires_explicit_interim_validation_mode(self):
        manifest = _source([_entry("small", "toy")])
        with self.assertRaisesRegex(ValueError, "official QASMBench directory counts"):
            validate_qasmbench_source_manifest(manifest)
        payload = validate_qasmbench_source_manifest(
            manifest, require_complete=False)
        self.assertEqual(payload["counts"]["directories"]["small"], 1)

    def test_unique_transpiled_fallback_is_an_allowed_auditable_reason(self):
        entry = _entry("large", "ghz_n255")
        entry["selection_reason"] = "unique_transpiled_fallback"
        payload = validate_qasmbench_source_manifest(
            _source([entry]), require_complete=False)
        self.assertEqual(payload["entries"][0]["selection_reason"],
                         "unique_transpiled_fallback")


if __name__ == "__main__":
    unittest.main()
