"""Versioned, resumable quality-only continuation of the sealed H study.

The v1 protocol and implementation remain immutable. Only the missing verifier
module is added to the compiler export, from the same frozen commit. A claim is
never retried automatically, whether it succeeded, failed or was interrupted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import io
import json
import math
import os
import subprocess
import tarfile
import uuid

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "ZAC_zzx/results/horizon_extension_v2"
SOURCE = REPO / "IEEE_conference_template/build/horizon_extension_v2/frozen_source"
PREVIOUS = REPO / "ZAC_zzx/results/horizon_extension_v1"
PREVIOUS_SHA = "d4c5222c25235bac1507d42ece47c8cfcb57b5c5aa0fd9d2fcd9b5ca4237ed7f"
HELPER = PREVIOUS / "interrupted_environment/implementation.py"
HELPER_SHA = "4367bb03f44594b03f079a48dd6004bed55b743b725335b1e67d6e6d7fb84c17"
PROTOCOL = "horizon-shared12-seed012-frozen-abi9-v2"
COMMIT = "4122f61516b104ae9d9779f71ec843af63b57600"
ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
       "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
       "VECLIB_MAXIMUM_THREADS": "1"}
IDENTITY = ("dataset", "circuit", "horizon", "seed", "repetition", "job_id")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def checked(path, expected):
    if digest(path) != expected:
        raise ValueError(f"sealed file changed: {path}")
    return read(path)


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def now():
    return datetime.now(timezone.utc).isoformat()


def legacy():
    if digest(HELPER) != HELPER_SHA:
        raise ValueError("immutable v1 helper changed")
    spec = importlib.util.spec_from_file_location("horizon_v1_sealed_helper", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.REPO, module.ROOT, module.SOURCE, module.PROTOCOL = REPO, ROOT, SOURCE, PROTOCOL
    module.ACCEPTED = REPO / "ZAC_zzx/results/paper_zh_v2"
    module.OLD = REPO / "fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1"
    module.ENV = ENV
    return module


def snapshot():
    return {str(p.relative_to(SOURCE)): digest(p) for p in sorted(SOURCE.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def extract_source(previous):
    if SOURCE.exists():
        raise FileExistsError("v2 source directory must be new")
    helper = legacy()
    names = [f"ZAC_zzx/{name}" for name in helper.SOURCE_DIRS] + ["ZAC_zzx/verify_batches.py"]
    data = subprocess.check_output(["git", "archive", COMMIT, *names], cwd=REPO)
    SOURCE.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            target = (SOURCE / member.name).resolve()
            if SOURCE not in target.parents or not (member.isfile() or member.isdir()):
                raise ValueError("unsafe frozen archive member")
        archive.extractall(SOURCE)
    files = snapshot()
    added = set(files) - set(previous["frozen_source_files"])
    if added != {"ZAC_zzx/verify_batches.py"} or any(
            files.get(k) != v for k, v in previous["frozen_source_files"].items()):
        raise ValueError("v2 source must be exactly v1 plus the same-commit verifier")
    expected = subprocess.check_output(["git", "show", f"{COMMIT}:ZAC_zzx/verify_batches.py"], cwd=REPO)
    if files["ZAC_zzx/verify_batches.py"] != hashlib.sha256(expected).hexdigest():
        raise ValueError("verifier does not match the frozen commit")
    return files


def choose_smoke(circuits, jobs):
    result = []
    for dataset in ("zac18", "qmap154"):
        smallest = min((c for c in circuits if c["dataset"] == dataset),
                       key=lambda c: (c["canonical"]["gates_1q"] + c["canonical"]["gates_2q"], c["circuit"]))
        result.append(next(j["job_id"] for j in jobs if
                           (j["dataset"], j["circuit"], j["horizon"], j["seed"]) ==
                           (dataset, smallest["circuit"], 1, 0)))
    return result


def dependency_probe(protocol):
    code = """import importlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]); sys.path.insert(0,str(root))
modules={}
for name in ('experiments_v2.cli','experiments_v2.method_driver','experiments_v2.runner','verify_batches','evaluation','zzx.native_backend'):
    module=importlib.import_module(name); path=Path(module.__file__).resolve()
    if root not in path.parents: raise RuntimeError('module outside frozen export: '+str(path))
    modules[name]=str(path)
