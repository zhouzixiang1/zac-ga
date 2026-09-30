"""Frozen, append-only four-arm initialization pilot; never replaces main results.

Use the registered ABI9 Python with PYTHONPATH=ZAC_zzx.  --preflight reads
identities and the runtime without compiling any circuit.  --seal freezes the
reviewed dirty-worktree sources.  Only --run starts the 108 serial arm jobs.
--analyze creates a new immutable export; --check revalidates an existing one.
"""
from __future__ import annotations

import sys
from pathlib import Path
# A direct worker invocation must not shadow stdlib statistics with our sibling.
sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]

import argparse
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import csv
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import time
import traceback

REPO = Path(__file__).resolve().parents[2]
ORIGINAL_ROOT = REPO / "ZAC_zzx/results/physical_ga_initial_v1"
ROOT = ORIGINAL_ROOT / "corrected_driver_v1"
BUILD = REPO / "IEEE_conference_template/build/physical_ga_initial_v1/corrected_driver_v1"
PILOT = REPO / ("ZAC_zzx/results/initial_lookahead_v1/"
                "nine-family-h0-h2-sa-k4-rho07-b32-seed012-v1/protocol.json")
PILOT_SHA = "3f99d0207dcbf87ea44b12810bfeac0f5272f544013d72dffa9802a7342f8f69"
PROTOCOL_ID = "physical-ga-initial-nine-family-four-arm-seed012-v1-driverfix1"
ARMS = ("old_sa4_h2", "ga_h2", "ga_h0", "random_h2")
SEEDS = (0, 1, 2)
GA_CONFIG = {"population_size": 8, "elite_count": 2, "crossover_probability": .25,
             "swap_probability": .4, "insert_probability": .4, "reverse_probability": .2,
             "max_unique_evaluations": 32, "max_generations": 6,
             "early_stop_patience": 3, "max_proposals": 320,
             "horizon": 2, "rho": .7, "rollout_evaluations": 32}
ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1",
       "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}
SOURCE_DIRS = ("zzx", "zac", "streaming", "evaluation", "experiments_v2", "tests")
METRICS = ("log_fidelity", "move_batches", "move_time_us", "end_to_end_s",
           "initialization_s", "selection_s", "sa_s")
