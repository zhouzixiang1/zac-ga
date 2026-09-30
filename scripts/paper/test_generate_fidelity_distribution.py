"""Check distribution pairing, accepted cohort, and immutable-source verification."""

import csv
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_fidelity_distribution as distribution
from paper_paths import artifact_path


class FidelityDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.analysis = self.root / "analysis_units.csv"
        self.main = self.root / "main_rows.csv"
        self.values = self.root / "values.json"
        self.output = self.root / "distribution.dat"
        self.provenance = self.root / "distribution.provenance.json"
        rows = []
        for name, digest, logs, eligible in (
            ("alias_a1", "a" * 64, (-1001, -1001, -1000.5), True),
            ("alias_a2", "a" * 64, (-1003, -1003, -1002.5), True),
            ("b", "b" * 64, (-2, -2.1, -2.2), True),
            ("c", "c" * 64, (-5, -4.5, -4.5), True),
            ("invalid_alias", "a" * 64, (-9000, -9000, -8000), False),
        ):
            row = {"dataset": "qmap154", "canonical_sha256": digest,
                   "circuit": name, "common_fidelity": str(eligible)}
            for method, value in zip(distribution.METHODS, logs):
                row[method] = json.dumps({"log_fidelity": value, "fidelity": math.exp(value),
                                          "complete_fidelity": eligible})
            rows.append(row)
        self.main_records = rows
        self.unit_records = []
        for digest in ("a" * 64, "b" * 64, "c" * 64):
            members = [row for row in rows if row["canonical_sha256"] == digest
                       and row["common_fidelity"] == "True"]
            unit = {"dataset": "qmap154", "canonical_sha256": digest,
                    "circuits": json.dumps([row["circuit"] for row in members]), "member_N": len(members)}
            for method in distribution.METHODS:
                logf = statistics.fmean(json.loads(row[method])["log_fidelity"] for row in members)
                unit[method] = json.dumps({"log_fidelity": logf, "fidelity": math.exp(logf)})
            self.unit_records.append(unit)
        self._write_csv(self.main, self.main_records)
        self._write_csv(self.analysis, self.unit_records)
        self.values.write_text(json.dumps({"datasets": {"qmap154": {
            "common_canonical_N": 3, "common_file_N": 4,
            "comparisons": {"M1": {"wins": 2, "ties": 0, "losses": 1},
                            "M2": {"wins": 1, "ties": 1, "losses": 1}}}}}))

    @staticmethod
    def _write_csv(path, rows):
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def produce(self, check=False):
        return distribution.produce(self.analysis, self.main, self.values, self.output,
                                    self.provenance, check=check, expected_n=3)

    def test_aliases_are_one_unit_and_invalid_alias_is_excluded(self):
        rows = distribution.derive_rows(self.analysis, self.main, self.values, expected_n=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["member_N"] for row in rows), 4)
        alias = next(row for row in rows if row["sha"] == "a" * 64)
        self.assertEqual(alias["member_N"], 2)
        self.assertEqual(alias["dlog_vs_zac"], .5)
        self.assertEqual(alias["dlog_vs_iccad"], .5)
        # All displayed fidelities for these aliases underflow to zero.
        self.assertEqual(json.loads(self.unit_records[0]["Default"])["fidelity"], 0)

    def test_both_baselines_keep_identical_rank_and_sha_tie_break(self):
        rows = distribution.derive_rows(self.analysis, self.main, self.values, expected_n=3)
        self.assertEqual([row["sha"][0] for row in rows], ["b", "a", "c"])
        self.assertEqual([row["rank"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[-1]["dlog_vs_iccad"], 0)

    def test_duplicate_canonical_units_are_rejected(self):
        self._write_csv(self.analysis, self.unit_records + [self.unit_records[0]])
        values = json.loads(self.values.read_text())
        values["datasets"]["qmap154"]["common_canonical_N"] = 4
        self.values.write_text(json.dumps(values))
        with self.assertRaisesRegex(ValueError, "duplicate canonical"):
            distribution.derive_rows(self.analysis, self.main, self.values, expected_n=4)

    def test_membership_must_match_eligible_files(self):
        self.unit_records[0]["circuits"] = json.dumps(["alias_a1", "invalid_alias"])
        self._write_csv(self.analysis, self.unit_records)
        with self.assertRaisesRegex(ValueError, "alias membership"):
            distribution.derive_rows(self.analysis, self.main, self.values, expected_n=3)

    def test_generation_is_one_time_and_check_cannot_write(self):
        self.produce()
        paths = (self.analysis, self.main, self.values, self.output, self.provenance)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        with patch.object(Path, "write_text", side_effect=AssertionError("check attempted to write")):
            self.assertEqual(self.produce(check=True)["status"], "pass")
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.produce()

    def test_check_rejects_source_change_even_when_values_are_unchanged(self):
        self.produce()
        self.analysis.write_text(self.analysis.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "source hash changed"):
            self.produce(check=True)

    def test_check_rejects_a_changed_plotted_value(self):
        self.produce()
        lines = self.output.read_text().splitlines()
        values = lines[1].split()
        values[2] = "0.3"
        lines[1] = " ".join(values)
        self.output.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(ValueError, "plotting value or pairing changed"):
            self.produce(check=True)

    def test_other_bundle_cli_creates_and_checks_its_own_method_label(self):
        # The small cohort is a test fixture, never a main-experiment export.
        label = "GA-LK with physical GA initialization"
        args = ["--analysis-units", str(self.analysis), "--main-rows", str(self.main),
                "--values", str(self.values), "--output", str(self.output),
                "--provenance", str(self.provenance), "--expected-n", "3",
                "--method-label", label]
        source_bytes = {p: p.read_bytes() for p in (self.analysis, self.main, self.values)}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(distribution.main(args), 0)
        recorded = json.loads(self.provenance.read_text())
        self.assertEqual(recorded["methods"], {"M1": "ZAC", "M2": "ICCAD/QMAP", "Default": label})
        self.assertEqual(recorded["unit_count"], 3)
        expected = distribution.derive_rows(self.analysis, self.main, self.values, expected_n=3)
        self.assertEqual(self.output.read_text(), distribution.render_dat(expected))
        with redirect_stdout(io.StringIO()), patch.object(
                Path, "write_text", side_effect=AssertionError("check attempted to write")):
            self.assertEqual(distribution.main(args + ["--check"]), 0)
        self.assertEqual(source_bytes, {p: p.read_bytes() for p in source_bytes})
        with self.assertRaisesRegex(ValueError, "provenance"):
            # Omitting the new label must not silently relabel the frozen run.
            self.produce(check=True)

    def test_default_expected_count_is_not_inferred_from_another_bundle(self):
        with self.assertRaisesRegex(ValueError, "expected 120 canonical units"):
            distribution.derive_rows(self.analysis, self.main, self.values)

    def test_declared_count_must_match_even_when_expected_count_matches_rows(self):
        values = json.loads(self.values.read_text())
        values["datasets"]["qmap154"]["common_canonical_N"] = 4
        self.values.write_text(json.dumps(values))
        with self.assertRaisesRegex(ValueError, "summary declares 4"):
            self.produce()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.provenance.exists())

    def test_invalid_expected_counts_and_method_labels_never_create_artifacts(self):
        for count in (0, -1, True, 3.0):
            with self.subTest(expected_n=count), self.assertRaisesRegex(ValueError, "positive integer"):
                distribution.produce(self.analysis, self.main, self.values, self.output,
                                     self.provenance, check=False, expected_n=count)
        for label in ("", "  ", None):
            with self.subTest(method_label=label), self.assertRaisesRegex(ValueError, "non-empty string"):
                distribution.produce(self.analysis, self.main, self.values, self.output,
                                     self.provenance, check=False, expected_n=3, method_label=label)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.provenance.exists())

    def test_cli_mismatched_expected_count_reports_failure_without_writes(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = distribution.main([
                "--analysis-units", str(self.analysis), "--main-rows", str(self.main),
                "--values", str(self.values), "--output", str(self.output),
                "--provenance", str(self.provenance), "--expected-n", "4",
                "--method-label", "GA-LK with physical GA initialization"])
        self.assertEqual(code, 1)
        self.assertIn("expected 4 canonical units", json.loads(errors.getvalue())["error"])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.provenance.exists())

    def test_real_accepted_cohort_keeps_120_units_wins_and_full_range(self):
        rows = distribution.derive_rows(distribution.BUNDLE / "analysis_units.csv",
                                        distribution.BUNDLE / "main_rows.csv",
                                        distribution.BUNDLE / "default_initial_values.json")
        self.assertEqual(len(rows), 120)
        self.assertEqual(len({row["sha"] for row in rows}), 120)
        self.assertEqual(sum(row["member_N"] for row in rows), 122)
        self.assertEqual(distribution._wins_losses(rows, "dlog_vs_zac"),
                         {"wins": 49, "ties": 0, "losses": 71})
        self.assertEqual(distribution._wins_losses(rows, "dlog_vs_iccad"),
                         {"wins": 48, "ties": 0, "losses": 72})
        self.assertAlmostEqual(min(row["dlog_vs_zac"] for row in rows), -0.18732767095789882)
        self.assertAlmostEqual(max(row["dlog_vs_zac"] for row in rows), 10.773306112237478)
        self.assertAlmostEqual(min(row["dlog_vs_iccad"] for row in rows), -0.17370401375563915)
        self.assertAlmostEqual(max(row["dlog_vs_iccad"] for row in rows), 9.897544308621818)
        self.assertEqual([row["dlog_vs_zac"] for row in rows],
                         sorted(row["dlog_vs_zac"] for row in rows))

    def test_default_check_matches_the_frozen_plotting_artifact(self):
        self.assertEqual(distribution.DEFAULT_DATA, artifact_path(
            distribution.PAPER_ROOT, "figures/data/default_initial_qmap_distribution.dat"))
        self.assertEqual(distribution.DEFAULT_PROVENANCE, artifact_path(
            distribution.PAPER_ROOT, "figures/data/default_initial_qmap_distribution.provenance.json"))
        self.assertNotEqual(distribution.DEFAULT_DATA.parent, distribution.DEFAULT_PROVENANCE.parent)
        self.assertEqual(distribution.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
