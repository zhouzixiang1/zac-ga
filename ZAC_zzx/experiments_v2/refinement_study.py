"""Pre-registered, isolated return-candidate refinement of the frozen ABI9 method.

Only this study's new result directory and manuscript-local build are writable.
The historical compiler is exported from a Git object, never the dirty tree.
No unsuccessful study can change defaults, accepted results or the manuscript.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import statistics
import subprocess
import sys
import time


PROTOCOL_ID = "galk-return-coverage-refinement-v1"
SOURCE_COMMIT = "e85b29514caea66c31d305fbb421fd27c2ab1667"
SPLIT_SALT = "galk-refinement-v1|"
VARIANTS = {"reference": {}, "A_domain8": {"return_candidate_limit": 8},
            "B_assign6": {"return_assignment_k": 6}}
DEV_COUNTS = {"zac18": 4, "le_300": 3, "301_1500": 3, "gt_1500": 2}
LIMITS = {"minimum_fidelity_ratio": 1.001, "minimum_dataset_ratio": 1.0,
          "minimum_circuit_ratio": .99, "maximum_cpu_ratio": 1.20,
          "validation_minimum_circuits": 12, "validation_minimum_per_dataset": 4,
          "bootstrap_repetitions": 20000, "bootstrap_seed": 712}
ROOT = Path(__file__).resolve().parents[2]
PAPER_BASE = "fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1"
FREEZE = PAPER_BASE + "/freeze/freeze_manifest.json"
CONFIG = PAPER_BASE + "/configs/ablation/h8-seed0.json"
PYTHON = PAPER_BASE + "/build/abi9-h0fix-venv/bin/python"
ARCH = "ZAC_zzx/hardware_spec/full_architecture.json"
SOURCE_SCOPES = ("zzx", "zac", "streaming", "evaluation", "experiments_v2", "verify_batches.py")
SOURCE_REQUIRED = ("verify_batches.py", "zzx/zac_zzx.py", "zzx/native_backend.py",
                   "zac/ds/architecture.py", "evaluation/validator.py", "evaluation/scorer.py",
                   "experiments_v2/runtime_benchmark.py")
SEEDS = {"development": [0], "validation": [0, 1, 2]}
SMOKE_POLICY = "first-development-canonical-reference-seed0; diagnostic-only"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2,
                  allow_nan=False)
        stream.write("\n")


def finite_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [finite_json(item) for item in value]
    return value


def contained(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    if path == parent or parent not in path.parents:
        raise ValueError("a named child directory inside the study root is required")
    return path


def derive_split(freeze):
    excluded = {tuple(pair) for pair in freeze["parity_timing_cohort"]["identities"]}
    identities = [tuple(pair) for pair in freeze["ablation_cohort"]["identities"]]
    if len(identities) != 48 or len(set(identities)) != 48 or len(excluded) != 12:
        raise ValueError("the fixed 48/12 input design changed")
    excluded_hashes = {freeze["canonical_suites"][ds]["canonical_inputs"][name]
                       for ds, name in excluded}
    strata = {name: stratum for stratum, row in
              freeze["ablation_cohort"]["datasets"]["qmap154"]["strata"].items()
              for name in row["circuits"]}
    groups = defaultdict(dict)
    for ds, name in identities:
        digest = freeze["canonical_suites"][ds]["canonical_inputs"][name]
        if (ds, name) in excluded or digest in excluded_hashes:
            continue
        stratum = "zac18" if ds == "zac18" else strata[name]
        group = groups[stratum].setdefault(digest, {
            "dataset": ds, "canonical_sha256": digest, "stratum": stratum,
            "aliases": []})
        group["aliases"].append(name)
    result = {"development": [], "validation": [],
              "excluded": [list(item) for item in sorted(excluded)]}
    for stratum, count in DEV_COUNTS.items():
        ordered = sorted(groups[stratum].values(), key=lambda item:
                         hashlib.sha256((SPLIT_SALT + item["canonical_sha256"]).encode()).hexdigest())
        for index, group in enumerate(ordered):
            group["aliases"].sort()
            group["circuit"] = group["aliases"][0]
            result["development" if index < count else "validation"].append(group)
    hashes = [row["canonical_sha256"] for phase in ("development", "validation")
              for row in result[phase]]
    if len(result["development"]) != 12 or len(result["validation"]) != 23 or len(set(hashes)) != 35:
        raise ValueError("the approved 12/23 canonical split changed")
    return result


def variant_setting(base, name):
    if name not in VARIANTS:
        raise ValueError("unregistered refinement variant")
    if base.get("return_candidate_limit") != 6 or base.get("return_assignment_k") != 4:
        raise ValueError("the frozen reference return domain is not (6,4)")
    result = deepcopy(base)
    result.update(VARIANTS[name])
    changed = {key for key in set(base) | set(result) if base.get(key) != result.get(key)}
    if changed != set(VARIANTS[name]):
        raise ValueError("variant changed an unregistered setting")
    return result


def protected_hashes(repo):
    result = {}
    for directory in (repo / "ZAC_zzx/results/paper_zh_v2",
                      repo / "ZAC_zzx/results/initial_lookahead_v1"):
        if not directory.is_dir() or not any(directory.iterdir()):
            raise ValueError(f"required protected evidence is missing: {directory}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("protected evidence must not contain symlinks")
            if path.is_file():
                result[str(path.relative_to(repo))] = file_hash(path)
    return result


def verify_protected(repo, protocol):
    observed = protected_hashes(repo)
    if observed != protocol["protected_evidence"]:
        raise RuntimeError("existing evidence changed; stop without acceptance")
    return {"files": len(observed), "sha256": stable_hash(observed), "unchanged": True}


def export_source(repo, destination):
    prefixes = ["ZAC_zzx/" + name for name in SOURCE_SCOPES]
    names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", SOURCE_COMMIT,
                                     "--", *prefixes], cwd=repo, text=True).splitlines()
    hashes = {}
    for name in names:
        if not name.endswith(".py"):
            continue
        payload = subprocess.check_output(["git", "show", SOURCE_COMMIT + ":" + name], cwd=repo)
        target = destination / name.removeprefix("ZAC_zzx/")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(payload)
        hashes[str(target)] = hashlib.sha256(payload).hexdigest()
    if not hashes:
        raise ValueError("historical compiler export is empty")
    if any(str(destination / relative) not in hashes for relative in SOURCE_REQUIRED):
        raise ValueError("historical export lacks a required compiler/verifier dependency")
    return hashes


def runtime_env(source):
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0",
               OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    return env


def freeze_study(repo, name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,75}", name):
        raise ValueError("invalid study name")
    output = contained(repo / "ZAC_zzx/results/refinement_v1" / name,
                       repo / "ZAC_zzx/results/refinement_v1")
    build_root = repo / "IEEE_conference_template/build/refinement_v1"
    build = contained(build_root / name, build_root)
    if output.exists() or build.exists():
        raise FileExistsError("study and build directories must both be new")
    frozen = load(repo / FREEZE)
    split = derive_split(frozen)
    for phase in ("development", "validation"):
        for row in split[phase]:
            row["input"] = str(repo / "fidelity-lookahead-v2/artifacts/canonical" /
                               row["dataset"] / (row["circuit"] + ".qasm"))
            for alias in row["aliases"]:
                candidate = Path(row["input"]).with_name(alias + ".qasm")
                if file_hash(candidate) != row["canonical_sha256"]:
                    raise ValueError("canonical input/alias hash mismatch")
    base = load(repo / CONFIG)["base_config"]["zac_setting"][0]
    settings = {variant: variant_setting(base, variant) for variant in VARIANTS}
    evidence = protected_hashes(repo)
    source = build / "source"
    source.mkdir(parents=True)
    sources = export_source(repo, source)
    runner = build / "refinement_runner.py"
    with runner.open("xb") as stream:
        stream.write(Path(__file__).read_bytes())
    sources[str(runner)] = file_hash(runner)
    python = repo / PYTHON
    probe = subprocess.run([str(python), "-B", "-c",
        "import json, verify_batches; from evaluation import FidelityModel; "
        "from zzx.native_backend import build_info; "
        "value=build_info(require_registered_wheel=True, "
        "expected_wheel_sha256=" + repr(base["native_wheel_sha256"]) + "); "
        "print(json.dumps({'native':value,'model':FidelityModel().to_dict(),"
        "'verifier_file':verify_batches.__file__}))"],
        env=runtime_env(source), cwd=source, check=True, capture_output=True, text=True)
    probe_data = json.loads(probe.stdout)
    native = probe_data["native"]
    if (native.get("native_abi_version") != 9 or not native.get("wheel_registered")
            or native.get("native_wheel_sha256") != base["native_wheel_sha256"]
            or Path(probe_data["verifier_file"]).resolve() != source / "verify_batches.py"):
        raise ValueError("frozen ABI9/verifier probe failed")
    output.mkdir(parents=True)
    protocol = {"protocol_id": PROTOCOL_ID, "sealed_at": utc_now(), "repo": str(repo),
        "root": str(output), "build": str(build), "source": str(source),
        "runner": str(runner), "python": str(python), "source_commit": SOURCE_COMMIT,
        "execution_source_hashes": sources, "native": native,
        "scoring_model": probe_data["model"],
        "native_wheel_sha256": base["native_wheel_sha256"],
        "reference_config": str(repo / CONFIG), "reference_config_sha256": file_hash(repo / CONFIG),
        "architecture": str(repo / ARCH), "architecture_sha256": file_hash(repo / ARCH),
        "freeze_manifest": str(repo / FREEZE), "freeze_manifest_sha256": file_hash(repo / FREEZE),
        "split": split, "split_salt": SPLIT_SALT, "variants": settings, "limits": LIMITS,
        "seeds": SEEDS, "smoke_policy": SMOKE_POLICY,
        "workers": 1, "compile_timeout_s": 600, "memory_limit_bytes": 3 * 1024**3,
        "protected_evidence": evidence, "protected_evidence_sha256": stable_hash(evidence),
        "formal_paper_result": False, "automatic_promotion": False,
        "prior_exposure": "Historical results may have been seen; validation is not used in this round's selection.",
        "failure_policy": "No retry, overwrite, alias substitution or threshold change. Retain every outcome.",
        "runtime_policy": "Paired process CPU is an overhead gate, not a speedup claim; wall times under contention stay descriptive.",
        "quality_policy": "Circuit medians of seed log fidelity; reference-valid cohort fixed per phase; any candidate loss of reference coverage rejects it.",
        "selection_order": ["decreasing_fidelity_ratio", "increasing_cpu_ratio", "variant_name"],
        "validation_policy": "Select at most one development-passing variant, then freeze validation jobs. Failed validation retains the original method."}
    protocol["seal_sha256"] = stable_hash(protocol)
    write_json(output / "protocol.json", protocol)
    return {"status": "sealed", "protocol": str(output / "protocol.json"),
            "seal_sha256": protocol["seal_sha256"], "development_jobs": 36,
            "maximum_validation_jobs": 138, "protected_files": len(evidence)}


def read_protocol(path):
    protocol = load(path)
    seal = protocol.pop("seal_sha256")
    if stable_hash(protocol) != seal:
        raise ValueError("protocol seal mismatch")
    protocol["seal_sha256"] = seal
    if protocol["protocol_id"] != PROTOCOL_ID or protocol["limits"] != LIMITS:
        raise ValueError("unregistered protocol or changed thresholds")
    if (protocol.get("source_commit") != SOURCE_COMMIT or protocol.get("seeds") != SEEDS
            or protocol.get("split_salt") != SPLIT_SALT or protocol.get("smoke_policy") != SMOKE_POLICY
            or protocol.get("workers") != 1 or protocol.get("compile_timeout_s") != 600
            or protocol.get("memory_limit_bytes") != 3 * 1024**3):
        raise ValueError("unregistered source, queue, seed or resource policy")
    if Path(path).resolve() != Path(protocol["root"]) / "protocol.json":
        raise ValueError("protocol path mismatch")
    for filename, expected in protocol["execution_source_hashes"].items():
        if file_hash(filename) != expected:
            raise ValueError("frozen execution source changed")
    source = Path(protocol["source"])
    actual = {str(item) for item in source.rglob("*.py") if item.is_file()}
    expected = set(protocol["execution_source_hashes"]) - {protocol["runner"]}
    if actual != expected or any(str(source / item) not in actual for item in SOURCE_REQUIRED):
        raise ValueError("frozen source dependency closure changed")
    for label in ("architecture", "reference_config", "freeze_manifest"):
        if file_hash(protocol[label]) != protocol[label + "_sha256"]:
            raise ValueError("frozen source input changed")
    base = load(protocol["reference_config"])["base_config"]["zac_setting"][0]
    if protocol["variants"] != {name: variant_setting(base, name) for name in VARIANTS}:
        raise ValueError("reference/variant settings differ from the fixed one-factor design")
    expected_split = derive_split(load(protocol["freeze_manifest"]))
    observed_split = deepcopy(protocol["split"])
    for phase in ("development", "validation"):
        for row in observed_split[phase]:
            expected_input = str(Path(protocol["repo"]) / "fidelity-lookahead-v2/artifacts/canonical" /
                                 row["dataset"] / (row["circuit"] + ".qasm"))
            if row.pop("input", None) != expected_input:
                raise ValueError("canonical job input path changed")
    if observed_split != expected_split:
        raise ValueError("development/validation canonical groups changed")
    return protocol


def phase_jobs(protocol, phase, selected=None):
    if phase == "smoke":
        return [next(job for job in phase_jobs(protocol, "development") if job["variant"] == "reference")]
    if phase not in ("development", "validation"):
        raise ValueError("unknown phase")
    if phase == "validation" and selected not in ("A_domain8", "B_assign6"):
        raise ValueError("validation requires one sealed development selection")
    variants = list(VARIANTS) if phase == "development" else ["reference", selected]
    jobs = []
    for row in protocol["split"][phase]:
        for seed in protocol["seeds"][phase]:
            # Rotate fixed arm order without consulting any runtime or quality result.
            rotation = int(row["canonical_sha256"][:8], 16) % len(variants)
            ordered = variants[rotation:] + variants[:rotation]
            for variant in ordered:
                jobs.append({**row, "seed": seed, "variant": variant,
                             "job_id": f"{row['dataset']}-{row['circuit']}-s{seed}-{variant}"})
    return jobs


def read_phase_protocol(protocol, path):
    document = load(path)
    seal = document.pop("phase_sha256", None)
    if stable_hash(document) != seal:
        raise ValueError("phase seal mismatch")
    document["phase_sha256"] = seal
    phase = document["phase"]
    if phase not in ("development", "validation", "smoke"):
        raise ValueError("unknown sealed phase")
    if Path(path).resolve() != Path(protocol["root"]) / phase / "phase_protocol.json":
        raise ValueError("phase path mismatch")
    if document["protocol_sha256"] != protocol["seal_sha256"]:
        raise ValueError("phase does not belong to protocol")
    selected = document.get("selected_variant")
    if phase != "validation" and selected is not None:
        raise ValueError("selection cannot be injected before development analysis")
    if document["jobs"] != phase_jobs(protocol, phase, selected):
        raise ValueError("phase jobs differ from the registered queue")
    if phase == "validation":
        decision = Path(protocol["root"]) / "development/decision.json"
        if file_hash(decision) != document["development_decision_sha256"]:
            raise ValueError("development decision changed after validation was sealed")
        if load(decision).get("selected_variant") != selected:
            raise ValueError("validation candidate differs from development selection")
    return document


def logical_check(qasm, n_qubits, events, compiled_layers):
    from experiments_v2.runtime_benchmark import ordered_two_qubit_layer_ledger, two_qubit_layer_ledger
    expected, observed, pairs, event_layers = {}, {}, [], []
    single = re.compile(r"^(?:u1|u2|u3)\s*\([^;]*\)\s+q\[(\d+)\]\s*;$")
    double = re.compile(r"^cz\s+q\[(\d+)\]\s*,\s*q\[(\d+)\]\s*;$")
    for raw in Path(qasm).read_text().splitlines():
        line = raw.split("//", 1)[0].strip()
        one, two = single.match(line), double.match(line)
        if one:
            expected.setdefault(int(one[1]), []).append("1q")
        elif two:
            a, b = int(two[1]), int(two[2])
            pairs.append((a, b))
            expected.setdefault(a, []).append(f"cz:{b}")
            expected.setdefault(b, []).append(f"cz:{a}")
        elif line and not re.fullmatch(r'(?:OPENQASM\s+2\.0|include\s+"qelib1\.inc"|[qc]reg\s+\w+\[\d+\])\s*;', line):
            raise ValueError(f"unrecognized canonical QASM statement: {line}")
    for event in events:
        if event.kind == "one_qubit_gate":
            for q in event.atoms:
                observed.setdefault(q, []).append("1q")
        elif event.kind == "two_qubit_gate":
            event_layers.append(event.gate_pairs)
            for a, b in event.gate_pairs:
                observed.setdefault(a, []).append(f"cz:{b}")
                observed.setdefault(b, []).append(f"cz:{a}")
    actual = ordered_two_qubit_layer_ledger(n_qubits, event_layers)
    scheduled = ordered_two_qubit_layer_ledger(n_qubits, compiled_layers)
    canonical = two_qubit_layer_ledger(n_qubits, pairs)
    if expected != observed or actual != scheduled or canonical != scheduled:
        raise ValueError("logical gate sequence or layer ledger mismatch")
    return {"ok": True, "operation_ledger_sha256": stable_hash(expected),
            "layer_ledger_sha256": actual["layer_ledger_sha256"]}


def worker(protocol_path, phase_path, job_id):
    protocol = read_protocol(protocol_path)
    phase = read_phase_protocol(protocol, phase_path)
    jobs = [job for job in phase["jobs"] if job["job_id"] == job_id]
    if len(jobs) != 1:
        raise ValueError("worker job is not registered")
    job = jobs[0]
    source = Path(protocol["source"])
    sys.path.insert(0, str(source))
    from evaluation import FidelityModel, normalize_zair, score_trace, validate_trace_physics
    from verify_batches import verify as verify_zair
    from zac.ds.architecture import Architecture
    from zzx.native_backend import build_info
    from zzx.zac_zzx import ZAC_zzx
    for module_name in ("evaluation", "zzx.zac_zzx", "zzx.native_backend", "zac.ds.architecture", "verify_batches"):
        if source not in Path(sys.modules[module_name].__file__).resolve().parents:
            raise ValueError("worker imported the live tree instead of frozen source")
    native = build_info(require_registered_wheel=True, expected_wheel_sha256=protocol["native_wheel_sha256"])
    for key in ("native_abi_version", "native_wheel_sha256", "extension_sha256", "extension_path"):
        if native.get(key) != protocol["native"].get(key):
            raise ValueError("worker native runtime differs from the sealed ABI9 reference")
    if file_hash(job["input"]) != job["canonical_sha256"]:
        raise ValueError("worker canonical input changed")
    root = Path(phase_path).parent / "jobs" / job_id
    root.mkdir(parents=True, exist_ok=False)
    setting = deepcopy(protocol["variants"][job["variant"]])
    setting.update(seed=job["seed"], name=job["circuit"], dir=str(root) + "/",
                   arch_spec=protocol["architecture"], use_verifier=False, resyn=False)
    result = {"job_id": job_id, "variant": job["variant"], "seed": job["seed"],
              "circuit": job["circuit"], "dataset": job["dataset"],
              "canonical_sha256": job["canonical_sha256"], "aliases": job["aliases"],
              "protocol_sha256": protocol["seal_sha256"], "phase_sha256": phase["phase_sha256"],
              "setting": setting, "native_runtime": native, "scoring_model": protocol["scoring_model"]}
    try:
        spec = load(protocol["architecture"])
        with (root / "compiler.log").open("x") as log, redirect_stdout(log):
            random.seed(job["seed"])
            started, cpu_started = time.perf_counter_ns(), time.process_time_ns()
            compiler = ZAC_zzx()
            compiler._paper_ablation_contract = {"native_abi_version": 9, "max_horizon": 8}
            compiler.parse_setting(deepcopy(setting))
            architecture = Architecture(deepcopy(spec))
            architecture.preprocessing()
            compiler.set_architecture_spec_path(protocol["architecture"])
            compiler.set_architecture(architecture)
            compiler.set_program(job["input"])
            trace = compiler.solve(save_file=False)
            result["compile_wall_ns"] = time.perf_counter_ns() - started
            result["compile_cpu_ns"] = time.process_time_ns() - cpu_started
            trace_path = root / "native_trace.json.gz"
            with gzip.open(trace_path, "xt", encoding="utf-8") as stream:
                json.dump(trace, stream, ensure_ascii=False, allow_nan=False)
            result["native_trace_sha256"] = file_hash(trace_path)
            raw_gate = verify_zair(trace_path, Path(job["input"]))
            result["zair_validation"] = {**raw_gate, "ok": not any(raw_gate["errors"].values())}
            events = tuple(normalize_zair(trace, architecture=spec))
            result["validation"] = validate_trace_physics(
                tuple(normalize_zair(trace, architecture=spec)), n_qubits=compiler.n_q)
            result["logical_validation"] = logical_check(job["input"], compiler.n_q, events,
                                                         compiler.gate_scheduling)
            score = score_trace(events, FidelityModel.from_mapping(protocol["scoring_model"]),
                                n_qubits=compiler.n_q).to_dict()
            result["score"] = finite_json(score)
            result["initial_mapping_sha256"] = stable_hash(compiler.qubit_mapping[0])
            result["total_layers"] = len(compiler.gate_scheduling)
            result["status"] = ("physics_failure" if not result["validation"]["ok"] or not result["zair_validation"]["ok"] else
                                 "model_out_of_domain" if score["ood"] or not
                                 math.isfinite(score["log_fidelity"]) else "success")
        read_protocol(protocol_path)
        result["frozen_source_unchanged"] = True
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
    write_json(root / "result.json", result)
    return {"job_id": job_id, "status": result["status"]}


def group_rss(pid):
    output = subprocess.check_output(["ps", "-axo", "pgid=,rss="], text=True)
    return 1024 * sum(int(fields[1]) for line in output.splitlines()
                      if len(fields := line.split()) == 2 and int(fields[0]) == pid)


def kill_group(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


@contextmanager
def study_lock(protocol):
    import fcntl
    with (Path(protocol["build"]) / "execution.lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("this study already has a running coordinator") from error
        yield


def run_phase(protocol_path, phase):
    protocol = read_protocol(protocol_path)
    with study_lock(protocol):
        return _run_phase(protocol_path, phase)


def _run_phase(protocol_path, phase):
    protocol = read_protocol(protocol_path)
    repo, root = Path(protocol["repo"]), Path(protocol["root"])
    verify_protected(repo, protocol)
    selected = None
    if phase == "validation":
        # Recompute the immutable development decision from its own registered
        # evidence before allowing any validation job to run.
        analyze_phase(protocol_path, "development")
        decision = load(root / "development" / "decision.json")
        if decision.get("selected_variant") not in ("A_domain8", "B_assign6"):
            raise ValueError("development did not accept a candidate")
        for path, digest in decision["evidence_hashes"].items():
            if file_hash(path) != digest:
                raise ValueError("development evidence changed after selection")
        selected = decision["selected_variant"]
    phase_root = root / phase
    phase_root.mkdir(exist_ok=True)
    phase_path = phase_root / "phase_protocol.json"
    jobs = phase_jobs(protocol, phase, selected)
    if not phase_path.exists():
        document = {"phase": phase, "sealed_at": utc_now(), "jobs": jobs,
            "protocol_sha256": protocol["seal_sha256"], "selected_variant": selected,
            "development_decision_sha256": file_hash(root / "development/decision.json")
            if phase == "validation" else None}
        document["phase_sha256"] = stable_hash(document)
        write_json(phase_path, document)
    else:
        old = read_phase_protocol(protocol, phase_path)
        if old["jobs"] != jobs or old["protocol_sha256"] != protocol["seal_sha256"]:
            raise ValueError("attempt to alter sealed phase")
    receipts = phase_root / "receipts"
    receipts.mkdir(exist_ok=True)
    logs = Path(protocol["build"]) / phase / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for index, job in enumerate(jobs):
        receipt = receipts / (job["job_id"] + ".json")
        if receipt.exists():
            read_phase_records(root, phase, [job], protocol=protocol)
            continue
        started_file = receipts / (job["job_id"] + ".started.json")
        if started_file.exists():
            raise RuntimeError("unfinished started job cannot be silently retried")
        read_protocol(protocol_path)
        read_phase_protocol(protocol, phase_path)
        command = [protocol["python"], "-B", protocol["runner"], "worker",
                   "--protocol", str(protocol_path), "--phase-protocol", str(phase_path),
                   "--job-id", job["job_id"]]
        write_json(started_file, {"job": job, "started_at": utc_now(), "command": command,
                                  "phase_protocol_sha256": file_hash(phase_path)})
        started, status, peak = time.monotonic(), None, 0
        with (logs / (job["job_id"] + ".log")).open("x") as log:
            process = subprocess.Popen(command, cwd=protocol["source"], env=runtime_env(protocol["source"]),
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed > protocol["compile_timeout_s"]:
                        status = "timeout"
                        kill_group(process)
                        break
                    peak = max(peak, group_rss(process.pid))
                    if peak > protocol["memory_limit_bytes"]:
                        status = "memory_limit"
                        kill_group(process)
                        break
                    time.sleep(1)
            except BaseException:
                kill_group(process)
                write_json(receipt, {"job_id": job["job_id"], "status": "interrupted",
                                     "elapsed_s": time.monotonic() - started,
                                     "phase_protocol_sha256": file_hash(phase_path),
                                     "result_sha256": None})
                raise
        result_path = phase_root / "jobs" / job["job_id"] / "result.json"
        result = load(result_path) if result_path.exists() else None
        status = status or (result["status"] if result is not None and process.returncode == 0 else "failed")
        write_json(receipt, {"job_id": job["job_id"], "status": status,
            "elapsed_s": time.monotonic() - started, "max_rss_bytes": peak,
            "returncode": process.returncode, "completed_at": utc_now(),
            "result_sha256": file_hash(result_path) if result else None,
            "phase_protocol_sha256": file_hash(phase_path)})
        print(json.dumps({"phase": phase, "completed": index + 1, "planned": len(jobs),
                          "job_id": job["job_id"], "status": status}), flush=True)
    evidence_check = verify_protected(repo, protocol)
    if not (phase_root / "protected_evidence_check.json").exists():
        write_json(phase_root / "protected_evidence_check.json", evidence_check)
    return analyze_phase(protocol_path, phase)


def read_phase_records(root, phase, jobs, *, protocol):
    records, hashes = {}, {}
    phase_path = root / phase / "phase_protocol.json"
    phase_doc = read_phase_protocol(protocol, phase_path)
    hashes[str(phase_path)] = file_hash(phase_path)
    for job in jobs:
        receipt = root / phase / "receipts" / (job["job_id"] + ".json")
        if not receipt.exists():
            raise ValueError("phase is incomplete; cannot select")
        row = load(receipt)
        if row.get("job_id") != job["job_id"] or row.get("phase_protocol_sha256") != hashes[str(phase_path)]:
            raise ValueError("receipt identity or phase binding differs from the registered job")
        hashes[str(receipt)] = file_hash(receipt)
        result_path = root / phase / "jobs" / job["job_id"] / "result.json"
        result = load(result_path) if result_path.exists() else {}
        if result:
            digest = file_hash(result_path)
            if row.get("result_sha256") != digest:
                raise ValueError("worker result hash differs from receipt")
            hashes[str(result_path)] = digest
            for key in ("job_id", "variant", "seed", "dataset", "circuit", "canonical_sha256"):
                if result.get(key) != job[key]:
                    raise ValueError("worker result identity differs from the registered job")
            if result.get("protocol_sha256") != protocol["seal_sha256"] or result.get("phase_sha256") != phase_doc["phase_sha256"]:
                raise ValueError("worker result is not bound to this sealed phase")
            expected_setting = deepcopy(protocol["variants"][job["variant"]])
            expected_setting.update(seed=job["seed"], name=job["circuit"],
                                    dir=str(result_path.parent) + "/", arch_spec=protocol["architecture"],
                                    use_verifier=False, resyn=False)
            if result.get("setting") != expected_setting:
                raise ValueError("worker effective setting differs from its registered variant")
        elif row.get("result_sha256") is not None or row.get("status") == "success":
            raise ValueError("receipt claims a missing worker result")
        if result.get("native_trace_sha256"):
            trace = result_path.with_name("native_trace.json.gz")
            if file_hash(trace) != result["native_trace_sha256"]:
                raise ValueError("native trace differs from the worker's sealed payload")
            hashes[str(trace)] = result["native_trace_sha256"]
        if row.get("status") == "success":
            if (result.get("status") != "success" or not result.get("frozen_source_unchanged")
                    or not result.get("zair_validation", {}).get("ok")
                    or any(result.get("zair_validation", {}).get("errors", {"missing": [1]}).values())
                    or not result.get("validation", {}).get("ok")
                    or result.get("validation", {}).get("ghost_hits") != 0
                    or not result.get("logical_validation", {}).get("ok")
                    or not result.get("native_trace_sha256")
                    or result.get("scoring_model") != protocol["scoring_model"]):
                raise ValueError("successful result lacks the full real gate/score evidence")
            if (not isinstance(result["score"].get("log_fidelity"), (int, float))
                    or not math.isfinite(result["score"]["log_fidelity"])
                    or result["score"].get("ood") is not False
                    or result.get("compile_cpu_ns", 0) <= 0):
                raise ValueError("successful result has invalid quality or CPU metrics")
            for key in ("native_abi_version", "native_wheel_sha256", "extension_sha256", "extension_path"):
                if result.get("native_runtime", {}).get(key) != protocol["native"].get(key):
                    raise ValueError("successful result used a different native runtime")
        records[(job["canonical_sha256"], job["seed"], job["variant"])] = {
            **result, "status": row["status"], "circuit": job["circuit"], "dataset": job["dataset"]}
    return records, hashes


def compare_candidate(groups, seeds, records, variant, *, validation=False):
    rows, missing, excluded, failures = [], [], [], []
    for group in groups:
        key = group["canonical_sha256"]
        reference = [records.get((key, seed, "reference"), {}) for seed in seeds]
        candidate = [records.get((key, seed, variant), {}) for seed in seeds]
        for seed, row in zip(seeds, candidate):
            if row.get("status") in ("failed", "physics_failure", "memory_limit", "interrupted"):
                failures.append({"circuit": group["circuit"], "seed": seed, "status": row.get("status")})
        if not all(row.get("status") == "success" for row in reference):
            excluded.append({"circuit": group["circuit"], "reference_statuses": [row.get("status") for row in reference]})
            continue
        if not all(row.get("status") == "success" for row in candidate):
            missing.append({"circuit": group["circuit"], "candidate_statuses": [row.get("status") for row in candidate]})
            continue
        if any(a["initial_mapping_sha256"] != b["initial_mapping_sha256"] for a, b in zip(reference, candidate)):
            failures.append({"circuit": group["circuit"], "status": "initial_mapping_changed"})
            continue
        delta = statistics.median(row["score"]["log_fidelity"] for row in candidate) - statistics.median(row["score"]["log_fidelity"] for row in reference)
        cpu = statistics.median(row["compile_cpu_ns"] for row in candidate) / statistics.median(row["compile_cpu_ns"] for row in reference)
        rows.append({"circuit": group["circuit"], "dataset": group["dataset"],
                     "stratum": group["stratum"], "log_fidelity_difference": delta,
                     "fidelity_ratio": math.exp(delta), "cpu_ratio": cpu})
    checks = {"reference_coverage_preserved": not missing, "no_execution_or_mapping_failures": not failures,
              "nonempty_comparison": bool(rows)}
    fidelity = math.exp(statistics.mean(row["log_fidelity_difference"] for row in rows)) if rows else None
    cpu = math.exp(statistics.mean(math.log(row["cpu_ratio"]) for row in rows)) if rows else None
    datasets = {ds: [row for row in rows if row["dataset"] == ds] for ds in ("zac18", "qmap154")}
    dataset_ratios = {ds: math.exp(statistics.mean(row["log_fidelity_difference"] for row in members))
                      if members else None for ds, members in datasets.items()}
    checks.update(fidelity_gain=fidelity is not None and fidelity >= LIMITS["minimum_fidelity_ratio"],
                  cpu_overhead=cpu is not None and cpu <= LIMITS["maximum_cpu_ratio"],
                  both_datasets_noninferior=all(value is not None and value >= LIMITS["minimum_dataset_ratio"] - 1e-12
                                               for value in dataset_ratios.values()),
                  no_large_circuit_loss=all(row["fidelity_ratio"] >= LIMITS["minimum_circuit_ratio"] for row in rows))
    lower = None
    if validation:
        checks["sufficient_circuits"] = len(rows) >= LIMITS["validation_minimum_circuits"]
        checks["sufficient_per_dataset"] = all(len(members) >= LIMITS["validation_minimum_per_dataset"] for members in datasets.values())
        if rows:
            rng, values = random.Random(LIMITS["bootstrap_seed"]), [row["log_fidelity_difference"] for row in rows]
            boot = sorted(statistics.mean(rng.choices(values, k=len(values)))
                          for _ in range(LIMITS["bootstrap_repetitions"]))
            lower = math.exp(boot[int(.05 * len(boot))])
        checks["positive_one_sided_95_lower_bound"] = lower is not None and lower > 1.0
    return {"variant": variant, "passed": all(checks.values()), "checks": checks,
            "complete_circuits": len(rows), "fidelity_ratio": fidelity, "cpu_ratio": cpu,
            "dataset_ratios": dataset_ratios, "one_sided_95_lower_bound": lower,
            "coverage_losses": missing, "reference_ineligible": excluded,
            "execution_failures": failures, "per_circuit": rows}


def analyze_phase(protocol_path, phase):
    protocol = read_protocol(protocol_path)
    root = Path(protocol["root"])
    phase_doc = read_phase_protocol(protocol, root / phase / "phase_protocol.json")
    records, hashes = read_phase_records(root, phase, phase_doc["jobs"], protocol=protocol)
    hashes[str(protocol_path)] = file_hash(protocol_path)
    if phase == "smoke":
        report = {"protocol_id": PROTOCOL_ID, "protocol_sha256": protocol["seal_sha256"],
                  "phase": "smoke", "status": "passed" if all(row["status"] == "success" for row in records.values()) else "failed",
                  "acceptance_eligible": False, "formal_paper_result": False, "automatic_promotion": False,
                  "fixed_job": phase_doc["jobs"][0], "evidence_hashes": hashes,
                  "protected_evidence_check": verify_protected(Path(protocol["repo"]), protocol)}
        target = root / "smoke/smoke_report.json"
        if target.exists():
            if load(target) != report:
                raise ValueError("immutable smoke report already differs")
        else:
            write_json(target, report)
        return {key: value for key, value in report.items() if key != "evidence_hashes"}
    variants = list(VARIANTS)[1:] if phase == "development" else [phase_doc["selected_variant"]]
    comparisons = [compare_candidate(protocol["split"][phase], protocol["seeds"][phase],
                     records, variant, validation=phase == "validation") for variant in variants]
    eligible = sorted((row for row in comparisons if row["passed"]),
                       key=lambda row: (-row["fidelity_ratio"], row["cpu_ratio"], row["variant"]))
    verification = verify_protected(Path(protocol["repo"]), protocol)
    report = {"protocol_id": PROTOCOL_ID, "phase": phase, "comparisons": comparisons,
              "protocol_sha256": protocol["seal_sha256"], "phase_sha256": phase_doc["phase_sha256"],
              "selected_variant": eligible[0]["variant"] if eligible else None,
              "status_counts": dict(Counter(row["status"] for row in records.values())),
              "formal_paper_result": False, "automatic_promotion": False,
              "keep_original_method": not eligible, "evidence_hashes": hashes,
              "protected_evidence_check": verification}
    target = root / phase / ("decision.json" if phase == "development" else "validation_report.json")
    if target.exists():
        if load(target) != report:
            raise ValueError("immutable decision/report already differs")
    else:
        write_json(target, report)
    return {key: value for key, value in report.items() if key not in ("evidence_hashes", "comparisons")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--name", required=True)
    for command in ("run", "analyze"):
        item = sub.add_parser(command)
        item.add_argument("--protocol", required=True, type=Path)
        item.add_argument("--phase", choices=("development", "validation", "smoke"), required=True)
    child = sub.add_parser("worker")
    child.add_argument("--protocol", required=True, type=Path)
    child.add_argument("--phase-protocol", required=True, type=Path)
    child.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        result = freeze_study(args.repo.resolve(), args.name)
    elif args.command == "worker":
        result = worker(args.protocol.resolve(), args.phase_protocol.resolve(), args.job_id)
    elif args.command == "run":
        result = run_phase(args.protocol.resolve(), args.phase)
    else:
        result = analyze_phase(args.protocol.resolve(), args.phase)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
