#!/usr/bin/env python3
"""Audit the sealed five-H study and export quality-only manuscript values.

No compiler is imported or invoked. All 84 new terminal receipts and 96 reused
manifests are checked before publication. The runner's fidelity-only summary is
recomputed separately; the table uses one common cohort for fidelity AND MOVE
batches. --check never creates, rewrites, or repairs a file.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import statistics

STUDY_RELATIVE = Path("ZAC_zzx/results/horizon_extension_v2")
PROTOCOL_ID = "horizon-shared12-seed012-frozen-abi9-v2"
PROTOCOL_SHA256 = "d899e7995eb252e51e19543bae98321979f7ff163f7858c10633ffaa017c643e"
# Append-only classification/continuation; original v2 receipts stay immutable.
CONTINUATION_SHA256 = "0dbd768b470982a178d844efb48b0156adacf33b3ae74733008ccfcc5c39350d"
HORIZONS = (0, 1, 2, 4, 8)
SEEDS = (0, 1, 2)
WORDS = {0: "Zero", 1: "One", 2: "Two", 4: "Four", 8: "Eight"}
COHORT = tuple(("zac18", n) for n in (
    "ghz_n23", "seca_n11_transpiled", "ghz_n40_transpiled", "ising_n42",
    "ghz_n78_transpiled", "ising_n98_transpiled")) + tuple(("qmap154", n) for n in (
    "4gt11_84", "4gt5_76", "squar5_261", "adr4_197", "clip_206", "hwb8_113"))
TERMINAL = {"success", "timeout", "oom", "compiler_error", "verifier_fail", "scorer_error", "runner_error"}
DERIVED_FILENAMES = ("horizon_extension_values.tex", "paper_horizon_extension_values.json",
    "paper_horizon_extension_provenance.json")
IDENTITY = ("dataset", "circuit", "horizon", "seed")
_UNPINNED = object()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def safe_json(value):
    """Keep nonfinite raw observations explicit without emitting invalid JSON."""
    if isinstance(value, dict):
        return {k: safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite": str(value)}
    return value


def compare(expected, actual, context):
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{context}: expected object")
        for key, value in expected.items():
            require(key in actual, f"{context}: missing {key}")
            compare(value, actual[key], f"{context}/{key}")
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(expected) == len(actual), f"{context}: list coverage changed")
        for i, (a, b) in enumerate(zip(expected, actual)):
            compare(a, b, f"{context}/{i}")
    elif isinstance(expected, float):
        same = (finite(actual) and math.isclose(expected, actual, rel_tol=1e-10, abs_tol=1e-12))
        require(same or safe_json(expected) == safe_json(actual), f"{context}: metric mismatch")
    else:
        require(type(expected) is type(actual) and expected == actual, f"{context}: value/type mismatch")


def identity(row):
    require(type(row.get("horizon")) is int and type(row.get("seed")) is int,
        "H and seed identities must be integers")
    return tuple(row[k] for k in IDENTITY)


class Evidence:
    """Rebase recorded repository paths without falling back to another checkout."""
    def __init__(self, root, original_root):
        self.root = Path(root).resolve()
        self.original_root = Path(original_root)
        self.hashes = {}

    def path(self, value):
        path = Path(value)
        if not path.is_absolute():
            require(".." not in path.parts, f"unsafe relative evidence path: {path}")
            return self.root / path
        try:
            return self.root / path.relative_to(self.original_root)
        except ValueError:
            return path  # A genuinely external frozen dependency must still exist and hash-match.

    def name(self, path):
        try:
            return str(Path(path).relative_to(self.root))
        except ValueError:
            return str(path)

    def check(self, value, expected=_UNPINNED):
        path = self.path(value)
        digest = sha256(path)
        if expected is not _UNPINNED:
            require(isinstance(expected, str) and len(expected) == 64,
                f"missing SHA-256 evidence pin: {self.name(path)}")
            require(digest == expected, f"evidence hash mismatch: {self.name(path)}")
        name = self.name(path)
        require(name not in self.hashes or self.hashes[name] == digest, f"evidence changed during audit: {name}")
        self.hashes[name] = digest
        return path

    def read(self, value, expected=_UNPINNED):
        return json.loads(self.check(value, expected).read_text(encoding="utf-8"))


def effective(wrapper, horizon, seed, wheel_hash):
    compare({"base_method": "M4", "search_policy": "ga", "run_kind": "ablation",
        "controls": {"decision_policy": "optimize", "fitness_phase_mode": "phase",
            "lookahead_horizon": horizon, "routing_batcher": "coloring", "search_policy": "ga"}},
        wrapper, "config wrapper")
    settings = wrapper["base_config"]["zac_setting"]
    require(len(settings) == 1, "expected one effective compiler setting")
    setting = deepcopy(settings[0])
    compare({"seed": seed, "backend": "native", "native_abi_version": 9,
        "native_wheel_sha256": wheel_hash, "engine": "ga", "init_engine": "sa",
        "objective": "physical_log_fidelity", "max_unique_evaluations": 576,
        "return_candidate_limit": 6, "return_assignment_k": 4,
        "alpha_lookahead": .5, "lookahead_horizon": {"max_horizon": horizon,
            "rho": .7, "epsilon": .05, "decay": "geometric", "mode": "decay",
            "policy": "physical_terminal_decay_v1"}}, setting, "effective config")
    setting.pop("dir", None)
    setting.pop("seed")
    setting["lookahead_horizon"].pop("max_horizon")
    return setting


def validate_protocol(protocol, evidence):
    compare({"protocol_id": PROTOCOL_ID, "horizons": list(HORIZONS), "seeds": list(SEEDS),
        "wheel": {"abi": 9}, "native": {"paper_native_abi_version": "9"}}, protocol, "protocol")
    require(all(type(x) is int for x in protocol["horizons"] + protocol["seeds"]), "invalid numeric identities")
    require(tuple((r["dataset"], r["circuit"]) for r in protocol["circuits"]) == COHORT,
        "fixed twelve-circuit cohort changed")
    jobs, reused = protocol["quality_jobs"], protocol["reused_quality"]
    new_keys, old_keys = [identity(x) for x in jobs], [identity(x) for x in reused]
    expected_old = {(d, c, h, s) for d, c in COHORT for h in HORIZONS for s in SEEDS
        if h in (0, 8) or (h in (2, 4) and s == 0)}
    full = {(d, c, h, s) for d, c in COHORT for h in HORIZONS for s in SEEDS}
    require(len(jobs) == len(set(new_keys)) == 84 and len(reused) == len(set(old_keys)) == 96
        and set(old_keys) == expected_old and set(new_keys) == full - expected_old,
        "expected disjoint 84 new and 96 reused identities; no omissions or double counting")
    for job in jobs:
        d, c, h, s = identity(job)
        require(job["job_id"] == f"{d}-{c}-h{h}-s{s}" and type(job["repetition"]) is int
            and job["repetition"] == 0, "job ID or repetition changed")
    for label in ("architecture", "model", "wheel", "predecessor", "immutable_helper", "execution_plan"):
        evidence.check(protocol[label]["path"], protocol[label]["sha256"])
    evidence.check(protocol["extension_script"], protocol["extension_script_sha256"])
    for item in protocol["source_reports"].values():
        evidence.check(item["path"], item["sha256"])
    for prefix in ("paper_native_python", "paper_native_extension"):
        evidence.check(protocol["native"][prefix + "_path"], protocol["native"][prefix + "_sha256"])
    files = protocol["frozen_source_files"]
    require(stable_hash(files) == protocol["frozen_source_sha256"], "frozen source inventory hash mismatch")
    source = evidence.path(protocol["frozen_source_root"])
    actual = {str(p.relative_to(source)) for p in source.rglob("*") if p.is_file()
        and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    require(actual == set(files), "frozen source inventory changed")
    for name, digest in files.items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts, "unsafe frozen source member")
        evidence.check(source / name, digest)
    for row in protocol["circuits"]:
        evidence.check(row["canonical"]["canonical_path"], row["canonical"]["canonical_sha256"])
    require(set(protocol["configs"]) == {f"{h}:{s}" for h in HORIZONS for s in SEEDS}, "config coverage changed")
    common = None
    for h in HORIZONS:
        for s in SEEDS:
            item = protocol["configs"][f"{h}:{s}"]
            normalized = effective(evidence.read(item["path"], item["sha256"]), h, s, protocol["wheel"]["sha256"])
            common = normalized if common is None else common
            require(normalized == common, "H comparison changes search budget or another compiler setting")
    return common


def command_arg(manifest, flag):
    command = manifest["command"]
    require(command.count(flag) == 1 and command.index(flag) + 1 < len(command), f"missing/ambiguous {flag}")
    return command[command.index(flag) + 1]


def validate_manifest(manifest, item, protocol, evidence, common, new):
    d, c, h, s = identity(item)
    compare({"dataset": d, "circuit": c, "seed": s, "repetition": 0,
        "method": "M4", "run_kind": "ablation"}, manifest, "manifest identity")
    require(manifest["status"] in TERMINAL - {"runner_error"}, "manifest is not terminal")
    canonical = next(x["canonical"] for x in protocol["circuits"] if (x["dataset"], x["circuit"]) == (d, c))
    compare({"input_sha256": canonical["canonical_sha256"],
        "architecture_sha256": protocol["architecture"]["sha256"],
        "model_sha256": protocol["model"]["sha256"]}, manifest, "manifest inputs")
    evidence.check(command_arg(manifest, "--input"), canonical["canonical_sha256"])
    # Old architecture paths may refer to retired checkouts: the sealed equivalent
    # architecture is verified above, while the manifest records its input hash.
    wrapper = evidence.read(command_arg(manifest, "--config"), manifest["config_sha256"])
    require(effective(wrapper, h, s, protocol["wheel"]["sha256"]) == common,
        "manifest used a different budget or compiler setting")
    if new:
        require(manifest["config_sha256"] == protocol["configs"][f"{h}:{s}"]["sha256"], "new manifest config mismatch")
        compare({"horizon_extension_protocol": PROTOCOL_ID,
            "horizon_frozen_source_commit": protocol["frozen_source_commit"],
            "horizon_frozen_source_sha256": protocol["frozen_source_sha256"]},
            manifest["package_versions"], "new compiler source")
    if manifest["status"] == "success":
        compare({"native_abi_version": 9, "backend": "native", "python_fallback": False,
            "native_wheel_sha256": protocol["wheel"]["sha256"], "verifier_ok": True,
            "ghost_hits": 0, "qubits": canonical["qubits"],
            "expected_gates_1q": canonical["gates_1q"], "observed_gates_1q": canonical["gates_1q"],
            "expected_gates_2q": canonical["gates_2q"], "observed_gates_2q": canonical["gates_2q"]},
            manifest, "successful manifest")
        compare({"native_abi_version": 9, "native_wheel_sha256": protocol["wheel"]["sha256"],
            "extension_sha256": protocol["native"]["paper_native_extension_sha256"],
            "wheel_registered": True}, manifest["compiler_and_flags"], "native execution")
        compare({"configured_depth": h, "alpha_lookahead": .5, "rho": .7, "epsilon": .05},
            manifest["forecast_summary"], "forecast setting")
        require(manifest["expected_gate_ledger_sha256"] == manifest["observed_gate_ledger_sha256"]
            and manifest["canonical_input_layer_ledger_sha256"] == manifest["observed_transition_layer_ledger_sha256"],
            "logical input/output ledger mismatch")


def runner_valid(manifest):
    f = manifest.get("fidelity")
    return (manifest.get("status") == "success" and manifest.get("fidelity_ood") is not True
        and finite(f) and 0 < f <= 1 and manifest.get("ghost_hits") in (None, 0))


def quality_record(manifest, item):
    result = {k: manifest.get(k) for k in ("dataset", "circuit", "status", "fidelity", "fidelity_ood",
        "move_batches", "transfers", "idle_exposures", "move_time_us", "full_compile_ns")}
    result.update(horizon=item["horizon"], seed=item["seed"], process_cpu_ns=manifest.get("cpu_time_ns"),
        forecast_summary=manifest.get("forecast_summary", {}), source=item["manifest"],
        source_sha256=item["manifest_sha256"], old_timing_not_reused_as_new_measurement=True)
    if not runner_valid(manifest):
        result["status"] = "invalid_quality"
    return result


def runner_aggregate(records, circuits):
    """Independent reproduction of the runner's fidelity-only summary contract."""
    index = {identity(r): r for r in records}
    require(len(index) == len(records), "duplicate summary record")
    rows, excluded, ratios = [], [], {str(h): [] for h in HORIZONS}
    for d, c in circuits:
        values = {}
        for h in HORIZONS:
            block = [index.get((d, c, h, s)) for s in SEEDS]
            if all(r and r["status"] == "success" and finite(r["fidelity"]) and r["fidelity"] > 0 for r in block):
                values[str(h)] = statistics.median(r["fidelity"] for r in block)
        rows.append({"dataset": d, "circuit": c, "median_fidelity": values})
        missing = [h for h in HORIZONS if str(h) not in values]
        if missing:
            excluded.append({"dataset": d, "circuit": c, "incomplete_horizons": missing})
        else:
            for h in HORIZONS:
                ratios[str(h)].append(math.log(values[str(h)]) - math.log(values["8"]))
    return {"planned_circuits": len(circuits), "complete_circuits": len(circuits) - len(excluded),
        "excluded": excluded, "per_circuit": rows, "seeds": list(SEEDS), "timing_aggregation": "not pooled",
        "fidelity_ratio_vs_h8": {h: math.exp(statistics.mean(v)) if v else None for h, v in ratios.items()}}


