"""Replay four interrupted workers' completed evidence without compiling.

The default invocation is read-only. ``--apply`` appends one independent audit
and missing recovered_success receipts, never changing an existing artifact.
Unknown process exit codes and supervisor timing remain null. Worker-recorded
solve timing stays in the original result and is ineligible for timing claims.
Run with the sealed protocol's Python and ``-B``; no experiment is rerun.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid


# Direct script execution must not let experiments_v2/statistics.py shadow
# Python's standard-library statistics module while importing frozen readers.
sys.path[:] = [entry for entry in sys.path
               if Path(entry).resolve() != Path(__file__).resolve().parent]

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "ZAC_zzx/results/default_initial_v1"
PROTOCOL_SHA256 = "56bf06493df746e7efa6f0ddec805dc6e88086fa000afb673634430ef274071d"
PROTOCOL_ID = "default-initial-canonical169-seed012-v1"
JOB_IDS = ("5b244278f3e4846b-s0", "5b244278f3e4846b-s1",
           "5b244278f3e4846b-s2", "5c1c11e8fdb3c2c2-s1")
FIXED = {"horizon": 2, "candidates": 4, "rho": .7, "rollout_evaluations": 32}
CHECK_NAMES = {"result_stdout_sha", "native_trace_sha", "input_sha", "native_identity",
               "id_seed", "instruction_sha", "selected_mapping_sha", "config",
               "candidate_hashes", "selected_candidate_mapping", "physics_exact",
               "logical_exact", "score_exact", "canonical_gate_count", "layers_count", "quality"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pin(path):
    return {"path": str(Path(path).resolve()), "sha256": digest(path)}


def stable(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def read_json(path):
    def invalid(value):
        raise ValueError("non-finite JSON constant: " + value)
    value = json.loads(Path(path).read_text(), parse_constant=invalid)
    require(isinstance(value, dict), "expected JSON object: " + str(path))
    return value


def verify_pin(reference):
    require(digest(reference["path"]) == reference["sha256"],
            "evidence hash changed: " + reference["path"])
    return True


def write_new(path, value):
    """Exclusive creation: no overwrite, rename, deletion, or partial retry."""
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_protocol(root=ROOT):
    path = root / "protocol.json"
    reference = read_json(root / "protocol.sha256.json")
    require(Path(reference["path"]).resolve() == path.resolve(), "protocol pin path differs")
    require(reference["sha256"] == PROTOCOL_SHA256, "not the authorized sealed protocol")
    verify_pin(reference)
    protocol = read_json(path)
    require(protocol["protocol_id"] == PROTOCOL_ID, "protocol identity differs")
    checks = {name: verify_pin(protocol[name]) for name in
              ("driver", "config", "architecture", "accepted_manifest")}
    checks["python"] = digest(protocol["python"]) == protocol["python_sha256"]
    native = protocol["native"]
    checks["native_extension"] = digest(native["extension_path"]) == native["extension_sha256"]
    registration = read_json(native["wheel_registration_path"])
    checks["native_wheel"] = (registration["extension_sha256"] == native["extension_sha256"]
                             and registration["wheel_sha256"] == native["native_wheel_sha256"]
                             and digest(registration["wheel_path"]) == native["native_wheel_sha256"])
    frozen = Path(protocol["frozen_source"])
    mismatches = [name for name, sha in protocol["frozen_source_files"].items()
                  if digest(frozen / name) != sha]
    require(all(checks.values()) and not mismatches, "sealed source/runtime changed")
    checks.update(frozen_source_file_count=len(protocol["frozen_source_files"]),
                  frozen_source_mismatches=mismatches)
    return protocol, checks


def load_replayers(protocol):
    """Import only hash-verified frozen validators, never invoke a solver."""
    require(digest(sys.executable) == protocol["python_sha256"],
            "use the sealed protocol's Python for independent replay")
    sys.dont_write_bytecode = True
    frozen = Path(protocol["frozen_source"]).resolve()
    sys.path.insert(0, str(frozen))
    from evaluation import normalize_zair, score_trace, validate_trace_physics
    from experiments_v2.initial_lookahead_runner import logical_trace_receipt
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] in {"evaluation", "experiments_v2", "zac", "zzx", "streaming"}:
            path = getattr(module, "__file__", None)
            if path:
                require(Path(path).resolve().is_relative_to(frozen),
                        "replayer imported outside frozen source: " + name)
    return normalize_zair, score_trace, validate_trace_physics, logical_trace_receipt


def live_target_processes(output, job_ids=JOB_IDS):
    matches = []
    for line in output.splitlines():
        try:
            pid, command = line.strip().split(None, 1)
            argv = shlex.split(command)
        except ValueError:
            continue
        if int(pid) == os.getpid():
            continue
        is_driver = any(Path(token).name == "default_initial_study.py" for token in argv)
        if is_driver and any(token in job_ids for token in argv):
            matches.append({"pid": int(pid), "command": command})
    return matches


def assert_no_workers(job_ids=JOB_IDS):
    process_list = subprocess.run(["ps", "-axo", "pid=,args="], check=True,
                                  capture_output=True, text=True).stdout
    matches = live_target_processes(process_list, job_ids)
    require(not matches, "target worker is still alive: " + json.dumps(matches))


def assert_missing_terminals(root=ROOT, job_ids=JOB_IDS):
    for job_id in job_ids:
        terminal = root / "quality/receipts" / (job_id + ".json")
        require(not terminal.exists() and not terminal.is_symlink(),
                "terminal receipt already exists; never overwrite: " + str(terminal))


@contextmanager
def recovery_lock(root=ROOT):
    # Recovery touches only already-claimed identities. The continuation
    # coordinator explicitly excludes all started claims from its dispatch;
    # using a separate lock lets that disjoint work continue. Exact process
    # checks plus exclusive receipt creation still apply before recovery.
    folder = root / "recovery"
    folder.mkdir(exist_ok=True)
    with (folder / ".recovery.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate_quality(protocol, job, result, trace, replayers, architecture):
    normalize, rescore, physical, logical = replayers
    events = tuple(normalize(trace, architecture=architecture))
    validation = physical(events, n_qubits=job["qubits"])
    score = rescore(events, n_qubits=job["qubits"]).to_dict()
    observed_layers = [event.gate_pairs for event in events if event.kind == "two_qubit_gate"]
    # Recompute input operation order and all layer hashes. The original
    # compiled schedule is not serialized, so compare against its recorded
    # ledger hash; never pretend this reruns the compiler scheduler.
    ledger = logical(job["input"]["path"], job["qubits"], events, observed_layers)
    init = next(instruction for instruction in trace["instructions"] if instruction["type"] == "init")
    mapping = [row[1:] for row in sorted(init["init_locs"])]
    selection = result["selection"]
    candidates = selection["candidates"]
    selected = selection["selected_candidate"]
    require(type(selected) is int and 0 <= selected < len(candidates), "invalid selected candidate")
    checks = {
        "input_sha": (verify_pin(job["input"]) and result["input_sha256"] == job["canonical_sha256"]),
        "native_identity": result["native"] == protocol["native"],
        "id_seed": (result["canonical_job_id"] == job["job_id"] and result["seed"] == job["seed"]
                    and result["n_qubits"] == job["qubits"]),
        "instruction_sha": stable(trace["instructions"]) == result["native_instruction_sha256"],
        "selected_mapping_sha": stable(mapping) == result["selected_mapping_sha256"] == selection["selected_mapping_sha256"],
        "config": selection["config"] == {**FIXED, "seed": job["seed"]},
        "candidate_hashes": (len(candidates) == FIXED["candidates"] and
                             all(stable(c["mapping"]) == c["mapping_sha256"] for c in candidates)),
        "selected_candidate_mapping": candidates[selected]["mapping"] == mapping,
        "physics_exact": validation == result["validation"] and validation["ok"] and validation["ghost_hits"] == 0,
        "logical_exact": ledger == result["logical_validation"] and ledger["ok"],
        "score_exact": score == result["score"],
        "canonical_gate_count": (score["counts"]["one_qubit_gates"] == job["gates_1q"] and
                                 score["counts"]["two_qubit_gates"] == job["gates_2q"]),
        "layers_count": len(observed_layers) == result["total_layers"],
        "quality": (result["status"] == "success" and result["origin"] == "fresh"
                    and result["quality_classification"] == "valid_fidelity" and not score["ood"]),
    }
    failed = [name for name, ok in checks.items() if ok is not True]
    require(not failed, "quality replay failed: " + ", ".join(failed))
    return checks


def audit_job(root, protocol, job, replayers, architecture):
    job_id = job["job_id"]
    require(job_id in JOB_IDS and job["origin"] == "fresh", "unauthorized recovery identity")
    folder = root / "quality/jobs" / job_id
    receipts = root / "quality/receipts"
    result_ref = pin(folder / "result.json")
    result = read_json(result_ref["path"])
    stdout_ref = pin(receipts / (job_id + ".log"))
    stdout = read_json(stdout_ref["path"])
    started_ref = pin(receipts / (job_id + ".started.json"))
    started = read_json(started_ref["path"])
    require(started["job_id"] == job_id and isinstance(started["at"], str) and bool(started["at"]),
            "started marker identity/time missing")
    datetime.fromisoformat(started["at"])
    require(stdout["result"] == result_ref and stdout["quality_classification"] == result["quality_classification"],
            "worker stdout result hash/classification differs")
    trace_ref = result["native_trace"]
    require(Path(trace_ref["path"]).resolve() == (folder / "native_trace.json.gz").resolve(),
            "trace is not this worker's original output")
    verify_pin(trace_ref)
    trace = json.loads(gzip.decompress(Path(trace_ref["path"]).read_bytes()))
    checks = validate_quality(protocol, job, result, trace, replayers, architecture)
    checks.update(result_stdout_sha=True, native_trace_sha=True)
    require(set(checks) == CHECK_NAMES, "incomplete quality audit")
    return {"job_id": job_id, "canonical_sha256": job["canonical_sha256"], "seed": job["seed"],
            "result": result_ref, "native_trace": trace_ref, "started_receipt": started_ref,
            "worker_stdout": stdout_ref, "compiler_log": pin(folder / "compiler.log"),
            "checks": checks, "observed_returncode": None, "exit_reason": "unknown",
            "timing_eligible": False, "quality_classification": result["quality_classification"]}


def audit(root=ROOT):
    protocol, source_checks = load_protocol(root)
    assert_no_workers()
    replayers = load_replayers(protocol)
    architecture = read_json(protocol["architecture"]["path"])
    jobs = {job["job_id"]: job for job in protocol["jobs"]}
    rows = [audit_job(root, protocol, jobs[job_id], replayers, architecture) for job_id in JOB_IDS]
    # Detect source or output drift across replay before emitting any audit.
    _, after = load_protocol(root)
    require(after == source_checks, "source changed during replay")
    for row in rows:
        for field in ("result", "native_trace", "started_receipt", "worker_stdout", "compiler_log"):
            verify_pin(row[field])
    assert_no_workers()
    return {"audit_schema": 1, "audit_type": "interrupted_worker_quality_recovery",
            "audited_at": datetime.now(timezone.utc).isoformat(), "auditor": pin(__file__),
            "protocol": pin(root / "protocol.json"), "protocol_sha256": PROTOCOL_SHA256,
            "source_checks": source_checks, "jobs": rows, "supervision_gap": True,
            "exit_reason": "unknown", "timing_eligible": False, "formal_paper_result": False,
            "logical_replay_boundary": "Input operation order and trace layer ledgers are recomputed; "
            "the compiled layer hash is checked against the original recorded hash, without rerunning scheduling."}


def make_receipt(row, audit_ref):
    return {"job_id": row["job_id"], "phase": "quality", "status": "recovered_success",
            "protocol_sha256": PROTOCOL_SHA256, "result": row["result"],
            "quality_classification": row["quality_classification"], "started_at": None,
            "returncode": None, "elapsed_s": None, "ended_at": None,
            "peak_observed_rss_bytes": None,
            "recovery": {"supervision_gap": True, "returncode_observed": False,
                         "timing_eligible": False, "exit_reason": "unknown",
                         "started_receipt": row["started_receipt"], "audit": audit_ref}}


def apply_audit(report, root=ROOT):
    require(tuple(row["job_id"] for row in report["jobs"]) == JOB_IDS, "recovery target set changed")
    assert_no_workers()
    assert_missing_terminals(root)
    load_protocol(root)
    for row in report["jobs"]:
        require(set(row["checks"]) == CHECK_NAMES and all(ok is True for ok in row["checks"].values()),
                "refusing an incomplete recovery audit")
        for field in ("result", "native_trace", "started_receipt", "worker_stdout", "compiler_log"):
            verify_pin(row[field])
    folder = root / "recovery"
    folder.mkdir(exist_ok=True)
    path = folder / ("audit-" + str(uuid.uuid4()) + ".json")
    write_new(path, report)
    audit_ref = pin(path)
    receipt_refs = []
    for row in report["jobs"]:
        target = root / "quality/receipts" / (row["job_id"] + ".json")
        write_new(target, make_receipt(row, audit_ref))
        receipt_refs.append(pin(target))
    return {"audit": audit_ref, "receipts": receipt_refs, "status": "recovered_success",
            "timing_eligible": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="append audited missing receipts exclusively")
    args = parser.parse_args()
    if args.apply:
        with recovery_lock():
            assert_missing_terminals()
            output = apply_audit(audit())
    else:
        output = audit()
    print(json.dumps(output, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
