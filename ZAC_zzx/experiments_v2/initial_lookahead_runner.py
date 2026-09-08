"""Small paired initialization-lookahead runs, separate from frozen protocols.

Example (from the repository root, with a matching registered ABI9 runtime)::

  PYTHONPATH=ZAC_zzx python -m experiments_v2.initial_lookahead_runner \
    --input /path/to/canonical.qasm --config /path/to/ablation/h8-seed0.json \
    --root IEEE_conference_template/build/initial-lookahead/smoke --horizons 0 2

The config must be the validated existing paper_h8_ga wrapper. Its dynamic
GA-LK settings are held fixed. SA is run once, before either horizon is scored.
Dirty source trees are recorded by content hashes rather than cleaned/committed.
This is a bounded research/smoke runner, not the accepted paper experiment.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import re
import resource
import subprocess
import sys
import time

from evaluation import normalize_zair, score_trace, validate_trace_physics
from experiments_v2.ablation import validate_ablation_config
from experiments_v2.runtime_benchmark import ordered_two_qubit_layer_ledger, two_qubit_layer_ledger
from zac.ds.architecture import Architecture
from zzx.initial_lookahead import (InitialLookaheadConfig, POLICY_ID,
                                  select_initial_mapping, stable_hash)
from zzx.native_backend import build_info
from zzx.zac_zzx import ZAC_zzx


REPO = Path(__file__).resolve().parents[2]


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_snapshot():
    scope = ("ZAC_zzx/zzx", "ZAC_zzx/zac", "ZAC_zzx/streaming",
             "ZAC_zzx/evaluation", "ZAC_zzx/experiments_v2")
    paths = subprocess.run(
        ["git", "ls-files", "-z", "--", *scope], cwd=REPO,
        check=True, capture_output=True).stdout.decode().split("\0")
    # Include new in-scope Python modules and tests before they are committed.
    # Generated result directories are deliberately outside this source scope.
    paths += [str(path.relative_to(REPO)) for directory in (*scope, "ZAC_zzx/tests")
              for path in (REPO / directory).rglob("*.py")]
    hashes = {path: file_hash(REPO / path) for path in sorted(set(paths))
              if path and (REPO / path).is_file()}
    status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=REPO,
                            check=True, capture_output=True, text=True).stdout
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                            check=True, capture_output=True, text=True).stdout.strip()
    return {"snapshot_schema": 2, "commit": commit, "dirty": bool(status), "status": status,
            "untracked_python_included": True, "python_scope": [*scope, "ZAC_zzx/tests"],
            "source_files": hashes, "source_sha256": stable_hash(hashes)}


def write_json(path, payload):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def peak_rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def stage_receipt(root, stage):
    write_json(root / f"stage_started_{stage}.json", {
        "stage": stage, "monotonic_ns": time.perf_counter_ns(),
        "process_cpu_ns": time.process_time_ns(), "peak_rss_bytes": peak_rss_bytes()})


def logical_trace_receipt(qasm, n_qubits, events, compiled_layers):
    """Check per-atom operation order, not only the two aggregate gate counts."""
    expected, observed, pairs, event_layers = {}, {}, [], []
    single = re.compile(r"^(?:u1|u2|u3)\s*\([^;]*\)\s+q\[(\d+)\]\s*;$")
    double = re.compile(r"^cz\s+q\[(\d+)\]\s*,\s*q\[(\d+)\]\s*;$")
    for raw in Path(qasm).read_text().splitlines():
        line = raw.split("//", 1)[0].strip()
        one, two = single.match(line), double.match(line)
        if one:
            expected.setdefault(int(one[1]), []).append("1q")
        elif two:
            q0, q1 = int(two[1]), int(two[2]); pairs.append((q0, q1))
            expected.setdefault(q0, []).append(f"cz:{q1}")
            expected.setdefault(q1, []).append(f"cz:{q0}")
    for event in events:
        if event.kind == "one_qubit_gate":
            for q in event.atoms:
                observed.setdefault(q, []).append("1q")
        elif event.kind == "two_qubit_gate":
            event_layers.append(event.gate_pairs)
            for q0, q1 in event.gate_pairs:
                observed.setdefault(q0, []).append(f"cz:{q1}")
                observed.setdefault(q1, []).append(f"cz:{q0}")
    if expected != observed:
        raise ValueError("canonical/native per-atom operation-sequence mismatch")
    actual = ordered_two_qubit_layer_ledger(n_qubits, event_layers)
    scheduled = ordered_two_qubit_layer_ledger(n_qubits, compiled_layers)
    if actual != scheduled:
        raise ValueError("native/compiled transition-layer ledger mismatch")
    canonical = two_qubit_layer_ledger(n_qubits, pairs)
    return {"ok": True, "expected_gate_ledger_sha256": stable_hash(expected),
            "observed_gate_ledger_sha256": stable_hash(observed),
            "canonical_layer_ledger_sha256": canonical["layer_ledger_sha256"],
            "compiled_layer_ledger_sha256": scheduled["layer_ledger_sha256"],
            "observed_layer_ledger_sha256": actual["layer_ledger_sha256"],
            "canonical_and_compiled_layers_equal": canonical == scheduled}


def prepare_compiler(setting, spec, qasm, architecture_path, *, initial_mapping=None):
    compiler = ZAC_zzx()
    compiler._paper_ablation_contract = {"native_abi_version": 9, "max_horizon": 8}
    compiler.parse_setting(deepcopy(setting))
    architecture = Architecture(deepcopy(spec))
    architecture.preprocessing()
    compiler.set_architecture_spec_path(str(architecture_path))
    compiler.set_architecture(architecture)
    compiler.set_program(str(qasm))
    if initial_mapping is not None:
        compiler.set_initial_mapping(deepcopy(initial_mapping))
    return compiler


def run(args):
    root = Path(args.root).resolve()
    allowed = [REPO / "IEEE_conference_template/build/initial-lookahead",
               REPO / "ZAC_zzx/results/initial_lookahead_v1"]
    if not any(root == parent or parent in root.parents for parent in allowed):
        raise ValueError("output must be beneath IEEE_conference_template/build/initial-lookahead "
                         "or ZAC_zzx/results/initial_lookahead_v1")
    if len(set(args.horizons)) != len(args.horizons) or 0 not in args.horizons:
        raise ValueError("paired horizons must be unique and include H_init=0")
    configs = [InitialLookaheadConfig(horizon=h, candidates=args.candidates, seed=args.seed,
                                      rho=args.rho, rollout_evaluations=args.rollout_evaluations)
               for h in args.horizons]
    qasm, config_path, arch_path = (Path(value).resolve() for value in
                                   (args.input, args.config, args.architecture))
    payload = json.loads(config_path.read_text())
    base, variant = validate_ablation_config(payload, expected_variant="paper_h8_ga", expected_method="M4")
    if variant.protocol_version != 2:
        raise ValueError("this runner requires the registered ABI9 paper_h8_ga wrapper")
    setting = deepcopy(base["zac_setting"][0])
    if setting.get("init_engine", "sa") != "sa":
        raise ValueError("paired candidate pool requires the original SA initializer")
    setting.update(seed=args.seed, name=qasm.stem, dir=str(root) + "/",
                   arch_spec=str(arch_path), use_verifier=False, resyn=False)
    setting["init_strategy"] = "legacy"  # This runner selects candidates itself, after one SA run.
    setting.pop("initial_lookahead", None)
    native = build_info(require_registered_wheel=True,
                        expected_wheel_sha256=setting["native_wheel_sha256"])
    spec = json.loads(arch_path.read_text())
    before = source_snapshot()
    root.mkdir(parents=True, exist_ok=False)
    common = {"runner_schema": 2, "policy_id": POLICY_ID, "formal_paper_result": False,
              "input": str(qasm), "input_sha256": file_hash(qasm),
              "config_path": str(config_path), "config_sha256": file_hash(config_path),
              "architecture": str(arch_path), "architecture_sha256": file_hash(arch_path),
              "native": native, "repository": before, "dynamic_setting": setting,
              "initial_configs": [asdict(config) for config in configs],
              "python": sys.executable, "python_version": sys.version,
              "full_compile_variants": ["sa_reference"] + [f"h{h}" for h in args.horizons],
              "selection_uses_full_compile_results": False}
    write_json(root / "protocol.json", common)
    results = []
    try:
        with (root / "compiler.log").open("x", encoding="utf-8") as log, redirect_stdout(log):
            base_compiler = prepare_compiler(setting, spec, qasm, arch_path)
            base_compiler.scheduling()
            base_compiler.qubit_mapping = []
            stage_receipt(root, "sa_initialization")
            sa_started = time.perf_counter_ns()
            sa_cpu_started = time.process_time_ns()
            if base_compiler.gate_scheduling:
                base_compiler.place_qubit_initial()
            else:
                base_compiler.place_trivial()
            sa_ns = time.perf_counter_ns() - sa_started
            sa_cpu_ns = time.process_time_ns() - sa_cpu_started
            base_mapping = deepcopy(base_compiler.qubit_mapping[0])
            rng_after_sa = random.getstate()
            write_json(root / "base_mapping.json", {
                "source": "original_sa" if base_compiler.gate_scheduling else "no_2q_trivial",
                "mapping": base_mapping, "mapping_sha256": stable_hash(base_mapping),
                "sa_initialization_ns": sa_ns, "sa_runs": int(bool(base_compiler.gate_scheduling)),
                "sa_initialization_cpu_ns": sa_cpu_ns,
                "total_layers": len(base_compiler.gate_scheduling)})
            pool_hash = None
            selections = []
            for config in configs:
                stage_receipt(root, f"h{config.horizon}_selection")
                random.setstate(rng_after_sa)
                selection_cpu_started = time.process_time_ns()
                selected, selection = select_initial_mapping(
                    base_compiler.architecture, base_mapping, base_compiler.gate_scheduling,
                    one_qubit=base_compiler.gate_1q_scheduling,
                    leading_one_qubit=base_compiler.dict_g_1q_parent.get(-1, ()),
                    params=base_compiler.zzx_params, config=config)
                selection["selection_cpu_ns"] = time.process_time_ns() - selection_cpu_started
                if pool_hash is not None and selection["candidate_pool_sha256"] != pool_hash:
                    raise AssertionError("H_init arms do not share the candidate pool")
                pool_hash = selection["candidate_pool_sha256"]
                write_json(root / f"h{config.horizon}_selection.json", selection)
                selections.append((f"h{config.horizon}", config.horizon, selected, selection))
            # Both choices are sealed before any full-circuit result exists.
            selections.insert(0, ("sa_reference", None, base_mapping, {
                "selected_candidate": 0, "selected_mapping_sha256": stable_hash(base_mapping),
                "selection_ns": 0, "selection_cpu_ns": 0}))
            write_json(root / "sealed_choices.json", {
                "candidate_pool_sha256": pool_hash,
                "selection_uses_full_compile_results": False,
                "choices": [{"variant": label, "horizon": horizon,
                             "mapping_sha256": selection["selected_mapping_sha256"],
                             "candidate": selection["selected_candidate"]}
                            for label, horizon, _, selection in selections]})
            for label, horizon, selected, selection in selections:
                stage_receipt(root, f"{label}_compile")
                # The final compiler receives only a mapping, not preview state.
                random.setstate(rng_after_sa)
                compiler = prepare_compiler(setting, spec, qasm, arch_path, initial_mapping=selected)
                started = time.perf_counter_ns()
                cpu_started = time.process_time_ns()
                native_trace = compiler.solve(save_file=False)
                compile_ns = time.perf_counter_ns() - started
                compile_cpu_ns = time.process_time_ns() - cpu_started
                events = tuple(normalize_zair(native_trace, architecture=spec))
                validation = validate_trace_physics(events, n_qubits=compiler.n_q)
                logical = logical_trace_receipt(qasm, compiler.n_q, events, compiler.gate_scheduling)
                score = score_trace(events, n_qubits=compiler.n_q)
                result = {"variant": label, "horizon": horizon, "status": "success",
                          "n_qubits": compiler.n_q, "total_layers": len(compiler.gate_scheduling),
                          "candidate_pool_sha256": pool_hash,
                          "selected_candidate": selection["selected_candidate"],
                          "selected_mapping_sha256": selection["selected_mapping_sha256"],
                          "dynamic_setting_sha256": stable_hash(setting),
                          "sa_initialization_ns": sa_ns, "selection_ns": selection["selection_ns"],
                          "compile_with_selected_mapping_ns": compile_ns,
                          "compile_with_selected_mapping_cpu_ns": compile_cpu_ns,
                          "sa_initialization_cpu_ns": sa_cpu_ns,
                          "selection_cpu_ns": selection["selection_cpu_ns"],
                          "end_to_end_ns": sa_ns + selection["selection_ns"] + compile_ns,
                          "end_to_end_cpu_ns": sa_cpu_ns + selection["selection_cpu_ns"] + compile_cpu_ns,
                          "peak_rss_bytes": peak_rss_bytes(),
                          "validation": validation, "score": score.to_dict(),
                          "logical_validation": logical,
                          "native_instruction_sha256": stable_hash(native_trace["instructions"])}
                write_json(root / f"{label}_native.json", native_trace)
                write_json(root / f"{label}_result.json", result)
                results.append(result)
        after = source_snapshot()
        if after["source_sha256"] != before["source_sha256"]:
            raise RuntimeError("compiler source changed during the paired run; results are not accepted")
        report = {"status": "success", "formal_paper_result": False,
                  "source_stable": True, "results": results, "root": str(root),
                  "peak_rss_bytes": peak_rss_bytes(), "process_cpu_ns": time.process_time_ns()}
        write_json(root / "summary.json", report)
        return report
    except Exception as error:
        failure = {"status": "failed", "error": f"{type(error).__name__}: {error}",
                   "completed_variants": [result["variant"] for result in results]}
        if hasattr(error, "initial_lookahead_report"):
            failure["selection"] = error.initial_lookahead_report
        write_json(root / "failure.json", failure)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--architecture", default=str(REPO / "ZAC_zzx/hardware_spec/full_architecture.json"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--horizons", nargs="+", type=int, default=[0, 2])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--candidates", type=int, default=4)
    parser.add_argument("--rho", type=float, default=.7)
    parser.add_argument("--rollout-evaluations", type=int, default=32)
    args = parser.parse_args(argv)
    report = run(args)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
