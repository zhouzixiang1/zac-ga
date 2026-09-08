"""Parallel missing-coverage extension of the sealed initialization pilot.

The target is the 39 complete dynamic-H0/H8 analysis circuits. Reuse seven
pilot circuit identities by source link, and run only the 32 missing circuits.
The original pilot, including its Ising timeout, is never changed or retried.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from experiments_v2.initial_lookahead_batch import (ARMS, DEFAULT_ARCH, DEFAULT_CONFIG,
    collect, summarize_records, utc_now)
from experiments_v2.initial_lookahead_runner import REPO, file_hash, source_snapshot, write_json
from zzx.native_backend import build_info


PROTOCOL_ID = "initial-physical-prefix-dynamic39-missing32-v1"
PILOT = REPO / "ZAC_zzx/results/initial_lookahead_v1/nine-family-h0-h2-sa-k4-rho07-b32-seed012-v1"
PILOT_PROTOCOL_SHA = "3f99d0207dcbf87ea44b12810bfeac0f5272f544013d72dffa9802a7342f8f69"
PILOT_SUMMARY_SHA = "c4c020e9fb0bf5b4cd8af2027e761df45db28eb1edad7aac7121009470b12477"
DYNAMIC_REPORT = REPO / "fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1/reports/run-paper-ablation.json"
DYNAMIC_TABLE = REPO / "ZAC_zzx/results/paper_zh_v2/ablation.csv"
EXPECTED_NEW = {
    "bv_n19_transpiled", "bv_n30_transpiled", "bv_n70_transpiled", "cat_n22_transpiled",
    "cat_n35_transpiled", "ghz_n40_transpiled", "ghz_n78_transpiled", "ising_n98_transpiled",
    "knn_n31_transpiled", "multiply_n13_transpiled", "qft_n29_transpiled", "seca_n11_transpiled",
    "swap_test_n25_transpiled", "4gt10-v1_81", "4gt11_84", "4gt5_76", "4mod7-v0_94",
    "alu-bdd_288", "cm152a_212", "con1_216", "ground_state_estimation_10", "majority_239",
    "one-two-three-v0_98", "qe_qft_4", "rd32_270", "rd53_130", "rd53_251", "rd73_252",
    "sf_276", "sqrt8_260", "squar5_261", "z4_268",
}


def cohort_definition(rows, design, pilot):
    """Use prior coverage/domain flags only, never new initialization outcomes."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row["circuit"], {})[row["variant"]] = row
    names = list(design["cohort"]["datasets"]["zac18"]["circuits"])
    names += [name for stratum in design["cohort"]["datasets"]["qmap154"]["strata"].values()
              for name in stratum["circuits"]]
    if len(names) != 48 or len(set(names)) != 48 or set(names) != set(grouped):
        raise ValueError("the prespecified ZAC18 + stratified QMAP30 design changed")
    complete, excluded = [], []
    for name in names:
        pair = [grouped[name][variant] for variant in ("paper_h0_ga", "paper_h8_ga")]
        if all(row["status"] == "success" and row["valid"] == row["fidelity_valid"] == "3"
               and row["N"] == row["fidelity_N"] == "3" for row in pair):
            complete.append(name)
        else:
            excluded.append({"circuit": name,
                "reason": "prior_dynamic_timeout" if any(row["status"] == "timeout" for row in pair)
                          else "prior_dynamic_linear_fidelity_out_of_domain",
                "prior_coverage": [{key: row[key] for key in
                    ("variant", "status", "valid", "N", "fidelity_valid", "fidelity_N")} for row in pair]})
    old = {row["circuit"] for row in pilot["circuits"]}
    new = set(complete) - old
    if len(complete) != 39 or new != EXPECTED_NEW:
        raise ValueError("the audited complete cohort or the fixed missing-32 list changed")
    return {"target": complete, "new": [name for name in complete if name in new],
            "reused": [name for name in complete if name in old],
            "pilot_outside_target": sorted(old - set(complete)), "excluded": excluded}


