import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import acquire_benchmarks as acquire
import render_bundled_paper as paper
import prepare_current_suite as suite
from portable_reproduce import PUBLICATION_BUNDLE, PUBLICATION_FILES, PUBLICATION_INPUTS, sha, write


class MaterialTests(unittest.TestCase):
    def publication_fixture(self, root):
        directory = root / "IEEE_conference_template"
        (directory / "figures").mkdir(parents=True)
        (directory / "sections").mkdir()
        (directory / "paper_zh.tex").write_text("\n".join(
            "\\input{" + name + "}" for name in PUBLICATION_INPUTS))
        (directory / "figures/overall_framework_standalone.tex").write_text("figure")
        (directory / "sections/03_method.tex").write_text(paper.FIGURE_LOCAL)
        bundle = root / PUBLICATION_BUNDLE
        bundle.mkdir(parents=True)
        for name in PUBLICATION_FILES:
            (bundle / name).write_text(name)
        hashes = {path.relative_to(root).as_posix(): sha(path)
                  for path in root.rglob("*") if path.is_file()}
        write(root / "portable_source_manifest.json", {"files": hashes})
        return hashes

    def render_stub(self, root, mutation=None):
        def run(command, *, cwd, **kwargs):
            (cwd / "paper_zh.pdf").write_bytes(b"pdf")
            (cwd / "paper_zh.log").write_text("clean")
            (cwd / "figures/overall_framework.pdf").write_bytes(b"figure")
            if mutation:
                mutation(cwd)
        def info(command, **kwargs):
            return "Pages: 1\n" if "overall_framework" in command[-1] else "Pages: 9\n"
        with patch.object(paper.shutil, "which", return_value="tool"), patch.object(paper.subprocess, "run", side_effect=run), patch.object(paper.subprocess, "check_output", side_effect=info), contextlib.redirect_stdout(io.StringIO()):
            return paper.render(root, "render")

    def test_render_copies_and_binds_all_publication_files_without_original_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = self.publication_fixture(root)
            result = self.render_stub(root)
            output = Path(result["output"])
            receipt = json.loads((output / "render_input.json").read_text())
            expected = {name: digest for name, digest in before.items() if name.startswith(str(PUBLICATION_BUNDLE))}
            self.assertEqual(receipt["publication_source_hashes"], expected)
            self.assertEqual(len(receipt["adapted_publication_inputs"]), 2)
            for name, digest in expected.items():
                self.assertEqual(sha(output / "source/publication/default_initial_v1" / Path(name).name), digest)
            main = (output / "source/paper_zh.tex").read_text()
            self.assertNotIn("../ZAC_zzx", main)
            for name in PUBLICATION_INPUTS.values():
                self.assertIn("\\input{" + name + "}", main)
            self.assertEqual({name: sha(root / name) for name in before}, before)
            self.assertFalse(result["historical_evidence_validated"])

    def test_render_rejects_any_unbound_publication_file_before_writes(self):
        for name in PUBLICATION_FILES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.publication_fixture(root)
                (root / PUBLICATION_BUNDLE / name).write_text("changed")
                with self.assertRaisesRegex(ValueError, "publication bundle differs"):
                    self.render_stub(root)
                self.assertFalse((root / "IEEE_conference_template/build").exists())

    def test_render_rejects_unmanifested_publication_file_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = self.publication_fixture(root)
            del before[str(PUBLICATION_BUNDLE / "main_rows.csv")]
            (root / "portable_source_manifest.json").write_text(json.dumps({"files": before}))
            with self.assertRaisesRegex(ValueError, "publication bundle differs"):
                self.render_stub(root)
            self.assertFalse((root / "IEEE_conference_template/build").exists())

    def test_render_rejects_missing_companion_and_symlink_parent_before_writes(self):
        for variant in ("missing", "symlink", "parent_symlink"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.publication_fixture(root)
                path = root / PUBLICATION_BUNDLE / "analysis_units.csv"
                if variant == "parent_symlink":
                    bundle = path.parent
                    bundle.rename(root / "outside")
                    bundle.symlink_to(root / "outside", target_is_directory=True)
                else:
                    path.unlink()
                    if variant == "symlink":
                        path.symlink_to(root / PUBLICATION_BUNDLE / "main_rows.csv")
                with self.assertRaises(ValueError):
                    self.render_stub(root)
                self.assertFalse((root / "IEEE_conference_template/build").exists())

    def test_render_rejects_live_and_copied_publication_drift(self):
        for variant in ("live", "copy"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.publication_fixture(root)
                def mutate(cwd):
                    path = root / PUBLICATION_BUNDLE if variant == "live" else cwd / "publication/default_initial_v1"
                    (path / "mechanism.csv").write_text("changed")
                with self.assertRaisesRegex(ValueError, "publication bundle changed during rendering"):
                    self.render_stub(root, mutation=mutate)
                self.assertFalse((root / "IEEE_conference_template/build/bundled-render/render/render_report.json").exists())

    def test_current_suite_preparation_is_relative_and_never_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rt = root / "IEEE_conference_template/build/portable/runtime"
            rt.mkdir(parents=True)
            write(rt / "bootstrap.json", {"test": True})
            write(rt / "current_config.json", {"qasm_list": ["toy"], "zac_setting": [{"init_strategy": "physical_prefix", "native_wheel_sha256": "new-wheel"}]})
            inputs = root / "IEEE_conference_template/build/benchmark-inputs/inputs"
            inputs.mkdir(parents=True)
            expected, records = [], []
            for index in range(172):
                ds = "zac18" if index < 18 else "qmap154"
                name = f"c{index}.qasm"; path = inputs / "canonical" / ds / name
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text("canonical")
                row = {"dataset": ds, "filename": name, "canonical_sha256": sha(path)}
                expected.append(row); records.append({**row, "canonical": str(path.relative_to(inputs))})
            (root / "docs").mkdir()
            write(root / acquire.MANIFEST, {"files": expected})
            write(inputs / "inputs.json", {"status": "verified", "manifest_sha256": sha(root / acquire.MANIFEST), "files": records})
            with patch.object(suite, "check_runtime", return_value=(rt, {"source_hashes": {"code.py": "sha"}})), patch.object(suite, "read_manifest", return_value={"files": expected}):
                result = suite.prepare(root, "runtime", "inputs", "suite", "zac18", 0)
                self.assertEqual(result["status"], "prepared-not-executed")
                self.assertEqual(result["jobs"], 18)
                protocol = json.loads((Path(result["output"]) / "protocol.json").read_text())
                self.assertNotIn(str(root), json.dumps(protocol))
                self.assertFalse(protocol["formal_paper_result"])
                self.assertFalse((Path(result["output"]) / "outputs").exists())
                with self.assertRaises(FileExistsError):
                    suite.prepare(root, "runtime", "inputs", "suite", "zac18", 0)
                (inputs / records[0]["canonical"]).write_text("changed")
                with self.assertRaises(ValueError):
                    suite.prepare(root, "runtime", "inputs", "other", "zac18", 0)

    def test_official_locator_rejects_host_and_path_injection(self):
        for args in [("other", "main", "LICENSE"), ("zac18", "../main", "LICENSE"),
                     ("qmap154", "main", "../secret"), ("zac18", "https://bad", "LICENSE")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                acquire.upstream_url(*args)
        self.assertEqual(acquire.upstream_url("qmap154", "v3.2.0", "examples/a.qasm"),
                         "https://raw.githubusercontent.com/munich-quantum-toolkit/qmap/v3.2.0/examples/a.qasm")

    def test_acquisition_inventory_has_exact_hash_pinned_18_154(self):
        root = Path(__file__).resolve().parents[1]
        value = acquire.read_manifest(root)
        self.assertEqual(len(value["files"]), 172)
        self.assertFalse(value["upstream_commit_recorded"])
        self.assertNotIn("/Users/", json.dumps(value))

    def test_missing_source_mode_fails_before_writing(self):
        root = Path(__file__).resolve().parents[1]
        with self.assertRaises(ValueError):
            acquire.acquire(root, "not-created", download=False, source_dirs=None)

    def test_bundled_render_rejects_unsafe_name_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                paper.render(root, "../outside")
            self.assertEqual(list(root.iterdir()), [])

    def test_bundled_render_does_not_waive_changed_source_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); directory = root / "IEEE_conference_template"
            (directory / "figures").mkdir(parents=True)
            (directory / "paper_zh.tex").write_text("source")
            (directory / "figures/overall_framework_standalone.tex").write_text("figure")
            write(root / "portable_source_manifest.json", {"files": {}})
            with patch.object(paper.shutil, "which", return_value="tool"), self.assertRaises(ValueError):
                paper.render(root, "render")
            self.assertFalse((directory / "build").exists())

    def test_bundled_render_reports_layout_only_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); directory = root / "IEEE_conference_template"
            (directory / "figures").mkdir(parents=True); (directory / "sections").mkdir()
            for name in ("paper_zh.tex", "figures/overall_framework_standalone.tex", "results_values_zh.tex"):
                (directory / name).write_text("source")
            method = directory / "sections/03_method.tex"; method.write_text(paper.FIGURE_LOCAL)
            before = sha(method)
            def run(command, *, cwd, **kwargs):
                (cwd / "paper_zh.pdf").write_bytes(b"pdf")
                (cwd / "paper_zh.log").write_text("clean")
                (cwd / "figures/overall_framework.pdf").write_bytes(b"figure")
            def info(command, **kwargs):
                return "Pages: 1\n" if "overall_framework" in command[-1] else "Pages: 9\n"
            with patch.object(paper.shutil, "which", return_value="tool"), patch.object(paper.subprocess, "run", side_effect=run), patch.object(paper.subprocess, "check_output", side_effect=info), contextlib.redirect_stdout(io.StringIO()):
                result = paper.render(root, "render")
            self.assertEqual(result["pages"], 9)
            self.assertFalse(result["historical_evidence_validated"])
            self.assertFalse(result["visual_review_completed"])
            self.assertEqual(sha(method), before)
            self.assertFalse((root / "build").exists())


if __name__ == "__main__":
    unittest.main()
