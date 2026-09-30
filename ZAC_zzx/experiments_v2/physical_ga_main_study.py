"""Independent GA-initialized main matrix: 507 identities, 27 pinned pilot outcomes.

Only --run starts the 480 unexecuted identities. --seal freezes the reviewed
source snapshot; no accepted main evidence or existing initializer default is changed.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import fcntl
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from experiments_v2 import physical_ga_initial_study as core

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / 'ZAC_zzx/results/physical_ga_main_v1'
BUILD = REPO / 'IEEE_conference_template/build/physical_ga_main_v1'
DEFAULT = REPO / 'ZAC_zzx/results/default_initial_v1/protocol.json'
DEFAULT_SHA = '56bf06493df746e7efa6f0ddec805dc6e88086fa000afb673634430ef274071d'
PILOT = REPO / 'ZAC_zzx/results/physical_ga_initial_v1/corrected_driver_v1/protocol.json'
PILOT_SHA = '634fb00b81921a7f81494cbeaaf4749cf9fe3bcfffdbd34f6488c496ea0244d7'
PROTOCOL_ID = 'physical-ga-main-canonical169-seed012-v1'
ORIGINAL_PILOT_LOAD = core.load_protocol
STOP = threading.Event()


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def build_plan():
    if core.digest(DEFAULT) != DEFAULT_SHA or core.digest(PILOT) != PILOT_SHA:
        raise ValueError('source protocol changed')
    default, pilot = core.read(DEFAULT), ORIGINAL_PILOT_LOAD(PILOT)
    baseline_source = REPO / 'scripts/paper/generate_default_initial_values.py'
    sys.path.insert(0, str(baseline_source.parent))
    try:
        baseline_module = module(baseline_source, 'main_baseline_verification')
    finally:
        sys.path.remove(str(baseline_source.parent))
    baseline_rows, inventory, baseline_provenance = baseline_module.load_accepted_baselines()
    if len(baseline_rows) != 344:
        raise ValueError('accepted baseline matrix changed')
    if (pilot['architecture']['sha256'] != baseline_provenance['accepted_architecture_sha256']
            or pilot['model']['sha256'] != baseline_provenance['accepted_model_sha256']):
        raise ValueError('GA and baseline hardware/model differ')
    if core.dynamic_contract(default['dynamic_setting']) != core.dynamic_contract(pilot['dynamic_setting']):
        raise ValueError('default and pilot dynamic controls differ')
    for relative, ref in pilot['source_files'].items():
        if relative.split('/')[0] in ('zzx', 'zac', 'streaming', 'evaluation'):
            if core.digest(REPO / 'ZAC_zzx' / relative) != ref['sha256']:
                raise ValueError('pilot computational source drift: ' + relative)
    selected_pilot = {j['job_id']: j for j in pilot['jobs'] if j['arm'] == 'ga_h2'}
    pilot_subset = {**pilot, 'jobs': list(selected_pilot.values())}
    pilot_rows, reuse_evidence = core.collect(PILOT, pilot_subset, verify_trace=False)
    if Counter(r['status'] for r in pilot_rows) != {'success': 24, 'timeout': 3}:
        raise ValueError('preserve all 27 pilot outcomes, including its three timeouts')
    pilot_index = {r['job_id']: r for r in pilot_rows}
    circuits, jobs = {}, []
    for original in default['jobs']:
        sha, seed = original['canonical_sha256'], original['seed']
        if core.digest(original['input']['path']) != sha:
            raise ValueError('canonical input changed')
        for label in original['labels']:
            declared = inventory[label['dataset']][label['circuit']]
            if any(declared[k] != label[k] for k in declared):
                raise ValueError('baseline/input label identity differs')
        circuits.setdefault(sha, {'dataset': original['dataset'], 'circuit': original['circuit'],
            'input_sha256': sha, 'input': original['input']['path'], 'labels': original['labels'],
            **{k: original[k] for k in ('qubits', 'gates_1q', 'gates_2q')}})
        job_id = f'{sha[:16]}-s{seed}-ga_h2'
        old = pilot_index.get(job_id)
        if old and (old['input_sha256'], old['seed'], old['arm']) != (sha, seed, 'ga_h2'):
            raise ValueError('pilot identity mismatch')
        jobs.append({'job_id': job_id, 'input_sha256': sha, 'seed': seed, 'arm': 'ga_h2',
            'dataset': original['dataset'], 'circuit': original['circuit'],
            'origin': 'reused' if old else 'fresh',
            'reuse_receipt': core.pin(PILOT.parent / 'receipts' / (job_id + '.json')) if old else None})
    if (len(circuits), len(jobs), Counter(j['origin'] for j in jobs)) != (169, 507, {'reused': 27, 'fresh': 480}):
        raise ValueError('main canonical matrix changed')
    setting = deepcopy(pilot['dynamic_setting'])
    from zzx.zac_zzx import ZAC_zzx
    ZAC_zzx().parse_setting(core.arm_setting(setting, 'ga_h2', 0))
    native = core.runtime_info(setting['native_wheel_sha256'])
    if native['extension_sha256'] != pilot['native']['extension_sha256']:
        raise ValueError('native implementation changed')
    source = core.source_files()
    git = lambda *args: subprocess.check_output(['git', *args], cwd=REPO, text=True)
    return {'protocol_id': PROTOCOL_ID, 'created_at_utc': core.now(), 'workspace': str(REPO),
        'study_role': 'independent full GA-initialized main evidence; no automatic promotion or replacement of accepted packages',
        'formal_paper_result': False, 'source_default_protocol': core.pin(DEFAULT),
        'source_pilot_protocol': core.pin(PILOT), 'baseline_loader': core.pin(baseline_source),
        'baseline_provenance': baseline_provenance, 'reuse_evidence': reuse_evidence,
        'circuits': list(circuits.values()), 'jobs': jobs, 'canonical_count': 169, 'display_count': 172,
        'arms': ['ga_h2'], 'seeds': [0, 1, 2], 'fresh_jobs': 480, 'reused_jobs': 27,
        'dynamic_setting': setting, 'dynamic_contract_sha256': core.stable(core.dynamic_contract(setting)),
        'initial_ga': deepcopy(core.GA_CONFIG), 'arm_setting_seed0': core.arm_setting(setting, 'ga_h2', 0),
        'architecture': pilot['architecture'], 'model': pilot['model'], 'native': native,
        'python': pilot['python'], 'environment': core.ENV, 'max_workers': 14,
        'timeout_s': 600, 'rss_limit_bytes': 3 * 1024**3,
        'aggregation': default['aggregation'],
        'baseline_policy': 'Reuse all 344 pinned original emitted baseline outcomes; retain their original ghost/OOD/failure policy. Never apply the new compiler ghost=0 filter to historical baselines.',
        'reuse_policy': 'Reuse all 27 identical pilot GA-H2 circuit/seed outcomes: 24 successes and three timeouts. Preserve their source identities and scheduling context; do not retry failures or relabel their timing as fresh.',
        'failure_policy': 'No retries, substitution, favorable-only reuse or automatic parameter changes. OOD is separate from program/validation failures. Explicit user pause saves interrupted receipts and all completed evidence.',
        'timing_policy': 'Fourteen-worker parallel quality execution; wall-clock time is descriptive only, not serial runtime evidence. Reused pilot timing belongs to its original mixed scheduling protocol.',
        'rng_semantics': pilot['rng_semantics'], 'budget_semantics': pilot['budget_semantics'],
        'source_files': source, 'source_snapshot_sha256': core.stable({k: v['sha256'] for k, v in source.items()}),
        'repository': {'commit': git('rev-parse', 'HEAD').strip(),
            'status': git('status', '--porcelain=v1', '--untracked-files=all', '--', 'ZAC_zzx'),
            'tracked_diff': git('diff', '--binary', '--no-ext-diff', 'HEAD', '--', 'ZAC_zzx')}}


def seal():
    if ROOT.exists() or BUILD.exists():
        raise FileExistsError('new study root or build already exists')
    p = build_plan()
    frozen = BUILD / 'frozen_source/ZAC_zzx'
    for relative, ref in p['source_files'].items():
        if core.digest(ref['path']) != ref['sha256']:
            raise ValueError('source changed while sealing')
        target = frozen / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref['path'], target)
    p['frozen_source'] = str(frozen)
    p['driver'] = core.pin(frozen / 'experiments_v2/physical_ga_main_study.py')
    for c in p['circuits']:
        target = ROOT / 'freeze/inputs' / (c['input_sha256'] + '.qasm')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(c['input'], target)
        if core.digest(target) != c['input_sha256']:
            raise ValueError('input changed while sealing')
        c['original_input'], c['input'] = c['input'], str(target)
    core.write_new(ROOT / 'protocol.json', p)
    core.write_new(ROOT / 'protocol.sha256.json', core.pin(ROOT / 'protocol.json'))
    for j in p['jobs']:
        if j['origin'] == 'reused':
            old = core.checked(j['reuse_receipt'])
            core.write_new(ROOT / 'reuse_receipts' / (j['job_id'] + '.json'), {
                **j, 'status': old['status'], 'source_receipt': j['reuse_receipt'],
                'protocol_sha256': core.digest(ROOT / 'protocol.json'),
                'timing_is_fresh': False, 'source_protocol': p['source_pilot_protocol']})
    return {'protocol': core.pin(ROOT / 'protocol.json'), 'fresh_jobs': 480, 'reused_jobs': 27}


def load_protocol(path):
    path = Path(path)
    ref = core.read(path.with_name('protocol.sha256.json'))
    if Path(ref['path']).resolve() != path.resolve():
        raise ValueError('protocol sidecar mismatch')
    p = core.checked(ref)
    if (p['protocol_id'] != PROTOCOL_ID or len(p['jobs']) != 507 or len(p['circuits']) != 169
            or p['arms'] != ['ga_h2'] or p['seeds'] != [0, 1, 2] or p['max_workers'] != 14
            or p['timeout_s'] != 600 or p['rss_limit_bytes'] != 3 * 1024**3):
        raise ValueError('frozen main contract differs')
    if len({(j['input_sha256'], j['seed']) for j in p['jobs']}) != 507:
        raise ValueError('duplicate main identity')
    for key in ('source_default_protocol', 'source_pilot_protocol', 'architecture', 'model'):
        core.checked(p[key])
    for key in ('driver', 'python'):
        if core.digest(p[key]['path']) != p[key]['sha256']:
            raise ValueError('frozen runtime changed: ' + key)
    for relative, ref in p['source_files'].items():
        if core.digest(Path(p['frozen_source']) / relative) != ref['sha256']:
            raise ValueError('frozen source changed: ' + relative)
    for c in p['circuits']:
        if core.digest(c['input']) != c['input_sha256']:
            raise ValueError('frozen input changed')
    if core.stable(core.dynamic_contract(p['dynamic_setting'])) != p['dynamic_contract_sha256']:
        raise ValueError('dynamic setting changed')
    return p


def monitor(process, timeout_s, rss_limit_bytes):
    start, peak, status = time.monotonic(), 0, None
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - start
            peak = max(peak, core.resident_bytes(process.pid))
            status = ('interrupted' if STOP.is_set() else 'timeout' if elapsed >= timeout_s
                      else 'memory_limit' if peak > rss_limit_bytes else None)
            if status:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                break
            try:
                process.wait(timeout=min(1., max(.01, timeout_s - elapsed)))
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise
    return {'limit_status': status, 'peak_observed_rss_bytes': peak,
            'elapsed_s': time.monotonic() - start, 'returncode': process.returncode}


def execute(protocol_path):
    p = load_protocol(protocol_path)
    root = Path(protocol_path).parent
    def requested_stop(signum, frame):
        if not STOP.is_set():
            core.write_new(root / ('pause-' + str(time.time_ns()) + '.json'),
                           {'at_utc': core.now(), 'signal': signum, 'policy': 'stop unstarted jobs; preserve interrupted and terminal evidence'})
        STOP.set()
    signal.signal(signal.SIGINT, requested_stop)
    signal.signal(signal.SIGTERM, requested_stop)
    core.monitor = monitor
    def perform(job):
        if STOP.is_set():
            return None
        row = core.run_one(protocol_path, p, job)
        print(json.dumps({'job_id': job['job_id'], 'status': row['status'],
                          'elapsed_s': row.get('elapsed_s')}), flush=True)
        return row
    with (root / '.execution.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        core.write_new(root / 'execution.json', {'pid': os.getpid(), 'started_at_utc': core.now(),
            'max_workers': 14, 'protocol': core.pin(protocol_path), 'fresh_planned': 480, 'reused_fixed': 27})
        with ThreadPoolExecutor(max_workers=14) as pool:
            results = list(pool.map(perform, [j for j in p['jobs'] if j['origin'] == 'fresh']))
        rows = [r for r in results if r is not None]
        report = {'completed_at_utc': core.now(), 'paused': STOP.is_set(), 'fresh_terminal': len(rows),
                  'fresh_statuses': dict(Counter(r['status'] for r in rows)),
                  'reused_statuses': {'success': 24, 'timeout': 3}, 'protocol': core.pin(protocol_path)}
        core.write_new(root / 'execution_completion.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('preflight', 'seal', 'run'):
        modes.add_argument('--' + name, action='store_true')
    modes.add_argument('--worker')
    parser.add_argument('--protocol', type=Path, default=ROOT / 'protocol.json')
    args = parser.parse_args(argv)
    if args.worker:
        core.load_protocol = load_protocol
        return core.worker(args.protocol, args.worker)
    if args.preflight:
        p = build_plan()
        answer = {'status': 'pass', 'canonical': 169, 'planned': 507, 'fresh': 480, 'reused': 27,
                  'source_snapshot_sha256': p['source_snapshot_sha256']}
    elif args.seal:
        answer = seal()
    else:
        core.load_protocol = load_protocol
        answer = execute(args.protocol)
    print(json.dumps(answer, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
