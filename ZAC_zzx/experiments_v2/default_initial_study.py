"""Append-only evaluation of the default physical-prefix initializer.

Seal first, run four deterministic parity smokes, validate historical traces,
then execute only missing canonical circuit/seed identities. Historical timing
is never presented as timing of the new public default entry point.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout
from copy import deepcopy
import csv
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import math
import os
import resource
import shutil
import signal
import subprocess
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "ZAC_zzx/results/default_initial_v1"
BUILD = REPO / "IEEE_conference_template/build/default_initial_v1"
PROTOCOL = "default-initial-canonical169-seed012-v1"
FIXED = {"horizon": 2, "candidates": 4, "rho": .7, "rollout_evaluations": 32}
ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
       "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
       "VECLIB_MAXIMUM_THREADS": "1"}
OLD = REPO / "ZAC_zzx/results/initial_lookahead_v1"
PHASES = ("nine-family-h0-h2-sa-k4-rho07-b32-seed012-v1",
          "dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1")


def read(path):
    return json.loads(Path(path).read_text())


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
        raise ValueError("evidence changed: " + ref["path"])
    return read(ref["path"])


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def now():
    return datetime.now(timezone.utc).isoformat()


def computational_setting(setting):
    """Only path/identity and the explicitly changed initializer are excluded."""
    ignored = {"name", "dir", "seed", "arch_spec", "init_strategy", "initial_lookahead"}
    return {k: v for k, v in setting.items() if k not in ignored}


def inventory():
    rows = []
    for dataset in ("zac18", "qmap154"):
        with (REPO / f"ZAC_zzx/results/paper_zh_v2/{dataset}.csv").open() as stream:
            for row in csv.DictReader(stream):
                rows.append({"dataset": dataset, "circuit": row["circuit"],
                    "canonical_sha256": row["canonical_sha256"],
                    **{k: int(row[k]) for k in ("qubits", "gates_1q", "gates_2q")}})
    if len(rows) != 172 or len({(r["dataset"], r["circuit"]) for r in rows}) != 172:
        raise ValueError("accepted display inventory changed")
    return rows


def build_plan():
    rows = inventory()
    groups = defaultdict(list)
    for row in rows:
        groups[row["canonical_sha256"]].append(row)
    if len(groups) != 169:
        raise ValueError("expected 18+151 distinct canonical inputs")
    config_path = REPO / "ZAC_zzx/exp_setting/ga_lk_default.json"
    setting = read(config_path)["zac_setting"][0]
    if setting.get("initial_lookahead") != FIXED:
        raise ValueError("fixed initializer configuration changed")
    old = {}
    timeouts = []
    for phase in PHASES:
        p = OLD / phase
        protocol = read(p / "protocol.json")
        for job in protocol["job_order"]:
            receipt = p / "receipts" / (job["job_id"] + ".json")
            rec = read(receipt)
            root = p / "jobs" / job["job_id"]
            if rec["status"] == "timeout":
                timeouts.append({"job_id": job["job_id"], "receipt": pin(receipt)})
            if rec["status"] != "success" or not (root / "h2_result.json").exists():
                continue
            jp = read(root / "protocol.json")
            result = read(root / "h2_result.json")
            selection = read(root / "h2_selection.json")
            if (computational_setting(jp["dynamic_setting"]) != computational_setting(setting)
                    or selection["config"] != {**FIXED, "seed": job["seed"]}
                    or result["status"] != "success" or not result["validation"]["ok"]
                    or result["validation"]["ghost_hits"] != 0
                    or not read(root / "summary.json")["source_stable"]):
                raise ValueError("historical identity/config/physical check differs: " + str(root))
            key = (jp["input_sha256"], job["seed"])
            if key in old:
                raise ValueError("duplicate historical reuse identity")
            old[key] = {"root": str(root), "native": jp["native"],
                "evidence": {name: pin(path) for name, path in {
                    "phase_protocol": p / "protocol.json", "phase_summary": p / "summary.json",
                    "receipt": receipt, "protocol": root / "protocol.json",
                    "result": root / "h2_result.json", "native_trace": root / "h2_native.json",
                    "selection": root / "h2_selection.json", "choices": root / "sealed_choices.json",
                    "base_mapping": root / "base_mapping.json", "summary": root / "summary.json"}.items()}}
    jobs = []
    for sha, labels in sorted(groups.items()):
        labels.sort(key=lambda r: (r["dataset"], r["circuit"]))
        representative = labels[0]
        for label in labels:
            path = REPO / f"fidelity-lookahead-v2/artifacts/canonical/{label['dataset']}/{label['circuit']}.qasm"
            if digest(path) != sha:
                raise ValueError("canonical input changed")
        path = REPO / f"fidelity-lookahead-v2/artifacts/canonical/{representative['dataset']}/{representative['circuit']}.qasm"
        for seed in (0, 1, 2):
            jobs.append({**representative, "seed": seed, "job_id": f"{sha[:16]}-s{seed}",
                "input": pin(path), "labels": labels, "origin": "reused" if (sha, seed) in old else "fresh",
                "reuse": old.get((sha, seed))})
    if Counter(j["origin"] for j in jobs) != {"fresh": 389, "reused": 118}:
        raise ValueError("planned fresh/reused matrix changed")
    smokes = []
    for dataset in ("zac18", "qmap154"):
        options = sorted((j for j in jobs if j["dataset"] == dataset and j["seed"] == 0
                          and j["origin"] == "reused"),
                         key=lambda j: (j["gates_1q"] + j["gates_2q"], j["circuit"]))
        smokes += [options[0]["job_id"], options[len(options) // 2]["job_id"]]
    return {"protocol_id": PROTOCOL, "created_at_utc": now(), "jobs": jobs,
        "display_count": 172, "canonical_count": 169, "seeds": [0, 1, 2],
        "fixed_initialization": FIXED, "dynamic_setting": setting,
        "config": pin(config_path), "architecture": pin(REPO / "ZAC_zzx/hardware_spec/full_architecture.json"),
        "accepted_manifest": pin(REPO / "ZAC_zzx/results/paper_zh_v2/final_manifest.json"),
        "old_timeouts_preserved": timeouts, "smoke_job_ids": smokes,
        "timeout_s": 600, "rss_limit_bytes": 3 * 1024**3, "max_workers": 4,
        "environment": ENV, "formal_paper_result": False,
        "reuse_policy": "Quality only; verify all source pins, original trace physics/logic/model and canonical identity. Never relabel historical timing as the public default entry point.",
        "aggregation": "Three-seed medians within display circuit; aliases share one execution. Collapse aliases by canonical SHA before dataset inference. Preserve all planned outcomes and OOD classifications.",
        "failure_policy": "No automatic retry, replacement, parameter selection or silent model substitution. Old paired timeouts remain intact; new single-arm identities have their own 600-second limit.",
        "timing_policy": "Fresh full public solve wall/process CPU only. Concurrent quality runs do not establish speedup versus historical serial timing. Reused quality has no new default-entry timing."}


def seal():
    if (ROOT / "protocol.json").exists() or (BUILD / "frozen_source").exists():
        raise FileExistsError("protocol/frozen source already exists")
    protocol = build_plan()
    # The original registered wheel is used; optional 0.5.35 mechanisms are not enabled.
    original = read(OLD / PHASES[1] / "protocol.json")
    protocol["python"] = original["python"]
    protocol["python_sha256"] = digest(original["python"])
    protocol["native"] = original["native"]
    protocol["driver"] = pin(__file__)
    protocol["workspace"] = str(REPO)
    source = BUILD / "frozen_source/ZAC_zzx"
    files = {}
    for directory in ("zzx", "zac", "streaming", "evaluation", "experiments_v2"):
        for path in sorted((REPO / "ZAC_zzx" / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(REPO / "ZAC_zzx")
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            files[str(relative)] = digest(target)
    shutil.copyfile(REPO / "ZAC_zzx/verify_batches.py", source / "verify_batches.py")
    files["verify_batches.py"] = digest(source / "verify_batches.py")
    protocol.update(frozen_source=str(source), frozen_source_files=files)
    write_new(ROOT / "protocol.json", protocol)
    write_new(ROOT / "protocol.sha256.json", pin(ROOT / "protocol.json"))
    return {"protocol": pin(ROOT / "protocol.json"), "jobs": len(protocol["jobs"]),
            "counts": dict(Counter(j["origin"] for j in protocol["jobs"])), "smokes": protocol["smoke_job_ids"]}


def load_protocol(path):
    path = Path(path)
    protocol = checked(read(path.with_name("protocol.sha256.json")))
    if protocol["protocol_id"] != PROTOCOL:
        raise ValueError("wrong protocol")
    checked(protocol["config"])
    checked(protocol["architecture"])
    checked(protocol["accepted_manifest"])
    if digest(protocol["driver"]["path"]) != protocol["driver"]["sha256"]:
        raise ValueError("driver changed after seal")
    if digest(protocol["python"]) != protocol["python_sha256"]:
        raise ValueError("Python changed")
    for name, sha in protocol["frozen_source_files"].items():
        if digest(Path(protocol["frozen_source"]) / name) != sha:
            raise ValueError("frozen source changed: " + name)
    return protocol


def compare_score(old, new):
    for key in ("fidelity", "log_fidelity", "move_batches", "move_time_us", "ood", "counts"):
        a, b = old[key], new[key]
        if isinstance(a, float) and isinstance(b, (float, int)):
            equal = math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
        else:
            equal = a == b
        if not equal:
            raise ValueError("historical scoring differs: " + key)


def worker(protocol_path, job_id, phase):
    protocol = load_protocol(protocol_path)
    job = next(j for j in protocol["jobs"] if j["job_id"] == job_id)
    if digest(job["input"]["path"]) != job["canonical_sha256"]:
        raise ValueError("input identity changed")
    sys.path.insert(0, protocol["frozen_source"])
    from evaluation import normalize_zair, score_trace, validate_trace_physics
    from experiments_v2.initial_lookahead_runner import logical_trace_receipt
    from zac.ds.architecture import Architecture
    from zzx.native_backend import build_info
    from zzx.zac_zzx import ZAC_zzx
    native = build_info(require_registered_wheel=True,
                        expected_wheel_sha256=protocol["native"]["native_wheel_sha256"])
    if native["extension_sha256"] != protocol["native"]["extension_sha256"]:
        raise ValueError("native extension changed")
    root = Path(protocol_path).parent / phase / "jobs" / job_id
    root.mkdir(parents=True, exist_ok=False)
    setting = deepcopy(protocol["dynamic_setting"])
    setting.pop("init_strategy", None)
    setting.pop("initial_lookahead", None)
    setting.update(seed=job["seed"], name=job["circuit"], dir=str(root) + "/",
                   arch_spec=protocol["architecture"]["path"], resyn=False, use_verifier=False)
    spec = checked(protocol["architecture"])
    compiler = ZAC_zzx()
    compiler.parse_setting(setting)  # Public API, no historical private contract.
    if compiler.zzx_params.get("init_strategy") != "physical_prefix":
        raise ValueError("public default initializer is not enabled")
    architecture = Architecture(deepcopy(spec))
    with (root / "compiler.log").open("x") as log, redirect_stdout(log):
        architecture.preprocessing()
        compiler.set_architecture_spec_path(protocol["architecture"]["path"])
        compiler.set_architecture(architecture)
        compiler.set_program(job["input"]["path"])
        result = {"status": "success", "input_sha256": job["canonical_sha256"],
                  "seed": job["seed"], "canonical_job_id": job_id, "n_qubits": compiler.n_q,
                  "origin": "reused" if phase == "reuse" else "fresh", "native": native}
        if phase == "reuse":
            source = {k: checked(v) for k, v in job["reuse"]["evidence"].items()}
            original, trace, selection = source["result"], source["native_trace"], source["selection"]
            if stable(trace["instructions"]) != original["native_instruction_sha256"]:
                raise ValueError("original instructions hash differs")
            compiler.scheduling()
            result["source_evidence"] = job["reuse"]["evidence"]
            result["native_trace"] = job["reuse"]["evidence"]["native_trace"]
            result["historical_timing_not_default_entry"] = {
                key: original[key] for key in ("end_to_end_ns", "end_to_end_cpu_ns") if key in original}
        else:
            start, cpu = time.monotonic_ns(), time.process_time_ns()
            trace = compiler.solve(save_file=False)
            result.update(end_to_end_ns=time.monotonic_ns() - start,
                          end_to_end_cpu_ns=time.process_time_ns() - cpu)
            selection = compiler.zzx_initial_lookahead_report
            if selection["config"] != {**FIXED, "seed": job["seed"]}:
                raise ValueError("public default selected an unexpected initialization policy")
            target = root / "native_trace.json.gz"
            with target.open("xb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
                    stream.write(json.dumps(trace, sort_keys=True, allow_nan=False).encode())
            result["native_trace"] = pin(target)
        events = tuple(normalize_zair(trace, architecture=spec))
        validation = validate_trace_physics(events, n_qubits=compiler.n_q)
        logical = logical_trace_receipt(job["input"]["path"], compiler.n_q, events, compiler.gate_scheduling)
        score = score_trace(events, n_qubits=compiler.n_q).to_dict()
        if not validation["ok"] or validation["ghost_hits"] != 0 or not logical["ok"]:
            raise ValueError("physics/logical validation failed")
        if (compiler.n_q != job["qubits"] or score["counts"]["one_qubit_gates"] != job["gates_1q"]
                or score["counts"]["two_qubit_gates"] != job["gates_2q"]):
            raise ValueError("canonical gate counts differ")
        init = next(i for i in trace["instructions"] if i["type"] == "init")
        mapping = [r[1:] for r in sorted(init["init_locs"])]
        if stable(mapping) != selection["selected_mapping_sha256"]:
            raise ValueError("selected mapping differs from executed init")
        if phase == "reuse":
            compare_score(original["score"], score)
        if phase == "smoke":
            original = checked(job["reuse"]["evidence"]["result"])
            compare_score(original["score"], score)
            if (stable(trace["instructions"]) != original["native_instruction_sha256"]
                    or selection["selected_mapping_sha256"] != original["selected_mapping_sha256"]):
                raise ValueError("default/manual-H2 smoke parity failed")
            result["historical_h2_parity"] = True
        result.update(validation=validation, logical_validation=logical, score=score,
                      total_layers=len(compiler.gate_scheduling), selection=selection,
                      selected_mapping_sha256=selection["selected_mapping_sha256"],
                      native_instruction_sha256=stable(trace["instructions"]),
                      quality_classification="model_out_of_domain" if score["ood"] else "valid_fidelity",
                      peak_rss_bytes=int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
        write_new(root / "result.json", result)
    return {"result": pin(root / "result.json"), "quality_classification": result["quality_classification"]}


def run_one(protocol_path, protocol, job, phase):
    folder = Path(protocol_path).parent / phase / "receipts"
    folder.mkdir(parents=True, exist_ok=True)
    receipt_path = folder / (job["job_id"] + ".json")
    if receipt_path.exists():
        return read(receipt_path)
    write_new(folder / (job["job_id"] + ".started.json"), {"job_id": job["job_id"], "at": now()})
    command = [protocol["python"], "-B", protocol["driver"]["path"], "--worker", job["job_id"],
               "--protocol", str(protocol_path), "--phase", phase]
    env = {**os.environ, **ENV, "PYTHONPATH": protocol["frozen_source"]}
    started = time.monotonic()
    receipt = {"job_id": job["job_id"], "phase": phase, "started_at": now(), "command": command,
               "protocol_sha256": digest(protocol_path), "peak_observed_rss_bytes": 0}
    with (folder / (job["job_id"] + ".log")).open("x") as log:
        process = subprocess.Popen(command, env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        while process.poll() is None:
            rss = subprocess.run(["ps", "-o", "rss=", "-p", str(process.pid)],
                                 capture_output=True, text=True).stdout.strip()
            rss = int(rss or 0) * 1024
            receipt["peak_observed_rss_bytes"] = max(receipt["peak_observed_rss_bytes"], rss)
            limit = "timeout" if time.monotonic() - started >= protocol["timeout_s"] else (
                "memory_limit" if rss > protocol["rss_limit_bytes"] else None)
            if limit:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                receipt["status"] = limit
                break
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    receipt.setdefault("status", "success" if process.returncode == 0 else "runner_error")
    receipt.update(returncode=process.returncode, elapsed_s=time.monotonic() - started, ended_at=now())
    result_path = folder.parent / "jobs" / job["job_id"] / "result.json"
    if receipt["status"] == "success":
        if not result_path.exists():
            raise RuntimeError("worker exited successfully without evidence")
        receipt.update(result=pin(result_path), quality_classification=read(result_path)["quality_classification"])
    write_new(receipt_path, receipt)
    return receipt


def execute(phase, workers):
    path = ROOT / "protocol.json"
    protocol = load_protocol(path)
    if not 1 <= workers <= (2 if phase == "reuse" else 4):
        raise ValueError("worker budget exceeded")
    if phase != "smoke":
        for job_id in protocol["smoke_job_ids"]:
            receipt = read(ROOT / "smoke/receipts" / (job_id + ".json"))
            if receipt["status"] != "success" or not checked(receipt["result"]).get("historical_h2_parity"):
                raise ValueError("all four default-entry parity smokes must pass")
    jobs = [j for j in protocol["jobs"] if (j["job_id"] in protocol["smoke_job_ids"] if phase == "smoke"
            else j["origin"] == ("reused" if phase == "reuse" else "fresh"))]
    with (ROOT / ".execution.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_one, path, protocol, job, phase) for job in jobs]
            for index, future in enumerate(as_completed(futures), 1):
                rec = future.result()
                print(f"[{phase} {index}/{len(jobs)}] {rec['job_id']} {rec['status']} {rec['elapsed_s']:.1f}s", flush=True)
    return summarize(protocol)


def summarize(protocol):
    records = []
    unique_counts = Counter()
    for job in protocol["jobs"]:
        phase = "reuse" if job["origin"] == "reused" else "quality"
        path = ROOT / phase / "receipts" / (job["job_id"] + ".json")
        receipt = read(path) if path.exists() else {"status": "pending"}
        unique_counts[receipt["status"]] += 1
        for label in job["labels"]:
            records.append({**label, "seed": job["seed"], "canonical_job_id": job["job_id"],
                "origin": job["origin"], "alias_of": job["circuit"] if label["circuit"] != job["circuit"] else None,
                "status": receipt["status"], "quality_classification": receipt.get("quality_classification"),
                "result": receipt.get("result"), "receipt": pin(path) if path.exists() else None})
    report = {"protocol_id": PROTOCOL, "protocol_sha256": digest(ROOT / "protocol.json"),
              "records": records, "unique_status_counts": dict(unique_counts),
              "canonical_jobs": 507, "display_records": 516,
              "new_jobs_pending": unique_counts["pending"], "formal_paper_result": False}
    target = ROOT / ("quality_summary.json" if not unique_counts["pending"] else f"summaries/{uuid.uuid4()}.json")
    if target.exists():
        if read(target) != report:
            raise ValueError("immutable final summary differs")
    else:
        write_new(target, report)
    return {"summary": pin(target), "counts": dict(unique_counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("--execute", choices=("smoke", "reuse", "quality"))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--worker")
    parser.add_argument("--phase", choices=("smoke", "reuse", "quality"))
    parser.add_argument("--protocol", type=Path, default=ROOT / "protocol.json")
    args = parser.parse_args()
    if args.plan:
        plan = build_plan()
        output = {"jobs": len(plan["jobs"]), "counts": dict(Counter(j["origin"] for j in plan["jobs"])),
                  "smokes": [(j["dataset"], j["circuit"]) for j in plan["jobs"] if j["job_id"] in plan["smoke_job_ids"]]}
    elif args.seal:
        output = seal()
    elif args.worker:
        output = worker(args.protocol, args.worker, args.phase)
    elif args.execute:
        output = execute(args.execute, args.workers)
    else:
        parser.error("choose --plan, --seal or --execute")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