def paired_quality(outcomes):
    """One common fifteen-run circuit block for both published metrics."""
    index = {identity(r): r for r in outcomes}
    require(len(index) == len(outcomes) == 180, "expected 180 unique planned outcomes")
    rows = []
    for d, c in COHORT:
        row = {"dataset": d, "circuit": c, "complete": True, "horizons": {}, "exclusions": []}
        for h in HORIZONS:
            block = [index[d, c, h, s] for s in SEEDS]
            reasons = [{"seed": r["seed"], "reasons": r["exclusions"]} for r in block if r["exclusions"]]
            if reasons:
                row["complete"] = False
                row["exclusions"].append({"horizon": h, "runs": reasons})
            else:
                row["horizons"][str(h)] = {"median_fidelity": statistics.median(r["fidelity"] for r in block),
                    "median_move_batches": statistics.median(r["move_batches"] for r in block)}
        if row["complete"] and row["horizons"]["8"]["median_move_batches"] <= 0:
            row["complete"] = False
            row["exclusions"].append({"reason": "nonpositive H8 median batch denominator"})
        if row["complete"]:
            reference = row["horizons"]["8"]
            for value in row["horizons"].values():
                value["fidelity_ratio_vs_h8"] = value["median_fidelity"] / reference["median_fidelity"]
                value["move_batches_ratio_vs_h8"] = value["median_move_batches"] / reference["median_move_batches"]
        rows.append(row)
    complete = [r for r in rows if r["complete"]]
    require(complete, "no complete valid five-H/three-seed circuit block; publication refused")
    values = {"HorizonExtTargetN": 12, "HorizonExtCompleteN": len(complete), "HorizonExtSeedN": 3,
        "HorizonExtExcludedN": 12 - len(complete)}
    for h in HORIZONS:
        for suffix, field in (("FidelityRatio", "fidelity_ratio_vs_h8"), ("BatchRatio", "move_batches_ratio_vs_h8")):
            ratios = [r["horizons"][str(h)][field] for r in complete]
            require(all(finite(v) and v >= 0 for v in ratios), "nonfinite paired metric")
            values[f"HorizonExt{WORDS[h]}{suffix}"] = (0.0 if 0 in ratios else math.exp(statistics.mean(map(math.log, ratios))))
    return values, rows


