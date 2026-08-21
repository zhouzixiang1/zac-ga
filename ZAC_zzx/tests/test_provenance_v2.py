"""Formal baseline-reproduction provenance tests."""

from __future__ import annotations

import copy
import importlib.metadata
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from experiments_v2.provenance import (  # noqa: E402
    build_reproduction_provenance,
    validate_locked_python_environment,
    validate_reproduction_provenance,
)


class ReproductionProvenanceTests(unittest.TestCase):
    def test_complete_lock_is_probed_in_the_selected_venv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "environment.lock.txt"
            version = importlib.metadata.version("packaging")
            lock.write_text(
                f"# Interpreter: CPython {sys.version.split()[0]}\n"
                f"packaging=={version}\n",
                encoding="utf-8",
            )
            evidence = validate_locked_python_environment(sys.executable, lock)
            self.assertEqual(evidence["python_version"], sys.version.split()[0])
            lock.write_text(
                f"# Interpreter: CPython {sys.version.split()[0]}\n"
                "packaging==0.0.0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "environment drifted"):
                validate_locked_python_environment(sys.executable, lock)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        experiment = root / "ZAC_zzx" / "experiments_v2"
        experiment.mkdir(parents=True)
        paths = {
            "plan": experiment / "plan.json",
            "main_arch": root / "main_arch.json",
            "model": root / "model.json",
            "zac_truth": root / "zac.csv",
            "iccad_truth": root / "iccad.csv",
            "zac_arch": root / "zac_repro_arch.json",
            "zac_lock": experiment / "environment_zac_qiskit124.lock.txt",
            "iccad_lock": experiment / "environment_iccad_qmap320.lock.txt",
        }
        for key, path in paths.items():
            path.write_text(f"{key}\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Schema Two"],
                       cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "schema2@example.test"],
                       cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "frozen"], cwd=root, check=True)
        self.root = root
        self.paths = paths
        self.plan = SimpleNamespace(
            path=paths["plan"],
            repo_root=root,
            architecture_path=paths["main_arch"],
            model_path=paths["model"],
            reproduction={
                "zac_truth": str(paths["zac_truth"]),
                "iccad_truth": str(paths["iccad_truth"]),
                "zac_execute": {"architecture": str(paths["zac_arch"])},
            },
            package_versions={"canonical_qiskit": "1.2.4"},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_clean_commit_and_all_frozen_files_are_bound(self) -> None:
        payload = build_reproduction_provenance(
            self.plan, {"zac": {"qiskit": "1.2.4"}})
        validate_reproduction_provenance(self.plan, payload)
        self.assertFalse(payload["repository"]["dirty"])
        self.assertEqual(
            set(payload["frozen_files"]),
            {
                "experiment_plan", "main_architecture", "fidelity_model",
                "environment_zac_qiskit124.lock.txt",
                "environment_iccad_qmap320.lock.txt", "zac_truth",
                "iccad_truth", "zac_reproduction_architecture",
            },
        )

    def test_dirty_or_different_commit_cannot_reuse_report(self) -> None:
        payload = build_reproduction_provenance(self.plan, {})
        self.paths["zac_lock"].write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "exact clean commit"):
            validate_reproduction_provenance(self.plan, payload)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "new lock"],
                       cwd=self.root, check=True)
        with self.assertRaisesRegex(RuntimeError, "exact clean commit"):
            validate_reproduction_provenance(self.plan, payload)

    def test_payload_tampering_is_rejected(self) -> None:
        payload = build_reproduction_provenance(self.plan, {})
        changed = copy.deepcopy(payload)
        changed["machine"]["hostname"] = "forged"
        with self.assertRaisesRegex(ValueError, "payload changed"):
            validate_reproduction_provenance(self.plan, changed)

    def test_unversioned_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as unversioned:
            plan = copy.copy(self.plan)
            plan.repo_root = Path(unversioned)
            with self.assertRaisesRegex(RuntimeError, "clean, versioned"):
                build_reproduction_provenance(plan, {})


if __name__ == "__main__":
    unittest.main()