from zzx.native_backend import build_info
native=build_info(require_registered_wheel=True,expected_wheel_sha256=sys.argv[2])
import qiskit
print(json.dumps({'modules':modules,'native':native,'qiskit_version':qiskit.__version__}))
"""
    result = subprocess.run([protocol["python"], "-B", "-c", code, str(SOURCE / "ZAC_zzx"),
                             protocol["wheel"]["sha256"]], cwd=SOURCE / "ZAC_zzx",
                            env={**os.environ, **ENV, "PYTHONPATH": str(SOURCE / "ZAC_zzx")},
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("frozen dependency preflight failed: " + result.stderr[-4000:])
    return json.loads(result.stdout)


def seal(workers=2):
    if workers not in (1, 2):
        raise ValueError("at most two quality workers are authorized")
    if ROOT.exists():
        raise FileExistsError("v2 result directory must be new")
    previous = checked(PREVIOUS / "protocol.json", PREVIOUS_SHA)
    helper = legacy()
    final, reports, base, circuits, reuse, parity = helper.audit_and_design()
    jobs = helper.missing_quality_jobs(circuits)
    for key, actual in (("circuits", circuits), ("reused_quality", reuse), ("quality_jobs", jobs)):
        if actual != previous[key]:
            raise ValueError(f"prespecified v1 {key} changed")
    if sys.executable != previous["python"]:
        raise ValueError("seal v2 with the original frozen ABI9 interpreter")
    for prefix in ("paper_native_python", "paper_native_extension"):
        if digest(previous["native"][prefix + "_path"]) != previous["native"][prefix + "_sha256"]:
            raise ValueError("immutable native runtime changed")
    if digest(previous["wheel"]["path"]) != previous["wheel"]["sha256"]:
        raise ValueError("immutable native wheel changed")
    files = extract_source(previous)
    protocol = deepcopy(previous)
    protocol.update(protocol_id=PROTOCOL, sealed_at=now(), quality_workers=workers,
                    frozen_source_root=str(SOURCE), frozen_source_files=files,
                    frozen_source_sha256=stable_hash(files), extension_script=str(Path(__file__).resolve()),
                    extension_script_sha256=digest(__file__), environment=ENV,
                    predecessor={"path": str(PREVIOUS / "protocol.json"), "sha256": PREVIOUS_SHA},
                    immutable_helper={"path": str(HELPER), "sha256": HELPER_SHA},
                    source_change="same-commit verify_batches.py added; original 115 files unchanged",
                    smoke_job_ids=choose_smoke(circuits, jobs), timing_execution_enabled=False,
                    resume_policy="exclusive claims; never retry completed, failed or interrupted claims; preserve every receipt",
                    dispatch_policy="at most two in flight; stop issuing work on any failure other than a circuit timeout")
    for label, relative in (("architecture", "hardware_spec/full_architecture.json"),
                            ("model", "evaluation/fidelity_model_zac.json")):
        path = SOURCE / "ZAC_zzx" / relative
        if digest(path) != previous[label]["sha256"]:
            raise ValueError(f"frozen {label} changed")
        protocol[label] = {"path": str(path), "sha256": digest(path)}
    probe = dependency_probe(protocol)
    ROOT.mkdir(parents=True)
    configs = {}
    for horizon in helper.HORIZONS:
        for seed in helper.SEEDS:
            path = ROOT / f"configs/h{horizon}-s{seed}.json"
            config = helper.make_wrapper(base, horizon, seed)
            config["base_config"]["zac_setting"][0]["dir"] = "results/horizon_extension_v2/isolated/"
            write_new(path, config)
            configs[f"{horizon}:{seed}"] = {"path": str(path), "sha256": digest(path)}
    protocol["configs"] = configs
    helper.bootstrap(protocol)
    from experiments_v2.ablation import validate_ablation_config
    for config in configs.values():
        validate_ablation_config(read(config["path"]))
    write_new(ROOT / "protocol.json", protocol)
    write_new(ROOT / "protocol.sha256.json", {"sha256": digest(ROOT / "protocol.json")})
    write_new(ROOT / "dependency_preflight.json", {"at": now(), "protocol_sha256": digest(ROOT / "protocol.json"), **probe})
    helper.reanalyze(protocol)
    return {"root": str(ROOT), "source": str(SOURCE), "protocol_sha256": digest(ROOT / "protocol.json"),
            "frozen_files": len(files), "new_quality_jobs": len(jobs), "reused_quality_jobs": len(reuse),
            "smoke_jobs": protocol["smoke_job_ids"], "timing_started": False}


def verify_protocol():
    protocol = checked(ROOT / "protocol.json", read(ROOT / "protocol.sha256.json")["sha256"])
    if protocol["protocol_id"] != PROTOCOL or digest(__file__) != protocol["extension_script_sha256"]:
        raise ValueError("v2 protocol or driver drift")
    if sys.executable != protocol["python"] or snapshot() != protocol["frozen_source_files"]:
        raise ValueError("v2 interpreter or frozen source drift")
    for item in [protocol["predecessor"], protocol["immutable_helper"], *protocol["configs"].values(),
                 protocol["architecture"], protocol["model"], protocol["wheel"], protocol["execution_plan"]]:
        if digest(item["path"]) != item["sha256"]:
            raise ValueError(f"immutable dependency drift: {item['path']}")
    for prefix in ("paper_native_python", "paper_native_extension"):
        if digest(protocol["native"][prefix + "_path"]) != protocol["native"][prefix + "_sha256"]:
            raise ValueError("immutable runtime drift")
    for circuit in protocol["circuits"]:
        if digest(circuit["canonical"]["canonical_path"]) != circuit["canonical"]["canonical_sha256"]:
            raise ValueError("canonical input drift")
    for item in protocol["reused_quality"]:
        checked(item["manifest"], item["manifest_sha256"])
    return protocol


def receipt_state(protocol, job):
    receipt = ROOT / "receipts/quality" / (job["job_id"] + ".json")
    claim = ROOT / "claims/quality" / (job["job_id"] + ".json")
    if receipt.exists():
        item = read(receipt)
        if any(item.get(k) != job[k] for k in IDENTITY) or item.get("protocol_sha256") != digest(ROOT / "protocol.json"):
            raise ValueError("receipt identity or protocol drift")
        if "manifest" in item:
            manifest = checked(item["manifest"], item["manifest_sha256"])
            if manifest["status"] != item["status"] or any(manifest[k] != job[k] for k in ("dataset", "circuit", "seed")):
                raise ValueError("receipt and manifest disagree")
        elif item["status"] == "success":
            raise ValueError("success receipt lacks an evaluated manifest")
        return item
    if claim.exists():
        item = read(claim)
        if any(item.get(k) != job[k] for k in IDENTITY) or item.get("protocol_sha256") != digest(ROOT / "protocol.json"):
            raise ValueError("claim identity or protocol drift")
        return {**item, "status": "interrupted_without_receipt"}
    return None


def claim_job(job):
    write_new(ROOT / "claims/quality" / (job["job_id"] + ".json"),
              {**job, "at": now(), "protocol_sha256": digest(ROOT / "protocol.json")})


def record_valid(manifest):
    value = manifest.get("fidelity")
    return (manifest.get("status") == "success" and manifest.get("fidelity_ood") is not True and
            isinstance(value, (int, float)) and math.isfinite(value) and 0 < value <= 1 and
            manifest.get("ghost_hits") in (None, 0))


def summarize(protocol):
    helper = legacy()
    records, receipts = [], []
    for item in protocol["reused_quality"]:
        manifest = checked(item["manifest"], item["manifest_sha256"])
        record = helper.quality_record(manifest, horizon=item["horizon"], seed=item["seed"],
                                       source=item["manifest"], source_sha256=item["manifest_sha256"])
        if not record_valid(manifest):
            record["status"] = "invalid_quality"
        records.append(record)
    for job in protocol["quality_jobs"]:
        receipt = receipt_state(protocol, job)
        if receipt is None:
            continue
        receipts.append(receipt)
        if "manifest" in receipt:
            manifest = checked(receipt["manifest"], receipt["manifest_sha256"])
            record = helper.quality_record(manifest, horizon=job["horizon"], seed=job["seed"],
                                           source=receipt["manifest"], source_sha256=receipt["manifest_sha256"])
            if not record_valid(manifest):
                record["status"] = "invalid_quality"
            records.append(record)
    summary = helper.aggregate_quality(records, protocol["circuits"], helper.SEEDS)
    summary.update(protocol_sha256=digest(ROOT / "protocol.json"), at=now(), receipts=receipts, records=records,
                   new_jobs_planned=len(protocol["quality_jobs"]), new_jobs_claimed=len(receipts),
                   new_status_counts=dict(Counter(r["status"] for r in receipts)),
                   new_jobs_pending=len(protocol["quality_jobs"]) - len(receipts),
                   reused_records=len(protocol["reused_quality"]), timing_started=False,
                   dataset_summaries={d: helper.aggregate_quality(records, [c for c in protocol["circuits"] if
                       c["dataset"] == d], helper.SEEDS) for d in ("zac18", "qmap154")})
    return summary


@contextmanager
def execution_lock():
    with (ROOT / ".quality.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another quality dispatcher holds the lock") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def execute(protocol, smoke=False):
    with execution_lock():
        helper = legacy()
        helper.bootstrap(protocol)
        dependency_probe(protocol)
        states = {j["job_id"]: receipt_state(protocol, j) for j in protocol["quality_jobs"]}
        if not smoke and any(not states[k] or states[k]["status"] != "success" for k in protocol["smoke_job_ids"]):
            raise ValueError("both prespecified smoke jobs must succeed before full quality dispatch")
        if any(v and v["status"] not in ("success", "timeout") for v in states.values()):
            raise ValueError("failed or interrupted claim requires review; automatic continuation disabled")
        jobs = [j for j in protocol["quality_jobs"] if states[j["job_id"]] is None and
                (not smoke or j["job_id"] in protocol["smoke_job_ids"])]
        session = uuid.uuid4().hex
        write_new(ROOT / "sessions" / (session + ".started.json"),
                  {"at": now(), "smoke": smoke, "jobs_pending": len(jobs), "protocol_sha256": digest(ROOT / "protocol.json")})
        queue, active, completed, stopped = iter(jobs), {}, [], False
        with ThreadPoolExecutor(max_workers=protocol["quality_workers"]) as pool:
            def dispatch():
                job = next(queue, None)
                if job is not None:
                    claim_job(job)
                    active[pool.submit(helper.run_one, protocol, job, "quality")] = job
                    print("START " + job["job_id"], flush=True)
            for _ in range(protocol["quality_workers"]):
                dispatch()
            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    try:
                        receipt = future.result()
                        if receipt["status"] == "success" and not record_valid(checked(receipt["manifest"], receipt["manifest_sha256"])):
                            raise ValueError("nominal success failed independent quality-domain checks")
                    except Exception as error:
                        receipt = {**job, "phase": "quality", "status": "runner_error", "error": repr(error)}
                    receipt.update(protocol_sha256=digest(ROOT / "protocol.json"), completed_at=now(), session=session)
                    write_new(ROOT / "receipts/quality" / (job["job_id"] + ".json"), receipt)
                    completed.append(receipt)
                    print(f"DONE {job['job_id']}: {receipt['status']}", flush=True)
                    stopped = stopped or receipt["status"] not in ("success", "timeout")
                if not stopped:
                    for _ in range(protocol["quality_workers"] - len(active)):
                        dispatch()
        summary = summarize(protocol)
        write_new(ROOT / "summaries" / (session + ".json"), summary)
        write_new(ROOT / "sessions" / (session + ".finished.json"),
                  {"at": now(), "stopped_on_failure": stopped, "completed_this_session": len(completed),
                   "new_status_counts": summary["new_status_counts"], "pending": summary["new_jobs_pending"]})
        if summary["new_jobs_pending"] == 0 and not (ROOT / "quality_summary.json").exists():
            write_new(ROOT / "quality_summary.json", summary)
        return {k: summary[k] for k in ("new_status_counts", "new_jobs_pending", "complete_circuits", "fidelity_ratio_vs_h8")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ("seal", "smoke", "quality", "worker", "verify", "summary"):
        mode.add_argument("--" + name, action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    args, extra = parser.parse_known_args(argv)
    if extra and not args.worker:
        parser.error("unrecognized arguments")
    if args.seal:
        result = seal(args.workers)
    else:
        protocol = verify_protocol()
        if args.worker:
            legacy().bootstrap(protocol)
            from experiments_v2.method_driver import main as compile_main
            compile_main(extra[1:] if extra[:1] == ["--"] else extra)
            return
        if args.verify:
            result = {"verified": True, "dependency_probe": dependency_probe(protocol)}
        elif args.summary:
            result = summarize(protocol)
        else:
            result = execute(protocol, smoke=args.smoke)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
