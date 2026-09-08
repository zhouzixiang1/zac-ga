"""Resume unstarted quality jobs without altering the sealed study or old attempts.

The launcher detaches a supervisor with durable stdout/stderr and a live state
file. Existing claims, results, and terminal failures are never retried here.
Interrupted-worker recovery is a separate independently audited operation.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import traceback

SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[2]
BASE_PATH = SCRIPT.with_name("default_initial_study.py")
SPEC = importlib.util.spec_from_file_location("sealed_default_initial_study", BASE_PATH)
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)
RUNS = BASE.BUILD / "resumptions"


def select_unstarted(protocol, root):
    selected, unresolved = [], []
    for job in protocol["jobs"]:
        if job["origin"] != "fresh":
            continue
        receipts = root / "quality/receipts"
        job_id = job["job_id"]
        if (receipts / (job_id + ".json")).exists():
            continue
        if (receipts / (job_id + ".started.json")).exists():
            unresolved.append(job_id)
            continue
        if (root / "quality/jobs" / job_id).exists():
            raise ValueError("unclaimed output requires separate audit: " + job_id)
        selected.append(job)
    return selected, unresolved


def state(run_dir, **values):
    # Operational snapshot only; immutable experimental receipts use write_new.
    target = run_dir / "state.json"
    temp = run_dir / "state.json.tmp"
    temp.write_text(json.dumps({"updated_at": BASE.now(), "pid": os.getpid(), **values},
                               indent=2, sort_keys=True) + "\n")
    os.replace(temp, target)


def validate_run_dir(path):
    path = Path(path).resolve()
    if path.parent != RUNS.resolve() or not path.is_dir():
        raise ValueError("run directory must be a new dedicated resumptions child")
    return path


def execute(run_dir):
    run_dir = validate_run_dir(run_dir)
    launch = BASE.checked(BASE.read(run_dir / "launch.sha256.json"))
    BASE.checked(launch["protocol"])
    if BASE.digest(SCRIPT) != launch["supervisor"]["sha256"]:
        raise ValueError("supervisor changed after launch")
    protocol_path = BASE.ROOT / "protocol.json"
    protocol = BASE.load_protocol(protocol_path)
    workers = launch["workers"]
    if workers != 4 or workers > protocol["max_workers"]:
        raise ValueError("resume must retain the sealed four-worker ceiling")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with (BASE.ROOT / ".execution.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        selected, unresolved = select_unstarted(protocol, BASE.ROOT)
        for job_id in protocol["smoke_job_ids"]:
            rec = BASE.read(BASE.ROOT / "smoke/receipts" / (job_id + ".json"))
            if rec["status"] != "success" or not BASE.checked(rec["result"])["historical_h2_parity"]:
                raise ValueError("public-default parity prerequisites failed")
        BASE.write_new(run_dir / "execution.json", {
            "pid": os.getpid(), "started_at": BASE.now(), "protocol": BASE.pin(protocol_path),
            "selected_jobs": [j["job_id"] for j in selected],
            "unresolved_previous_claims": unresolved, "workers": workers,
            "timeout_s": protocol["timeout_s"], "rss_limit_bytes": protocol["rss_limit_bytes"],
            "retry_policy": "Only previously unstarted identities; no retry of any terminal outcome or claim."})
        completed, errors = 0, []
        state(run_dir, status="running", selected=len(selected), completed=completed,
              unresolved_previous_claims=unresolved, errors=errors)
        print(f"Resuming {len(selected)} unstarted jobs with {workers} workers; "
              f"{len(unresolved)} previous claims require separate audit.", flush=True)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {pool.submit(BASE.run_one, protocol_path, protocol, job, "quality"): job
                           for job in selected}
                while pending:
                    finished, _ = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
                    for future in finished:
                        job = pending.pop(future)
                        try:
                            receipt = future.result()
                            print(f"[{completed + 1}/{len(selected)}] {job['job_id']} "
                                  f"{receipt['status']} {receipt['elapsed_s']:.1f}s", flush=True)
                        except Exception as exc:
                            event = {"job_id": job["job_id"], "at": BASE.now(),
                                     "error": repr(exc), "traceback": traceback.format_exc(),
                                     "action": "No synthetic terminal receipt; preserve claim and inspect."}
                            BASE.write_new(run_dir / "errors" / (job["job_id"] + ".json"), event)
                            errors.append(job["job_id"])
                            print(event["traceback"], flush=True)
                        completed += 1
                    state(run_dir, status="running", selected=len(selected), completed=completed,
                          outstanding=len(pending), unresolved_previous_claims=unresolved, errors=errors)
            summary = BASE.summarize(protocol)
            BASE.write_new(run_dir / "completion.json", {
                "at": BASE.now(), "completed_dispatches": completed, "errors": errors,
                "summary": summary, "experiment_complete": not summary["counts"].get("pending", 0)})
            state(run_dir, status="finished" if not errors else "needs_attention",
                  selected=len(selected), completed=completed, errors=errors, summary=summary)
            print(json.dumps(summary, indent=2), flush=True)
        except BaseException:
            state(run_dir, status="supervisor_error", selected=len(selected), completed=completed,
                  errors=errors, traceback=traceback.format_exc())
            raise


def launch():
    protocol_path = BASE.ROOT / "protocol.json"
    protocol = BASE.load_protocol(protocol_path)
    with (BASE.ROOT / ".execution.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        selected, unresolved = select_unstarted(protocol, BASE.ROOT)
    if not selected:
        raise ValueError("no unstarted jobs; inspect unresolved claims or final summary")
    RUNS.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="resume-", dir=RUNS))
    BASE.write_new(run_dir / "launch.json", {
        "at": BASE.now(), "protocol": BASE.pin(protocol_path), "supervisor": BASE.pin(SCRIPT),
        "workers": 4, "unstarted_at_launch": len(selected),
        "unresolved_previous_claims": unresolved, "original_exit_reason": "unknown"})
    BASE.write_new(run_dir / "launch.sha256.json", BASE.pin(run_dir / "launch.json"))
    command = [sys.executable, "-B", str(SCRIPT), "--execute", "--run-dir", str(run_dir)]
    with (run_dir / "supervisor.log").open("xb") as log:
        process = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                   env={**os.environ, **BASE.ENV}, close_fds=True)
    BASE.write_new(run_dir / "process.json", {"pid": process.pid, "command": command,
                                               "launched_at": BASE.now()})
    time.sleep(0.5)
    if process.poll() is not None:
        raise RuntimeError("supervisor exited during launch; inspect " + str(run_dir / "supervisor.log"))
    return {"pid": process.pid, "run_dir": str(run_dir), "log": str(run_dir / "supervisor.log"),
            "selected": len(selected), "unresolved_previous_claims": len(unresolved)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--launch", action="store_true")
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--plan", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    if args.launch:
        print(json.dumps(launch(), indent=2))
    elif args.execute:
        execute(args.run_dir)
    else:
        protocol = BASE.load_protocol(BASE.ROOT / "protocol.json")
        selected, unresolved = select_unstarted(protocol, BASE.ROOT)
        print(json.dumps({"unstarted": len(selected), "unresolved_claims": unresolved}, indent=2))


if __name__ == "__main__":
    main()