def protocol_payload(workers=3):
    if workers not in (2, 3):
        raise ValueError("the audited resource plan supports two or three workers")
    if file_hash(PILOT / "protocol.json") != PILOT_PROTOCOL_SHA or file_hash(PILOT / "summary.json") != PILOT_SUMMARY_SHA:
        raise ValueError("pilot evidence changed")
    pilot = json.loads((PILOT / "protocol.json").read_text())
    design = json.loads(DYNAMIC_REPORT.read_text())
    with DYNAMIC_TABLE.open() as stream:
        rows = list(csv.DictReader(stream))
    cohort = cohort_definition(rows, design, pilot)
    inventory = {}
    for dataset in ("zac18", "qmap154"):
        with (REPO / f"ZAC_zzx/results/paper_zh_v2/{dataset}.csv").open() as stream:
            for row in csv.DictReader(stream):
                inventory[row["circuit"]] = {key: row[key] for key in
                    ("dataset", "circuit", "qubits", "gates_1q", "gates_2q", "canonical_sha256")}
    circuits = []
    for name in cohort["new"]:
        meta = inventory[name]
        path = REPO / f"fidelity-lookahead-v2/artifacts/canonical/{meta['dataset']}/{name}.qasm"
        if file_hash(path) != meta["canonical_sha256"]:
            raise ValueError(f"canonical input hash mismatch: {name}")
        circuits.append({"circuit": name, "dataset": meta["dataset"], "input": str(path),
            "input_sha256": meta["canonical_sha256"],
            **{key: int(meta[key]) for key in ("qubits", "gates_1q", "gates_2q")}})
    setting = json.loads(DEFAULT_CONFIG.read_text())["base_config"]["zac_setting"][0]
    native = build_info(require_registered_wheel=True,
                        expected_wheel_sha256=setting["native_wheel_sha256"])
    return {"protocol_id": PROTOCOL_ID, "sealed_at_utc": utc_now(), "formal_paper_result": False,
        "circuits": circuits, "seeds": [0, 1, 2], "arms": list(ARMS), "horizons": [0, 2],
        "candidates": 4, "rho": .7, "rollout_evaluations": 32, "dynamic_horizon": 8,
        "workers": workers, "timeout_per_paired_job_s": 600, "max_rss_per_job_bytes": 3 * 1024**3,
        "deadline_semantics": "600 seconds wall time from each subprocess start, including SA, both selections and all three full compiles; queue wait excluded. Parallel contention is part of observed wall time.",
        "timing_policy": "New jobs run concurrently in isolated single-threaded processes. Do not pool new wall times with the serial pilot or historical dynamic runs into a compilation-speed conclusion. Report wall and process CPU separately where available.",
        "memory_policy": "At most three workers; each subprocess is checked every two seconds and terminated above 3 GiB RSS. Resource-limit outcomes remain failures, with no replacement.",
        "selection_policy": pilot["selection_policy"], "information_boundary": pilot["information_boundary"],
        "aggregation": "Average seed log fidelity and physical metrics within each circuit, then summarize circuits. New-phase timing stays separate. Link seven prior circuits (including their missing Ising seed) to describe the fixed target of 39 without rerunning them. Keep the two other pilot circuits separate.",
        "reference_policy": "Re-form SA_reference in each new paired run. The read-only audit found matching input/config/native/dynamic/RNG behavior in three representative old circuits, but new SA mappings are not yet sealed and no all-target trace-reuse certificate exists. No historical full result is used to choose a mapping or reported as a new timing measurement.",
        "reference_reuse_audit": {"representatives": ["bv_n19_transpiled", "qft_n29_transpiled", "cm152a_212"],
            "old_runtime_commit": "e85b29514caea66c31d305fbb421fd27c2ab1667",
            "known_behavior_difference_found": False, "reuse_enabled": False},
        "sa_seed_semantics": "The inherited SA initializer fixes its own global RNG seed to 0. Trial seeds 0/1/2 control candidate perturbations and the local dynamic-search RNG, not three independent SA random restarts.",
        "failure_policy": "All 96 planned jobs retained, including timeout, resource, physics, model-domain and source failures. No retry, substitution or outcome-based parameter changes.",
        "cohort": cohort, "pilot_root": str(PILOT), "pilot_protocol_sha256": PILOT_PROTOCOL_SHA,
        "pilot_summary_sha256": PILOT_SUMMARY_SHA,
        "dynamic_design_path": str(DYNAMIC_REPORT), "dynamic_design_sha256": file_hash(DYNAMIC_REPORT),
        "dynamic_table_path": str(DYNAMIC_TABLE), "dynamic_table_sha256": file_hash(DYNAMIC_TABLE),
        "config_path": str(DEFAULT_CONFIG), "config_sha256": file_hash(DEFAULT_CONFIG),
        "architecture_path": str(DEFAULT_ARCH), "architecture_sha256": file_hash(DEFAULT_ARCH),
        "python": sys.executable, "python_realpath": str(Path(sys.executable).resolve()),
        "python_sha256": file_hash(sys.executable), "python_version": sys.version, "native": native,
        "repository": source_snapshot(),
        "environment": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1",
                        "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"},
        "job_order": [{"circuit": row["circuit"], "seed": seed, "job_id": f"{row['circuit']}-s{seed}"}
                      for row in circuits for seed in (0, 1, 2)]}


