"""Sealed H0/1/2/4/8 extension; never changes the accepted paper evidence.

Run this file by absolute path with the frozen ABI9 interpreter.  Compilation
and evaluation are imported from a content-checked archive of commit 4122f615,
not from the concurrently edited checkout.  The sole new ablation registration
is process-local.  Quality runs and serial repeated timing have separate phases.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Direct-file invocation must not let experiments_v2/statistics.py shadow the
# standard-library module before the frozen package is bootstrapped.
sys.path[:] = [entry for entry in sys.path
               if Path(entry).resolve() != Path(__file__).resolve().parent]

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import csv
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
import random
import statistics
import subprocess
import tarfile


REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "ZAC_zzx/results/horizon_extension_v1"
ACCEPTED = REPO / "ZAC_zzx/results/paper_zh_v2"
OLD = REPO / "fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1"
FROZEN_COMMIT = "4122f61516b104ae9d9779f71ec843af63b57600"
SOURCE = REPO / "build/horizon_extension_v1/frozen_source"
SOURCE_DIRS = ("zzx", "zac", "streaming", "evaluation", "experiments_v2", "hardware_spec")
PROTOCOL = "horizon-shared12-seed012-frozen-abi9-v1"
HORIZONS = (0, 1, 2, 4, 8)
SEEDS = (0, 1, 2)
ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
       "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
       "VECLIB_MAXIMUM_THREADS": "1"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def checked_json(path, expected):
    if digest(path) != expected:
        raise ValueError(f"source hash mismatch: {path}")
    return read(path)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def effective(wrapper):
    return wrapper["base_config"]["zac_setting"][0]


def make_wrapper(template, horizon, seed):
    if horizon not in HORIZONS or seed not in SEEDS:
        raise ValueError("unregistered extension horizon/seed")
    wrapper = deepcopy(template)
    name = f"horizon_extension_h{horizon}_ga"
    wrapper["ablation_variant"] = name
    wrapper["controls"]["lookahead_horizon"] = horizon
    setting = effective(wrapper)
    setting["lookahead_horizon"]["max_horizon"] = horizon
    setting["seed"] = seed
    setting["dir"] = "results/horizon_extension_v1/isolated/"
    return wrapper


def comparable_setting(wrapper):
    value = deepcopy(effective(wrapper))
    value.pop("dir", None)
    return value


def missing_quality_jobs(circuits):
    jobs = []
    for row in circuits:
        for horizon in (1, 2, 4):
            for seed in SEEDS:
                if horizon in (2, 4) and seed == 0:
                    continue
                jobs.append({"dataset": row["dataset"], "circuit": row["circuit"],
                             "horizon": horizon, "seed": seed, "repetition": 0,
                             "job_id": f"{row['dataset']}-{row['circuit']}-h{horizon}-s{seed}"})
    random.Random(20260908).shuffle(jobs)
    return jobs


def timing_jobs(circuits):
    jobs = []
    for repeat in range(3):
        block = list(circuits)
        random.Random(20260908 + repeat).shuffle(block)
        for index, row in enumerate(block):
            values = (2, 4, 8)
            shift = (repeat + index) % len(values)
            for horizon in values[shift:] + values[:shift]:
                jobs.append({"dataset": row["dataset"], "circuit": row["circuit"],
                             "horizon": horizon, "seed": 0, "repetition": repeat,
                             "job_id": f"{row['dataset']}-{row['circuit']}-h{horizon}-r{repeat}"})
    warmups = []
    for dataset in ("zac18", "qmap154"):
        row = min((r for r in circuits if r["dataset"] == dataset),
                  key=lambda r: (r["canonical"]["gates_1q"] + r["canonical"]["gates_2q"], r["circuit"]))
        for horizon in (2, 4, 8):
            warmups.append({"dataset": dataset, "circuit": row["circuit"],
                            "horizon": horizon, "seed": 0, "repetition": 0,
                            "job_id": f"warmup-{dataset}-{row['circuit']}-h{horizon}"})
    return jobs, warmups


def source_snapshot():
    return {str(path.relative_to(SOURCE)): digest(path)
            for path in sorted(SOURCE.rglob("*")) if path.is_file() and
            "__pycache__" not in path.parts and path.suffix != ".pyc"}


def extract_frozen_source():
    if SOURCE.exists():
        raise FileExistsError("frozen source directory already exists; inspect it before reuse")
    archive = subprocess.check_output(["git", "archive", FROZEN_COMMIT,
        *[f"ZAC_zzx/{name}" for name in SOURCE_DIRS]], cwd=REPO)
    SOURCE.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            target = (SOURCE / member.name).resolve()
            if SOURCE not in target.parents or member.issym() or member.islnk():
                raise ValueError("unsafe archived source member")
        tar.extractall(SOURCE)
    return source_snapshot()


def source_records():
    final = read(ACCEPTED / "final_manifest.json")
    reports, records = {}, {}
    for phase in ("ablation", "sensitivity"):
        item = final["paper_run_reports"][f"run-paper-{phase}.json"]
        report = checked_json(item["path"], item["sha256"])
        reports[phase] = item
        rows = report["execution"]["attempted"] + report["execution"]["skipped_existing"]
        records[phase] = []
        for row in rows:
            manifest = checked_json(row["manifest"], row["manifest_sha256"])
            records[phase].append({"manifest": row["manifest"],
                "manifest_sha256": row["manifest_sha256"], "data": manifest})
    return final, reports, records


def audit_and_design():
    final, reports, records = source_records()
    defaults = [r for r in records["sensitivity"]
                if r["data"]["ablation_variant"] == "paper_sensitivity_default"]
    if len(defaults) != 12:
        raise ValueError("the prespecified twelve-circuit cohort changed")
    base = read(OLD / "configs/ablation/h8-seed0.json")
    sensitivity = read(OLD / "configs/sensitivity-wrappers/default.json")
    if comparable_setting(base) != comparable_setting(sensitivity):
        raise ValueError("H8 ablation and sensitivity effective settings differ")
    reuse = []
    lookup = {(r["data"]["dataset"], r["data"]["circuit"],
               r["data"]["ablation_variant"], r["data"]["seed"]): r
              for phase in records.values() for r in phase}
    canonical = {}
    for dataset in ("zac18", "qmap154"):
        for item in read(OLD / f"freeze/canonical/{dataset}.suite.manifest.json"):
            canonical[(dataset, Path(item["canonical_path"]).stem)] = item
    fields = ("fidelity", "log_fidelity", "transfers", "idle_exposures", "move_batches",
              "move_time_us", "forecast_summary", "input_sha256", "architecture_sha256",
              "model_sha256", "native_wheel_sha256", "layer_ledger_sha256")
    circuits, parity = [], []
    for row in defaults:
        m = row["data"]
        key = (m["dataset"], m["circuit"])
        other = lookup[key + ("paper_h8_ga", 0)]["data"]
        if any(m[field] != other[field] for field in fields):
            raise ValueError(f"H8 reference parity mismatch: {key}")
        meta = deepcopy(canonical[key])
        meta["canonical_path"] = str(REPO / f"fidelity-lookahead-v2/artifacts/canonical/{key[0]}/{key[1]}.qasm")
        if digest(meta["canonical_path"]) != meta["canonical_sha256"]:
            raise ValueError("canonical input changed")
        circuits.append({"dataset": key[0], "circuit": key[1], "canonical": meta})
        parity.append({"dataset": key[0], "circuit": key[1], "fields_equal": list(fields),
            "visible_depth_counts": m["forecast_summary"]["visible_depth_counts"],
            "effective_depth_counts": m["forecast_summary"]["effective_depth_counts"]})
        for horizon in HORIZONS:
            for seed in SEEDS:
                if horizon in (0, 8):
                    name = f"paper_h{horizon}_ga"
                elif horizon in (2, 4) and seed == 0:
                    name = f"paper_sensitivity_horizon_{horizon}"
                else:
                    continue
                previous = lookup[key + (name, seed)]
                manifest = previous["data"]
                if manifest["native_wheel_sha256"] != final["abi9_native_wheel_sha256"]:
                    raise ValueError("reused native wheel differs")
                config_path = manifest["command"][manifest["command"].index("--config") + 1]
                checked_json(config_path, manifest["config_sha256"])
                expected = comparable_setting(make_wrapper(base, horizon, seed))
                if comparable_setting(read(config_path)) != expected:
                    raise ValueError(f"reused effective config mismatch: {key}, H={horizon}, seed={seed}")
                reuse.append({k: previous[k] for k in ("manifest", "manifest_sha256")} |
                             {"dataset": key[0], "circuit": key[1], "horizon": horizon, "seed": seed})
    if len(reuse) != 96:
        raise ValueError("expected 96 reusable quality runs")
    return final, reports, base, circuits, reuse, parity


def bootstrap(protocol):
    package = Path(protocol["frozen_source_root"]) / "ZAC_zzx"
    sys.path.insert(0, str(package))
    from experiments_v2 import ablation
    if Path(ablation.__file__).resolve().parent.parent != package:
        raise RuntimeError("current checkout modules were loaded before frozen bootstrap")
    for horizon in HORIZONS:
        name = f"horizon_extension_h{horizon}_ga"
        ablation.PAPER_ABLATION_VARIANTS[name] = ablation.PaperAblationVariant(name, "M4", horizon, "ga")


def seal(workers):
    if workers not in (1, 2, 3):
        raise ValueError("quality workers must be between one and three")
    if ROOT.exists():
        raise FileExistsError("extension result directory already exists")
    final, reports, base, circuits, reuse, parity = audit_and_design()
    old_report = read(reports["sensitivity"]["path"])
    native = old_report["native_python"]
    if str(Path(sys.executable).absolute()) != native["paper_native_python_path"]:
        raise ValueError("seal with the frozen ABI9 interpreter, not the current Python")
    for prefix in ("paper_native_python", "paper_native_extension"):
        if digest(native[prefix + "_path"]) != native[prefix + "_sha256"]:
            raise ValueError("frozen runtime changed")
    wheel = old_report["native_wheel"]
    if digest(wheel["path"]) != final["abi9_native_wheel_sha256"]:
        raise ValueError("frozen native wheel changed")
    source = extract_frozen_source()
    ROOT.mkdir(parents=True)
    configs = {}
    for horizon in HORIZONS:
        for seed in SEEDS:
            path = ROOT / f"configs/h{horizon}-s{seed}.json"
            write(path, make_wrapper(base, horizon, seed))
            configs[f"{horizon}:{seed}"] = {"path": str(path), "sha256": digest(path)}
    architecture = SOURCE / "ZAC_zzx/hardware_spec/full_architecture.json"
    model = SOURCE / "ZAC_zzx/evaluation/fidelity_model_zac.json"
    reference = read(reuse[0]["manifest"])
    for label, path in (("architecture", architecture), ("model", model)):
        if digest(path) != reference[label + "_sha256"]:
            raise ValueError(f"frozen {label} differs from accepted experiments")
    quality = missing_quality_jobs(circuits)
    timing, warmups = timing_jobs(circuits)
    protocol = {"protocol_id": PROTOCOL, "sealed_at": utc_now(), "formal_paper_result": False,
        "cohort_selection": "unchanged accepted stratified twelve; never selected by new outcomes",
        "circuits": circuits, "horizons": list(HORIZONS), "seeds": list(SEEDS),
        "quality_jobs": quality, "reused_quality": reuse, "quality_workers": workers,
        "timing_jobs": timing, "timing_warmups": warmups, "timing_workers": 1,
        "timing_requires_explicit_separate_authorization": True,
        "timing_definition": "full_compile_ns and transition_decision_ns from isolated sequential ABI9 runs; three repetitions, all six warmups excluded; never pool historical concurrent wall time",
        "quality_aggregation": "median across three valid seeds within each circuit, then geometric mean of paired fidelity ratios; retain all planned circuits and incomplete/model-domain outcomes",
        "failure_policy": "600 s per attempt; no automatic retry, substitution, or outcome-dependent changes; incomplete blocks excluded only from paired aggregates and retained in coverage tables",
        "window_semantics": "visible_depth_counts measures available future offsets, not successful candidate replay depth; report separately from configured and effective caps",
        "source_reports": reports, "reference_parity": parity, "configs": configs,
        "native": native, "wheel": wheel, "python": sys.executable,
        "frozen_source_commit": FROZEN_COMMIT, "frozen_source_root": str(SOURCE),
        "frozen_source_files": source, "frozen_source_sha256": stable_hash(source),
        "extension_script": str(Path(__file__).resolve()), "extension_script_sha256": digest(__file__),
        "execution_plan": {"path": str(REPO / "ZAC_zzx/experiments_v2/experiment_plan_v2.json"),
                           "sha256": digest(REPO / "ZAC_zzx/experiments_v2/experiment_plan_v2.json")},
        "architecture": {"path": str(architecture), "sha256": digest(architecture)},
        "model": {"path": str(model), "sha256": digest(model)},
        "environment": ENV, "timeout_seconds": 600, "rss_limit_bytes": 3 * 1024**3,
        "root_checkout_provenance_is_not_compiler_source": True}
    bootstrap(protocol)
    from experiments_v2.ablation import validate_ablation_config
    for item in configs.values():
        validate_ablation_config(read(item["path"]))
    write(ROOT / "protocol.json", protocol)
    write(ROOT / "protocol.sha256.json", {"sha256": digest(ROOT / "protocol.json")})
    reanalyze(protocol)
    return {"root": str(ROOT), "sha256": digest(ROOT / "protocol.json"),
            "new_quality_jobs": len(quality), "reused_quality_jobs": len(reuse),
            "timing_jobs": len(timing), "warmups": len(warmups)}


def verify_protocol():
    protocol = checked_json(ROOT / "protocol.json", read(ROOT / "protocol.sha256.json")["sha256"])
    if protocol["protocol_id"] != PROTOCOL or digest(__file__) != protocol["extension_script_sha256"]:
        raise ValueError("extension protocol/script changed after sealing")
    if source_snapshot() != protocol["frozen_source_files"]:
        raise ValueError("frozen compiler source changed")
    if sys.executable != protocol["python"]:
        raise ValueError("wrong execution interpreter")
    for prefix in ("paper_native_python", "paper_native_extension"):
        if digest(protocol["native"][prefix + "_path"]) != protocol["native"][prefix + "_sha256"]:
            raise ValueError("execution runtime changed")
    for item in [*protocol["configs"].values(), protocol["architecture"], protocol["model"],
                 protocol["wheel"], protocol["execution_plan"]]:
        if digest(item["path"]) != item["sha256"]:
            raise ValueError("sealed input/config/runtime changed")
    for row in protocol["circuits"]:
        if digest(row["canonical"]["canonical_path"]) != row["canonical"]["canonical_sha256"]:
            raise ValueError("canonical input changed")
    return protocol


def quality_record(manifest, *, horizon, seed, source, source_sha256):
    return {"dataset": manifest["dataset"], "circuit": manifest["circuit"],
        "horizon": horizon, "seed": seed, "status": manifest["status"],
        "fidelity": manifest.get("fidelity"), "fidelity_ood": manifest.get("fidelity_ood"),
        "move_batches": manifest.get("move_batches"), "transfers": manifest.get("transfers"),
        "idle_exposures": manifest.get("idle_exposures"), "move_time_us": manifest.get("move_time_us"),
        "full_compile_ns": manifest.get("full_compile_ns"),
        "process_cpu_ns": manifest.get("cpu_time_ns"),
        "forecast_summary": manifest.get("forecast_summary", {}),
        "source": source, "source_sha256": source_sha256,
        "old_timing_not_reused_as_new_measurement": True}


def aggregate_quality(records, circuits, seeds):
    index = {(r["dataset"], r["circuit"], r["horizon"], r["seed"]): r for r in records}
    if len(index) != len(records):
        raise ValueError("duplicate quality identity")
    ratios = {h: [] for h in HORIZONS}
    complete, excluded, circuit_values = [], [], []
    for c in circuits:
        key = (c["dataset"], c["circuit"])
        values, reason = {}, []
        for h in HORIZONS:
            block = [index.get(key + (h, seed)) for seed in seeds]
            if all(r and r["status"] == "success" and r["fidelity"] is not None and
                   math.isfinite(r["fidelity"]) and r["fidelity"] > 0 for r in block):
                values[h] = statistics.median(r["fidelity"] for r in block)
            else:
                reason.append(h)
        circuit_values.append({"dataset": key[0], "circuit": key[1], "median_fidelity": values})
        if reason:
            excluded.append({"dataset": key[0], "circuit": key[1], "incomplete_horizons": reason})
            continue
        complete.append(key)
        for h in HORIZONS:
            ratios[h].append(values[h] / values[8])
    return {"planned_circuits": len(circuits), "complete_circuits": len(complete),
            "excluded": excluded, "per_circuit": circuit_values,
            "fidelity_ratio_vs_h8": {str(h): math.exp(statistics.mean(map(math.log, v))) if v else None
                                     for h, v in ratios.items()},
            "seeds": list(seeds), "timing_aggregation": "not pooled"}


def reanalyze(protocol):
    records = []
    for item in protocol["reused_quality"]:
        manifest = checked_json(item["manifest"], item["manifest_sha256"])
        records.append(quality_record(manifest, horizon=item["horizon"], seed=item["seed"],
            source=item["manifest"], source_sha256=item["manifest_sha256"]))
    result = {"protocol_sha256": digest(ROOT / "protocol.json"), "records": records,
              "reference_parity": protocol["reference_parity"],
              "visible_window_is_not_completed_rollout_depth": True}
    # H1 is deliberately absent in this pre-run analysis; do not manufacture it.
    result["seed0_fidelity_ratios_vs_h8"] = {}
    lookup = {(r["dataset"], r["circuit"], r["horizon"]): r for r in records if r["seed"] == 0}
    for h in (0, 2, 4, 8):
        pairs = [(lookup[(c["dataset"], c["circuit"], h)],
                  lookup[(c["dataset"], c["circuit"], 8)]) for c in protocol["circuits"]]
        values = [a["fidelity"] / b["fidelity"] for a, b in pairs if a["fidelity"] and b["fidelity"]]
        result["seed0_fidelity_ratios_vs_h8"][str(h)] = {
            "n": len(values), "geometric_mean": math.exp(statistics.mean(map(math.log, values)))}
    write(ROOT / "existing_data_reanalysis.json", result)


def run_one(protocol, job, phase):
    from experiments_v2.cli import UnifiedEvaluationGate, _attempt_spec
    from experiments_v2.contracts import CanonicalCircuitManifest
    from experiments_v2.plan import load_experiment_plan
    from experiments_v2.runner import run_attempt
    plan = load_experiment_plan(protocol["execution_plan"]["path"])
    plan = replace(plan, architecture_path=Path(protocol["architecture"]["path"]),
                   model_path=Path(protocol["model"]["path"]), python=protocol["python"],
                   output_root=ROOT / "runs" / phase)
    meta = next(c for c in protocol["circuits"] if
                (c["dataset"], c["circuit"]) == (job["dataset"], job["circuit"]))
    canonical = CanonicalCircuitManifest(**meta["canonical"])
    config = protocol["configs"][f"{job['horizon']}:{job['seed']}"]
    variant = f"horizon_extension_h{job['horizon']}_ga"
    spec = _attempt_spec(plan, plan.datasets[job["dataset"]], canonical, "M4", job["seed"],
        job["repetition"], "ablation", config_path=Path(config["path"]),
        ablation_variant=variant, experiment_id=stable_hash([PROTOCOL, phase, digest(ROOT / "protocol.json")]))
    command = [protocol["python"], protocol["extension_script"], "--worker", "--",
               *list(spec.command)[3:]]
    spec = replace(spec, command=command, output_root=ROOT / "runs" / phase / job["job_id"],
        require_clean_git=False, timeout_seconds=protocol["timeout_seconds"],
        rss_limit_bytes=protocol["rss_limit_bytes"],
        concurrency_limit=protocol["quality_workers"] if phase == "quality" else 1,
        environment={**ENV, "PYTHONPATH": str(SOURCE / "ZAC_zzx")}, cwd=SOURCE / "ZAC_zzx",
        package_versions={**spec.package_versions, **protocol["native"],
            "horizon_extension_protocol": PROTOCOL, "horizon_extension_phase": phase,
            "horizon_frozen_source_commit": FROZEN_COMMIT,
            "horizon_frozen_source_sha256": protocol["frozen_source_sha256"],
            "paper_search_policy": "ga", "compiler_python_path": protocol["python"]})
    gate = UnifiedEvaluationGate(plan, canonical, "M4")
    manifest = run_attempt(spec, verifier=gate.verifier, scorer=gate.scorer)
    path = Path(manifest.artifact_dir) / "manifest.json"
    return {**job, "phase": phase, "status": manifest.status,
            "manifest": str(path), "manifest_sha256": digest(path)}


def execute_quality(protocol):
    write(ROOT / "quality_started.json", {"at": utc_now(), "protocol_sha256": digest(ROOT / "protocol.json")})
    existing = read(ROOT / "existing_data_reanalysis.json")["records"]
    receipts = []
    with ThreadPoolExecutor(max_workers=protocol["quality_workers"]) as pool:
        futures = {pool.submit(run_one, protocol, job, "quality"): job for job in protocol["quality_jobs"]}
        for future in as_completed(futures):
            job = futures[future]
            try:
                receipt = future.result()
            except Exception as error:
                receipt = {**job, "phase": "quality", "status": "runner_error", "error": repr(error)}
            write(ROOT / "receipts/quality" / f"{job['job_id']}.json", receipt)
            receipts.append(receipt)
            print(f"[{len(receipts)}/84] {job['job_id']}: {receipt['status']}", flush=True)
    records = list(existing)
    for item in receipts:
        if "manifest" in item:
            manifest = checked_json(item["manifest"], item["manifest_sha256"])
            records.append(quality_record(manifest, horizon=item["horizon"], seed=item["seed"],
                source=item["manifest"], source_sha256=item["manifest_sha256"]))
    summary = aggregate_quality(records, protocol["circuits"], SEEDS)
    summary.update(receipts=receipts, records=records,
                   protocol_sha256=digest(ROOT / "protocol.json"), completed_at=utc_now())
    write(ROOT / "quality_summary.json", summary)
    return {"complete_circuits": summary["complete_circuits"],
            "fidelity_ratio_vs_h8": summary["fidelity_ratio_vs_h8"]}


def execute_timing(protocol, authorized):
    if not authorized:
        raise ValueError("strict timing requires separate explicit serial authorization")
    write(ROOT / "timing_started.json", {"at": utc_now(), "protocol_sha256": digest(ROOT / "protocol.json")})
    receipts = []
    for phase, jobs in (("timing_warmup", protocol["timing_warmups"]), ("timing", protocol["timing_jobs"])):
        for job in jobs:
            item = run_one(protocol, job, phase)
            write(ROOT / "receipts" / phase / f"{job['job_id']}.json", item)
            receipts.append(item)
            print(f"{phase}: {job['job_id']} {item['status']}", flush=True)
        if phase == "timing_warmup" and any(r["status"] != "success" for r in receipts):
            write(ROOT / "timing_warmup_failed.json", receipts)
            raise RuntimeError("warmup gate failed; formal timing not started")
    summary = {"receipts": receipts, "warmups_excluded": 6, "formal_runs": 108,
               "protocol_sha256": digest(ROOT / "protocol.json"), "per_circuit": [],
               "timing_scope": "new serial phase only"}
    ratios = {h: [] for h in (2, 4, 8)}
    for c in protocol["circuits"]:
        times = {}
        for h in (2, 4, 8):
            block = [r for r in receipts if r["phase"] == "timing" and
                     (r["dataset"], r["circuit"], r["horizon"]) == (c["dataset"], c["circuit"], h)]
            if len(block) == 3 and all(r["status"] == "success" for r in block):
                values = [checked_json(r["manifest"], r["manifest_sha256"])["full_compile_ns"] / 1e9 for r in block]
                times[h] = {"median_s": statistics.median(values), "min_s": min(values), "max_s": max(values)}
        summary["per_circuit"].append({"dataset": c["dataset"], "circuit": c["circuit"], "times": times})
        if len(times) == 3:
            for h in times:
                ratios[h].append(times[h]["median_s"] / times[8]["median_s"])
    summary["paired_time_ratios_vs_h8"] = {str(h): {"n": len(v), "geometric_mean":
        math.exp(statistics.mean(map(math.log, v))) if v else None} for h, v in ratios.items()}
    write(ROOT / "timing_summary.json", summary)
    return summary["paired_time_ratios_vs_h8"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--seal", action="store_true")
    modes.add_argument("--quality", action="store_true")
    modes.add_argument("--timing", action="store_true")
    modes.add_argument("--worker", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--serial-authorized", action="store_true")
    args, extra = parser.parse_known_args(argv)
    if args.seal:
        result = seal(args.workers)
    else:
        protocol = verify_protocol()
        bootstrap(protocol)
        if args.worker:
            from experiments_v2.method_driver import main as compile_main
            compile_main(extra[1:] if extra[:1] == ["--"] else extra)
            return
        result = execute_quality(protocol) if args.quality else execute_timing(protocol, args.serial_authorized)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
