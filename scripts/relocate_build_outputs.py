#!/usr/bin/env python3
"""One-time, non-overwriting relocation of the author-selected build directory.

Default: inspect the fixed source and destination. --execute preserves the old
manuscript build first, inventories both trees, then moves the root build intact.
No experiment is run and no frozen file contents are rewritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

from archive_repository_material import _rename_no_replace


def inventory(path):
    entries = {}
    for item in sorted(path.rglob('*')):
        key = item.relative_to(path).as_posix()
        info = item.lstat()
        if item.is_symlink():
            entries[key] = {'type': 'symlink', 'target': os.readlink(item)}
        elif item.is_dir():
            entries[key] = {'type': 'directory'}
        elif stat.S_ISREG(info.st_mode):
            with item.open('rb') as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1048576), b''):
                    digest.update(block)
            entries[key] = {'type': 'file', 'bytes': info.st_size,
                            'sha256': digest.hexdigest()}
        else:
            raise ValueError(f'Unexpected special file: {item}')
    return entries


def write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')


def preflight_cache_moves(source, target):
    moves = []
    for name in ('ctest', 'wheel'):
        old = source / 'native' / name
        if not os.path.lexists(old):
            continue
        destination_before = source / 'native/prior-root-cache' / name
        if not old.is_dir() or old.is_symlink():
            raise ValueError(f'Expected an ordinary native cache directory: {old}')
        for parent in (old.parent, destination_before.parent):
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError(f'Unsafe native cache parent: {parent}')
        if os.path.lexists(destination_before):
            raise ValueError(f'Native cache archive already exists: {destination_before}')
        moves.append({'source': f'build/native/{name}',
                      'destination': str((target / 'native/prior-root-cache' / name).relative_to(source.parent))})
    return moves


def relocated_relative(relative, cache_moves):
    relative = Path(relative)
    for move in cache_moves:
        old = Path(move['source']).relative_to('build')
        if relative.is_relative_to(old):
            return Path('native/prior-root-cache') / old.name / relative.relative_to(old)
    return relative


def preflight_links(source, target, original, cache_moves):
    links = []
    for relative, value in original.items():
        if value['type'] != 'symlink':
            continue
        old_target = Path(value['target'])
        if not old_target.is_absolute():
            old_target = (source / relative).parent / old_target
        try:
            resolved = old_target.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f'Unresolvable symlink target: {relative}') from error
        # Resolve .. and intermediate symlinks before the containment check.
        # A lexical prefix alone can falsely accept build/../outside.
        if not resolved.is_relative_to(source):
            raise ValueError(f'Unexpected external symlink: {relative}')
        link = target / relocated_relative(relative, cache_moves)
        new_target = target / relocated_relative(resolved.relative_to(source), cache_moves)
        links.append({'path': str(link.relative_to(source.parent)),
                      'before': value['target'],
                      'after': os.path.relpath(new_target, link.parent),
                      'target_relative': str(new_target.relative_to(source.parent))})
    return links


def execute(root, *, perform=False):
    root = root.resolve(strict=True)
    source = root / 'build'
    target = root / 'IEEE_conference_template/build'
    archive = root / 'archive/build-location-20260907'
    audit = root / 'archive/_manifests/build-location-20260907'
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Expected the original root build directory')
    if not target.is_dir() or target.is_symlink():
        raise ValueError('Expected the existing manuscript-local build directory')
    if os.path.lexists(archive) or os.path.lexists(audit):
        raise ValueError('A relocation record already exists; inspect it, do not overwrite')
    for parent in (source.parent, target.parent, archive.parent, audit.parent):
        if parent.is_symlink():
            raise ValueError(f'Symlink parent: {parent}')
    before = {'root_build': inventory(source), 'old_manuscript_build': inventory(target)}
    if any(value['type'] == 'symlink' for value in before['old_manuscript_build'].values()):
        raise ValueError('Unexpected symlink in the old manuscript build; its archive needs a separate link plan')
    cache_moves = preflight_cache_moves(source, target)
    links_to_rebase = preflight_links(source, target, before['root_build'], cache_moves)
    pdf_sources = [('paper_zh/paper_zh.pdf', 'paper_zh.pdf'),
                   ('paper_en/paper_en.pdf', 'paper_en.pdf'),
                   ('paper_zh/figures/overall_framework.pdf', 'overall_framework.pdf')]
    for old, _ in pdf_sources:
        if not (source / old).is_file() or (source / old).is_symlink():
            raise ValueError(f'Expected a regular pre-migration PDF: {old}')
    summary = {'source': 'build', 'destination': 'IEEE_conference_template/build',
               'original_entries': len(before['root_build']),
               'old_manuscript_entries': len(before['old_manuscript_build'])}
    if not perform:
        return {'status': 'preview', **summary}
    audit.mkdir(parents=True, exist_ok=False)
    archive.mkdir(parents=True, exist_ok=False)
    write_new(audit / 'before.json', before)
    write_new(audit / 'intent.json', summary)
    pdfs = archive / 'pre-migration-pdfs'
    pdfs.mkdir()
    for old, name in pdf_sources:
        shutil.copy2(source / old, pdfs / name)
    _rename_no_replace(target, archive / 'previous-manuscript-build')
    if inventory(archive / 'previous-manuscript-build') != before['old_manuscript_build']:
        raise ValueError('Old manuscript build preservation mismatch')
    _rename_no_replace(source, target)
    if inventory(target) != before['root_build']:
        raise ValueError('Root build relocation content mismatch')
    # CMake caches embed the old absolute directory. Preserve rather than reuse
    # them; normal make native-* targets will create fresh caches at the new path.
    for move in cache_moves:
        old = target / Path(move['source']).relative_to('build')
        destination = root / move['destination']
        expected = inventory(old)
        destination.parent.mkdir(exist_ok=True)
        _rename_no_replace(old, destination)
        if inventory(destination) != expected:
            raise ValueError('Native cache preservation mismatch')
    # Only pytest convenience links refer inside the moved tree. Rebase them
    # after preserving their original link text; never touch their target data.
    links = []
    for planned in links_to_rebase:
        link = root / planned['path']
        new_target = root / planned['target_relative']
        if not new_target.exists() or not link.is_symlink() or os.readlink(link) != planned['before']:
            raise ValueError(f'Cannot safely rebase link: {planned["path"]}')
        link.unlink()
        link.symlink_to(planned['after'])
        if link.resolve(strict=True) != new_target:
            raise ValueError(f'Rebased link target mismatch: {planned["path"]}')
        links.append({key: planned[key] for key in ('path', 'before', 'after')})
    report = {'status': 'passed', **summary, 'all_payload_hashes_matched': True,
              'root_build_absent': not os.path.lexists(source),
              'native_cache_moves': cache_moves, 'rebased_pytest_links': links,
              'old_manuscript_build': str((archive / 'previous-manuscript-build').relative_to(root)),
              'pdf_backup': str(pdfs.relative_to(root)),
              'experiment_data_deleted': False}
    write_new(audit / 'receipt.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    print(json.dumps(execute(args.root, perform=args.execute), indent=2))
