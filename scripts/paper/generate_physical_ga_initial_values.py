#!/usr/bin/env python3
"""Derive independent pilot macros from the completed four-arm export.

This reads the frozen export and recomputes the common-circuit comparison from
all-run CSV rows. It never invokes a compiler or changes experiment evidence.
"""
from __future__ import annotations

from paper_paths import PAPER_ROOT, PROJECT_ROOT, artifact_path, tool_path, notes_path

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

PAPER = PAPER_ROOT
STUDY = PAPER.parent / 'ZAC_zzx/results/physical_ga_initial_v1/corrected_driver_v1'
EXPORT = STUDY / 'exports/final'
ARMS = ('old_sa4_h2', 'ga_h2', 'ga_h0', 'random_h2')
PAIRS = (('GaVsSa', 'old_sa4_h2'), ('GaVsHZero', 'ga_h0'), ('GaVsRandom', 'random_h2'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_verified():
    protocol_path = STUDY / 'protocol.json'
    protocol = json.loads(protocol_path.read_text())
    seal = json.loads((STUDY / 'protocol.sha256.json').read_text())
    require(digest(protocol_path) == seal['sha256'], 'protocol hash changed')
    require(protocol['protocol_id'] == 'physical-ga-initial-nine-family-four-arm-seed012-v1-driverfix1',
            'unexpected pilot protocol')
    require(tuple(protocol['arms']) == ARMS and protocol['seeds'] == [0, 1, 2], 'four-arm/seed contract changed')
    provenance = json.loads((EXPORT / 'provenance.json').read_text())
    for name, expected in provenance['files'].items():
        require(digest(EXPORT / name) == expected, 'export hash changed: ' + name)
    require(provenance['protocol']['sha256'] == seal['sha256'], 'export uses another protocol')
    execution = json.loads((EXPORT / 'execution_context.provenance.json').read_text())
    for name, expected in execution['files'].items():
        require(digest(EXPORT / name) == expected, 'execution addendum hash changed: ' + name)
    require(digest(EXPORT / 'provenance.json') == execution['frozen_export_provenance']['sha256'],
            'execution addendum uses another export')
    report = json.loads((EXPORT / 'result.json').read_text())
    with (EXPORT / 'runs.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    expected = {(j['input_sha256'], str(j['seed']), j['arm']) for j in protocol['jobs']}
    index = {(r['input_sha256'], r['seed'], r['arm']): r for r in rows}
    require(len(rows) == len(index) == len(expected) == 108 and set(index) == expected,
            'missing/duplicate planned run identity')
    require(not any(r['status'] in ('pending', 'interrupted') for r in rows), 'pilot is unfinished')
    units = []
    unit_ids = []
    for circuit in protocol['circuits']:
        per_arm = {arm: [index[circuit['input_sha256'], str(seed), arm] for seed in (0, 1, 2)]
                   for arm in ARMS}
        if not all(r['status'] == 'success' and r['quality_classification'] == 'valid_fidelity'
                   and r['log_fidelity'] and math.isfinite(float(r['log_fidelity']))
                   for items in per_arm.values() for r in items):
            continue
        unit_ids.append(circuit['input_sha256'])
        units.append({arm: statistics.median(float(r['log_fidelity']) for r in values)
                      for arm, values in per_arm.items()})
    require(len(units) == report['overall']['complete_circuits'], 'common-circuit count differs')
    search_arms = ARMS[1:]
    search_rows = [r for r in rows if r['arm'] in search_arms]
    search_ids = []
    for circuit in protocol['circuits']:
        subset = [index[circuit['input_sha256'], str(seed), arm]
                  for arm in search_arms for seed in (0, 1, 2)]
        if all(r['status'] == 'success' and r['quality_classification'] == 'valid_fidelity'
               and r['log_fidelity'] and math.isfinite(float(r['log_fidelity'])) for r in subset):
            search_ids.append(circuit['input_sha256'])
    require(set(search_ids) == set(unit_ids),
            'three-arm cohort changed; independent comparisons need recomputation')
    values = {'PlannedCircuitN': len(protocol['circuits']), 'CompleteN': len(units), 'RunN': len(rows),
              'SuccessN': sum(r['status'] == 'success' for r in rows),
              'TimeoutN': sum(r['status'] == 'timeout' for r in rows),
              'OtherFailureN': sum(r['status'] not in ('success', 'timeout') for r in rows),
              'OODN': sum(r['status'] == 'success' and r['quality_classification'] != 'valid_fidelity' for r in rows)}
    values.update({'SearchRunN': len(search_rows), 'SearchCompleteN': len(search_ids),
                   'SearchSuccessN': sum(r['status'] == 'success' for r in search_rows),
                   'SearchTimeoutN': sum(r['status'] == 'timeout' for r in search_rows)})
    for label, arm in zip(('SA', 'GaHTwo', 'GaHZero', 'Random'), ARMS):
        selected = [r for r in rows if r['arm'] == arm]
        values[label + 'TimeoutN'] = sum(r['status'] == 'timeout' for r in selected)
        values[label + 'SuccessN'] = sum(r['status'] == 'success' for r in selected)
        values[label + 'OtherFailureN'] = sum(r['status'] not in ('success', 'timeout') for r in selected)
    for label, control in PAIRS:
        differences = [r['ga_h2'] - r[control] for r in units]
        ratio = math.exp(statistics.fmean(differences)) if differences else None
        values[label + 'Ratio'] = ratio
        values[label + 'GainPercent'] = 100 * math.expm1(statistics.fmean(differences)) if differences else None
        for word, count in (('Wins', sum(d > 1e-12 for d in differences)),
                            ('Ties', sum(abs(d) <= 1e-12 for d in differences)),
                            ('Losses', sum(d < -1e-12 for d in differences))):
            values[label + word] = count
        if differences:
            actual = report['overall']['comparisons']['ga_h2_vs_' + control]
            require(actual['N'] == len(units) and math.isclose(ratio, actual['fidelity_ratio_geometric_mean'],
                    rel_tol=1e-12, abs_tol=1e-12), 'recomputed ratio differs: ' + label)
            require(actual['fidelity_win_tie_loss'] == dict(wins=values[label+'Wins'],
                    ties=values[label+'Ties'], losses=values[label+'Losses']), 'win/tie/loss differs: ' + label)
    return values, {'generator': {'path': str(Path(__file__).resolve().relative_to(PAPER.parent)), 'sha256': digest(Path(__file__))},
                    'protocol': {'path': str(protocol_path.relative_to(PAPER.parent)), 'sha256': seal['sha256']},
                    'source_files': {str(p.relative_to(PAPER.parent)): digest(p)
                        for p in (EXPORT/'result.json', EXPORT/'runs.csv', EXPORT/'circuits.csv', EXPORT/'provenance.json', EXPORT/'execution_context.json', EXPORT/'execution_context.provenance.json', EXPORT/'execution_addendum.md')},
                    'aggregation': report['aggregation'],
                    'three_arm_view': {'arms': list(search_arms), 'cohort_sha256': search_ids,
                        'same_cohort_as_original_four_arm': True,
                        'role': 'GA horizon and same-domain random comparisons; no run replaced or removed from the original export'},
                    'planned_timing_policy': report['timing_policy'],
                    'timing_policy': 'Actual serial/parallel mixed scheduling; wall-clock times descriptive only. The execution addendum supersedes the frozen report serial-plan wording.'}


def outputs():
    values, provenance = load_verified()
    lines = ['% Generated from the independent physical GA initialization pilot; main results unchanged.']
    for key, value in values.items():
        text = '--' if value is None else str(value) if isinstance(value, int) else f'{value:.2f}' if key.endswith('GainPercent') else f'{value:.4f}'
        lines.append('\\newcommand{\\PhysicalInit' + key + '}{' + text + '}')
    return {'physical_ga_initial_values.tex': '\n'.join(lines)+'\n',
            'paper_physical_ga_initial_values.json': json.dumps(values, indent=2, sort_keys=True)+'\n',
            'paper_physical_ga_initial_provenance.json': json.dumps(provenance, indent=2, sort_keys=True)+'\n'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for name, content in outputs().items():
        path = artifact_path(PAPER, name)
        if args.check:
            require(path.is_file() and path.read_text() == content, 'derived file differs: ' + name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    print(json.dumps({'status': 'pass', 'mode': 'check' if args.check else 'generate'}))


if __name__ == '__main__':
    main()
