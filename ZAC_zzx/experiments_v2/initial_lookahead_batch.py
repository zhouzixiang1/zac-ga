"""Predeclared nine-circuit initialization-only development experiment.

Run ``--seal-only`` first, then ``--execute`` with the same root. The protocol
and all job receipts are immutable. A timeout retains every completed arm and
does not trigger a retry or a replacement circuit. This is not paper_zh_v2.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

from experiments_v2.initial_lookahead_runner import REPO, file_hash, source_snapshot, write_json
from zzx.initial_lookahead import stable_hash
from zzx.native_backend import build_info


PROTOCOL_ID = "initial-physical-prefix-nine-family-seed3-v1"
SEEDS = (0, 1, 2)
ARMS = ("sa_reference", "h0", "h2")
CIRCUITS = (
    ("zac18", "bv_n14_transpiled", "Bernstein-Vazirani"),
    ("zac18", "ghz_n23", "GHZ state preparation"),
    ("zac18", "wstate_n27_transpiled", "W-state preparation"),
    ("zac18", "ising_n42", "Ising simulation"),
    ("zac18", "qft_n18_transpiled", "quantum Fourier transform"),
    ("qmap154", "ex1_226", "small reversible benchmark"),
    ("qmap154", "cm82a_208", "medium reversible benchmark"),
    ("qmap154", "hwb5_53", "hidden weighted bit"),
    ("qmap154", "adr4_197", "adder benchmark"),
)
DEFAULT_CONFIG = REPO / "fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1/configs/ablation/h8-seed0.json"
DEFAULT_ARCH = REPO / "ZAC_zzx/hardware_spec/full_architecture.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def protocol_payload(config_path, architecture_path):
    """Select by named family/input size; never read fidelity/quality columns."""
    config_path, architecture_path = Path(config_path).resolve(), Path(architecture_path).resolve()
    metadata = {}
    for dataset in {entry[0] for entry in CIRCUITS}:
        inventory = REPO / f"ZAC_zzx/results/paper_zh_v2/{dataset}.csv"
        with inventory.open() as stream:
            for row in csv.DictReader(stream):
                metadata[dataset, row["circuit"]] = {
                    key: row[key] for key in ("qubits", "gates_1q", "gates_2q", "canonical_sha256")}
    circuits = []
    for dataset, circuit, family in CIRCUITS:
        path = REPO / f"fidelity-lookahead-v2/artifacts/canonical/{dataset}/{circuit}.qasm"
        row = metadata[dataset, circuit]
        if file_hash(path) != row["canonical_sha256"]:
            raise ValueError(f"canonical input hash mismatch: {circuit}")
        circuits.append({"dataset": dataset, "circuit": circuit, "family": family,
                         "input": str(path), "input_sha256": file_hash(path),
                         **{key: int(row[key]) for key in ("qubits", "gates_1q", "gates_2q")}})
    payload = json.loads(config_path.read_text())
    setting = payload["base_config"]["zac_setting"][0]
    native = build_info(require_registered_wheel=True,
                        expected_wheel_sha256=setting["native_wheel_sha256"])
    return {
        "protocol_id": PROTOCOL_ID, "sealed_at_utc": utc_now(), "formal_paper_result": False,
        "selection_basis": "Five ZAC algorithm families plus four size-diverse circuits from the old initial-placement validation list; no new quality result was used.",
        "scope": "Bounded development sample, not a random or exhaustive benchmark evaluation.",
        "circuits": circuits, "seeds": list(SEEDS), "arms": list(ARMS),
        "horizons": [0, 2], "candidates": 4, "rho": .7, "rollout_evaluations": 32,
        "dynamic_horizon": 8, "timeout_per_paired_job_s": 600,
        "timeout_policy": "600 seconds for the complete circuit-seed paired subprocess, including SA, both selections, and all three full compiles; preserve partial arms, no retries.",
        "selection_policy": "One SA mapping; identical SA-plus-swap candidate pool; seal both horizon choices before any full compile; no full-result selection.",
        "information_boundary": "Both arms retain the common full-input SA interaction prior. Only additional ordered physical-prefix evaluation differs. Inner rollout policy is H=0.",
        "timing_policy": "Serial jobs; end-to-end per arm = one shared SA time + own selection time + own full solve time. SA time is accounted in every arm, not summed across arms.",
        "aggregation": "Average each circuit's seed log fidelity, batches and time first. Across complete circuits use geometric fidelity and arithmetic batches/time. Paired fidelity gains are exp(mean circuit log difference)-1; batch reductions are ratios of arithmetic means. Seeds are repetitions, not independent circuits.",
        "failure_policy": "Report every planned circuit and seed; primary paired summaries require all three seeds and arms physically valid, with finite in-domain log fidelity. Report incomplete/OOD cases separately without substitutions.",
        "config_path": str(config_path), "config_sha256": file_hash(config_path),
        "architecture_path": str(architecture_path), "architecture_sha256": file_hash(architecture_path),
        "python": sys.executable, "python_realpath": str(Path(sys.executable).resolve()),
        "python_sha256": file_hash(sys.executable), "python_version": sys.version,
        "native": native, "repository": source_snapshot(),
        "environment": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
                        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
        "job_order": [{"circuit": circuit, "seed": seed, "job_id": f"{circuit}-s{seed}"}
                      for _, circuit, _ in CIRCUITS for seed in SEEDS],
    }


def valid_result(result):
    score = result.get("score", {})
    logf = score.get("log_fidelity")
    return (result.get("status") == "success" and result.get("validation", {}).get("ok") is True
            and result["validation"].get("ghost_hits") == 0 and not score.get("ood", True)
            and isinstance(logf, (int, float)) and math.isfinite(logf))


def summarize_records(protocol, records):
    """Circuit is the analysis unit; incomplete circuits remain in the report."""
    per_circuit = []
    for entry in protocol["circuits"]:
        circuit = entry["circuit"]
        jobs = [records.get((circuit, seed), {}) for seed in protocol["seeds"]]
        complete = all(job.get("accepted", False) and all(valid_result(job.get(arm, {}))
                       for arm in ARMS) for job in jobs)
        row = {**entry, "complete": complete, "planned_seeds": protocol["seeds"],
               "job_statuses": [job.get("status", "missing") for job in jobs], "arms": {}}
        for arm in ARMS:
            available = [job[arm] for job in jobs if arm in job]
            valid = [result for result in available if valid_result(result)]
            arm_row = {"completed_runs": len(available), "valid_runs": len(valid),
                       "selected_candidates": [result.get("selected_candidate") for result in available]}
            if valid:
                arm_row.update(
                    mean_log_fidelity=statistics.mean(r["score"]["log_fidelity"] for r in valid),
                    mean_move_batches=statistics.mean(r["score"]["move_batches"] for r in valid),
                    mean_move_time_us=statistics.mean(r["score"]["move_time_us"] for r in valid),
                    mean_sa_s=statistics.mean(r["sa_initialization_ns"] / 1e9 for r in valid),
                    mean_selection_s=statistics.mean(r["selection_ns"] / 1e9 for r in valid),
                    mean_full_compile_s=statistics.mean(r["compile_with_selected_mapping_ns"] / 1e9 for r in valid),
                    mean_end_to_end_s=statistics.mean(r["end_to_end_ns"] / 1e9 for r in valid))
                arm_row["geometric_fidelity"] = math.exp(arm_row["mean_log_fidelity"])
            row["arms"][arm] = arm_row
        per_circuit.append(row)
    cohort = [row for row in per_circuit if row["complete"]]
    overall = {"planned_circuits": len(per_circuit), "complete_circuits": len(cohort),
               "analysis_unit": "circuit (three seeds averaged within circuit)", "arms": {}, "comparisons": {}}
    if cohort:
        for arm in ARMS:
            values = [row["arms"][arm] for row in cohort]
            means = {key: statistics.mean(value[key] for value in values)
                     for key in values[0] if key.startswith("mean_")}
            means["geometric_fidelity"] = math.exp(means["mean_log_fidelity"])
            overall["arms"][arm] = means
        for treatment, control in (("h2", "h0"), ("h0", "sa_reference"), ("h2", "sa_reference")):
            differences = [row["arms"][treatment]["mean_log_fidelity"] -
                           row["arms"][control]["mean_log_fidelity"] for row in cohort]
            batch_differences = [row["arms"][control]["mean_move_batches"] -
                                 row["arms"][treatment]["mean_move_batches"] for row in cohort]
            def wtl(values):
                return {"wins": sum(v > 1e-12 for v in values),
                        "ties": sum(abs(v) <= 1e-12 for v in values),
                        "losses": sum(v < -1e-12 for v in values)}
            ta, ca = overall["arms"][treatment], overall["arms"][control]
            overall["comparisons"][f"{treatment}_vs_{control}"] = {
                "fidelity_relative_gain_percent": math.expm1(statistics.mean(differences)) * 100,
                "move_batches_reduction_percent": 100 * (1 - ta["mean_move_batches"] / ca["mean_move_batches"]),
                "end_to_end_time_ratio": ta["mean_end_to_end_s"] / ca["mean_end_to_end_s"],
                "fidelity_win_tie_loss": wtl(differences), "batch_win_tie_loss": wtl(batch_differences)}
    return {"protocol_id": protocol["protocol_id"], "formal_paper_result": False,
            "per_circuit": per_circuit, "overall": overall}


def collect(root, protocol):
    records = {}
    inventory = {entry["circuit"]: entry for entry in protocol["circuits"]}
    for job in protocol["job_order"]:
        jobroot = root / "jobs" / job["job_id"]
        receipt_path = root / "receipts" / f"{job['job_id']}.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {"status": "missing"}
        summary_path = jobroot / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        record = {"status": receipt["status"], "accepted": receipt["status"] == "success"
                  and summary.get("source_stable") is True}
        for arm in ARMS:
            path = jobroot / f"{arm}_result.json"
            if path.exists():
                record[arm] = json.loads(path.read_text())
                expected = inventory[job["circuit"]]
                counts = record[arm]["score"]["counts"]
                if (record[arm].get("n_qubits") != expected["qubits"]
                        or counts["one_qubit_gates"] != expected["gates_1q"]
                        or counts["two_qubit_gates"] != expected["gates_2q"]):
                    record["accepted"] = False
                    record["input_ledger_mismatch"] = True
        selection_paths = [jobroot / f"h{h}_selection.json" for h in (0, 2)]
        if all(path.exists() for path in selection_paths):
            low, high = (json.loads(path.read_text()) for path in selection_paths)
            same_pool = low["candidate_pool_sha256"] == high["candidate_pool_sha256"]
            same_policy = low["rollout_config"] == high["rollout_config"]
            prefix_equal = all(a.get("rows", [None])[0] == b.get("rows", [None])[0]
                for a, b in zip(low["candidates"], high["candidates"])
                if a["status"] == b["status"] == "success")
            record["initialization_audit"] = {
                "same_pool": same_pool, "same_rollout_policy": same_policy,
                "same_common_prefix": prefix_equal,
                "candidate_failures": low["failed_candidates"] + high["failed_candidates"],
                "choices_sealed": (jobroot / "sealed_choices.json").exists(),
                "candidate_evaluations": len(low["candidates"]) + len(high["candidates"])}
            if not same_pool or not same_policy or not prefix_equal:
                record["accepted"] = False
        records[job["circuit"], job["seed"]] = record
    return records


def execute(root, protocol):
    if source_snapshot()["source_sha256"] != protocol["repository"]["source_sha256"]:
        raise RuntimeError("source changed after protocol seal")
    if str(Path(sys.executable).resolve()) != protocol["python_realpath"] or file_hash(sys.executable) != protocol["python_sha256"]:
        raise RuntimeError("execution runtime differs from sealed ABI9 runtime")
    for key in ("config", "architecture"):
        if file_hash(protocol[f"{key}_path"]) != protocol[f"{key}_sha256"]:
            raise RuntimeError(f"{key} changed after seal")
    for circuit in protocol["circuits"]:
        if file_hash(circuit["input"]) != circuit["input_sha256"]:
            raise RuntimeError("input changed after seal")
    write_json(root / "execution_started.json", {"started_at_utc": utc_now(),
               "protocol_sha256": file_hash(root / "protocol.json")})
    (root / "receipts").mkdir()
    (root / "jobs").mkdir()
    inputs = {entry["circuit"]: entry["input"] for entry in protocol["circuits"]}
    for index, job in enumerate(protocol["job_order"]):
        jobroot = root / "jobs" / job["job_id"]
        command = [sys.executable, "-m", "experiments_v2.initial_lookahead_runner",
                   "--input", inputs[job["circuit"]], "--config", protocol["config_path"],
                   "--architecture", protocol["architecture_path"], "--root", str(jobroot),
                   "--horizons", "0", "2", "--seed", str(job["seed"]),
                   "--candidates", "4", "--rho", "0.7", "--rollout-evaluations", "32"]
        env = {**os.environ, **protocol["environment"], "PYTHONPATH": str(REPO / "ZAC_zzx")}
        started = time.perf_counter()
        receipt = {**job, "command": command, "started_at_utc": utc_now()}
        with (root / "receipts" / f"{job['job_id']}.log").open("x") as stream:
            process = subprocess.Popen(command, cwd=REPO, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=protocol["timeout_per_paired_job_s"])
                receipt.update(status="success" if code == 0 else "failed", returncode=code)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                receipt.update(status="timeout", returncode=process.returncode)
        receipt.update(elapsed_s=time.perf_counter() - started, completed_at_utc=utc_now())
        write_json(root / "receipts" / f"{job['job_id']}.json", receipt)
        print(f"[{index + 1}/{len(protocol['job_order'])}] {job['job_id']}: {receipt['status']} ({receipt['elapsed_s']:.1f}s)", flush=True)
        if source_snapshot()["source_sha256"] != protocol["repository"]["source_sha256"]:
            write_json(root / "source_changed.json", {"after_job": job["job_id"], "at_utc": utc_now()})
            break
    records = collect(root, protocol)
    report = summarize_records(protocol, records)
    report.update(protocol_sha256=file_hash(root / "protocol.json"), completed_at_utc=utc_now(),
                  source_stable=source_snapshot()["source_sha256"] == protocol["repository"]["source_sha256"],
                  job_statuses={status: sum(r["status"] == status for r in records.values())
                                for status in ("success", "failed", "timeout", "missing")},
                  physical_checks={"completed_arm_count": sum(arm in r for r in records.values() for arm in ARMS),
                    "physically_valid_arm_count": sum(r[arm].get("validation", {}).get("ok") is True
                        and r[arm]["validation"].get("ghost_hits") == 0 for r in records.values() for arm in ARMS if arm in r),
                    "fidelity_ood_arm_count": sum(r[arm]["score"].get("ood", False)
                        for r in records.values() for arm in ARMS if arm in r)},
                  initialization_checks={f"{circuit}-s{seed}": record.get("initialization_audit")
                                         for (circuit, seed), record in records.items()},
                  input_ledger_mismatch_jobs=[f"{circuit}-s{seed}" for (circuit, seed), record in records.items()
                                              if record.get("input_ledger_mismatch")])
    write_json(root / "summary.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--architecture", default=str(DEFAULT_ARCH))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seal-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    allowed = (REPO / "IEEE_conference_template/build/initial-lookahead",
               REPO / "ZAC_zzx/results/initial_lookahead_v1")
    if not any(parent in root.parents for parent in allowed):
        raise ValueError("a new version directory beneath an allowed result root is required")
    if args.seal_only:
        protocol = protocol_payload(args.config, args.architecture)
        root.mkdir(parents=True, exist_ok=False)
        write_json(root / "protocol.json", protocol)
        print(json.dumps({"root": str(root), "protocol_sha256": file_hash(root / "protocol.json"),
                          "jobs": len(protocol["job_order"])}, indent=2))
    else:
        protocol = json.loads((root / "protocol.json").read_text())
        print(json.dumps(execute(root, protocol)["overall"], indent=2))


if __name__ == "__main__":
    main()
