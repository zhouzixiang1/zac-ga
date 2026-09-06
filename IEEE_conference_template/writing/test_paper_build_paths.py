"""Build-directory and stale-output regressions; no TeX installation required."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "paper_build_verifier", SOURCE_ROOT / "verify_paper_zh.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class PaperBuildPathsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.paper = self.project / "IEEE_conference_template"
        self.paper.mkdir()
        self.build = self.project / "build/paper_zh"
        self.figure = self.build / "figures/overall_framework.pdf"
        self.wrapper = self.paper / verifier.OVERALL_FIGURE_WRAPPER

    def make_wrapper(self):
        self.wrapper.parent.mkdir(parents=True, exist_ok=True)
        self.wrapper.write_text("standalone fixture", encoding="utf-8")

    def make_old_figure(self):
        self.figure.parent.mkdir(parents=True, exist_ok=True)
        self.figure.write_bytes(b"stale generated figure")

    def test_paths_are_under_repository_build(self):
        self.assertEqual((self.paper / verifier.BUILD_DIRECTORY).resolve(), self.build)
        self.assertEqual((self.paper / verifier.OVERALL_FIGURE_PDF).resolve(), self.figure)
        self.assertEqual(
            verifier._reported_relative_path(self.figure, self.paper),
            "../build/paper_zh/figures/overall_framework.pdf")

    def test_fresh_figure_build_uses_build_for_output_and_auxiliaries(self):
        self.make_wrapper()
        self.make_old_figure()

        def run(command, *, cwd):
            self.assertEqual(cwd, self.paper)
            self.assertFalse(self.figure.exists())
            self.assertIn(f"-outdir={self.figure.parent}", command)
            self.assertIn(f"-auxdir={self.figure.parent}", command)
            self.figure.write_bytes(b"fresh generated figure")
            return subprocess.CompletedProcess(command, 0, "built")

        with patch.object(verifier, "_run", side_effect=run):
            _, passed = verifier._build_overall_figure(self.paper)
        self.assertTrue(passed)
        self.assertFalse((self.paper / "tmp").exists())

    def test_failed_figure_build_removes_old_and_partial_pdf(self):
        self.make_wrapper()
        self.make_old_figure()

        def run(command, *, cwd):
            self.assertFalse(self.figure.exists())
            self.figure.write_bytes(b"partial failed build")
            return subprocess.CompletedProcess(command, 1, "TeX failure")

        with patch.object(verifier, "_run", side_effect=run):
            _, passed = verifier._build_overall_figure(self.paper)
        self.assertFalse(passed)
        self.assertFalse(self.figure.exists())

    def test_missing_wrapper_cannot_reuse_old_figure(self):
        self.make_old_figure()
        with patch.object(verifier, "_run") as run:
            _, passed = verifier._build_overall_figure(self.paper)
        self.assertFalse(passed)
        self.assertFalse(self.figure.exists())
        run.assert_not_called()

    def test_figure_failure_stops_main_build_and_removes_old_main_pdf(self):
        self.build.mkdir(parents=True)
        main_pdf = self.build / "paper_zh.pdf"
        main_pdf.write_bytes(b"stale manuscript")
        with patch.object(verifier, "_build_overall_figure", return_value=(
                {"output_tail": "standalone failed"}, False)), \
                patch.object(verifier, "_run") as run:
            report = verifier.verify(
                self.paper, compile_pdf=True, allow_experiment_placeholders=True,
                expected_pages=9)
        run.assert_not_called()
        self.assertFalse(main_pdf.exists())
        self.assertIn("overall_figure_xelatex_compile_failed", report["errors"])

    def test_main_build_uses_build_directory_and_reports_external_artifact(self):
        main_pdf = self.build / "paper_zh.pdf"

        def run(command, *, cwd):
            if command[0] == "latexmk":
                self.assertIn(f"-outdir={self.build}", command)
                self.assertIn(f"-auxdir={self.build}", command)
                main_pdf.write_bytes(b"fresh manuscript fixture")
            return subprocess.CompletedProcess(command, 0, "REFERENCES")

        with patch.object(verifier, "_build_overall_figure", return_value=({}, True)), \
                patch.object(verifier, "_audit_overall_figure_pdf", return_value={}), \
                patch.object(verifier, "_pdf_pages", return_value=9), \
                patch.object(verifier, "_run", side_effect=run):
            report = verifier.verify(
                self.paper, compile_pdf=True, allow_experiment_placeholders=True,
                expected_pages=9)
        self.assertIn("../build/paper_zh/paper_zh.pdf", report["artifacts"])
        self.assertFalse((self.paper / "paper_zh.pdf").exists())

    def test_read_only_verification_does_not_fall_back_to_source_pdf(self):
        (self.paper / "paper_zh.pdf").write_bytes(b"stale source-directory PDF")
        report = verifier.verify(
            self.paper, compile_pdf=False, allow_experiment_placeholders=True,
            expected_pages=9)
        self.assertIn("missing_required_file:paper_zh.pdf", report["errors"])
        self.assertFalse(report["artifacts"])

    def manifest_fixture(self):
        (self.paper / "paper_zh.tex").write_text("original source", encoding="utf-8")
        self.make_old_figure()
        (self.build / "paper_zh.pdf").write_bytes(b"built paper")
        return verifier._scientific_source_hashes(self.paper)

    def test_successful_compile_records_source_and_pdf_hashes(self):
        before = self.manifest_fixture()
        errors = []
        audit = verifier._source_build_manifest(
            self.paper, compile_pdf=True, source_before=before, errors=errors)
        self.assertFalse(errors)
        self.assertTrue(audit["written_by_this_compile"])
        manifest = json.loads((self.build / verifier.SOURCE_MANIFEST_NAME).read_text())
        self.assertEqual(manifest["source_sha256"], before)
        self.assertEqual(set(manifest["artifacts"]), {"paper_zh.pdf", "figures/overall_framework.pdf"})

    def test_read_only_check_cannot_create_source_manifest(self):
        self.manifest_fixture()
        errors = []
        verifier._source_build_manifest(
            self.paper, compile_pdf=False, source_before=None, errors=errors)
        self.assertIn("source_build_manifest_missing", errors)
        self.assertFalse((self.build / verifier.SOURCE_MANIFEST_NAME).exists())

    def test_old_mtime_new_source_cannot_refresh_manifest_in_read_only_check(self):
        before = self.manifest_fixture()
        verifier._source_build_manifest(self.paper, compile_pdf=True, source_before=before, errors=[])
        manifest = self.build / verifier.SOURCE_MANIFEST_NAME
        original = manifest.read_bytes()
        old_stat = manifest.stat()
        source = self.paper / "paper_zh.tex"
        source_stat = source.stat()
        source.write_text("modified source", encoding="utf-8")
        os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        errors = []
        verifier._source_build_manifest(
            self.paper, compile_pdf=False, source_before=None, errors=errors)
        self.assertIn("source_build_manifest_mismatch", errors)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(manifest.stat().st_mtime_ns, old_stat.st_mtime_ns)

    def test_source_change_during_compile_prevents_manifest(self):
        before = self.manifest_fixture()
        (self.paper / "paper_zh.tex").write_text("edited during compilation", encoding="utf-8")
        errors = []
        verifier._source_build_manifest(
            self.paper, compile_pdf=True, source_before=before, errors=errors)
        self.assertIn("scientific_sources_changed_during_compile", errors)
        self.assertFalse((self.build / verifier.SOURCE_MANIFEST_NAME).exists())

    def test_failed_compile_invalidates_previous_manifest(self):
        before = self.manifest_fixture()
        verifier._source_build_manifest(self.paper, compile_pdf=True, source_before=before, errors=[])
        verifier._source_build_manifest(self.paper, compile_pdf=True, source_before=before,
                                        errors=["xelatex_compile_failed"])
        self.assertFalse((self.build / verifier.SOURCE_MANIFEST_NAME).exists())


if __name__ == "__main__":
    unittest.main()
