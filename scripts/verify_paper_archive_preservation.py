#!/usr/bin/env python3
"""Read-only preservation audit across an explicit repository relocation map.

The original accepted-results seal is never rewritten. Its old relative paths
are resolved through the longest matching source prefix in the approved move
plan. By default the JSON report is written only to stdout. --output can create
one new report below IEEE_conference_template/build/repository-reorganization; it never overwrites one.
This tool does not run experiments, compile papers, or perform relocations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

DEFAULT_SEAL = 'ZAC_zzx/results/supplementary_20260907/accepted_results_seal.json'
DEFAULT_PLAN = 'IEEE_conference_template/build/repository-reorganization/move_plan.json'
DEFAULT_BUILD = 'IEEE_conference_template/build/paper_zh/source_build_manifest.json'
DEFAULT_KEEP = 'IEEE_conference_template/build/repository-reorganization/evidence_plan.json'
DEFAULT_PRIMARY = 'ZAC_zzx/results/paper_zh_v2/final_manifest.json'
SHA = re.compile(r'[0-9a-f]{64}\Z')
PUBLICATION_SOURCE_PREFIX = '../ZAC_zzx/results/default_initial_v1/paper_exports/'
PUBLICATION_SOURCE_NAMES = frozenset({
    'analysis_units.csv', 'default_initial_values.json',
    'default_initial_values.tex', 'main_rows.csv', 'mechanism.csv',
    'representative_cases.tex',
})


class PreservationError(RuntimeError):
    pass


def relative(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PreservationError('expected a nonempty literal relative path')
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or
            any(p in ('.', '..') for p in value.split('/')) or
            any(ord(c) < 32 for c in value) or '\\' in value):
        raise PreservationError(f'noncanonical or escaping relative path: {value!r}')
    return value


def contains(parent: str, child: str) -> bool:
    return child == parent or child.startswith(parent + '/')


def paper_source_relative(value: object) -> str:
    """Map only the six audited publication inputs outside the paper folder."""
    if isinstance(value, str) and value.startswith(PUBLICATION_SOURCE_PREFIX):
        name = value[len(PUBLICATION_SOURCE_PREFIX):]
        if name not in PUBLICATION_SOURCE_NAMES:
            raise PreservationError(f'unrecognized external paper source: {value!r}')
        return relative(value[3:])
    return 'IEEE_conference_template/' + relative(value)


def root_relative(root: Path, value: str | Path) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            value = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise PreservationError(f'path is outside the repository: {path}') from exc
    return relative(str(value))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreservationError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def load(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise PreservationError(f'invalid JSON: {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise PreservationError(f'expected JSON object: {path}')
    return value, hashlib.sha256(raw).hexdigest()


def fingerprint(path: Path) -> dict:
    # Match the old seal: hash the referenced file, following existing symlinks.
    # No symlink or target is changed. This includes already sealed interpreters.
    before_path = path.stat()
    if not stat.S_ISREG(before_path.st_mode):
        raise PreservationError(f'not a regular file: {path}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        after = os.fstat(stream.fileno())
    after_path = path.stat()
    attrs = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before_path, a) != getattr(before, a) or
           getattr(before, a) != getattr(after, a) or
           getattr(after, a) != getattr(after_path, a) for a in attrs):
        raise PreservationError(f'file changed during hashing: {path}')
    return {'bytes': after.st_size, 'sha256': digest.hexdigest()}


def expected_record(value, *, require_bytes=True) -> dict:
    if isinstance(value, str):
        value = {'sha256': value}
    if not isinstance(value, dict) or not SHA.fullmatch(str(value.get('sha256', ''))):
        raise PreservationError('invalid expected SHA-256 record')
    if require_bytes and ('bytes' not in value or type(value['bytes']) is not int or value['bytes'] < 0):
        raise PreservationError('expected file record lacks a valid byte count')
    result = {'sha256': value['sha256']}
    if 'bytes' in value:
        if type(value['bytes']) is not int or value['bytes'] < 0:
            raise PreservationError('invalid byte count')
        result['bytes'] = value['bytes']
    return result


class RelocationMap:
    def __init__(self, payload: dict):
        if set(payload) != {'moves'} or not isinstance(payload['moves'], list):
            raise PreservationError('move plan must contain only a moves list')
        self.moves = []
        sources, destinations = set(), set()
        for item in payload['moves']:
            if not isinstance(item, dict) or set(item) != {'source', 'destination'}:
                raise PreservationError('invalid move record')
            source, destination = (relative(item[k]) for k in ('source', 'destination'))
            if not destination.startswith('archive/') or source in sources or destination in destinations:
                raise PreservationError('invalid or duplicate move endpoints')
            if contains(source, destination) or contains(destination, source):
                raise PreservationError('a move overlaps its own source and destination')
            sources.add(source)
            destinations.add(destination)
            self.moves.append({'source': source, 'destination': destination})
        self.moves.sort(key=lambda m: (-len(PurePosixPath(m['source']).parts), -len(m['source']), m['source']))

    def map(self, old: str) -> str:
        old = relative(old)
        for move in self.moves:
            if contains(move['source'], old):
                return move['destination'] + old[len(move['source']):]
        return old


def audit(root: Path, *, seal=DEFAULT_SEAL, plan=DEFAULT_PLAN,
          paper_build=DEFAULT_BUILD, keep_plan=DEFAULT_KEEP, primary=DEFAULT_PRIMARY,
          expected_seal_count=20676, expected_source_count=45,
          expected_seal_sha256=None, expected_build_sha256=None) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise PreservationError('root must be a non-filesystem-root directory')
    paths = {name: root_relative(root, p) for name, p in (
        ('seal', seal), ('plan', plan), ('paper_build', paper_build),
        ('keep_plan', keep_plan), ('primary', primary))}
    payloads, identities = {}, {}
    for name, value in paths.items():
        payloads[name], identities[name] = load(root / value)
    mapping = RelocationMap(payloads['plan'])
    failures = []
    sections = {}

    def fail(section, issue, **details):
        failures.append({'section': section, 'issue': issue, **details})

    for name, expected in (('seal', expected_seal_sha256), ('paper_build', expected_build_sha256)):
        if expected is not None and (not SHA.fullmatch(expected) or identities[name] != expected):
            fail('metadata', 'external_anchor_mismatch', path=paths[name], expected=expected, actual=identities[name])
    files = payloads['seal'].get('files')
    if payloads['seal'].get('schema') != 1 or not isinstance(files, dict):
        raise PreservationError('unsupported original accepted-results seal')
    if expected_seal_count is not None and len(files) != expected_seal_count:
        fail('accepted_seal', 'unexpected_file_count', expected=expected_seal_count, actual=len(files))

    # A post-relocation audit must not silently fall back to an unmoved old path.
    move_ok = 0
    for move in mapping.moves:
        old_exists = os.path.lexists(root / move['source'])
        new_exists = os.path.lexists(root / move['destination'])
        if old_exists or not new_exists:
            fail('move_state', 'relocation_not_complete', **move, source_exists=old_exists, destination_exists=new_exists)
        else:
            move_ok += 1
    sections['move_state'] = {'planned': len(mapping.moves), 'completed_paths': move_ok}

    def check(section, old, expected, *, remap=True, require_bytes=True):
        old = relative(old)
        destination = mapping.map(old) if remap else old
        record = expected_record(expected, require_bytes=require_bytes)
        count = sections.setdefault(section, {'checked': 0, 'matched': 0, 'relocated': 0, 'verified_file_bytes': 0})
        count['checked'] += 1
        count['relocated'] += destination != old
        try:
            actual = fingerprint(root / destination)
            if any(actual[k] != v for k, v in record.items()):
                fail(section, 'content_mismatch', old_path=old, actual_path=destination, expected=record, actual=actual)
            else:
                count['matched'] += 1
                count['verified_file_bytes'] += actual['bytes']
        except (OSError, PreservationError) as exc:
            fail(section, 'unreadable_or_unstable', old_path=old, actual_path=destination, error=str(exc))

    for old, expected in files.items():
        check('accepted_seal', old, expected)

    build = payloads['paper_build']
    sources, artifacts = build.get('source_sha256'), build.get('artifacts')
    if build.get('protocol') != 'paper-source-build-v1' or not isinstance(sources, dict) or not isinstance(artifacts, dict):
        raise PreservationError('unsupported paper source/build manifest')
    if expected_source_count is not None and len(sources) != expected_source_count:
        fail('paper_sources', 'unexpected_source_count', expected=expected_source_count, actual=len(sources))
    for source, expected in sources.items():
        check('paper_sources', paper_source_relative(source), expected, remap=False, require_bytes=False)
    build_dir = PurePosixPath(paths['paper_build']).parent.as_posix()
    for artifact, expected in artifacts.items():
        check('paper_artifacts', build_dir + '/' + relative(artifact), expected, remap=False, require_bytes=False)
    if 'paper_zh.pdf' not in artifacts or 'figures/overall_framework.pdf' not in artifacts:
        fail('paper_artifacts', 'required_pdf_missing_from_build_manifest')

    entries = payloads['keep_plan'].get('entries')
    if not isinstance(entries, list):
        raise PreservationError('KEEP plan requires an entries list')
    keeps = [e for e in entries if e.get('action') == 'KEEP']
    keep_ok = 0
    for keep in keeps:
        old = root_relative(root, keep['path'])
        if mapping.map(old) != old:
            fail('keep_paths', 'KEEP_path_scheduled_for_relocation', path=old)
        elif not (root / old).exists():
            fail('keep_paths', 'KEEP_path_missing', path=old)
        elif 'is_dir' in keep and (root / old).is_dir() != keep['is_dir']:
            fail('keep_paths', 'KEEP_path_type_changed', path=old)
        else:
            keep_ok += 1
    sections['keep_paths'] = {'checked': len(keeps), 'present_at_original_path': keep_ok}

    # The primary manifest must be anchored in the immutable older seal, not
    # merely validate a possibly edited list of hashes against edited results.
    if paths['primary'] not in files:
        fail('primary_manifest', 'primary_manifest_not_anchored_in_original_seal', path=paths['primary'])
    else:
        check('primary_manifest', paths['primary'], files[paths['primary']], remap=False)
    primary_files = payloads['primary'].get('files')
    if not isinstance(primary_files, dict) or not primary_files:
        raise PreservationError('primary final manifest requires a nonempty files map')
    primary_dir = PurePosixPath(paths['primary']).parent.as_posix()
    for source, expected in primary_files.items():
        check('primary_files', primary_dir + '/' + relative(source), expected, remap=False)

    # Fail if an author or another process changes the control documents while
    # this potentially long read-only content audit is running.
    for name, old in identities.items():
        try:
            current = hashlib.sha256((root / paths[name]).read_bytes()).hexdigest()
            if current != old:
                fail('metadata', 'control_document_changed_during_audit', path=paths[name], before=old, after=current)
        except OSError as exc:
            fail('metadata', 'control_document_disappeared', path=paths[name], error=str(exc))
    return {'schema': 'paper-archive-preservation-v1', 'status': 'passed' if not failures else 'failed',
            'root': str(root), 'read_only': True, 'sections': sections, 'failures': failures,
            'control_documents': {k: {'path': paths[k], 'sha256': h} for k, h in identities.items()},
            'external_anchors': {'seal': expected_seal_sha256, 'paper_build': expected_build_sha256},
            'notes': ['Original seal and historical path declarations are unchanged.',
                      'Longest old-prefix relocation mapping is an audit view, not an update to old claims.',
                      'Existing file symlinks are hashed using the same follow-target semantics as the original seal.',
                      'Newly added files are outside this preservation check; every originally sealed file is checked.',
                      'Nonsealed archived payloads are covered by the separate relocation executor inventory.']}


def write_report_new(root: Path, output: Path, result: dict) -> None:
    root = root.resolve(strict=True)
    rel = root_relative(root, output)
    if not rel.startswith('IEEE_conference_template/build/repository-reorganization/') or not rel.endswith('.json'):
        raise PreservationError('report must be a new JSON below IEEE_conference_template/build/repository-reorganization/')
    path = root / rel
    # Do not create directories or follow a report-directory symlink.
    current = root
    for part in PurePosixPath(rel).parts[:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise PreservationError(f'unsafe or missing report parent: {current}')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--seal', default=DEFAULT_SEAL)
    parser.add_argument('--plan', default=DEFAULT_PLAN)
    parser.add_argument('--paper-build', default=DEFAULT_BUILD)
    parser.add_argument('--keep-plan', default=DEFAULT_KEEP)
    parser.add_argument('--primary', default=DEFAULT_PRIMARY)
    parser.add_argument('--expected-seal-count', type=int, default=20676)
    parser.add_argument('--expected-source-count', type=int, default=45)
    parser.add_argument('--expected-seal-sha256')
    parser.add_argument('--expected-build-sha256')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit(args.root, seal=args.seal, plan=args.plan, paper_build=args.paper_build,
                       keep_plan=args.keep_plan, primary=args.primary,
                       expected_seal_count=args.expected_seal_count, expected_source_count=args.expected_source_count,
                       expected_seal_sha256=args.expected_seal_sha256, expected_build_sha256=args.expected_build_sha256)
        if args.output:
            write_report_new(args.root, args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result['status'] == 'passed' else 1
    except (OSError, PreservationError, KeyError, TypeError) as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
