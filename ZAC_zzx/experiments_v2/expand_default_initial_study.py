"""Add two cooperative workers to a live four-worker resume supervisor.

Exclusive original job claims prevent duplicate executions. A separate pinned
amendment records the six-worker interval, including affected primary jobs.
This changes resource contention and potentially timeout coverage, not just
timing comparability; original outcomes and computational controls stay intact.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[2]
SPEC = importlib.util.spec_from_file_location("default_initial_base", SCRIPT.with_name("default_initial_study.py"))
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)
RUNS = BASE.BUILD / "expansions"


def live_processes():
    output = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid=,command="], text=True)
    rows = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3:
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
    return rows


def read_complete(path):
    for attempt in range(10):
        try:
            return BASE.read(path)
        except json.JSONDecodeError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def pressure_free_percent():
    output = subprocess.check_output(["memory_pressure", "-Q"], text=True, timeout=10)
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", output)
    if not match:
        raise ValueError("cannot read memory pressure; do not admit extra worker")
    return int(match.group(1))


def select_reverse(protocol, execution, root):
    selected = set(execution["selected_jobs"])
    jobs = {j["job_id"]: j for j in protocol["jobs"] if j["origin"] == "fresh"}
    if not selected <= jobs.keys():
        raise ValueError("primary selection is not a subset of fresh protocol jobs")
    receipts = root / "quality/receipts"
    return [jobs[job_id] for job_id in reversed(execution["selected_jobs"])
            if not (receipts / (job_id + ".json")).exists()
            and not (receipts / (job_id + ".started.json")).exists()]


def verify_primary(primary):
    primary = Path(primary).resolve()
    launch = BASE.checked(BASE.read(primary / "launch.sha256.json"))
    execution = BASE.read(primary / "execution.json")
    if launch["workers"] != 4 or execution["workers"] != 4:
        raise ValueError("expected exactly four primary workers")
    if BASE.digest(launch["supervisor"]["path"]) != launch["supervisor"]["sha256"]:
        raise ValueError("primary supervisor drift")
    pid = execution["pid"]
    processes = live_processes()
    if not any(p == pid and "resume_default_initial_study.py --execute" in c for p, _, c in processes):
        raise ValueError("primary supervisor is not running")
    workers = [(p, parent) for p, parent, c in processes
               if "default_initial_study.py --worker " in c and str(BASE.ROOT / "protocol.json") in c]
    if len(workers) > 4 or any(parent != pid for _, parent in workers):
        raise ValueError("unexpected existing or orphan workers; manual audit required")
    return launch, execution


def execute(run_dir):
    run_dir = Path(run_dir).resolve()
    if run_dir.parent != RUNS.resolve():
        raise ValueError("invalid expansion output directory")
    amendment = BASE.checked(BASE.read(run_dir / "amendment.sha256.json"))
    if BASE.digest(SCRIPT) != amendment["supervisor"]["sha256"]:
        raise ValueError("expansion supervisor drift")
    protocol_path = BASE.ROOT / "protocol.json"
    protocol = BASE.load_protocol(protocol_path)
    BASE.checked(amendment["protocol"])
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with (BASE.ROOT / ".expansion.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        primary_launch, execution = verify_primary(amendment["primary_run_dir"])
        if BASE.pin(Path(amendment["primary_run_dir"]) / "execution.json") != amendment["primary_execution"]:
            raise ValueError("primary execution drift")
        jobs = select_reverse(protocol, execution, BASE.ROOT)
        started_at = BASE.now()
        BASE.write_new(run_dir / "execution.json", {
            "pid": os.getpid(), "started_at": started_at,
            "primary_pid": execution["pid"], "extra_workers": 2, "total_ceiling": 6,
            "reverse_queue": [j["job_id"] for j in jobs],
            "primary_launch": primary_launch, "amendment": BASE.pin(run_dir / "amendment.json")})
        admission = threading.Lock()

        def run_extra(job):
            job_id = job["job_id"]
            receipts = BASE.ROOT / "quality/receipts"
            claim, terminal = receipts / (job_id + ".started.json"), receipts / (job_id + ".json")
            if terminal.exists():
                return {"job_id": job_id, "action": "already_terminal", "status": read_complete(terminal)["status"]}
            if claim.exists():
                return {"job_id": job_id, "action": "primary_claimed"}
            while True:
                with admission:
                    free = pressure_free_percent()
                    if free >= amendment["minimum_memory_free_percent"]:
                        break
                print(f"Memory admission paused for {job_id}: free={free}%", flush=True)
                time.sleep(5)
            try:
                receipt = BASE.run_one(protocol_path, protocol, job, "quality")
                return {"job_id": job_id, "action": "terminal_observed", "status": receipt["status"]}
            except FileExistsError as exc:
                if Path(exc.filename or "").resolve() != claim.resolve():
                    raise
                return {"job_id": job_id, "action": "exclusive_claim_race"}
            except json.JSONDecodeError:
                if not terminal.exists():
                    raise
                return {"job_id": job_id, "action": "concurrent_terminal_observed",
                        "status": read_complete(terminal)["status"]}

        errors = []
        print(f"Expansion started: primary4 + extra2, {len(jobs)} reverse candidates.", flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(run_extra, job): job for job in jobs}
            for index, future in enumerate(as_completed(futures), 1):
                job = futures[future]
                try:
                    event = {**future.result(), "at": BASE.now()}
                except Exception as exc:
                    event = {"job_id": job["job_id"], "action": "supervisor_error",
                             "at": BASE.now(), "error": repr(exc), "traceback": traceback.format_exc()}
                    errors.append(job["job_id"])
                BASE.write_new(run_dir / "events" / (job["job_id"] + ".json"), event)
                print(f"[{index}/{len(jobs)}] {event}", flush=True)
        ended_at = BASE.now()
        affected = []
        for job in protocol["jobs"]:
            if job["origin"] != "fresh":
                continue
            terminal = BASE.ROOT / "quality/receipts" / (job["job_id"] + ".json")
            claim = BASE.ROOT / "quality/receipts" / (job["job_id"] + ".started.json")
            if not claim.exists():
                continue
            begin = read_complete(claim)["at"]
            record = read_complete(terminal) if terminal.exists() else {}
            end = record.get("ended_at")
            if begin <= ended_at and (end is None or end >= started_at):
                affected.append(job["job_id"])
        BASE.write_new(run_dir / "completion.json", {
            "started_at": started_at, "ended_at": ended_at, "errors": errors,
            "contention_affected_job_ids": affected,
            "includes_primary_jobs": True, "requires_final_independent_audit": True,
            "claim": "No retries; concurrent wall-time limits may change completion coverage."})
        print("Expansion finished; final summary requires both supervisors to be idle.", flush=True)


def launch(primary):
    protocol = BASE.load_protocol(BASE.ROOT / "protocol.json")
    with (BASE.ROOT / ".expansion.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, execution = verify_primary(primary)
    if pressure_free_percent() < 25:
        raise ValueError("not enough memory headroom to expand")
    RUNS.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="extra-two-", dir=RUNS))
    amendment = {"amendment_type": "author_requested_concurrency_expansion", "at": BASE.now(),
        "protocol": BASE.pin(BASE.ROOT / "protocol.json"), "supervisor": BASE.pin(SCRIPT),
        "primary_run_dir": str(Path(primary).resolve()),
        "primary_execution": BASE.pin(Path(primary) / "execution.json"),
        "original_workers": 4, "total_workers": 6, "extra_workers": 2,
        "minimum_memory_free_percent": 25, "timeout_s": protocol["timeout_s"],
        "rss_limit_bytes": protocol["rss_limit_bytes"], "computational_controls_changed": False,
        "timing_comparison_eligible": False,
        "coverage_caveat": "Additional contention can affect the 600-second wall-limit outcomes of both primary and extra workers.",
        "user_authorization": "可以增加并行来提速", "existing_results_preserved": True}
    BASE.write_new(run_dir / "amendment.json", amendment)
    BASE.write_new(run_dir / "amendment.sha256.json", BASE.pin(run_dir / "amendment.json"))
    command = [sys.executable, "-B", str(SCRIPT), "--execute", str(run_dir)]
    with (run_dir / "supervisor.log").open("xb") as log:
        process = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True,
                                   close_fds=True, env={**os.environ, **BASE.ENV})
    BASE.write_new(run_dir / "process.json", {"pid": process.pid, "command": command})
    time.sleep(.5)
    if process.poll() is not None:
        raise RuntimeError("expansion exited at launch; inspect " + str(run_dir / "supervisor.log"))
    return {"pid": process.pid, "run_dir": str(run_dir), "total_workers": 6}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--launch", type=Path, help="existing four-worker primary run directory")
    mode.add_argument("--execute", type=Path)
    args = parser.parse_args()
    if args.launch:
        print(json.dumps(launch(args.launch), indent=2))
    else:
        execute(args.execute)