PAIRS = (("ga_h2", "old_sa4_h2"), ("ga_h2", "ga_h0"), ("ga_h2", "random_h2"))


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stable(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def pin(path):
    return {"path": str(Path(path).resolve()), "sha256": digest(path)}


def checked(ref):
    if digest(ref["path"]) != ref["sha256"]:
        raise ValueError("pinned evidence changed: " + ref["path"])
    return read(ref["path"])


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = value if isinstance(value, str) else json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(content)


def arm_setting(base, arm, seed):
    if arm not in ARMS or seed not in SEEDS:
        raise ValueError("unregistered arm or seed")
    setting = deepcopy(base)
    for key in ("init_strategy", "initial_lookahead", "initial_ga"):
        setting.pop(key, None)
    setting.update(seed=seed, resyn=False, use_verifier=False)
    if arm == "old_sa4_h2":
        setting.update(init_engine="sa", init_strategy="physical_prefix",
                       initial_lookahead={"horizon": 2, "candidates": 4, "rho": .7,
                                          "rollout_evaluations": 32})
    else:
        setting.pop("init_pop", None)
        setting.pop("init_gens", None)
        setting.update(init_engine="ga", init_strategy="physical_prefix_ga",
                       initial_ga={**GA_CONFIG, "seed": seed,
                                   "horizon": 0 if arm == "ga_h0" else 2,
                                   "search_policy": "random" if arm == "random_h2" else "ga"})
    return setting


def dynamic_contract(setting):
    ignored = {"seed", "name", "dir", "arch_spec", "init_engine", "init_strategy", "initial_lookahead", "initial_ga", "init_pop", "init_gens"}
    return {key: value for key, value in setting.items() if key not in ignored}


def job_order(circuits):
    # Rotate arms within each seed to avoid one policy always running first.
    jobs = []
    for ci, circuit in enumerate(circuits):
        for seed in SEEDS:
            offset = (ci + seed) % len(ARMS)
            for arm in ARMS[offset:] + ARMS[:offset]:
                jobs.append({"job_id": f"{circuit['input_sha256'][:16]}-s{seed}-{arm}",
                             "circuit": circuit["circuit"], "dataset": circuit["dataset"],
                             "input_sha256": circuit["input_sha256"], "seed": seed, "arm": arm})
    return jobs


def runtime_info(expected_wheel):
    from zzx.native_backend import build_info
    native = build_info(require_registered_wheel=True, expected_wheel_sha256=expected_wheel)
    if native["native_abi_version"] != 9 or native.get("openmp") or native.get("fast_math"):
        raise ValueError("requires registered serial ABI9 without fast math")
    return native


def source_files():
    root = REPO / "ZAC_zzx"
    paths = set()
    for name in SOURCE_DIRS:
        paths.update(p for p in (root / name).rglob("*") if p.is_file()
                     and p.suffix in (".py", ".json") and "__pycache__" not in p.parts)
    paths.add(root / "verify_batches.py")
    for name in ("native/src", "native/include"):
        paths.update(p for p in (root / name).rglob("*") if p.is_file() and p.suffix in (".cpp", ".h", ".hpp"))
    return {str(p.relative_to(root)): pin(p) for p in sorted(paths)}


def correction_provenance():
    original = checked(read(ORIGINAL_ROOT / "protocol.sha256.json"))
    receipts = sorted((ORIGINAL_ROOT / "receipts").glob("*.json"))
    failures = sorted((ORIGINAL_ROOT / "jobs").glob("*/failure.json"))
    return {"superseded_protocol": pin(ORIGINAL_ROOT / "protocol.json"),
            "superseded_protocol_id": original["protocol_id"],
            "preserved_records": [pin(path) for path in receipts + failures],
            "reason": "The original driver compared an in-memory tuple-containing selection report directly with its JSON list representation, misclassifying completed compilations as program_error.",
            "behavioral_fix": "Compare stable JSON hashes on both sides of the existing unchanged-selection guard; no selector, dynamic solver, parameter, cohort, seed, order or budget change.",
            "restart_policy": "Restart the entire prespecified 108-job matrix under a distinct corrected driver protocol, not a selective outcome retry. Preserve every original receipt, failure and interrupted claim; no original outcome enters corrected statistics."}


def build_plan():
    if digest(PILOT) != PILOT_SHA:
        raise ValueError("the prespecified nine-family cohort protocol changed")
    pilot = read(PILOT)
    circuits = deepcopy(pilot["circuits"])
    if len(circuits) != 9 or len({c["input_sha256"] for c in circuits}) != 9:
        raise ValueError("expected nine distinct canonical circuits")
    for c in circuits:
        if digest(c["input"]) != c["input_sha256"]:
            raise ValueError("canonical input changed: " + c["circuit"])
    config = pin(pilot["config_path"])
    if config["sha256"] != pilot["config_sha256"]:
        raise ValueError("historical H8 dynamic configuration changed")
    base = checked(config)["base_config"]["zac_setting"][0]
    if base["lookahead_horizon"]["max_horizon"] != 8:
        raise ValueError("dynamic H8 is required")
    base.update(resyn=False, use_verifier=False)
    native = runtime_info(base["native_wheel_sha256"])
    from zzx.zac_zzx import ZAC_zzx
    for arm in ARMS:
        compiler = ZAC_zzx()
        compiler.parse_setting(arm_setting(base, arm, 0))
    architecture = pin(pilot["architecture_path"])
    if architecture["sha256"] != pilot["architecture_sha256"]:
        raise ValueError("architecture changed")
    source = source_files()
    git = lambda *args: subprocess.check_output(["git", *args], cwd=REPO, text=True)
    return {"protocol_id": PROTOCOL_ID, "created_at_utc": now(), "workspace": str(REPO),
            "driver_correction": correction_provenance(),
            "study_role": "independent initialization pilot; not a replacement for accepted main results",
            "formal_paper_result": False, "cohort_source": pin(PILOT), "circuits": circuits,
            "arms": list(ARMS), "seeds": list(SEEDS), "jobs": job_order(circuits),
            "dynamic_setting": base, "dynamic_contract_sha256": stable(dynamic_contract(base)),
            "arm_settings": {arm: arm_setting(base, arm, 0) for arm in ARMS},
            "config": config, "architecture": architecture,
            "model": pin(REPO / "ZAC_zzx/evaluation/fidelity_model_zac.json"),
            "native": native, "python": {"path": sys.executable, "realpath": str(Path(sys.executable).resolve()),
                "sha256": digest(sys.executable)}, "python_version": sys.version,
            "environment": ENV, "max_workers": 1, "timeout_s": 600,
            "rss_limit_bytes": 3 * 1024**3,
            "failure_policy": "No retries, substitutions or overwrites; OOD is a completed physical run with unavailable fidelity, not a program error.",
            "deadline_semantics": "Each arm has 600 seconds from subprocess start, including its own initialization, complete compile and validation; queue time excluded.",
            "rng_semantics": "Trial seed controls local initialization and dynamic streams. Before the dynamic placement entry, Python and NumPy global RNGs are explicitly reset to the trial seed in all four arms and their state hashes are recorded. Historical SA internally fixes its own seed to zero.",
            "budget_semantics": "B32 is at most 32 distinct outer mapping evaluation attempts; inner rollout has its own 32-encoding-per-layer cap. Infeasible attempts count; cached duplicates do not count again.",
            "pairing_contract": "New three arms share seat-domain and initial-population hashes for a circuit/seed. Their later populations may differ with objective/policy.",
            "aggregation": "Median of each field across all three seeds within a canonical circuit. Primary comparisons use the same all-four-arm complete in-domain circuit cohort. Fidelity ratios use log differences; other ratios are paired by circuit before geometric averaging.",
            "timing_policy": "Descriptive serial times across three stochastic configurations, not fixed-seed timing repetitions or a claim of runtime speedup.",
            "repository": {"commit": git("rev-parse", "HEAD").strip(),
                           "status": git("status", "--porcelain=v1", "--untracked-files=all", "--", "ZAC_zzx"),
                           "tracked_diff": git("diff", "--binary", "--no-ext-diff", "HEAD", "--", "ZAC_zzx")},
            "source_files": source, "source_snapshot_sha256": stable({k: v["sha256"] for k, v in source.items()})}


def seal():
    if ROOT.exists() or BUILD.exists():
        raise FileExistsError("study output or frozen-source directory already exists")
    protocol = build_plan()  # All read-only checks precede output creation.
    frozen = BUILD / "frozen_source/ZAC_zzx"
    for relative, ref in protocol["source_files"].items():
        if digest(ref["path"]) != ref["sha256"]:
            raise ValueError("source changed during seal")
        target = frozen / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref["path"], target)
        if digest(target) != ref["sha256"]:
            raise ValueError("source snapshot copy differs")
    protocol["frozen_source"] = str(frozen)
    protocol["driver"] = pin(frozen / "experiments_v2/physical_ga_initial_study.py")
    for c in protocol["circuits"]:
        target = ROOT / "freeze/inputs" / (c["input_sha256"] + ".qasm")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(c["input"], target)
        if digest(target) != c["input_sha256"]:
            raise ValueError("input changed during seal")
        c["original_input"] = c["input"]
        c["input"] = str(target)
    write_new(ROOT / "protocol.json", protocol)
    write_new(ROOT / "protocol.sha256.json", pin(ROOT / "protocol.json"))
    return {"protocol": pin(ROOT / "protocol.json"), "planned_jobs": len(protocol["jobs"])}


