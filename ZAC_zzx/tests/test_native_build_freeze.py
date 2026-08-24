from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.native_build_freeze import (  # noqa: E402
    NativeBuildFreezeError,
    create_native_build_attestation,
    freeze_native_build,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NativeBuildFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.artifacts = self.base / "artifacts"
        self.native = self.repo / "ZAC_zzx" / "native"
        (self.native / "src").mkdir(parents=True)
        (self.native / "include").mkdir()
        (self.native / "bindings").mkdir()
        (self.native / "CMakeLists.txt").write_text(
            "project(fake LANGUAGES CXX)\n", encoding="utf-8")
        (self.native / "pyproject.toml").write_text(
            "[build-system]\nrequires=[]\n", encoding="utf-8")
        (self.native / "src" / "core.cpp").write_text(
            "int answer() { return 42; }\n", encoding="utf-8")
        (self.native / "include" / "core.hpp").write_text(
            "int answer();\n", encoding="utf-8")
        (self.native / "bindings" / "module.cpp").write_text(
            "// binding\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.repo,
            check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "fixture"], cwd=self.repo,
            check=True)

        self.extension = self.artifacts / "zac_native_core.so"
        self.extension.parent.mkdir(parents=True)
        self.extension.write_bytes(b"registered-extension-bytes")
        self.wheel = self.artifacts / "zac_native-0.5.0-test.whl"
        self.member = "zac_native_core.so"
        with ZipFile(self.wheel, "w") as archive:
            archive.writestr(self.member, self.extension.read_bytes())
        self.registration = self.artifacts / "wheel-registration.json"
        self.registration.write_text(json.dumps({
            "schema": 1,
            "wheel_path": str(self.wheel.resolve()),
            "wheel_sha256": _sha256(self.wheel),
            "extension_member": self.member,
            "extension_sha256": _sha256(self.extension),
        }), encoding="utf-8")
        self.build_info = {
            "native_abi_version": 8,
            "backend": "cpp-native-v8",
            "flat_wire_version": 1,
            "rich_boundary_wire_version": 7,
            "rng_version": "python-random-mt19937-v1",
            "cxx_standard": 17,
            "build_type": "Release",
            "openmp": False,
            "fast_math": False,
            "compiler_id": "FixtureClang",
            "compiler_version": "1.0",
            "extension_path": str(self.extension.resolve()),
            "extension_sha256": _sha256(self.extension),
            "wheel_registration_path": str(self.registration.resolve()),
            "wheel_registered": True,
            "native_wheel_sha256": _sha256(self.wheel),
        }
        self.attestation = self.artifacts / "build_attestation.json"
        self.output = self.artifacts / "build_manifest.json"
        self.build_info_patch = mock.patch(
            "experiments_v2.native_build_freeze._default_build_info",
            side_effect=lambda **_kwargs: dict(self.build_info))
        self.build_info_patch.start()

    def tearDown(self):
        self.build_info_patch.stop()
        self.temporary.cleanup()

    def _attest(self):
        return create_native_build_attestation(
            repo_root=self.repo, native_root=self.native,
            wheel_path=self.wheel, output_path=self.attestation)

    def _write_evidence(self):
        wheel_sha = _sha256(self.wheel)
        extension_sha = _sha256(self.extension)
        native_build = {
            "native_abi_version": 8,
            "backend": "cpp-native-v8",
            "flat_wire_version": 1,
            "rich_boundary_wire_version": 7,
            "rng_version": "python-random-mt19937-v1",
            "native_wheel_sha256": wheel_sha,
            "extension_sha256": extension_sha,
        }
        micro = self.artifacts / "micro.json"
        micro.write_text(json.dumps({
            "schema": 2,
            "protocol": "abi8-registered-native-microbenchmark-v1",
            "native_build": native_build,
            "one_call": {"speedup": 5.1},
            "fitness_core": {"speedup": 10.1},
            "ghost_core": {"speedup": 10.2},
        }), encoding="utf-8")
        repetitions = [{
            "mapping": True, "rng": True, "safety_rng": True,
            "stay_return": True, "winner": True,
            "unique_evaluations": True,
            "current_nll_max_abs_error": 0.0,
            "forecast_nll_max_abs_error": 0.0,
            "search_nll_max_abs_error": 0.0,
        }]
        real = self.artifacts / "real.json"
        real.write_text(json.dumps({
            "benchmark_id": "abi8-exact-real-boundary-ising-n42-v2",
            "native_build": native_build,
            "parity": {"all_passed": True, "repetitions": repetitions},
            "timing": {
                "primary_speedup": 5.2,
                "meets_5x_complete_boundary_gate": True,
            },
        }), encoding="utf-8")
        parity = {
            "mapping_equal": True, "registry_equal": True,
            "rng_equal": True, "max_current_nll_abs_error": 0.0,
            "max_forecast_nll_abs_error": 0.0,
        }
        pipeline = self.artifacts / "pipeline.json"
        pipeline.write_text(json.dumps({
            "protocol": "resident-python-vs-abi8-medium-v2",
            "backend": "cpp-native-v8",
            "wheel_sha256": wheel_sha,
            "accepted": True,
            "horizons": {
                "0": {"accepted": True, "speedup": 5.3,
                      "parity": parity},
                "8": {"accepted": True, "speedup": 5.4,
                      "parity": parity},
            },
        }), encoding="utf-8")
        return micro, real, pipeline

    def _freeze(self):
        micro, real, pipeline = self._write_evidence()
        return freeze_native_build(
            repo_root=self.repo, native_root=self.native,
            wheel_path=self.wheel, attestation_path=self.attestation,
            micro_benchmark_path=micro,
            real_boundary_benchmark_path=real,
            full_pipeline_benchmark_path=pipeline,
            output_path=self.output)

    def test_dirty_repository_is_rejected_without_writing_attestation(self):
        (self.native / "src" / "core.cpp").write_text(
            "int answer() { return 0; }\n", encoding="utf-8")
        with self.assertRaisesRegex(NativeBuildFreezeError, "clean repository"):
            self._attest()
        self.assertFalse(self.attestation.exists())

    def test_source_hash_drift_is_rejected_without_overwriting_output(self):
        self._attest()
        self.output.write_text("sentinel\n", encoding="utf-8")
        (self.native / "src" / "core.cpp").write_text(
            "int answer() { return 43; }\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "source drift"], cwd=self.repo,
            check=True)
        with self.assertRaisesRegex(NativeBuildFreezeError,
                                    "source tree hash drifted"):
            self._freeze()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel\n")

    def test_clean_matching_build_is_atomically_frozen(self):
        attestation = self._attest()
        result = self._freeze()
        persisted = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(result, persisted)
        self.assertEqual(result["artifact_status"], "frozen")
        self.assertFalse(result["requires_clean_commit_resign"])
        self.assertEqual(
            result["source"]["native_source_tree"]["algorithm"],
            "tracked-tree-v1")
        self.assertEqual(
            result["evidence"]["build_attestation"]["record_sha256"],
            attestation["record_sha256"])
        self.assertTrue(result["benchmark"]["all_gates_passed"])
        self.assertEqual(result["wheel"]["native_abi_version"], 8)
        self.assertEqual(result["wheel"]["rich_boundary_wire_version"], 7)
        self.assertEqual(result["wheel"]["backend"], "cpp-native-v8")

    def test_legacy_backend_evidence_is_rejected(self):
        self._attest()
        micro, real, pipeline = self._write_evidence()
        payload = json.loads(pipeline.read_text(encoding="utf-8"))
        payload["backend"] = "cpp-native-v6"
        pipeline.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(NativeBuildFreezeError,
                                    "full-pipeline backend"):
            freeze_native_build(
                repo_root=self.repo, native_root=self.native,
                wheel_path=self.wheel, attestation_path=self.attestation,
                micro_benchmark_path=micro,
                real_boundary_benchmark_path=real,
                full_pipeline_benchmark_path=pipeline,
                output_path=self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
