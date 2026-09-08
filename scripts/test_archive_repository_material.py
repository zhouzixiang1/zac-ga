"""Safety regression tests. All relocations use disposable temporary Git roots."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("archive_repository_material.py")
SPEC = importlib.util.spec_from_file_location("archive_repository_material", MODULE_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class ArchiveRepositoryMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / "old").mkdir()
        (self.root / "old/payload.bin").write_bytes(b"preserve\x00bytes\xff")
        (self.root / "old/empty").mkdir()
        self.plan = self.root / "plan.json"
        self.run = "build/repository-reorganization/test-run"
        self.moves = [{"source": "old", "destination": "archive/history/old"}]
        self.write_plan()

    def write_plan(self, moves=None):
        self.plan.write_text(json.dumps({"moves": self.moves if moves is None else moves}))

    def seal(self):
        return tool.seal(self.root, self.plan, self.run)

    def execute(self):
        return tool.execute(self.root, self.plan, self.run)

    def verify(self):
        return tool.verify(self.root, self.plan, self.run)

    def snapshot(self):
        return {str(p.relative_to(self.root)): ("dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.root.rglob("*") if not p.is_symlink()}

    def test_default_preview_is_read_only(self):
        before = self.snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(tool.main(["--root", str(self.root), "--plan", str(self.plan)]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "preview_only")
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / "build").exists())

    def test_seal_is_exclusive_and_readonly_and_preserves_git_status(self):
        self.seal()
        run = self.root / self.run
        original = (run / "seal.json").read_bytes()
        data = json.loads(original)
        self.assertIn("old/", data["git_before"]["porcelain_v1_z"])
        self.assertEqual((run / "seal.json").stat().st_mode & 0o222, 0)
        with self.assertRaises(tool.ArchiveError):
            self.seal()
        self.assertEqual((run / "seal.json").read_bytes(), original)
        self.assertTrue((self.root / "old/payload.bin").exists())

    def test_successful_move_preserves_bytes_inode_empty_directories_and_symlink(self):
        (self.root / "old/link").symlink_to("payload.bin")
        inode = (self.root / "old/payload.bin").stat().st_ino
        self.seal()
        self.assertEqual(self.execute()["status"], "verified")
        destination = self.root / "archive/history/old"
        self.assertFalse((self.root / "old").exists())
        self.assertEqual((destination / "payload.bin").read_bytes(), b"preserve\x00bytes\xff")
        self.assertEqual((destination / "payload.bin").stat().st_ino, inode)
        self.assertEqual(os.readlink(destination / "link"), "payload.bin")
        self.assertTrue((destination / "empty").is_dir())
        self.assertEqual(self.verify()["status"], "verified")
        receipts = (self.root / self.run / "receipts/0000.json").read_bytes()
        self.execute()
        self.assertEqual((self.root / self.run / "receipts/0000.json").read_bytes(), receipts)

    def test_regular_file_source_supported(self):
        self.write_plan([{"source": "old/payload.bin", "destination": "archive/payload.bin"}])
        self.seal()
        self.execute()
        self.assertEqual((self.root / "archive/payload.bin").read_bytes(), b"preserve\x00bytes\xff")

    def test_dangerous_sources_rejected(self):
        for source in (".", "..", "/", "/tmp", "../outside", "old/../other", "old//item", "old/", "./old",
                       "old/*", "old/?", "old/[a]", "old/{a,b}", "~/old", ".git", ".git/objects", ".venv",
                       ".venv_qmap32", "ZAC/.venv", "env", "archive", "build", "docs", "old\\other"):
            with self.subTest(source=source):
                self.write_plan([{"source": source, "destination": "archive/history/old"}])
                with self.assertRaises(tool.ArchiveError):
                    tool.preview(self.root, self.plan)

    def test_dangerous_destinations_rejected(self):
        for destination in ("archive", "archive/../outside", "outside/item", "/archive/item", "archive/.git/items", "archive/.venv/items"):
            with self.subTest(destination=destination):
                self.write_plan([{"source": "old", "destination": destination}])
                with self.assertRaises(tool.ArchiveError):
                    tool.preview(self.root, self.plan)

    def test_overlapping_moves_rejected(self):
        plans = [
            [self.moves[0], {"source": "old/empty", "destination": "archive/second"}],
            [self.moves[0], {"source": "other", "destination": "archive/history"}],
            [self.moves[0], {"source": "archive/history/old/nested", "destination": "archive/second"}],
            [{"source": "archive/group", "destination": "archive/group/child"}],
            [{"source": "archive/group/child", "destination": "archive/group"}],
            [self.moves[0], self.moves[0]],
        ]
        for moves in plans:
            with self.subTest(moves=moves):
                self.write_plan(moves)
                with self.assertRaises(tool.ArchiveError):
                    tool.preview(self.root, self.plan)

    def test_existing_destination_rejected_even_if_empty(self):
        destination = self.root / "archive/history/old"
        destination.mkdir(parents=True)
        with self.assertRaises(tool.ArchiveError):
            self.seal()
        self.assertTrue((self.root / "old/payload.bin").exists())

    def test_symlink_source_or_ancestor_rejected(self):
        (self.root / "alias").symlink_to(self.root / "old", target_is_directory=True)
        for source in ("alias", "alias/empty"):
            with self.subTest(source=source):
                self.write_plan([{"source": source, "destination": "archive/new"}])
                with self.assertRaises(tool.ArchiveError):
                    self.seal()

    def test_symlink_destination_ancestor_rejected(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (self.root / "archive").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(tool.ArchiveError):
            self.seal()
        self.assertEqual(list(outside.iterdir()), [])

    def test_embedded_escaping_symlink_rejected(self):
        (self.root / "old/link").symlink_to("../../outside")
        with self.assertRaises(tool.ArchiveError):
            self.seal()

    def test_cross_tree_relative_symlink_rejected_before_it_can_change_meaning(self):
        (self.root / "shared").write_text("external referent")
        (self.root / "old/link").symlink_to("../shared")
        with self.assertRaisesRegex(tool.ArchiveError, "cross-tree"):
            self.seal()
        self.assertFalse((self.root / "build").exists())

    def test_absolute_internal_symlink_rejected_before_it_can_become_dangling(self):
        (self.root / "old/link").symlink_to(self.root / "old/payload.bin")
        with self.assertRaisesRegex(tool.ArchiveError, "absolute symlinks"):
            self.seal()
        self.assertFalse((self.root / "build").exists())

    def test_embedded_environment_rejected(self):
        (self.root / "old/.venv").mkdir()
        with self.assertRaises(tool.ArchiveError):
            self.seal()

    def test_plan_may_not_move_itself(self):
        self.plan = self.root / "old/plan.json"
        self.write_plan()
        with self.assertRaises(tool.ArchiveError):
            self.seal()

    def test_invalid_plan_schema_and_duplicate_keys_rejected(self):
        for raw in ('{}', '{"moves": []}', '{"moves": [], "moves": []}',
                    '{"moves":[{"source":"old","destination":"archive/new","overwrite":true}]}',
                    '{"moves":"old"}'):
            with self.subTest(raw=raw):
                self.plan.write_text(raw)
                with self.assertRaises(tool.ArchiveError):
                    tool.preview(self.root, self.plan)

    def test_audit_run_location_restrictions(self):
        for run in ("build", "docs", "build/existing/test", "docs/old-results/test", "../run", "archive/run", "build/repository-reorganization"):
            with self.subTest(run=run):
                with self.assertRaises(tool.ArchiveError):
                    tool.seal(self.root, self.plan, run)
        self.assertFalse((self.root / "build").exists())

    def test_docs_run_supported_but_cannot_overlap_source(self):
        self.run = "docs/repository-reorganization/reviewed-run"
        self.seal()
        self.execute()
        self.assertEqual(self.verify()["status"], "verified")

    def test_source_content_drift_stops_before_any_move(self):
        (self.root / "second").mkdir()
        (self.root / "second/x").write_text("before")
        self.write_plan(self.moves + [{"source": "second", "destination": "archive/second"}])
        self.seal()
        (self.root / "second/x").write_text("after")
        with self.assertRaisesRegex(tool.ArchiveError, "drift"):
            self.execute()
        self.assertTrue((self.root / "old/payload.bin").exists())
        self.assertFalse((self.root / "archive").exists())

    def test_added_file_or_empty_directory_is_drift(self):
        self.seal()
        (self.root / "old/added").mkdir()
        with self.assertRaisesRegex(tool.ArchiveError, "drift"):
            self.execute()

    def test_plan_drift_rejected(self):
        self.seal()
        self.plan.write_text(self.plan.read_text() + "\n")
        with self.assertRaisesRegex(tool.ArchiveError, "plan changed"):
            self.execute()

    def test_tool_hash_drift_rejected(self):
        self.seal()
        real_read = tool._read_regular
        def altered(path):
            data = real_read(path)
            return data + b"changed" if path == MODULE_PATH.resolve() else data
        with mock.patch.object(tool, "_read_regular", side_effect=altered):
            with self.assertRaisesRegex(tool.ArchiveError, "tool changed"):
                self.execute()

    def test_seal_tampering_rejected(self):
        self.seal()
        path = self.root / self.run / "seal.json"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(tool.ArchiveError, "digest mismatch"):
            self.execute()

    def test_partial_execution_stops_and_resumes_without_replacing_receipt(self):
        (self.root / "second").mkdir()
        (self.root / "second/x").write_text("second")
        self.write_plan(self.moves + [{"source": "second", "destination": "archive/second"}])
        self.seal()
        real_rename = tool._rename_no_replace
        def fail_second(source, destination):
            if source.name == "second":
                raise OSError("injected failure")
            real_rename(source, destination)
        with mock.patch.object(tool, "_rename_no_replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                self.execute()
        receipt = self.root / self.run / "receipts/0000.json"
        first_receipt = receipt.read_bytes()
        self.assertFalse((self.root / "old").exists())
        self.assertTrue((self.root / "second/x").exists())
        self.execute()
        self.assertEqual(receipt.read_bytes(), first_receipt)
        self.assertEqual(self.verify()["moves"], 2)

    def test_interruption_after_rename_before_receipt_is_recoverable(self):
        self.seal()
        real_write = tool._write_json_new
        def fail_receipt(path, data):
            if path.parent.name == "receipts":
                raise OSError("injected receipt failure")
            real_write(path, data)
        with mock.patch.object(tool, "_write_json_new", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "receipt failure"):
                self.execute()
        self.assertFalse((self.root / "old").exists())
        with self.assertRaises(tool.ArchiveError):
            self.verify()
        self.execute()
        receipt = json.loads((self.root / self.run / "receipts/0000.json").read_text())
        self.assertTrue(receipt["recovered_after_interruption"])

    def test_existing_unreceipted_destination_cannot_be_adopted(self):
        self.seal()
        destination = self.root / "archive/history/old"
        destination.parent.mkdir(parents=True)
        os.rename(self.root / "old", destination)  # Simulates an unrecorded external change.
        with self.assertRaisesRegex(tool.ArchiveError, "without an execution intent"):
            self.execute()

    def test_exclusive_rename_does_not_overwrite_destination_created_during_execution(self):
        self.seal()
        real_rename = tool._rename_no_replace
        def create_destination(source, destination):
            destination.mkdir()
            (destination / "author-data").write_text("keep")
            real_rename(source, destination)
        with mock.patch.object(tool, "_rename_no_replace", side_effect=create_destination):
            with self.assertRaises(OSError):
                self.execute()
        self.assertTrue((self.root / "old/payload.bin").exists())
        self.assertEqual((self.root / "archive/history/old/author-data").read_text(), "keep")

    def test_verification_detects_changed_bytes_and_restored_source(self):
        self.seal()
        self.execute()
        (self.root / "archive/history/old/payload.bin").write_bytes(b"tampered")
        with self.assertRaisesRegex(tool.ArchiveError, "drift"):
            self.verify()
        (self.root / "old").mkdir()
        with self.assertRaisesRegex(tool.ArchiveError, "ambiguous"):
            self.verify()

    def test_verification_detects_symlink_target_drift(self):
        (self.root / "old/link").symlink_to("payload.bin")
        self.seal()
        self.execute()
        link = self.root / "archive/history/old/link"
        link.unlink()
        link.symlink_to("empty")
        with self.assertRaisesRegex(tool.ArchiveError, "drift"):
            self.verify()

    def test_changed_unrelated_author_file_does_not_block_relocation(self):
        (self.root / "manuscript.tex").write_text("before")
        self.seal()
        (self.root / "manuscript.tex").write_text("author edit")
        self.execute()
        self.assertEqual((self.root / "manuscript.tex").read_text(), "author edit")

    def test_receipt_tampering_rejected(self):
        self.seal()
        self.execute()
        receipt = self.root / self.run / "receipts/0000.json"
        value = json.loads(receipt.read_text())
        value["seal_sha256"] = "tampered"
        receipt.chmod(0o600)
        receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(tool.ArchiveError, "record mismatch"):
            self.verify()

    def test_symlink_audit_directory_rejected(self):
        target = self.root / "outside-metadata"
        target.mkdir()
        (self.root / "build").symlink_to(target, target_is_directory=True)
        with self.assertRaises(tool.ArchiveError):
            self.seal()
        self.assertEqual(list(target.iterdir()), [])

    def test_atomic_receipt_publication_can_resume_after_interrupted_publish(self):
        self.seal()
        real_publish = tool._exclusive_rename_at
        def fail_receipt(source_fd, source, destination_fd, destination):
            if destination == "0000.json" and (self.root / "archive/history/old").exists():
                raise OSError("injected publication failure")
            real_publish(source_fd, source, destination_fd, destination)
        with mock.patch.object(tool, "_exclusive_rename_at", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.execute()
        receipt_dir = self.root / self.run / "receipts"
        self.assertFalse((receipt_dir / "0000.json").exists())
        pending = list(receipt_dir.glob(".pending-*"))
        self.assertEqual(len(pending), 1)
        self.execute()
        self.assertTrue(pending[0].exists())  # Failed publication evidence is retained.
        self.assertEqual(self.verify()["status"], "verified")

    def test_execute_rejects_parent_replaced_by_symlink_since_seal(self):
        self.seal()
        outside = self.root.parent / "external"
        outside.mkdir()
        (self.root / "archive").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(tool.ArchiveError):
            self.execute()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.root / "old/payload.bin").exists())

    def test_exclusive_rename_refuses_even_empty_destination(self):
        destination = self.root / "already-exists"
        destination.mkdir()
        with self.assertRaises(OSError):
            tool._rename_no_replace(self.root / "old", destination)
        self.assertTrue((self.root / "old/payload.bin").exists())

    def test_verify_is_readonly(self):
        self.seal()
        self.execute()
        before = self.snapshot()
        self.verify()
        self.assertEqual(before, self.snapshot())

    def test_lock_prevents_concurrent_execution(self):
        self.seal()
        with tool._execution_lock(self.root / self.run):
            with self.assertRaisesRegex(tool.ArchiveError, "holds the run lock"):
                self.execute()
        self.assertTrue((self.root / "old/payload.bin").exists())


if __name__ == "__main__":
    unittest.main()