def load_protocol(path):
    path = Path(path)
    ref = read(path.with_name("protocol.sha256.json"))
    if Path(ref["path"]).resolve() != path.resolve():
        raise ValueError("protocol sidecar points to another protocol")
    p = checked(ref)
    correction = p["driver_correction"]
    checked(correction["superseded_protocol"])
    for preserved in correction["preserved_records"]:
        if digest(preserved["path"]) != preserved["sha256"]:
            raise ValueError("superseded evidence changed: " + preserved["path"])
    if (p["protocol_id"] != PROTOCOL_ID or p["arms"] != list(ARMS)
            or p["seeds"] != list(SEEDS) or len(p["circuits"]) != 9
            or p["jobs"] != job_order(p["circuits"]) or len(p["jobs"]) != 108
            or p["max_workers"] != 1 or p["timeout_s"] != 600 or p["rss_limit_bytes"] != 3 * 1024**3):
        raise ValueError("frozen experiment contract differs")
    for key in ("cohort_source", "config", "architecture", "model"):
        checked(p[key])
    for key in ("driver", "python"):
        if digest(p[key]["path"]) != p[key]["sha256"]:
            raise ValueError("frozen runtime changed: " + key)
    for relative, ref in p["source_files"].items():
        if digest(Path(p["frozen_source"]) / relative) != ref["sha256"]:
            raise ValueError("frozen source changed: " + relative)
    for c in p["circuits"]:
        if digest(c["input"]) != c["input_sha256"]:
            raise ValueError("frozen input changed")
    if stable(dynamic_contract(p["dynamic_setting"])) != p["dynamic_contract_sha256"]:
        raise ValueError("dynamic configuration changed")
    return p


class EvidenceValidationError(RuntimeError):
    pass


