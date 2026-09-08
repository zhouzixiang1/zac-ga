"""Append-only continuation: preserve v2 receipts and distinguish model OOD.

Successful physical execution with the original scorer's explicit linear-model
OOD flag is retained with null fidelity, never converted to a numerical score.
No completed, failed or interrupted claim is automatically executed again.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
import importlib.util
import json
import math
import statistics
import uuid

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "ZAC_zzx/results/horizon_extension_v2"
ROOT = BASE / "continuation_v1"
DRIVER = REPO / "ZAC_zzx/experiments_v2/horizon_extension_v2.py"
BASE_SHA = "d899e7995eb252e51e19543bae98321979f7ff163f7858c10633ffaa017c643e"
PROTOCOL_ID = "horizon-shared12-v2-ood-continuation-v1"


def base_module():
    protocol = json.loads((BASE / "protocol.json").read_text())
    import hashlib
    if hashlib.sha256((BASE / "protocol.json").read_bytes()).hexdigest() != BASE_SHA:
        raise ValueError("base protocol changed")
    if hashlib.sha256(DRIVER.read_bytes()).hexdigest() != protocol["extension_script_sha256"]:
        raise ValueError("base driver changed")
    spec = importlib.util.spec_from_file_location("horizon_v2_sealed_base", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reference(module, path):
    return {"path": str(path), "sha256": module.digest(path)}


def classification(manifest, scorer):
    if manifest["status"] != "success":
        return "execution_failure"
    if manifest.get("verifier_ok") is not True or manifest.get("ghost_hits") != 0:
        raise ValueError("physical verification did not pass")
    result = scorer["result"]
    if result["fidelity"] != manifest.get("fidelity") or result["ood"] != manifest.get("fidelity_ood"):
        raise ValueError("scorer and manifest disagree")
    if result["ood"] is True:
        coherence = result["components"]["coherence_linear"]
        if (manifest.get("fidelity") is not None or manifest.get("log_fidelity") is not None or
                result.get("log_fidelity") is not None or coherence.get("fidelity") is not None or
                coherence.get("log_fidelity") is not None or not any(
                    text.startswith("linear coherence model out of domain for atoms:") for text in result["warnings"])):
            raise ValueError("not the original scorer's explicit linear-coherence OOD")
        return "model_out_of_domain"
    value = manifest.get("fidelity")
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError("invalid in-domain fidelity")
    return "valid_fidelity"


def check_manifest(module, base, job, path):
    manifest = module.read(path)
    for key in ("dataset", "circuit", "seed"):
        if manifest[key] != job[key]:
            raise ValueError("manifest identity mismatch")
    config = base["configs"][f"{job['horizon']}:{job['seed']}"]
    if (manifest["config_sha256"] != config["sha256"] or
            manifest["architecture_sha256"] != base["architecture"]["sha256"] or
            manifest["model_sha256"] != base["model"]["sha256"] or
            manifest["native_wheel_sha256"] != base["wheel"]["sha256"]):
        raise ValueError("manifest settings/runtime mismatch")
    if manifest["status"] != "success":
        return manifest, None, "execution_failure"
    scorer_path = Path(path).parent / "fidelity.json"
    scorer = module.read(scorer_path)
    if (not manifest.get("expected_gate_ledger_sha256") or
            manifest["expected_gate_ledger_sha256"] != manifest.get("observed_gate_ledger_sha256") or
            not manifest.get("layer_ledger_sha256") or
            manifest["layer_ledger_sha256"] != manifest.get("observed_transition_layer_ledger_sha256") or
            manifest["layer_ledger_sha256"] != manifest.get("canonical_input_layer_ledger_sha256")):
        raise ValueError("logical gate or layer ledger mismatch")
    for field, expected in (("ghost_policy", "strict_zero"), ("physicalization_policy", "in_method_ghost_safe"),
                            ("trace_protocol", "ours_lk_ghost_safe_v2")):
        if manifest.get(field) != expected or scorer.get(field) != expected:
            raise ValueError("physical/scorer policy mismatch")
    return manifest, scorer_path, classification(manifest, scorer)


def seal():
    module = base_module()
    base = module.verify_protocol()
    if ROOT.exists():
        raise FileExistsError("continuation directory must be new")
    prior, sidecars, pending = [], [], []
    known_ood = set()
    for item in base["reused_quality"]:
        old = module.checked(item["manifest"], item["manifest_sha256"])
        if old.get("fidelity_ood") is True:
            known_ood.add((item["dataset"], item["circuit"]))
    for job in base["quality_jobs"]:
        path = BASE / "receipts/quality" / (job["job_id"] + ".json")
        claim = BASE / "claims/quality" / (job["job_id"] + ".json")
        if not path.exists():
            if claim.exists() or (BASE / "runs/quality" / job["job_id"]).exists():
                raise ValueError("unreceipted prior attempt cannot be retried")
            pending.append(job["job_id"])
            continue
        receipt = module.receipt_state(base, job)
        item = {"job_id": job["job_id"], **reference(module, path), "raw_status": receipt["status"],
                "claim": reference(module, claim)}
        if receipt["status"] == "runner_error":
            if (receipt.get("error") != "ValueError('nominal success failed independent quality-domain checks')" or
                    (job["dataset"], job["circuit"]) not in known_ood):
                raise ValueError("unknown prior runner failure requires separate review")
            matches = list((BASE / "runs/quality" / job["job_id"]).glob("*/manifest.json"))
            if len(matches) != 1:
                raise ValueError("prior OOD attempt must have one unambiguous manifest")
            manifest, scorer, kind = check_manifest(module, base, job, matches[0])
            if kind != "model_out_of_domain":
                raise ValueError("prior failure is not the reviewed OOD classification")
            sidecar = {"job_id": job["job_id"], "original_receipt": {**reference(module, path), "status": "runner_error"},
                       "manifest": {**reference(module, matches[0]), "status": "success"},
                       "scorer": reference(module, scorer), "classification": "success_model_ood",
                       "trace_zair": reference(module, matches[0].parent / "trace.zair.json.gz"),
                       "canonical_trace": reference(module, matches[0].parent / "canonical_trace.jsonl.gz"),
                       "physical_checks": {"verifier_ok": True, "ghost_hits": 0},
                       "fidelity": None, "log_fidelity": None, "base_protocol_sha256": BASE_SHA}
            sidecars.append((ROOT / "classifications" / (job["job_id"] + ".json"), sidecar))
        elif receipt["status"] == "success":
            check_manifest(module, base, job, receipt["manifest"])
        else:
            raise ValueError("unexpected prior execution failure")
        prior.append(item)
    if len(prior) != 23 or len(pending) != 61 or len(sidecars) != 2:
        raise ValueError("reviewed continuation boundary must be 23 completed plus 61 missing and two OOD overlays")
    ROOT.mkdir(parents=True)
    for path, data in sidecars:
        module.write_new(path, data)
    protocol = {"schema": 1, "protocol_id": PROTOCOL_ID, "sealed_at": module.now(),
                "base_protocol": reference(module, BASE / "protocol.json"),
                "base_driver": reference(module, DRIVER), "continuation_driver": reference(module, Path(__file__).resolve()),
                "prior_receipts": prior, "pending_job_ids": pending,
                "ood_sidecars": [reference(module, p) for p, _ in sidecars],
                "quality_workers": 2, "timeout_seconds": 600, "environment": base["environment"],
                "classification_policy": "Original scorer linear-coherence OOD with verifier_ok=true and ghost_hits=0 is a valid physical execution with null fidelity; no alternative model substitution",
                "retry_policy": "No existing original or continuation claim may be executed again",
                "failure_policy": "Stop dispatch on compiler/physical/runner failure; retain circuit timeouts; never relax verification",
                "timing_execution_enabled": False, "formal_paper_result": False}
    module.write_new(ROOT / "protocol.json", protocol)
    module.write_new(ROOT / "protocol.sha256.json", {"sha256": module.digest(ROOT / "protocol.json")})
    return {"protocol": str(ROOT / "protocol.json"), "sha256": module.digest(ROOT / "protocol.json"),
            "prior": 23, "ood_overlays": 2, "pending": 61}


def verify():
    module = base_module()
    base = module.verify_protocol()
    protocol = module.checked(ROOT / "protocol.json", module.read(ROOT / "protocol.sha256.json")["sha256"])
    if protocol["protocol_id"] != PROTOCOL_ID:
        raise ValueError("wrong continuation protocol")
    for item in [protocol["base_protocol"], protocol["base_driver"], protocol["continuation_driver"],
                 *protocol["prior_receipts"], *protocol["ood_sidecars"],
                 *[p["claim"] for p in protocol["prior_receipts"]]]:
        if module.digest(item["path"]) != item["sha256"]:
            raise ValueError("sealed continuation dependency changed")
    if set(p.name for p in (BASE / "receipts/quality").glob("*.json")) != {Path(p["path"]).name for p in protocol["prior_receipts"]}:
        raise ValueError("original receipt set changed after continuation seal")
    return module, base, protocol


def original_receipts(module, base, protocol):
    sidecars = {module.read(p["path"])["job_id"]: p for p in protocol["ood_sidecars"]}
    receipts = []
    for item in protocol["prior_receipts"]:
        row = module.checked(item["path"], item["sha256"])
        row["receipt"] = {"path": item["path"], "sha256": item["sha256"]}
        if row["job_id"] in sidecars:
            ref = sidecars[row["job_id"]]
            sidecar = module.checked(ref["path"], ref["sha256"])
            for dependency in (sidecar["original_receipt"], sidecar["manifest"], sidecar["scorer"]):
                module.checked(dependency["path"], dependency["sha256"])
            for field in ("trace_zair", "canonical_trace"):
                if module.digest(sidecar[field]["path"]) != sidecar[field]["sha256"]:
                    raise ValueError("original OOD trace changed")
            job = next(j for j in base["quality_jobs"] if j["job_id"] == row["job_id"])
            _, _, kind = check_manifest(module, base, job, sidecar["manifest"]["path"])
            if kind != "model_out_of_domain":
                raise ValueError("OOD overlay no longer matches physical/scorer evidence")
            row.update(ood_sidecar=ref, resolved_manifest={k: sidecar["manifest"][k] for k in ("path", "sha256")},
                       quality_classification="model_out_of_domain")
        receipts.append(row)
    return receipts


def current_state(module, job):
    receipt = ROOT / "receipts/quality" / (job["job_id"] + ".json")
    claim = ROOT / "claims/quality" / (job["job_id"] + ".json")
    if receipt.exists():
        data = module.read(receipt)
        if any(data.get(k) != job[k] for k in module.IDENTITY) or data.get("protocol_sha256") != module.digest(ROOT / "protocol.json"):
            raise ValueError("continuation receipt identity drift")
        if "manifest" in data:
            manifest = module.checked(data["manifest"], data["manifest_sha256"])
            if data["status"] != manifest["status"]:
                raise ValueError("continuation receipt disagrees with manifest")
        elif data["status"] == "success":
            raise ValueError("success without manifest")
        return {**data, "receipt": reference(module, receipt)}
    if claim.exists():
        raise ValueError("interrupted continuation claim cannot be automatically retried")
    return None


def summarize(module, base, protocol):
    helper = module.legacy()
    receipts = original_receipts(module, base, protocol)
    for job in base["quality_jobs"]:
        if job["job_id"] in protocol["pending_job_ids"]:
            state = current_state(module, job)
            if state is not None:
                receipts.append(state)
    records = []
    sources = [dict(item, path=item["manifest"], sha256=item["manifest_sha256"]) for item in base["reused_quality"]]
    for item in receipts:
        ref = item.get("resolved_manifest") or ({"path": item["manifest"], "sha256": item["manifest_sha256"]} if "manifest" in item else None)
        if ref:
            sources.append({**item, **ref})
    for item in sources:
        manifest = module.checked(item["path"], item["sha256"])
        row = helper.quality_record(manifest, horizon=item["horizon"], seed=item["seed"],
                                    source=item["path"], source_sha256=item["sha256"])
        if not module.record_valid(manifest):
            row["status"] = "invalid_quality"
        records.append(row)
    summary = helper.aggregate_quality(records, base["circuits"], helper.SEEDS)
    summary.update(protocol_sha256=BASE_SHA, continuation_protocol=reference(module, ROOT / "protocol.json"),
                   at=module.now(), receipts=receipts, records=records, reused_records=96,
                   new_jobs_planned=84, new_jobs_claimed=len(receipts), new_jobs_pending=84-len(receipts),
                   new_status_counts=dict(Counter(r["status"] for r in receipts)),
                   physical_execution_status_counts=dict(Counter(module.read(r["source"])["status"] for r in records)),
                   model_ood_records=sum(r.get("fidelity_ood") is True for r in records), timing_started=False)
    index = {(r["dataset"], r["circuit"], r["horizon"], r["seed"]): r for r in records}
    common, medians = [], {}
    for circuit in base["circuits"]:
        key = (circuit["dataset"], circuit["circuit"])
        block = [index.get(key + (h, s)) for h in helper.HORIZONS for s in helper.SEEDS]
        if all(r and r["status"] == "success" and module.record_valid(r) for r in block):
            common.append({"dataset": key[0], "circuit": key[1]})
            medians[key] = {h: {metric: statistics.median(index[key + (h, s)][metric] for s in helper.SEEDS)
                                for metric in ("fidelity", "move_batches")} for h in helper.HORIZONS}
    summary["common_fidelity_cohort"] = common
    summary["horizon_comparison"] = {str(h): {"paired_circuits": len(common),
        "fidelity_ratio_vs_h8": math.exp(statistics.mean(math.log(values[h]["fidelity"]/values[8]["fidelity"]) for values in medians.values())) if common else None,
        "move_batches_ratio_vs_h8": math.exp(statistics.mean(math.log(values[h]["move_batches"]/values[8]["move_batches"]) for values in medians.values())) if common else None}
        for h in helper.HORIZONS}
    summary["per_circuit_quality_medians"] = [{"dataset": key[0], "circuit": key[1], "horizons": values}
                                               for key, values in medians.items()]
    return summary


def execute():
    module, base, protocol = verify()
    with module.execution_lock():
        module.dependency_probe(base)
        helper = module.legacy()
        helper.bootstrap(base)
        jobs = []
        for job in base["quality_jobs"]:
            if job["job_id"] not in protocol["pending_job_ids"]:
                continue
            state = current_state(module, job)
            if state and state["status"] not in ("success", "timeout"):
                raise ValueError("prior continuation failure requires review")
            if state is None:
                jobs.append(job)
        session = uuid.uuid4().hex
        module.write_new(ROOT / "sessions" / (session + ".started.json"),
                         {"at": module.now(), "pending": len(jobs), "protocol_sha256": module.digest(ROOT / "protocol.json")})
        queue, active, stopped = iter(jobs), {}, False
        with ThreadPoolExecutor(max_workers=2) as pool:
            def dispatch():
                job = next(queue, None)
                if job is not None:
                    module.write_new(ROOT / "claims/quality" / (job["job_id"] + ".json"),
                                     {**job, "at": module.now(), "protocol_sha256": module.digest(ROOT / "protocol.json")})
                    active[pool.submit(helper.run_one, base, job, "quality")] = job
                    print("START " + job["job_id"], flush=True)
            dispatch(); dispatch()
            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    receipt = None
                    try:
                        receipt = future.result()
                        manifest, scorer, kind = check_manifest(module, base, job, receipt["manifest"])
                        receipt["quality_classification"] = kind
                        if scorer:
                            receipt["scorer"] = reference(module, scorer)
                    except Exception as error:
                        receipt = {**job, "status": "runner_error", "error": repr(error), "raw_attempt": receipt}
                    receipt.update(protocol_sha256=module.digest(ROOT / "protocol.json"), base_protocol_sha256=BASE_SHA,
                                   session=session, completed_at=module.now())
                    module.write_new(ROOT / "receipts/quality" / (job["job_id"] + ".json"), receipt)
                    print(f"DONE {job['job_id']}: {receipt['status']} ({receipt.get('quality_classification', '')})", flush=True)
                    stopped = stopped or receipt["status"] not in ("success", "timeout")
                if not stopped:
                    for _ in range(2-len(active)):
                        dispatch()
        summary = summarize(module, base, protocol)
        module.write_new(ROOT / "summaries" / (session + ".json"), summary)
        module.write_new(ROOT / "sessions" / (session + ".finished.json"),
                         {"at": module.now(), "stopped_on_failure": stopped, "pending": summary["new_jobs_pending"]})
        if summary["new_jobs_pending"] == 0 and not (BASE / "combined_quality_summary.json").exists():
            module.write_new(BASE / "combined_quality_summary.json", summary)
        return {k: summary[k] for k in ("new_status_counts", "new_jobs_pending", "model_ood_records", "horizon_comparison")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--seal", action="store_true")
    modes.add_argument("--quality", action="store_true")
    modes.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.seal:
        result = seal()
    elif args.quality:
        result = execute()
    else:
        module, base, protocol = verify()
        result = {"verified": True, "prior_receipts": len(protocol["prior_receipts"]), "pending_jobs": len(protocol["pending_job_ids"])}
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
