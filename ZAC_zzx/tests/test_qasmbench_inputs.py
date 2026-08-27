"""Focused tests for the pinned QASMBench source/canonical contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.qasmbench_inputs import (  # noqa: E402
    QASMBENCH_CANONICAL_PROFILE,
    QASMBENCH_COMMIT,
    QASMBenchCanonicalError,
    QASMBenchInputEntry,
    QASMBenchInventory,
    canonicalize_qasmbench_source,
    enumerate_qasmbench_sources,
    load_qasmbench_source_manifest,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=root, text=True).strip()


def _write_qasm(path: Path, body: str = "h q[0];\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\n' + body,
        encoding="utf-8",
    )


class QASMBenchGitFixture:
    def __init__(self, root: Path):
        self.root = root
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        _git(root, "config", "user.name", "QASMBench Test")
        _git(root, "config", "user.email", "qasmbench@example.test")

    def commit(self) -> str:
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "fixture")
        return _git(self.root, "rev-parse", "HEAD")


class TestQASMBenchInventory(unittest.TestCase):
    def test_inventory_uses_priority_git_blobs_and_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = QASMBenchGitFixture(root)
            _write_qasm(root / "small" / "alpha" / "alpha.qasm", "x q[0];\n")
            _write_qasm(
                root / "small" / "alpha" / "alpha_transpiled.qasm",
                "h q[0];\n",
            )
            _write_qasm(root / "small" / "beta" / "old.qasm", "x q[0];\n")
            _write_qasm(
                root / "small" / "beta" / "old_transpiled.qasm",
                "h q[0];\n",
            )
            _write_qasm(root / "medium" / "gamma" / "gamma.qasm")
            _write_qasm(root / "large" / "delta" / "only_input.qasm")
            no_source = root / "large" / "empty"
            no_source.mkdir(parents=True)
            (no_source / "README.md").write_text("no qasm\n", encoding="utf-8")
            commit = fixture.commit()

            first = enumerate_qasmbench_sources(
                root,
                require_pinned_commit=False,
                validate_official_counts=False,
            )
            second = enumerate_qasmbench_sources(
                root,
                require_pinned_commit=False,
                validate_official_counts=False,
            )

            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(first.upstream_commit, commit)
            keyed = {
                (entry.benchmark_scale, entry.benchmark_directory): entry
                for entry in first.entries
            }
            self.assertEqual(
                keyed[("small", "alpha")].selection_reason,
                "preferred_transpiled",
            )
            self.assertEqual(
                keyed[("small", "beta")].selection_reason,
                "unique_transpiled_fallback",
            )
            self.assertEqual(
                keyed[("medium", "gamma")].selection_reason,
                "preferred_named",
            )
            self.assertEqual(
                keyed[("large", "delta")].selection_reason,
                "sole_qasm",
            )
            self.assertEqual(keyed[("large", "empty")].status,
                             "no_qasm_source")
            alpha = keyed[("small", "alpha")]
            self.assertEqual(
                alpha.upstream_git_blob,
                _git(root, "rev-parse", f"{commit}:{alpha.upstream_path}"),
            )
            self.assertRegex(alpha.source_sha256, r"^[0-9a-f]{64}$")

    def test_inventory_rejects_wrong_commit_dirty_source_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = QASMBenchGitFixture(root)
            selected = root / "small" / "alpha" / "alpha.qasm"
            _write_qasm(selected)
            for scale in ("medium", "large"):
                marker = root / scale / "empty" / "README.md"
                marker.parent.mkdir(parents=True)
                marker.write_text("none\n", encoding="utf-8")
            fixture.commit()

            with self.assertRaisesRegex(ValueError, "must pin"):
                enumerate_qasmbench_sources(root)

            selected.write_text(selected.read_text(encoding="utf-8") + "x q[0];\n",
                                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from pinned blob"):
                enumerate_qasmbench_sources(
                    root,
                    require_pinned_commit=False,
                    validate_official_counts=False,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = QASMBenchGitFixture(root)
            _write_qasm(root / "small" / "ambiguous" / "one.qasm")
            _write_qasm(root / "small" / "ambiguous" / "two.qasm")
            for scale in ("medium", "large"):
                marker = root / scale / "empty" / "README.md"
                marker.parent.mkdir(parents=True)
                marker.write_text("none\n", encoding="utf-8")
            fixture.commit()
            with self.assertRaisesRegex(ValueError, "ambiguous QASM"):
                enumerate_qasmbench_sources(
                    root,
                    require_pinned_commit=False,
                    validate_official_counts=False,
                )


class TestQASMBenchCanonicalizer(unittest.TestCase):
    def test_expansion_is_deterministic_closed_basis_and_no_optimisation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.qasm"
            source.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
                'qreg a[2];\nqreg b[1];\ncreg c[1];\n'
                'h a[0];\nsx a[1];\ncp(pi/3) a[0],b[0];\n'
                'rzz(pi/5) a[1],b[0];\nccx a[0],a[1],b[0];\n'
                'barrier a,b;\nid b[0];\nmeasure b[0] -> c[0];\n',
                encoding="utf-8",
            )
            first = canonicalize_qasmbench_source(source, root / "first.qasm")
            second = canonicalize_qasmbench_source(source, root / "second.qasm")

            self.assertEqual(first.canonical_sha256, second.canonical_sha256)
            self.assertEqual(first.canonical_profile,
                             QASMBENCH_CANONICAL_PROFILE)
            self.assertEqual(first.upstream_commit, QASMBENCH_COMMIT)
            self.assertEqual((first.qubits, first.gates_1q, first.gates_2q),
                             (3, 35, 10))
            self.assertEqual(first.removed_operations, {
                "barrier": 1,
                "classical_register": 1,
                "id": 1,
                "measure": 1,
            })
            lines = (root / "first.qasm").read_text(encoding="utf-8").splitlines()
            operation_names = {
                line.split("(", 1)[0].split(None, 1)[0]
                for line in lines[3:]
            }
            self.assertLessEqual(operation_names, {"cz", "u1", "u2", "u3"})
            # Independent CX expansion retains its two H-equivalent U2 gates.
            self.assertIn("u2(0,pi) q[2];", lines)

    def test_reset_conditionals_and_mid_measurement_fail_without_stale_output(self):
        cases = {
            "reset": "reset q[0];\n",
            "conditional": "creg c[1];\nif(c==1) x q[0];\n",
            "mid-circuit": (
                "creg c[1];\nmeasure q[0] -> c[0];\nx q[0];\n"),
        }
        for label, body in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.qasm"
                output = root / "canonical.qasm"
                _write_qasm(source, body)
                output.write_text("stale\n", encoding="utf-8")
                sidecar = output.with_suffix(".qasm.manifest.json")
                sidecar.write_text("{}\n", encoding="utf-8")
                with self.assertRaises(QASMBenchCanonicalError):
                    canonicalize_qasmbench_source(source, output)
                self.assertFalse(output.exists())
                self.assertFalse(sidecar.exists())

    def test_wrong_upstream_commit_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.qasm"
            _write_qasm(source)
            with self.assertRaisesRegex(ValueError, "must pin commit"):
                canonicalize_qasmbench_source(
                    source,
                    root / "canonical.qasm",
                    upstream_commit="0" * 40,
                )

    def test_mechanically_appended_terminal_measurement_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.qasm"
            source.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg reg[1];\n'
                'h reg[0];\nmeasure q[0] -> c[0];\n',
                encoding="utf-8",
            )
            manifest = canonicalize_qasmbench_source(
                source, root / "canonical.qasm")
            self.assertEqual(manifest.removed_operations, {"measure": 1})
            self.assertNotIn(
                "measure", (root / "canonical.qasm").read_text(encoding="utf-8"))


class TestQASMBenchManifestLoad(unittest.TestCase):
    def test_round_trip_and_count_tamper_detection(self):
        entry = QASMBenchInputEntry(
            benchmark_scale="small",
            benchmark_directory="toy",
            upstream_path="small/toy/toy.qasm",
            upstream_git_blob="1" * 40,
            source_sha256="2" * 64,
            selection_reason="preferred_named",
        )
        inventory = QASMBenchInventory(
            entries=(entry,),
            upstream_commit="3" * 40,
            expected_counts_enforced=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source_manifest.json"
            payload = inventory.to_dict()
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_qasmbench_source_manifest(path)
            self.assertEqual(loaded.to_dict(), payload)

            payload["counts"]["selected_qasm"] = 7
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "count ledger mismatch"):
                load_qasmbench_source_manifest(path)


if __name__ == "__main__":
    unittest.main()