def validate_execution(job, circuit, trace, selection, compiler, spec):
    from evaluation import normalize_zair, score_trace, validate_trace_physics
    from experiments_v2.initial_lookahead_runner import logical_trace_receipt
    events = tuple(normalize_zair(trace, architecture=spec))
    validation = validate_trace_physics(events, n_qubits=compiler.n_q)
    try:
        logical = logical_trace_receipt(circuit["input"], compiler.n_q, events, compiler.gate_scheduling)
    except ValueError as exc:
        raise EvidenceValidationError(str(exc)) from exc
    score = score_trace(events, n_qubits=compiler.n_q).to_dict()
    if not validation["ok"] or validation["ghost_hits"] != 0 or not logical["ok"]:
        raise EvidenceValidationError("physical or logical execution validation failed")
    if (compiler.n_q != circuit["qubits"] or score["counts"]["one_qubit_gates"] != circuit["gates_1q"]
            or score["counts"]["two_qubit_gates"] != circuit["gates_2q"]):
        raise EvidenceValidationError("canonical qubits or gates differ")
    init = next(i for i in trace["instructions"] if i["type"] == "init")
    mapping = [r[1:] for r in sorted(init["init_locs"])]
    if stable(mapping) != selection["selected_mapping_sha256"]:
        raise EvidenceValidationError("selected and executed initial mappings differ")
    if not score["ood"] and not math.isfinite(score["log_fidelity"]):
        raise EvidenceValidationError("in-domain score must be finite")
    return validation, logical, score


def rng_fingerprint():
    import random
    import numpy as np
    state = np.random.get_state()
    return {"python": stable(random.getstate()),
            "numpy": stable([state[0], state[1].tolist(), int(state[2]), int(state[3]), float(state[4])])}


def install_dynamic_rng_reset(compiler, seed, callback=None):
    """Reset at the real dynamic entry, after the selected mapping is fixed."""
    import random
    import numpy as np
    original = compiler.place_qubit_intermedeiate
    def isolated_dynamic():
        before = rng_fingerprint()
        random.seed(seed)
        np.random.seed(seed)
        compiler.zzx_study_dynamic_rng = {"seed": seed, "before_reset": before,
            "at_dynamic_entry": rng_fingerprint(), "policy": "python-numpy-reset-before-dynamic-v1"}
        if callback is not None:
            callback()
        return original()
    compiler.place_qubit_intermedeiate = isolated_dynamic