def load_execution(protocol, study, evidence):
    """Read original receipts, or the explicitly sealed append-only continuation."""
    continuation = study / "continuation_v1"
    if not continuation.exists():
        summary = evidence.read(study / "quality_summary.json")
        names = {j["job_id"] + ".json" for j in protocol["quality_jobs"]}
        require({p.name for p in (study / "receipts/quality").glob("*.json")} == names,
            "terminal receipt coverage incomplete or contains unexpected jobs")
        receipts = []
        for job in protocol["quality_jobs"]:
            receipt = evidence.read(study / "receipts/quality" / (job["job_id"] + ".json"))
            compare({**job, "protocol_sha256": PROTOCOL_SHA256}, receipt, "terminal receipt")
            require(receipt.get("phase") == "quality" or
                (receipt.get("status") == "runner_error" and "phase" not in receipt), "receipt phase mismatch")
            receipts.append(receipt)
        compare(receipts, summary["receipts"], "summary receipts")
        return summary, receipts, receipts
    require(isinstance(CONTINUATION_SHA256, str) and len(CONTINUATION_SHA256) == 64,
        "continuation is not yet pinned; publication refused")
    cp = evidence.read(continuation / "protocol.json", CONTINUATION_SHA256)
    compare({"protocol_id": "horizon-shared12-v2-ood-continuation-v1", "timing_execution_enabled": False},
        cp, "continuation protocol")
    compare({"sha256": CONTINUATION_SHA256}, evidence.read(continuation / "protocol.sha256.json"), "continuation seal")
    for key in ("base_protocol", "base_driver", "continuation_driver"):
        evidence.check(cp[key]["path"], cp[key]["sha256"])
    require(cp["base_protocol"]["sha256"] == PROTOCOL_SHA256
        and cp["base_driver"]["sha256"] == protocol["extension_script_sha256"], "continuation base changed")
    summary = evidence.read(study / "combined_quality_summary.json")
    compare({"sha256": CONTINUATION_SHA256}, summary["continuation_protocol"], "combined continuation reference")
    require(evidence.path(summary["continuation_protocol"]["path"]) == continuation / "protocol.json",
        "combined summary refers to another continuation")
    priors = {r["job_id"]: r for r in cp["prior_receipts"]}
    pending = cp["pending_job_ids"]
    all_ids = {j["job_id"] for j in protocol["quality_jobs"]}
    require(len(priors) == len(cp["prior_receipts"]) == 23 and len(pending) == len(set(pending)) == 61
        and not set(priors) & set(pending) and set(priors) | set(pending) == all_ids,
        "continuation must preserve the original 23 and run only the remaining 61 jobs")
    for directory, expected in ((study / "receipts/quality", set(priors)),
                                (continuation / "receipts/quality", set(pending))):
        require({p.stem for p in directory.glob("*.json")} == expected,
            "continuation receipt coverage missing, duplicated, or unexpected")
    sidecars = {}
    for pin in cp["ood_sidecars"]:
        sidecar = evidence.read(pin["path"], pin["sha256"])
        job_id = sidecar["job_id"]
        require(job_id in priors and job_id not in sidecars, "duplicate or unplanned OOD classification")
        compare({"classification": "success_model_ood", "fidelity": None, "log_fidelity": None,
            "base_protocol_sha256": PROTOCOL_SHA256,
            "original_receipt": {"status": "runner_error", "sha256": priors[job_id]["sha256"]},
            "manifest": {"status": "success"}, "physical_checks": {"verifier_ok": True, "ghost_hits": 0}},
            sidecar, "sealed OOD sidecar")
        require(evidence.path(sidecar["original_receipt"]["path"]) == evidence.path(priors[job_id]["path"]),
            "OOD sidecar points to another original receipt")
        original = evidence.read(sidecar["original_receipt"]["path"], sidecar["original_receipt"]["sha256"])
        require(original["job_id"] == job_id and original["status"] == "runner_error", "OOD raw receipt changed")
        require(original.get("error") == "ValueError('nominal success failed independent quality-domain checks')",
            "sidecar must not reinterpret a different execution error")
        manifest = evidence.read(sidecar["manifest"]["path"], sidecar["manifest"]["sha256"])
        compare({"status": "success", "fidelity_ood": True, "fidelity": None, "log_fidelity": None,
            "verifier_ok": True, "ghost_hits": 0}, manifest, "OOD physical manifest")
        scorer = evidence.read(sidecar["scorer"]["path"], sidecar["scorer"]["sha256"])
        compare({"result": {"ood": True, "fidelity": None, "log_fidelity": None,
            "components": {"coherence_linear": {"fidelity": None, "log_fidelity": None}}}}, scorer, "OOD scorer")
        require(scorer["result"]["move_batches"] == manifest["move_batches"], "OOD scorer batch mismatch")
        require(any(isinstance(w, str) and w.startswith("linear coherence model out of domain for atoms:")
            for w in scorer["result"]["warnings"]), "missing original linear-coherence OOD diagnosis")
        for key, expected in (("ghost_policy", "strict_zero"), ("physicalization_policy", "in_method_ghost_safe"),
                              ("trace_protocol", "ours_lk_ghost_safe_v2")):
            require(manifest.get(key) == scorer.get(key) == expected, "OOD physical/scorer policy mismatch")
        for key in ("trace_zair", "canonical_trace"):
            evidence.check(sidecar[key]["path"], sidecar[key]["sha256"])
        sidecars[job_id] = (sidecar, pin)
    require(len(sidecars) == 2, "expected exactly two reviewed OOD classifications")
    receipts, resolved = [], []
    reported_by_id = {r["job_id"]: r for r in summary["receipts"]}
    require(len(reported_by_id) == len(summary["receipts"]) == 84 and set(reported_by_id) == all_ids,
        "combined summary receipt coverage changed")
    for job in protocol["quality_jobs"]:
        job_id = job["job_id"]
        reported = reported_by_id[job_id]
        if job_id in priors:
            pin = priors[job_id]
            path = study / "receipts/quality" / (job_id + ".json")
            require(evidence.path(pin["path"]) == path, "prior receipt location mismatch")
            receipt = evidence.read(path, pin["sha256"])
            evidence.check(pin["claim"]["path"], pin["claim"]["sha256"])
            require(receipt["status"] == pin["raw_status"], "prior receipt raw status changed")
            expected_protocol = {"protocol_sha256": PROTOCOL_SHA256}
        else:
            path = continuation / "receipts/quality" / (job_id + ".json")
            receipt = evidence.read(path)
            expected_protocol = {"protocol_sha256": CONTINUATION_SHA256, "base_protocol_sha256": PROTOCOL_SHA256}
        compare({**job, **expected_protocol}, receipt, "continuation terminal receipt")
        require(receipt.get("phase") == "quality" or
            (receipt.get("status") == "runner_error" and "phase" not in receipt), "continuation receipt phase mismatch")
        compare(receipt, reported, "combined raw receipt")
        compare({"sha256": sha256(path)}, reported["receipt"], "combined receipt locator")
        require(evidence.path(reported["receipt"]["path"]) == path, "combined receipt locator changed")
        effective_receipt = deepcopy(receipt)
        if job_id in sidecars:
            sidecar, pin = sidecars[job_id]
            compare({"quality_classification": "model_out_of_domain", "ood_sidecar": pin,
                "resolved_manifest": {k: sidecar["manifest"][k] for k in ("path", "sha256")}},
                reported, "combined OOD resolution")
            effective_receipt.update(manifest=sidecar["manifest"]["path"],
                manifest_sha256=sidecar["manifest"]["sha256"], status="success", raw_receipt_status=receipt["status"])
        elif "manifest" in receipt:
            manifest = evidence.read(receipt["manifest"], receipt["manifest_sha256"])
            if job_id in pending:
                classification = "model_out_of_domain" if manifest.get("fidelity_ood") is True else "valid_fidelity"
                if manifest["status"] == "success":
                    require(receipt["quality_classification"] == classification, "new quality classification mismatch")
                    scorer = evidence.read(receipt["scorer"]["path"], receipt["scorer"]["sha256"])
                    compare({"result": {"fidelity": manifest["fidelity"], "ood": manifest["fidelity_ood"]}},
                        scorer, "continuation scorer")
        receipts.append(receipt)
        resolved.append(effective_receipt)
    return summary, receipts, resolved


