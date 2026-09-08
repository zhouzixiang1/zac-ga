"""Narrow build-relocation regressions; every payload lives in a system tempdir."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent
ARCHIVE_SPEC = importlib.util.spec_from_file_location(
    'archive_repository_material', SCRIPTS / 'archive_repository_material.py')
archive_tool = importlib.util.module_from_spec(ARCHIVE_SPEC)
ARCHIVE_SPEC.loader.exec_module(archive_tool)
SPEC = importlib.util.spec_from_file_location('build_relocation', SCRIPTS / 'relocate_build_outputs.py')
relocate = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'archive_repository_material': archive_tool}):
    SPEC.loader.exec_module(relocate)


class RelocateBuildOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'build'
        self.target = self.root / 'IEEE_conference_template/build'
        self.archive = self.root / 'archive/build-location-20260907'
        self.audit = self.root / 'archive/_manifests/build-location-20260907'
        self.payloads = {
            'paper_zh/paper_zh.pdf': b'Chinese PDF\x00\xff',
            'paper_en/paper_en.pdf': b'English PDF\x01\xfe',
            'paper_zh/figures/overall_framework.pdf': b'vector Fig3\x00',
            'overleaf-sync/.git/config': b'[remote "origin"]\nurl = retained\n',
            'overleaf-sync/paper_zh.tex': b'cloud working copy',
            'payload.bin': bytes(range(256)),
            'tmp/pytest-0/case/result.txt': b'pytest target data',
        }
        for relative, content in self.payloads.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.target.mkdir(parents=True)
        (self.target / 'previous.aux').write_bytes(b'old manuscript aux\xff')
        (self.target / 'empty').mkdir()
        (self.source / 'empty').mkdir()

    def execute(self, **kwargs):
        return relocate.execute(self.root, **kwargs)

    def add_native_caches(self):
        for name in ('ctest', 'wheel'):
            cache = self.source / 'native' / name
            cache.mkdir(parents=True)
            (cache / 'CMakeCache.txt').write_text(f'ORIGINAL_CACHE={cache}\n')
            (cache / 'output.bin').write_bytes(f'{name} payload'.encode())

    def assert_preflight_failure(self, expression):
        before = relocate.inventory(self.root)
        with patch.object(relocate, '_rename_no_replace') as rename, \
                self.assertRaisesRegex(ValueError, expression):
            self.execute(perform=True)
        rename.assert_not_called()
        self.assertEqual(relocate.inventory(self.root), before)
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.audit.exists())

    def test_default_preview_does_not_write(self):
        before = relocate.inventory(self.root)
        with patch.object(relocate, '_rename_no_replace') as rename, \
                patch.object(relocate.shutil, 'copy2') as copy:
            report = self.execute()
        self.assertEqual(report['status'], 'preview')
        self.assertEqual(report['source'], 'build')
        self.assertEqual(report['destination'], 'IEEE_conference_template/build')
        self.assertEqual(before, relocate.inventory(self.root))
        rename.assert_not_called()
        copy.assert_not_called()
        self.assertFalse((self.root / 'archive').exists())

    def test_default_cli_is_preview_only(self):
        before = relocate.inventory(self.root)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / 'relocate_build_outputs.py'), '--root', str(self.root)],
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
            check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'preview')
        self.assertEqual(before, relocate.inventory(self.root))

    def test_existing_archive_is_not_overwritten(self):
        self.archive.mkdir(parents=True)
        (self.archive / 'author-data').write_bytes(b'keep this archive')
        before = relocate.inventory(self.root)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.execute(perform=True)
        self.assertEqual(before, relocate.inventory(self.root))

    def test_existing_audit_is_not_overwritten(self):
        self.audit.mkdir(parents=True)
        (self.audit / 'receipt.json').write_bytes(b'previous immutable receipt')
        before = relocate.inventory(self.root)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.execute(perform=True)
        self.assertEqual(before, relocate.inventory(self.root))

    def test_dangling_archive_path_is_not_overwritten(self):
        self.archive.parent.mkdir()
        self.archive.symlink_to('missing-old-archive')
        before = relocate.inventory(self.root)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.execute(perform=True)
        self.assertEqual(before, relocate.inventory(self.root))
        self.assertEqual(os.readlink(self.archive), 'missing-old-archive')

    def test_move_preserves_all_payload_bytes_and_previous_build(self):
        original = relocate.inventory(self.source)
        previous = relocate.inventory(self.target)
        payload_inode = (self.source / 'payload.bin').stat().st_ino
        report = self.execute(perform=True)
        self.assertEqual(report['status'], 'passed')
        self.assertFalse(os.path.lexists(self.source))
        self.assertEqual(relocate.inventory(self.target), original)
        self.assertEqual(relocate.inventory(self.archive / 'previous-manuscript-build'), previous)
        self.assertEqual((self.target / 'payload.bin').stat().st_ino, payload_inode)
        for relative, content in self.payloads.items():
            self.assertEqual((self.target / relative).read_bytes(), content)
        for source, name in (('paper_zh/paper_zh.pdf', 'paper_zh.pdf'),
                             ('paper_en/paper_en.pdf', 'paper_en.pdf'),
                             ('paper_zh/figures/overall_framework.pdf', 'overall_framework.pdf')):
            self.assertEqual((self.archive / 'pre-migration-pdfs' / name).read_bytes(), self.payloads[source])
        before = json.loads((self.audit / 'before.json').read_text())
        self.assertEqual(before, {'root_build': original, 'old_manuscript_build': previous})
        self.assertEqual(json.loads((self.audit / 'receipt.json').read_text()), report)
        self.assertFalse(report['experiment_data_deleted'])

    def test_native_old_caches_are_preserved_separately_without_rewriting_paths(self):
        self.add_native_caches()
        originals = {name: relocate.inventory(self.source / 'native' / name) for name in ('ctest', 'wheel')}
        report = self.execute(perform=True)
        for name, expected in originals.items():
            old = self.target / 'native' / name
            archived = self.target / 'native/prior-root-cache' / name
            self.assertFalse(old.exists())
            self.assertEqual(relocate.inventory(archived), expected)
            self.assertIn(str(self.source / 'native' / name), (archived / 'CMakeCache.txt').read_text())
        self.assertEqual(report['native_cache_moves'], [
            {'source': f'build/native/{name}',
             'destination': f'IEEE_conference_template/build/native/prior-root-cache/{name}'}
            for name in ('ctest', 'wheel')])

    def test_five_internal_pytest_symlinks_rebase_to_same_payload(self):
        links = []
        original_target = self.source / 'tmp/pytest-0/case'
        for index in range(5):
            link = self.source / 'tmp' / f'pytest-current-{index}'
            link.symlink_to(original_target, target_is_directory=True)
            links.append(link.relative_to(self.source))
        report = self.execute(perform=True)
        expected = self.target / 'tmp/pytest-0/case'
        self.assertEqual(len(report['rebased_pytest_links']), 5)
        for relative in links:
            link = self.target / relative
            self.assertFalse(Path(os.readlink(link)).is_absolute())
            self.assertEqual(link.resolve(strict=True), expected)
            self.assertEqual((link / 'result.txt').read_bytes(), b'pytest target data')
        self.assertTrue(all(item['before'] == str(original_target) for item in report['rebased_pytest_links']))

    def test_internal_relative_symlink_preserves_target(self):
        (self.source / 'tmp/pytest-current').symlink_to('pytest-0/case', target_is_directory=True)
        self.execute(perform=True)
        self.assertEqual((self.target / 'tmp/pytest-current').resolve(strict=True),
                         self.target / 'tmp/pytest-0/case')

    def test_symlink_inside_native_cache_uses_its_relocated_path(self):
        self.add_native_caches()
        (self.source / 'native/ctest/latest').symlink_to(self.source / 'native/ctest/output.bin')
        self.execute(perform=True)
        link = self.target / 'native/prior-root-cache/ctest/latest'
        self.assertEqual(link.resolve(strict=True), self.target / 'native/prior-root-cache/ctest/output.bin')
        self.assertEqual(link.read_bytes(), b'ctest payload')

    def test_external_symlink_is_rejected_before_any_mutation(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.source / 'tmp/bad-link').symlink_to(outside, target_is_directory=True)
        self.assert_preflight_failure('external symlink')

    def test_old_manuscript_build_link_requires_an_explicit_plan_before_archiving(self):
        (self.target / 'old-link').symlink_to(self.target / 'previous.aux')
        self.assert_preflight_failure('symlink in the old manuscript build')

    def test_lexical_build_prefix_with_dotdot_does_not_bypass_containment(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.source / 'tmp/bad-link').symlink_to(str(self.source / '../outside'), target_is_directory=True)
        self.assert_preflight_failure('external symlink')

    def test_missing_symlink_target_is_rejected_before_any_mutation(self):
        (self.source / 'tmp/bad-link').symlink_to(self.source / 'missing')
        self.assert_preflight_failure('Unresolvable symlink')

    def test_native_cache_archive_conflict_is_rejected_before_any_mutation(self):
        self.add_native_caches()
        destination = self.source / 'native/prior-root-cache/ctest'
        destination.mkdir(parents=True)
        (destination / 'keep').write_bytes(b'previous cache')
        self.assert_preflight_failure('Native cache archive already exists')

    def test_native_cache_archive_dangling_symlink_is_rejected_before_any_mutation(self):
        self.add_native_caches()
        destination = self.source / 'native/prior-root-cache/ctest'
        destination.parent.mkdir()
        destination.symlink_to('missing')
        self.assert_preflight_failure('Native cache archive already exists')

    def test_missing_required_pdf_is_rejected_before_any_mutation(self):
        (self.source / 'paper_en/paper_en.pdf').unlink()
        self.assert_preflight_failure('regular pre-migration PDF')


if __name__ == '__main__':
    unittest.main()
