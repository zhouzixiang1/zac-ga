"""Narrow preservation tests using tiny disposable fixtures only."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('preservation', Path(__file__).with_name('verify_paper_archive_preservation.py'))
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.put('old/group/raw.bin', b'original\0data')
        self.put('keep/record.json', b'{}')
        self.put('IEEE_conference_template/paper_zh.tex', b'author text')
        self.put('IEEE_conference_template/build/paper_zh/paper_zh.pdf', b'%PDF-main')
        self.put('IEEE_conference_template/build/paper_zh/figures/overall_framework.pdf', b'%PDF-figure')
        self.put('ZAC_zzx/results/paper_zh_v2/result.csv', b'a,b\n1,2\n')
        self.jput(tool.DEFAULT_PRIMARY, {'files': {'result.csv': self.fp('ZAC_zzx/results/paper_zh_v2/result.csv')}})
        self.sealed_files = ['old/group/raw.bin', 'keep/record.json', tool.DEFAULT_PRIMARY,
                             'ZAC_zzx/results/paper_zh_v2/result.csv']
        self.jput(tool.DEFAULT_SEAL, {'schema': 1, 'files': {p: self.fp(p) for p in self.sealed_files}})
        self.jput(tool.DEFAULT_PLAN, {'moves': [{'source': 'old/group', 'destination': 'archive/history/group'}]})
        self.jput(tool.DEFAULT_BUILD, {'protocol': 'paper-source-build-v1',
                    'source_sha256': {'paper_zh.tex': self.fp('IEEE_conference_template/paper_zh.tex')['sha256']},
                    'artifacts': {p: self.fp('IEEE_conference_template/build/paper_zh/' + p) for p in ('paper_zh.pdf', 'figures/overall_framework.pdf')}})
        self.jput(tool.DEFAULT_KEEP, {'entries': [{'action': 'KEEP', 'path': str(self.root / 'keep'), 'is_dir': True}]})
        self.root.joinpath('archive/history').mkdir(parents=True)
        os.rename(self.root / 'old/group', self.root / 'archive/history/group')

    def put(self, relative, data):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def jput(self, relative, value):
        self.put(relative, json.dumps(value).encode())

    def fp(self, relative):
        data = (self.root / relative).read_bytes()
        return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    def check(self, **kwargs):
        return tool.audit(self.root, expected_seal_count=4, expected_source_count=1, **kwargs)

    def snapshot(self):
        return {str(p.relative_to(self.root)): self.fp(p.relative_to(self.root))
                for p in self.root.rglob('*') if p.is_file()}

    def test_success_is_readonly_and_covers_all_sections(self):
        before = self.snapshot()
        result = self.check()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['sections']['accepted_seal']['checked'], 4)
        self.assertEqual(result['sections']['accepted_seal']['relocated'], 1)
        self.assertEqual(result['sections']['paper_sources']['matched'], 1)
        self.assertEqual(result['sections']['paper_artifacts']['matched'], 2)
        self.assertEqual(result['sections']['primary_manifest']['matched'], 1)
        self.assertEqual(before, self.snapshot())

    def test_longest_prefix_and_component_boundary(self):
        mapper = tool.RelocationMap({'moves': [
            {'source': 'old', 'destination': 'archive/general'},
            {'source': 'old/deeper', 'destination': 'archive/specific'}]})
        self.assertEqual(mapper.map('old/deeper/a'), 'archive/specific/a')
        self.assertEqual(mapper.map('oldish/a'), 'oldish/a')

    def test_mapping_rejects_escape_and_duplicate_endpoints(self):
        for source, target in [('../old', 'archive/x'), ('old', '../outside'),
                               ('old', '/outside'), ('old', 'archive/../x')]:
            with self.subTest(source=source, target=target):
                with self.assertRaises(tool.PreservationError):
                    tool.RelocationMap({'moves': [{'source': source, 'destination': target}]})
        with self.assertRaises(tool.PreservationError):
            tool.RelocationMap({'moves': [{'source': 'x', 'destination': 'archive/x'}] * 2})

    def test_changed_same_size_payload_fails_hash(self):
        self.put('archive/history/group/raw.bin', b'Xriginal\0data')
        result = self.check()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any(x['issue'] == 'content_mismatch' for x in result['failures']))

    def test_missing_mapped_file_does_not_fall_back_to_source(self):
        os.rename(self.root / 'archive/history/group', self.root / 'old/group')
        result = self.check()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any(x['section'] == 'move_state' for x in result['failures']))
        self.assertTrue(any(x['issue'] == 'unreadable_or_unstable' for x in result['failures']))

    def test_duplicate_old_and_new_paths_are_not_accepted(self):
        self.put('old/group/raw.bin', b'original\0data')
        self.assertEqual(self.check()['status'], 'failed')

    def test_missing_keep_path_fails(self):
        self.jput(tool.DEFAULT_KEEP, {'entries': [{'action': 'KEEP', 'path': 'nonexistent'}]})
        self.assertTrue(any(x['issue'] == 'KEEP_path_missing' for x in self.check()['failures']))

    def test_keep_path_must_not_be_scheduled_to_move(self):
        self.jput(tool.DEFAULT_KEEP, {'entries': [{'action': 'KEEP', 'path': 'old/group'}]})
        self.assertTrue(any(x['issue'] == 'KEEP_path_scheduled_for_relocation' for x in self.check()['failures']))

    def test_changed_paper_source_and_pdf_fail(self):
        self.put('IEEE_conference_template/paper_zh.tex', b'new author text')
        self.put('IEEE_conference_template/build/paper_zh/paper_zh.pdf', b'changed PDF')
        sections = {x['section'] for x in self.check()['failures']}
        self.assertTrue({'paper_sources', 'paper_artifacts'} <= sections)

    def test_external_publication_sources_are_audited_readonly(self):
        build = json.loads((self.root / tool.DEFAULT_BUILD).read_bytes())
        for name in tool.PUBLICATION_SOURCE_NAMES:
            source = tool.PUBLICATION_SOURCE_PREFIX + name
            self.put(source[3:], name.encode())
            build['source_sha256'][source] = self.fp(source[3:])['sha256']
        self.jput(tool.DEFAULT_BUILD, build)
        before = self.snapshot()
        result = tool.audit(self.root, expected_seal_count=4, expected_source_count=7)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['sections']['paper_sources']['matched'], 7)
        self.assertEqual(before, self.snapshot())
        self.put(tool.PUBLICATION_SOURCE_PREFIX[3:] + 'main_rows.csv', b'changed')
        result = tool.audit(self.root, expected_seal_count=4, expected_source_count=7)
        self.assertTrue(any(x['section'] == 'paper_sources' and x['issue'] == 'content_mismatch'
                            for x in result['failures']))

    def test_external_source_allowlist_does_not_relax_path_validation(self):
        prefix = tool.PUBLICATION_SOURCE_PREFIX
        self.assertEqual(tool.paper_source_relative('sections/03_method.tex'),
                         'IEEE_conference_template/sections/03_method.tex')
        for source in [prefix + 'unknown.tex', prefix + '../main_rows.csv',
                       prefix + './main_rows.csv', prefix + 'nested/main_rows.csv',
                       '../elsewhere/main_rows.csv', '../../ZAC_zzx/main_rows.csv',
                       '/tmp/main_rows.csv', prefix + 'main_rows.csv ', None]:
            with self.subTest(source=source):
                with self.assertRaises(tool.PreservationError):
                    tool.paper_source_relative(source)
        with self.assertRaises(tool.PreservationError):
            tool.relative(prefix + 'main_rows.csv')
        with self.assertRaises(tool.PreservationError):
            tool.RelocationMap({'moves': [{'source': prefix + 'main_rows.csv',
                                           'destination': 'archive/main_rows.csv'}]})

    def test_primary_manifest_self_is_anchored_in_old_seal(self):
        self.put('ZAC_zzx/results/paper_zh_v2/result.csv', b'new')
        self.jput(tool.DEFAULT_PRIMARY, {'files': {'result.csv': self.fp('ZAC_zzx/results/paper_zh_v2/result.csv')}})
        result = self.check()
        self.assertTrue(any(x['section'] == 'primary_manifest' and x['issue'] == 'content_mismatch' for x in result['failures']))

    def test_expected_counts_and_external_anchors_enforced(self):
        result = tool.audit(self.root, expected_seal_count=20676, expected_source_count=45,
                            expected_seal_sha256='0' * 64, expected_build_sha256='0' * 64)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(sum(x['issue'] == 'external_anchor_mismatch' for x in result['failures']), 2)
        self.assertEqual(sum(x['issue'].startswith('unexpected_') for x in result['failures']), 2)

    def test_legacy_sealed_symlink_hash_semantics_preserved(self):
        target = self.root.parent / (self.root.name + '-referent')
        target.write_bytes(b'{}')
        self.addCleanup(target.unlink)
        (self.root / 'keep/record.json').unlink()
        (self.root / 'keep/record.json').symlink_to(target)
        self.assertEqual(self.check()['status'], 'passed')

    def test_control_document_drift_during_audit_fails(self):
        real = tool.fingerprint
        changed = False
        def change(path):
            nonlocal changed
            value = real(path)
            if not changed:
                changed = True
                path = self.root / tool.DEFAULT_KEEP
                path.write_bytes(path.read_bytes() + b'\n')
            return value
        with mock.patch.object(tool, 'fingerprint', side_effect=change):
            result = self.check()
        self.assertTrue(any(x['issue'] == 'control_document_changed_during_audit' for x in result['failures']))

    def test_output_is_optional_exclusive_and_restricted(self):
        result = self.check()
        output = self.root / 'IEEE_conference_template/build/repository-reorganization/report.json'
        tool.write_report_new(self.root, output, result)
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            tool.write_report_new(self.root, output, result)
        self.assertEqual(output.read_bytes(), before)
        with self.assertRaises(tool.PreservationError):
            tool.write_report_new(self.root, self.root / 'keep/report.json', result)

    def test_default_stdout_cli_does_not_write(self):
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = tool.main(['--root', str(self.root), '--expected-seal-count', '4', '--expected-source-count', '1'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'passed')
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