def load_verified(project_root):
    root = Path(project_root).resolve()
    study = root / STUDY_RELATIVE
    require(sha256(study / "protocol.json") == PROTOCOL_SHA256, "sealed protocol hash mismatch")
    protocol = json.loads((study / "protocol.json").read_text())
    original = Path(protocol["extension_script"]).parents[2]
    evidence = Evidence(root, original)
    evidence.check(study / "protocol.json", PROTOCOL_SHA256)
    compare({"sha256": PROTOCOL_SHA256}, evidence.read(study / "protocol.sha256.json"), "protocol seal")
    # No session summary or in-progress claim can substitute for the final report.
    summary, receipts, resolved = load_execution(protocol, study, evidence)
    compare({"protocol_sha256": PROTOCOL_SHA256, "new_jobs_planned": 84, "new_jobs_claimed": 84,
        "new_jobs_pending": 0, "reused_records": 96, "timing_started": False}, summary, "completed study")
    common = validate_protocol(protocol, evidence)
    items = [(r, False) for r in protocol["reused_quality"]]
    for receipt in resolved:
        require(receipt["status"] in TERMINAL, "unfinished or unknown receipt status")
        require("manifest" in receipt or receipt["status"] == "runner_error", "terminal receipt lacks manifest evidence")
        items.append((receipt, True))
    compare(dict(Counter(r["status"] for r in receipts)), summary["new_status_counts"], "summary status counts")
    require(sum(summary["new_status_counts"].values()) == 84, "terminal status count mismatch")
    records, outcomes, sources, run_ids = [], [], set(), set()
    for item, new in items:
        key = identity(item)
        outcome = dict(zip(IDENTITY, key))
        outcome.update(phase="new" if new else "reused", exclusions=[])
        if "error" in item:
            outcome["receipt_error"] = item["error"]
        if "raw_receipt_status" in item:
            outcome["raw_receipt_status"] = item["raw_receipt_status"]
        if "manifest" not in item:
            outcome.update(status=item["status"], fidelity=None, move_batches=None,
                exclusions=[item["status"] + ": no manifest"])
        else:
            source = evidence.path(item["manifest"])
            require(str(source.resolve()) not in sources, "manifest reused under multiple planned identities")
            sources.add(str(source.resolve()))
            manifest = evidence.read(source, item["manifest_sha256"])
            validate_manifest(manifest, item, protocol, evidence, common, new)
            require(manifest["run_id"] not in run_ids, "duplicate run ID")
            run_ids.add(manifest["run_id"])
            if new:
                require(manifest["status"] == item["status"], "receipt/manifest status mismatch")
            records.append(quality_record(manifest, item))
            f, batches = manifest.get("fidelity"), manifest.get("move_batches")
            outcome.update(status=manifest["status"], fidelity=f, move_batches=batches,
                fidelity_ood=manifest.get("fidelity_ood"), manifest=evidence.name(source),
                manifest_sha256=item["manifest_sha256"])
            if manifest["status"] != "success":
                outcome["exclusions"].append(manifest["status"])
            if manifest.get("fidelity_ood") is not False:
                outcome["exclusions"].append("model domain is not confirmed valid")
            if not finite(f) or not 0 < f <= 1:
                outcome["exclusions"].append("fidelity must be finite and in (0, 1]")
            if not finite(batches) or batches < 0:
                outcome["exclusions"].append("MOVE batches must be finite and nonnegative")
        outcomes.append(outcome)
    summary_index = {identity(r): r for r in summary["records"]}
    require(len(summary_index) == len(summary["records"]) == len(records)
        and set(summary_index) == {identity(r) for r in records}, "summary record coverage or duplicate identity")
    for record in records:
        compare(record, summary_index[identity(record)], "summary manifest record")
    compare(runner_aggregate(records, COHORT), summary, "recomputed runner summary")
    if "dataset_summaries" in summary:
        for dataset in ("zac18", "qmap154"):
            compare(runner_aggregate(records, tuple(x for x in COHORT if x[0] == dataset)),
                summary["dataset_summaries"][dataset], f"runner summary/{dataset}")
    if "physical_execution_status_counts" in summary:
        compare(dict(Counter(r["status"] for r in outcomes if "manifest" in r)),
            summary["physical_execution_status_counts"], "physical status coverage")
        require(summary["model_ood_records"] == sum(r.get("fidelity_ood") is True for r in outcomes),
            "model-domain coverage mismatch")
    values, rows = paired_quality(outcomes)
    if "horizon_comparison" in summary:
        complete = [r for r in rows if r["complete"]]
        compare([{"dataset": r["dataset"], "circuit": r["circuit"]} for r in complete],
            summary["common_fidelity_cohort"], "combined common cohort")
        compare({str(h): {"paired_circuits": len(complete),
            "fidelity_ratio_vs_h8": values[f"HorizonExt{WORDS[h]}FidelityRatio"],
            "move_batches_ratio_vs_h8": values[f"HorizonExt{WORDS[h]}BatchRatio"]} for h in HORIZONS},
            summary["horizon_comparison"], "combined quality comparisons")
        compare([{"dataset": r["dataset"], "circuit": r["circuit"], "horizons": {
            h: {"fidelity": v["median_fidelity"], "move_batches": v["median_move_batches"]}
            for h, v in r["horizons"].items()}} for r in complete],
            summary["per_circuit_quality_medians"], "combined circuit medians")
    # Detect concurrent evidence mutation before returning any exportable values.
    for name, digest in evidence.hashes.items():
        require(sha256(evidence.path(name)) == digest, f"evidence changed during audit: {name}")
    return values, rows, outcomes, evidence.hashes