def worker(protocol_path, job_id):
    p = load_protocol(protocol_path)
    job = next(j for j in p["jobs"] if j["job_id"] == job_id)
    circuit = next(c for c in p["circuits"] if c["input_sha256"] == job["input_sha256"])
    root = Path(protocol_path).parent / "jobs" / job_id
    root.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, p["frozen_source"])
    stage = "load_runtime"
    try:
        from zac.ds.architecture import Architecture
        from zzx.zac_zzx import ZAC_zzx
        native = runtime_info(p["native"]["native_wheel_sha256"])
        if native["extension_sha256"] != p["native"]["extension_sha256"]:
            raise ValueError("registered native extension changed")
        setting = arm_setting(p["dynamic_setting"], job["arm"], job["seed"])
        setting.update(name=job["circuit"], dir=str(root) + "/", arch_spec=p["architecture"]["path"])
        if stable(dynamic_contract(setting)) != p["dynamic_contract_sha256"]:
            raise ValueError("arm changed dynamic compilation settings")
        write_new(root / "setting.json", setting)
        spec = checked(p["architecture"])
        compiler = ZAC_zzx()
        compiler.parse_setting(setting)
        def selected_before_dynamic():
            write_new(root / "selection.json", compiler.zzx_initial_lookahead_report)
            write_new(root / "dynamic_rng.json", compiler.zzx_study_dynamic_rng)
        install_dynamic_rng_reset(compiler, job["seed"], selected_before_dynamic)
        with (root / "compiler.log").open("x") as log, redirect_stdout(log):
            architecture = Architecture(deepcopy(spec))
            architecture.preprocessing()
            compiler.set_architecture_spec_path(p["architecture"]["path"])
            compiler.set_architecture(architecture)
            compiler.set_program(circuit["input"])
            stage = "initialize_and_compile"
            start, cpu = time.monotonic_ns(), time.process_time_ns()
            trace = compiler.solve(save_file=False)
            wall_ns, cpu_ns = time.monotonic_ns() - start, time.process_time_ns() - cpu
            selection = compiler.zzx_initial_lookahead_report
            if stable(read(root / "selection.json")) != stable(selection):
                raise ValueError("initial selection changed during dynamic compilation")
            stage = "validate_execution"
            target = root / "native_trace.json.gz"
            with target.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
                stream.write(json.dumps(trace, sort_keys=True, allow_nan=False).encode())
            validation, logical, score = validate_execution(job, circuit, trace, selection, compiler, spec)
            result = {**job, "status": "success", "protocol_sha256": digest(protocol_path),
                      "source_snapshot_sha256": p["source_snapshot_sha256"], "origin": "fresh",
                      "native": native, "setting": pin(root / "setting.json"),
                      "n_qubits": compiler.n_q, "total_layers": len(compiler.gate_scheduling),
                      "validation": validation, "logical_validation": logical, "score": score,
                      "selection": selection, "selection_file": pin(root / "selection.json"),
                      "selected_mapping_sha256": selection["selected_mapping_sha256"],
                      "native_trace": pin(target), "native_instruction_sha256": stable(trace["instructions"]),
                      "end_to_end_ns": wall_ns, "end_to_end_cpu_ns": cpu_ns,
                      "selection_ns": selection["selection_ns"],
                      "initialization_ns": compiler.zzx_stage_timing_ns["initial_placement_ns"],
                      "sa_initialization_ns": selection.get("sa_initialization_ns", 0),
                      "dynamic_rng": compiler.zzx_study_dynamic_rng,
                      "dynamic_rng_file": pin(root / "dynamic_rng.json"),
                      "peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
                      "quality_classification": "model_out_of_domain" if score["ood"] else "valid_fidelity"}
            write_new(root / "result.json", result)
        return 0
    except Exception as exc:
        report = getattr(exc, "initial_ga_report", getattr(exc, "initial_lookahead_report", None))
        if report is not None and not (root / "selection.json").exists():
            write_new(root / "selection.json", report)
        status = ("initialization_infeasible" if type(exc).__name__ == "InitialMappingInfeasible"
                  else "validation_error" if isinstance(exc, EvidenceValidationError) else "program_error")
        write_new(root / "failure.json", {**job, "status": status, "stage": stage,
                  "exception_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
        return 1


def resident_bytes(pid):
    result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(result.stdout.strip() or 0) * 1024 if result.returncode == 0 else 0


def monitor(process, timeout_s, rss_limit_bytes):
    start, peak, status = time.monotonic(), 0, None
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - start
            peak = max(peak, resident_bytes(process.pid))
            status = "timeout" if elapsed >= timeout_s else "memory_limit" if peak > rss_limit_bytes else None
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
    return {"limit_status": status, "peak_observed_rss_bytes": peak,
            "elapsed_s": time.monotonic() - start, "returncode": process.returncode}


def run_one(protocol_path, p, job):
    root = Path(protocol_path).parent
    receipt = root / "receipts" / (job["job_id"] + ".json")
    started = receipt.with_suffix(".started.json")
    if receipt.exists():
        return read(receipt)
    if started.exists():
        return {**job, "status": "interrupted", "reason": "started claim without final receipt; never retried"}
    write_new(started, {**job, "started_at_utc": now(), "protocol_sha256": digest(protocol_path)})
    command = [p["python"]["path"], "-B", p["driver"]["path"], "--worker", job["job_id"],
               "--protocol", str(protocol_path)]
    row = {**job, "started_at_utc": now(), "command": command,
           "protocol_sha256": digest(protocol_path), "origin": "fresh"}
    try:
        with receipt.with_suffix(".log").open("x") as log:
            process = subprocess.Popen(command, cwd=p["workspace"],
                env={**os.environ, **p["environment"], "PYTHONPATH": p["frozen_source"]},
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            row.update(monitor(process, p["timeout_s"], p["rss_limit_bytes"]))
        result_path = root / "jobs" / job["job_id"] / "result.json"
        failure_path = result_path.with_name("failure.json")
        row["status"] = row.pop("limit_status") or ("success" if row["returncode"] == 0 else "program_error")
        if row["status"] == "success":
            result = read(result_path)
            row.update(result=pin(result_path), quality_classification=result["quality_classification"])
        elif failure_path.exists():
            failure = read(failure_path)
            if row["status"] == "program_error":
                row["status"] = failure["status"]
            row["failure"] = pin(failure_path)
    except Exception as exc:
        row.update(status="runner_error", exception_type=type(exc).__name__, message=str(exc))
    row["completed_at_utc"] = now()
    write_new(receipt, row)
    return row


def execute(protocol_path, limit=None):
    p = load_protocol(protocol_path)
    rows = []
    with (Path(protocol_path).parent / ".execution.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = 0
        for job in p["jobs"]:
            if limit is not None and pending >= limit:
                break
            existed = (Path(protocol_path).parent / "receipts" / (job["job_id"] + ".json")).exists()
            load_protocol(protocol_path)  # Fail closed before each subprocess.
            row = run_one(protocol_path, p, job)
            rows.append(row)
            pending += not existed
            print(json.dumps({"job_id": job["job_id"], "status": row["status"],
                              "elapsed_s": row.get("elapsed_s")}), flush=True)
    return {"observed_jobs": len(rows), "statuses": dict(Counter(r["status"] for r in rows))}


def validate_result_identity(p, job, result):
    for key in ("job_id", "circuit", "dataset", "input_sha256", "arm", "seed"):
        if result.get(key) != job[key]:
            raise ValueError("result identity mismatch: " + key)
    if result.get("source_snapshot_sha256") != p["source_snapshot_sha256"] or result.get("origin") != "fresh":
        raise ValueError("wrong source or historical result substituted")
    if (not result.get("validation", {}).get("ok") or result["validation"].get("ghost_hits") != 0
            or not result.get("logical_validation", {}).get("ok")):
        raise ValueError("successful result lacks validated physical/logical evidence")


def collect(protocol_path, p, *, verify_trace=True):
    root = Path(protocol_path).parent
    rows, pins = [], []
    for job in p["jobs"]:
        receipt_path = root / "receipts" / (job["job_id"] + ".json")
        if not receipt_path.exists():
            status = "interrupted" if receipt_path.with_suffix(".started.json").exists() else "pending"
            rows.append({**job, "status": status})
            continue
        receipt = read(receipt_path)
        if any(receipt.get(k) != job[k] for k in ("job_id", "input_sha256", "arm", "seed")):
            raise ValueError("receipt identity changed")
        if receipt.get("protocol_sha256") != digest(protocol_path):
            raise ValueError("receipt is not bound to this protocol")
        pins.append(pin(receipt_path))
        row = {**job, "status": receipt["status"], "elapsed_s": receipt.get("elapsed_s")}
        if receipt["status"] == "success":
            result = checked(receipt["result"])
            validate_result_identity(p, job, result)
            if result["protocol_sha256"] != digest(protocol_path):
                raise ValueError("result is not bound to this protocol")
            actual_setting = checked(result["setting"])
            expected_setting = arm_setting(p["dynamic_setting"], job["arm"], job["seed"])
            if dynamic_contract(actual_setting) != dynamic_contract(expected_setting):
                raise ValueError("recorded dynamic settings differ")
            for field in ("init_strategy", "init_engine", "initial_ga", "initial_lookahead", "seed"):
                if actual_setting.get(field) != expected_setting.get(field):
                    raise ValueError("recorded initializer settings differ: " + field)
            if checked(result["dynamic_rng_file"]) != result["dynamic_rng"]:
                raise ValueError("dynamic RNG receipt differs")
            selection = checked(result["selection_file"])
            if selection != result["selection"]:
                raise ValueError("selection copy differs")
            trace_ref = result["native_trace"]
            if digest(trace_ref["path"]) != trace_ref["sha256"]:
                raise ValueError("native trace changed")
            if verify_trace:
                with gzip.open(trace_ref["path"], "rt") as stream:
                    trace = json.load(stream)
                if stable(trace["instructions"]) != result["native_instruction_sha256"]:
                    raise ValueError("instruction identity differs")
                # Replay the saved output only; never invoke the compiler.
                sys.path.insert(0, p["frozen_source"])
                from evaluation import normalize_zair, score_trace, validate_trace_physics
                events = tuple(normalize_zair(trace, architecture=checked(p["architecture"])))
                physics = validate_trace_physics(events, n_qubits=result["n_qubits"])
                score = score_trace(events, n_qubits=result["n_qubits"]).to_dict()
                if physics != result["validation"] or score != result["score"]:
                    raise ValueError("saved trace no longer reproduces validation/score")
            row.update(result=result)
            pins.extend((receipt["result"], result["setting"], result["selection_file"], result["dynamic_rng_file"], trace_ref))
        rows.append(row)
    return rows, pins


def valid_quality(row):
    result = row.get("result", {})
    score = result.get("score", {})
    return (row["status"] == "success"
            and result.get("validation", {}).get("ok") is True
            and result.get("validation", {}).get("ghost_hits") == 0
            and result.get("logical_validation", {}).get("ok") is True
            and not score.get("ood", True)
            and isinstance(score.get("log_fidelity"), (int, float))
            and math.isfinite(score["log_fidelity"]))


def wtl(differences):
    return {"wins": sum(x > 1e-12 for x in differences),
            "ties": sum(abs(x) <= 1e-12 for x in differences),
            "losses": sum(x < -1e-12 for x in differences)}


def summarize(p, records):
    keys = [(r["input_sha256"], r["seed"], r["arm"]) for r in records]
    expected = {(j["input_sha256"], j["seed"], j["arm"]) for j in p["jobs"]}
    if len(set(keys)) != len(keys) or set(keys) != expected:
        raise ValueError("duplicate or missing planned analysis identity")
    index = dict(zip(keys, records))
    circuits = []
    for c in p["circuits"]:
        relevant = [index[c["input_sha256"], seed, arm] for seed in p["seeds"] for arm in p["arms"]]
        complete = all(valid_quality(r) for r in relevant)
        row = {"dataset": c["dataset"], "circuit": c["circuit"], "input_sha256": c["input_sha256"],
               "complete": complete, "arms": {}, "exclusions": []}
        for seed in p["seeds"]:
            same_seed = [index[c["input_sha256"], seed, arm].get("result") for arm in ARMS]
            available = [r for r in same_seed if r]
            if available:
                states = [r["dynamic_rng"]["at_dynamic_entry"] for r in available]
                if any(r["dynamic_rng"]["seed"] != seed for r in available) or len({stable(v) for v in states}) != 1:
                    raise ValueError("dynamic RNG is not paired across arms")
            new = [index[c["input_sha256"], seed, arm].get("result") for arm in ARMS[1:]]
            if all(new):
                for field in ("initial_population_sha256", "seat_domain_sha256"):
                    values = [r["selection"].get(field) for r in new]
                    if None in values or len(set(values)) != 1:
                        raise ValueError("new-arm pairing differs: " + field)
        for r in relevant:
            if not valid_quality(r):
                row["exclusions"].append({"arm": r["arm"], "seed": r["seed"],
                    "reason": ("model_out_of_domain" if r.get("result", {}).get("score", {}).get("ood") else "invalid_evidence") if r["status"] == "success" else r["status"]})
        for arm in p["arms"]:
            values = [index[c["input_sha256"], seed, arm] for seed in p["seeds"]]
            entry = {"completed_runs": sum(v["status"] == "success" for v in values),
                     "valid_runs": sum(valid_quality(v) for v in values)}
            # Never impute a missing seed or form a primary median from two seeds.
            if all(valid_quality(v) for v in values):
                vectors = [{"log_fidelity": v["result"]["score"]["log_fidelity"],
                            "move_batches": v["result"]["score"]["move_batches"],
                            "move_time_us": v["result"]["score"]["move_time_us"],
                            "end_to_end_s": v["result"]["end_to_end_ns"] / 1e9,
                            "initialization_s": v["result"]["initialization_ns"] / 1e9,
                            "selection_s": v["result"]["selection_ns"] / 1e9,
                            "sa_s": v["result"]["sa_initialization_ns"] / 1e9} for v in values]
                entry.update({"median_" + key: statistics.median(v[key] for v in vectors) for key in METRICS})
                entry["geometric_fidelity"] = math.exp(entry["median_log_fidelity"])
            row["arms"][arm] = entry
        circuits.append(row)
    cohort = [c for c in circuits if c["complete"]]
    overall = {"planned_circuits": len(circuits), "complete_circuits": len(cohort),
               "cohort_sha256": [c["input_sha256"] for c in cohort], "arms": {}, "comparisons": {}}
    if cohort:
        for arm in p["arms"]:
            overall["arms"][arm] = {
                "fidelity_geometric_mean": math.exp(statistics.fmean(c["arms"][arm]["median_log_fidelity"] for c in cohort)),
                **{key + "_arithmetic_mean": statistics.fmean(c["arms"][arm]["median_" + key] for c in cohort)
                   for key in METRICS if key != "log_fidelity"}}
        for treatment, control in PAIRS:
            diff = [c["arms"][treatment]["median_log_fidelity"] - c["arms"][control]["median_log_fidelity"] for c in cohort]
            comparison = {"N": len(cohort), "fidelity_ratio_geometric_mean": math.exp(statistics.fmean(diff)),
                          "fidelity_gain_percent": 100 * math.expm1(statistics.fmean(diff)),
                          "fidelity_win_tie_loss": wtl(diff)}
            for key in METRICS[1:]:
                pairs = [(c["arms"][treatment]["median_" + key], c["arms"][control]["median_" + key]) for c in cohort]
                comparison[key + "_ratio_geometric_mean"] = (math.exp(statistics.fmean(math.log(a / b) for a, b in pairs))
                    if all(a > 0 and b > 0 for a, b in pairs) else None)
                comparison[key + "_ratio_N"] = len(cohort) if all(a > 0 and b > 0 for a, b in pairs) else 0
            overall["comparisons"][treatment + "_vs_" + control] = comparison
    return {"protocol_id": PROTOCOL_ID, "formal_paper_result": False, "study_role": p["study_role"],
            "aggregation": p["aggregation"], "timing_policy": p["timing_policy"],
            "planned_runs": len(p["jobs"]), "run_statuses": dict(Counter(r["status"] for r in records)),
            "model_ood_runs": sum(r["status"] == "success" and r["result"]["score"]["ood"] for r in records),
            "per_circuit": circuits, "overall": overall}


def csv_text(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def export_payload(p, records, report):
    runs = []
    for r in records:
        result, selection = r.get("result", {}), r.get("result", {}).get("selection", {})
        runs.append({key: r.get(key) for key in ("job_id", "dataset", "circuit", "input_sha256", "arm", "seed", "status")}
                    | {"quality_classification": result.get("quality_classification"),
                       **{key: result.get("score", {}).get(key) for key in ("log_fidelity", "fidelity", "move_batches", "move_time_us")},
                       "end_to_end_s": result.get("end_to_end_ns", 0) / 1e9 if result else None,
                       "initialization_s": result.get("initialization_ns", 0) / 1e9 if result else None,
                       "selection_s": result.get("selection_ns", 0) / 1e9 if result else None,
                       "sa_s": result.get("sa_initialization_ns", 0) / 1e9 if result else None,
                       **{key: selection.get(key) for key in ("unique_evaluations", "cache_hits", "proposals", "termination_reason", "selected_mapping_sha256")}})
    circuits = [{"dataset": c["dataset"], "circuit": c["circuit"], "input_sha256": c["input_sha256"],
                 "common_complete": c["complete"], "arm": arm, **values}
                for c in report["per_circuit"] for arm, values in c["arms"].items()]
    fields = ["dataset", "circuit", "input_sha256", "common_complete", "arm", "completed_runs", "valid_runs"]
    fields += ["median_" + key for key in METRICS] + ["geometric_fidelity"]
    lines = ["# Physical GA initialization pilot", "", report["study_role"], "", report["aggregation"], "",
             f"Planned runs: {report['planned_runs']}; statuses: {report['run_statuses']}; OOD runs: {report['model_ood_runs']}.",
             f"Common complete circuit cohort: {report['overall']['complete_circuits']}/{len(p['circuits'])}.", "",
             "| Comparison | N | Geometric fidelity ratio | Win/tie/loss |", "|---|---:|---:|---|",
             *[f"| {name} | {v['N']} | {v['fidelity_ratio_geometric_mean']:.8g} | {v['fidelity_win_tie_loss']} |"
               for name, v in report["overall"]["comparisons"].items()], "", report["timing_policy"], "",
             "No historical outcomes replace new missing/failed runs. Per-circuit and all-run CSVs retain the full planned inventory.", ""]
    return {"result.json": json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n",
            "runs.csv": csv_text(runs, list(runs[0])), "circuits.csv": csv_text(circuits, fields),
            "report.md": "\n".join(lines)}


def analyze(protocol_path, *, check=False, export_name="final"):
    p = load_protocol(protocol_path)
    records, refs = collect(protocol_path, p)
    report = summarize(p, records)
    payload = export_payload(p, records, report)
    out = Path(protocol_path).parent / "exports" / export_name
    if not export_name or Path(export_name).name != export_name or export_name in (".", ".."):
        raise ValueError("export name must be a plain directory name")
    provenance = {"protocol": pin(protocol_path), "sources": refs,
                  "files": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in payload.items()}}
    if check:
        if read(out / "provenance.json") != provenance:
            raise ValueError("export provenance changed")
        for name, content in payload.items():
            if (out / name).read_text() != content:
                raise ValueError("export differs from recomputed evidence: " + name)
    else:
        if out.exists():
            raise FileExistsError("export already exists; use --check or a new export name")
        for name, content in payload.items():
            write_new(out / name, content)
        write_new(out / "provenance.json", provenance)
    return {"export": str(out), "common_complete_circuits": report["overall"]["complete_circuits"],
            "statuses": report["run_statuses"], "mode": "check" if check else "create"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("preflight", "seal", "run", "analyze", "check"):
        modes.add_argument("--" + mode, action="store_true")
    modes.add_argument("--worker", metavar="JOB_ID")
    parser.add_argument("--protocol", type=Path, default=ROOT / "protocol.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--export-name", default="final")
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.protocol, args.worker)
    if args.preflight:
        p = build_plan()
        report = {"status": "pass", "planned_jobs": len(p["jobs"]), "source_snapshot_sha256": p["source_snapshot_sha256"],
                  "python": p["python"], "native": p["native"], "arms": p["arms"], "circuits": p["circuits"]}
    elif args.seal:
        report = seal()
    elif args.run:
        report = execute(args.protocol, args.limit)
    else:
        report = analyze(args.protocol, check=args.check, export_name=args.export_name)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
