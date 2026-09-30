"""Exact relocation must preserve logical provenance and all source checks."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import verify_ga_publication as gate


class RelocatedGAPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.physical = self.root / gate.BASELINE_PHYSICAL
        self.logical = self.root / gate.BASELINE_LOGICAL
        self.physical.parent.mkdir(parents=True)
        self.physical.write_bytes(gate.frozen_bytes())
        self.exporter = gate.load_exporter()

    def test_original_frozen_bytes_have_the_registered_hash(self):
        self.assertEqual(hashlib.sha256(gate.frozen_bytes()).hexdigest(), gate.BASELINE_SHA256)
        self.assertFalse(self.logical.exists())

    def test_provenance_preserves_logical_path_and_original_sort_order(self):
        other = self.root / "unrelated.txt"
        other.write_text("independently checked")
        expected_other = {"path": str(other), "sha256": self.exporter.sha256(other), "bytes": other.stat().st_size}
        with gate.relocated_loader(self.exporter, self.root) as mapping:
            sources = self.exporter.Sources(self.root)
            ref = sources.file({"path": str(self.logical), "sha256": gate.BASELINE_SHA256})
            self.assertEqual(ref, {"path": str(self.logical), "sha256": gate.BASELINE_SHA256,
                                   "bytes": self.physical.stat().st_size})
            self.assertEqual(sources.file(expected_other), expected_other)
            self.assertEqual(sources.finish(), sorted([ref, expected_other], key=lambda x: x["path"]))
            self.assertEqual(mapping["physical_path"], str(self.physical))
            self.assertNotIn(str(self.logical), sources.stats)
            self.assertIn(str(self.physical), sources.stats)
        self.assertFalse(self.logical.exists())

    def test_missing_and_tampered_frozen_copy_are_rejected(self):
        self.physical.write_bytes(b"altered source")
        with self.assertRaisesRegex(ValueError, "SHA256 drift"):
            with gate.relocated_loader(self.exporter, self.root):
                pass
        self.physical.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            with gate.relocated_loader(self.exporter, self.root):
                pass

    def test_declared_hash_size_and_later_mutation_are_still_checked(self):
        with gate.relocated_loader(self.exporter, self.root):
            sources = self.exporter.Sources(self.root)
            with self.assertRaisesRegex(ValueError, "unregistered.*SHA256"):
                sources.file({"path": str(self.logical), "sha256": "0" * 64})
            with self.assertRaisesRegex(ValueError, "size drift"):
                sources.file({"path": str(self.logical), "bytes": 1})
            sources.file(self.logical)
            self.physical.write_bytes(self.physical.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "changed during export"):
                sources.finish()

    def test_module_uses_original_logical_file_and_delegates_every_other_path(self):
        name = "_test_frozen_baseline_relocation"
        self.addCleanup(lambda: sys.modules.pop(name, None))
        sentinel = object()
        with patch.object(self.exporter, "module_at", return_value=sentinel) as original:
            with gate.relocated_loader(self.exporter, self.root):
                module = self.exporter.module_at(self.logical, name)
                self.assertEqual(Path(module.__file__), self.logical)
                self.assertEqual(module.PAPER, self.root / "IEEE_conference_template")
                self.assertEqual(module.REPO, self.root)
                self.assertIs(self.exporter.module_at("elsewhere.py", "other"), sentinel)
            original.assert_called_once_with("elsewhere.py", "other")
        self.assertFalse(self.logical.exists())

    def test_unregistered_sources_do_not_get_a_hash_exception(self):
        other = self.root / "other.py"
        other.write_text("arbitrary source")
        original_sources, original_loader = self.exporter.Sources, self.exporter.module_at
        with gate.relocated_loader(self.exporter, self.root):
            sources = self.exporter.Sources(self.root)
            with self.assertRaisesRegex(ValueError, "SHA256 drift"):
                sources.file({"path": str(other), "sha256": gate.BASELINE_SHA256})
        self.assertIs(self.exporter.Sources, original_sources)
        self.assertIs(self.exporter.module_at, original_loader)


if __name__ == "__main__":
    unittest.main()