def expected_exports(project_root):
    values, rows, outcomes, hashes = load_verified(project_root)
    generator_hash = sha256(__file__)
    lines = ["% Generated by writing/generate_horizon_extension_values.py; do not edit.",
        "% Independent five-H quality study; no timing or replacement of accepted main results.",
        f"% Protocol SHA-256: {PROTOCOL_SHA256}", f"% Generator SHA-256: {generator_hash}"]
    for key, value in values.items():
        display = str(value) if key.endswith("N") else f"{value:.4f}"
        lines.append(f"\\newcommand{{\\{key}}}{{{display}}}")
    output = {"export_schema": "horizon-extension-quality-paper-v1", "protocol_id": PROTOCOL_ID,
        "protocol_sha256": PROTOCOL_SHA256, "generator_sha256": generator_hash, "values": values,
        "horizons": list(HORIZONS), "seeds": list(SEEDS),
        "analysis_unit": "circuit; per-H median of three seeds, then geometric mean of paired ratios to H8",
        "cohort_policy": "same complete five-H/three-seed circuit cohort for fidelity and MOVE batches",
        "timing_policy": "no compilation time estimates; concurrent quality times are not pooled",
        "separate_from_frozen_main_results": True,
        "primary_cohort": [{"dataset": r["dataset"], "circuit": r["circuit"]} for r in rows if r["complete"]],
        "per_circuit": rows, "job_outcomes": safe_json(outcomes),
        "job_status_counts": dict(Counter(r["status"] for r in outcomes))}
    exports = {DERIVED_FILENAMES[0]: "\n".join(lines) + "\n", DERIVED_FILENAMES[1]: json_text(output)}
    provenance = {"export_schema": "horizon-extension-quality-provenance-v1",
        "protocol_sha256": PROTOCOL_SHA256, "generator_sha256": generator_hash,
        "generator_test_sha256": sha256(Path(__file__).with_name("test_generate_horizon_extension_values.py")),
        "input_sha256": hashes, "all_180_planned_outcomes_audited": True,
        "recomputed_from_manifests": True, "source_evidence_modified": False,
        "derived_file_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in exports.items()}}
    exports[DERIVED_FILENAMES[2]] = json_text(provenance)
    return exports, values


def check_exports(paper_root, exports):
    return [{"file": name, "reason": "missing" if not (Path(paper_root) / name).is_file() else "stale"}
        for name, expected in exports.items() if not (Path(paper_root) / name).is_file()
        or (Path(paper_root) / name).read_bytes() != expected.encode()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--paper-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        exports, values = expected_exports(args.project_root)
        if args.check:
            stale = check_exports(args.paper_root, exports)
            print(json_text({"status": "failed" if stale else "passed", "read_only": True, "stale_files": stale}), end="")
            return int(bool(stale))
        args.paper_root.mkdir(parents=True, exist_ok=True)
        for name, text in exports.items():
            (args.paper_root / name).write_text(text, encoding="utf-8")
        print(json_text({"status": "generated", "values": values}), end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
        print(json_text({"status": "failed", "read_only": args.check, "error": f"{type(error).__name__}: {error}"}), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