def resident_bytes(pid):
    output = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(output.stdout.strip()) * 1024 if output.returncode == 0 and output.stdout.strip() else 0


def run_job(root, protocol, job, inputs, stop):
    if stop.is_set():
        receipt = {**job, "status": "cancelled_source_change", "elapsed_s": 0}
        write_json(root / "receipts" / f"{job['job_id']}.json", receipt)
        return receipt
    command = [sys.executable, "-m", "experiments_v2.initial_lookahead_runner",
        "--input", inputs[job["circuit"]], "--config", protocol["config_path"],
        "--architecture", protocol["architecture_path"], "--root", str(root / "jobs" / job["job_id"]),
        "--horizons", "0", "2", "--seed", str(job["seed"]), "--candidates", "4", "--rho", "0.7",
        "--rollout-evaluations", "32"]
    env = {**os.environ, **protocol["environment"], "PYTHONPATH": str(REPO / "ZAC_zzx")}
    receipt = {**job, "command": command, "started_at_utc": utc_now(), "workers": protocol["workers"],
               "deadline_s": protocol["timeout_per_paired_job_s"], "max_observed_rss_bytes": 0}
    started = time.monotonic()
    with (root / "receipts" / f"{job['job_id']}.log").open("x") as stream:
        process = subprocess.Popen(command, cwd=REPO, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        while True:
            code = process.poll()
            if code is not None:
                receipt.update(status="success" if code == 0 else "failed", returncode=code)
                break
            elapsed = time.monotonic() - started
            rss = resident_bytes(process.pid)
            receipt["max_observed_rss_bytes"] = max(receipt["max_observed_rss_bytes"], rss)
            limit = ("timeout" if elapsed >= protocol["timeout_per_paired_job_s"] else
                     "memory_limit" if rss > protocol["max_rss_per_job_bytes"] else
                     "cancelled_source_change" if stop.is_set() else None)
            if limit:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                receipt.update(status=limit, returncode=process.returncode)
                break
            try:
                process.wait(timeout=min(2., max(.01, protocol["timeout_per_paired_job_s"] - elapsed)))
            except subprocess.TimeoutExpired:
                pass
    receipt.update(elapsed_s=time.monotonic() - started, completed_at_utc=utc_now())
    stages = list((root / "jobs" / job["job_id"]).glob("stage_started_*.json"))
    if stages:
        receipt["last_started_stage"] = max((json.loads(path.read_text()) for path in stages),
                                           key=lambda row: row["monotonic_ns"])["stage"]
    write_json(root / "receipts" / f"{job['job_id']}.json", receipt)
    return receipt


def execute(root, protocol):
    if protocol["protocol_id"] != PROTOCOL_ID or len(protocol["job_order"]) != 96:
        raise ValueError("wrong extension protocol")
    if source_snapshot()["source_sha256"] != protocol["repository"]["source_sha256"]:
        raise RuntimeError("source changed after seal")
    if str(Path(sys.executable).resolve()) != protocol["python_realpath"] or file_hash(sys.executable) != protocol["python_sha256"]:
        raise RuntimeError("runtime changed after seal")
    for field in ("config", "architecture", "dynamic_design", "dynamic_table"):
        if file_hash(protocol[f"{field}_path"]) != protocol[f"{field}_sha256"]:
            raise RuntimeError(f"{field} changed after seal")
    for row in protocol["circuits"]:
        if file_hash(row["input"]) != row["input_sha256"]:
            raise RuntimeError("input changed after seal")
    write_json(root / "execution_started.json", {"at_utc": utc_now(), "workers": protocol["workers"],
               "protocol_sha256": file_hash(root / "protocol.json")})
    (root / "jobs").mkdir(); (root / "receipts").mkdir()
    stop = threading.Event(); started = time.monotonic(); receipts = []
    inputs = {row["circuit"]: row["input"] for row in protocol["circuits"]}
    with ThreadPoolExecutor(max_workers=protocol["workers"]) as pool:
        futures = [pool.submit(run_job, root, protocol, job, inputs, stop) for job in protocol["job_order"]]
        for index, future in enumerate(as_completed(futures), 1):
            receipt = future.result(); receipts.append(receipt)
            print(f"[{index}/96] {receipt['job_id']}: {receipt['status']} ({receipt['elapsed_s']:.1f}s)", flush=True)
            if source_snapshot()["source_sha256"] != protocol["repository"]["source_sha256"]:
                stop.set()
    records = collect(root, protocol)
    report = summarize_records(protocol, records)
    report.update(source_stable=not stop.is_set(), protocol_sha256=file_hash(root / "protocol.json"),
        execution_wall_s=time.monotonic() - started, workers=protocol["workers"],
        execution_phase="parallel_extension_only", timing_not_pooled_with_serial_pilot=True,
        job_statuses={status: sum(r["status"] == status for r in receipts) for status in
            ("success", "failed", "timeout", "memory_limit", "cancelled_source_change")},
        max_observed_worker_rss_bytes=max(r.get("max_observed_rss_bytes", 0) for r in receipts),
        input_ledger_mismatch_jobs=[f"{c}-s{s}" for (c, s), r in records.items() if r.get("input_ledger_mismatch")],
        physical_checks={"completed_arms": sum(arm in r for r in records.values() for arm in ARMS),
            "valid_arms": sum(r[arm]["validation"]["ok"] and r[arm]["validation"]["ghost_hits"] == 0
                              for r in records.values() for arm in ARMS if arm in r),
            "ood_arms": sum(r[arm]["score"]["ood"] for r in records.values() for arm in ARMS if arm in r)},
        initialization_checks={f"{c}-s{s}": r.get("initialization_audit") for (c, s), r in records.items()},
        pilot_link={"root": protocol["pilot_root"], "protocol_sha256": PILOT_PROTOCOL_SHA,
            "summary_sha256": PILOT_SUMMARY_SHA, "target39_reused_circuits": protocol["cohort"]["reused"],
            "pilot_outside_target": protocol["cohort"]["pilot_outside_target"], "old_timeout_preserved": True})
    write_json(root / "summary.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workers", default=3, type=int)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--seal-only", action="store_true"); modes.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv); root = args.root.resolve()
    allowed = (REPO / "ZAC_zzx/results/initial_lookahead_v1",
               REPO / "IEEE_conference_template/build/initial-lookahead")
    if not any(parent in root.parents for parent in allowed):
        raise ValueError("new extension root must be below an allowed result directory")
    if args.seal_only:
        protocol = protocol_payload(args.workers)
        root.mkdir(parents=True, exist_ok=False); write_json(root / "protocol.json", protocol)
        print(json.dumps({"root": str(root), "protocol_sha256": file_hash(root / "protocol.json"),
                          "new_circuits": len(protocol["circuits"]), "jobs": len(protocol["job_order"])}, indent=2))
    else:
        report = execute(root, json.loads((root / "protocol.json").read_text()))
        print(json.dumps({"job_statuses": report["job_statuses"], "overall": report["overall"]}, indent=2))


if __name__ == "__main__":
    main()
