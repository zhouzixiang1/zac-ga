"""Narrow offline contracts; fixtures never use the author's results or envs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("portable_reproduce", Path(__file__).with_name("portable_reproduce.py"))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def put(self, name, value=b"source"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def fixture(self):
        for name in p.SCOPES:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for name in ("README.md", "Makefile", "ZAC/LICENSE", "ZAC_zzx/README.md",
                     "ZAC_zzx/run.py", "ZAC_zzx/verify_batches.py", str(p.CONFIG), str(p.REQUIREMENTS),
                     "scripts/portable_reproduce.py", "ZAC_zzx/native/CMakeLists.txt"):
            self.put(name)

    def runtime(self):
        self.fixture()
        output = self.root / p.BUILD / "runtime"
        output.mkdir(parents=True)
        wheel = self.put(str(p.BUILD / "runtime/new.whl"), b"wheel")
        for name in ("current_config.json", "resolved-packages.txt"):
            (output / name).write_text("{}")
        data = {"status": "ready", "runtime": str(p.BUILD / "runtime"),
                "source_hashes": p.execution_files(self.root), "wheel": str(wheel.relative_to(self.root)),
                "wheel_sha256": p.sha(wheel), "config_sha256": p.sha(output / "current_config.json"),
                "requirements_sha256": p.sha(self.root / p.REQUIREMENTS),
                "resolved_packages_sha256": p.sha(output / "resolved-packages.txt")}
        p.write(output / "bootstrap.json", data)
        return output

    def test_name_rejects_absolute_traversal_and_glob(self):
        for name in ("../escape", "/tmp/escape", "*", "", "one/two"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                p.new_output(self.root, name)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_output_only_under_manuscript_build_and_never_overwrites(self):
        output = p.new_output(self.root, "demo-v1")
        self.assertEqual(output, self.root / p.BUILD / "demo-v1")
        with self.assertRaises(FileExistsError):
            p.new_output(self.root, "demo-v1")
        self.assertFalse((self.root / "build").exists())

    def test_symlink_output_parent_is_rejected(self):
        (self.root / "outside").mkdir()
        (self.root / "IEEE_conference_template").symlink_to(self.root / "outside", target_is_directory=True)
        with self.assertRaises(ValueError):
            p.new_output(self.root, "demo")
        self.assertEqual(list((self.root / "outside").iterdir()), [])

    def test_export_is_clean_and_preserves_upstream_notice(self):
        self.fixture()
        self.put("IEEE_conference_template/build/stale.json")
        self.put("ZAC_zzx/native/build/old.so")
        self.put("documents/paper.pdf")
        self.put("archive/old.py")
        self.put(".git/config")
        result = p.export_source(self.root, "export-v1")
        dest = Path(result["source"])
        self.assertTrue((dest / "ZAC/LICENSE").is_file())
        for name in (".git", "archive", "documents", "IEEE_conference_template/build", "ZAC_zzx/native/build"):
            self.assertFalse((dest / name).exists())
        manifest = p.load(dest / "portable_source_manifest.json")
        self.assertFalse(manifest["historical_raw_runs_included"])
        self.assertNotIn(str(self.root), json.dumps(manifest))
        for relative, digest in manifest["files"].items():
            self.assertEqual(p.sha(dest / relative), digest)

    def test_export_rejects_source_symlink(self):
        self.fixture()
        (self.root / "ZAC_zzx/zzx/escape.py").symlink_to(self.root / "README.md")
        with self.assertRaises(ValueError):
            p.export_source(self.root, "export")
        self.assertFalse((self.root / p.BUILD).exists())

    def publication_fixture(self):
        self.fixture()
        self.put("IEEE_conference_template/paper_zh.tex", "\n".join(
            "\\input{" + value + "}" for value in p.PUBLICATION_INPUTS).encode())
        for name in p.PUBLICATION_FILES:
            self.put(str(p.PUBLICATION_BUNDLE / name), name.encode())

    def test_export_includes_only_six_publication_companions(self):
        self.publication_fixture()
        self.put(str(p.PUBLICATION_BUNDLE / "unrelated.csv"))
        self.put("ZAC_zzx/results/default_initial_v1/raw/trace.json")
        result = p.export_source(self.root, "publication")
        dest = Path(result["source"])
        files = p.load(dest / "portable_source_manifest.json")["files"]
        bundle_names = {str(p.PUBLICATION_BUNDLE / name) for name in p.PUBLICATION_FILES}
        self.assertEqual({name for name in files if "default_initial_v1" in name}, bundle_names)
        for name in bundle_names:
            self.assertEqual(files[name], p.sha(dest / name))

    def test_export_rejects_missing_publication_companion_before_writes(self):
        self.publication_fixture()
        (self.root / p.PUBLICATION_BUNDLE / "analysis_units.csv").unlink()
        with self.assertRaisesRegex(ValueError, "required publication source"):
            p.export_source(self.root, "publication")
        self.assertFalse((self.root / p.BUILD).exists())

    def test_export_rejects_publication_symlink(self):
        self.publication_fixture()
        path = self.root / p.PUBLICATION_BUNDLE / "mechanism.csv"
        path.unlink()
        path.symlink_to(self.root / "README.md")
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            p.export_source(self.root, "publication")
        self.assertFalse((self.root / p.BUILD).exists())

    def test_publication_input_cannot_escape_allowlist_or_be_duplicated(self):
        self.publication_fixture()
        for value in ("../outside.tex", "/outside.tex", "../../ZAC_zzx/results/default_initial_v1/paper_exports/default_initial_values.tex",
                      "../ZAC_zzx/results/default_initial_v1/paper_exports/../outside.tex"):
            with self.subTest(value=value):
                self.put("IEEE_conference_template/paper_zh.tex", ("\\input{" + value + "}").encode())
                with self.assertRaises(ValueError):
                    p.publication_files(self.root)
        for inputs in ((next(iter(p.PUBLICATION_INPUTS)),), (*p.PUBLICATION_INPUTS, next(iter(p.PUBLICATION_INPUTS)))):
            self.put("IEEE_conference_template/paper_zh.tex", "\n".join(
                "\\input{" + value + "}" for value in inputs).encode())
            with self.assertRaisesRegex(ValueError, "both exact TeX inputs once"):
                p.publication_files(self.root)

    def test_export_requires_real_verifier_and_license(self):
        self.fixture()
        (self.root / "ZAC_zzx/verify_batches.py").unlink()
        with self.assertRaises(ValueError):
            p.export_source(self.root, "export")

    def test_runtime_source_drift_fails(self):
        self.runtime()
        p.check_runtime(self.root, "runtime")
        self.put("ZAC_zzx/zzx/new.py")
        with self.assertRaisesRegex(ValueError, "source differs"):
            p.check_runtime(self.root, "runtime")

    def test_runtime_wheel_and_config_drift_fail(self):
        output = self.runtime()
        (output / "new.whl").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            p.check_runtime(self.root, "runtime")

    def test_portable_script_is_part_of_runtime_identity(self):
        self.runtime()
        self.put("scripts/portable_reproduce.py", b"changed")
        with self.assertRaisesRegex(ValueError, "source differs"):
            p.check_runtime(self.root, "runtime")

    def test_artifact_audit_is_read_only_and_not_raw_run_claim(self):
        path = self.put(str(p.RESULTS / "result.csv"), b"circuit,value\na,1\n")
        p.write(self.root / p.RESULTS / "final_manifest.json", {"files": {
            "result.csv": {"sha256": p.sha(path), "bytes": path.stat().st_size}}})
        result = p.audit_artifacts(self.root)
        self.assertEqual(result["files"], 1)
        self.assertFalse(result["historical_raw_runs_verified"])
        self.assertFalse((self.root / "IEEE_conference_template").exists())
        path.write_text("changed")
        with self.assertRaises(ValueError):
            p.audit_artifacts(self.root)

    def test_artifact_manifest_cannot_escape_bundle(self):
        self.put(str(p.RESULTS / "final_manifest.json"), json.dumps({"files": {
            "../escape": {"bytes": 0, "sha256": "x"}}}).encode())
        with self.assertRaises(ValueError):
            p.audit_artifacts(self.root)

    def test_missing_cmake_fails_before_creating_environment(self):
        with patch.object(p.shutil, "which", return_value=None), self.assertRaises(ValueError):
            p.bootstrap(self.root, "runtime")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_runtime_environment_removes_inherited_python_paths(self):
        output = self.root / "output"; output.mkdir()
        with patch.dict(p.os.environ, {"PYTHONPATH": "/old/code", "PYTHONHOME": "/old/env", "VIRTUAL_ENV": "/old/env"}):
            env = p.runtime_env(self.root, output)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertEqual(env["PYTHONPATH"], str(self.root / "ZAC_zzx"))
        self.assertEqual(env["CMAKE_BUILD_PARALLEL_LEVEL"], "2")
        self.assertEqual(env["PIP_CACHE_DIR"], str(output / "pip-cache"))


if __name__ == "__main__":
    unittest.main()
