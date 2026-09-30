"""Independently export the completed physical-GA main matrix.

Run with the registered Python from protocol.json and -B. No compiler is called.
The final export requires all 480 fresh receipts plus all 27 pinned pilot GA-H2
outcomes (including the three timeouts), and the supervisor completion receipt.
--check rechecks validated worker evidence and hash closure, recomputes all
aggregates and compares every output byte without writes or trace replay.
--audit-only --audit-limit 6 checks a bounded set of available records, without
physical replay or statistical export. CSV method keys intentionally remain
M1/M2/Default for the existing paper/table/distribution interfaces; every LaTeX
macro is independently named GAMain*. Existing exports are never overwritten.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Direct invocation must not import the sibling experiments_v2/statistics.py.
sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
sys.dont_write_bytecode = True

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
import hashlib
import importlib.util
import io
import json
import math
import statistics

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL = REPO / "ZAC_zzx/results/physical_ga_main_v1/protocol.json"
PROTOCOL_ID = "physical-ga-main-canonical169-seed012-v1"
PROTOCOL_SHA256 = "9bb0ae97d2f3f1536a5c496a65fe2e21e2192ce20830979dd75b30b0a3148fc7"
EXPORT_SCHEMA = "physical-ga-main-paper-values-v1"
NEW_FAILURES = {"program_error", "validation_error", "initialization_infeasible"}
PACKAGES = ("experiments_v2", "zzx", "zac", "streaming", "evaluation")
TIMING_AUDIT = {"path": str(REPO / "IEEE_conference_template/build/paper_zh/ga-narrative-20260913-103523/fixed_mapping_timing_stage_audit.json"),
                "sha256": "1650abbb8b27feaca454d2c0365db7c034c33a79b5e45004a6b732f69977dff9"}
OLD_MAIN_CONTEXT = {"path": str(REPO / "ZAC_zzx/results/default_initial_v1/paper_exports/main_rows.csv"),
                    "sha256": "d50af7f9138abfcb00b15b2ca85c5c3e393277062d7eaa0c8b7f752b325f5e3d"}
BUDGET_TIMING_AUDIT = {"path": str(REPO / "IEEE_conference_template/build/paper_zh/ga-narrative-20260913-103523/fixed_mapping_budget_timing.json"),
                       "sha256": "5562f839ad9ca1de2d9061e68fbb7cd78645231a947a8f2f782939ff79bc012e"}
BUDGET_TIMING_SCRIPT_SHA256 = "250d6c4cc45f16fdd594f7f00a0338ecdbebef48f817a3d4cbaad206133340ce"


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def read(path):
    def invalid(value):
        raise ValueError("non-finite JSON constant: " + value)
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=invalid)


class Sources:
    """Bind exact bytes, and reject source mutation during one export/check."""
    def __init__(self, repo):
        self.repo, self.records, self.stats = Path(repo).resolve(), {}, {}

    def file(self, reference):
        reference = {"path": str(reference)} if isinstance(reference, (str, Path)) else reference
        path = Path(reference["path"])
        path = (self.repo / path).resolve() if not path.is_absolute() else path.resolve()
        name = str(path)
        stat = path.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
        if name not in self.records:
            digest = sha256(path)
            after = path.stat()
            if stamp != (after.st_size, after.st_mtime_ns):
                raise ValueError("source changed while hashing: " + name)
            self.records[name] = {"path": name, "sha256": digest, "bytes": stat.st_size}
            self.stats[name] = stamp
        elif self.stats[name] != stamp:
            raise ValueError("source changed during export: " + name)
        ref = self.records[name]
        if reference.get("sha256", ref["sha256"]) != ref["sha256"]:
            raise ValueError("source SHA256 drift: " + name)
        if reference.get("bytes", ref["bytes"]) != ref["bytes"]:
            raise ValueError("source size drift: " + name)
        return ref

    def json(self, reference):
        return read(self.file(reference)["path"])

    def finish(self):
        for path, stamp in self.stats.items():
            stat = Path(path).stat()
            if (stat.st_size, stat.st_mtime_ns) != stamp:
                raise ValueError("source changed during export: " + path)
        return [self.records[name] for name in sorted(self.records)]


def module_at(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def frozen_modules(protocol, sources):
    """Load only the sealed driver/core; do not reuse live computational imports."""
    def owned(name):
        return any(name == prefix or name.startswith(prefix + ".") for prefix in PACKAGES)
    saved = {name: value for name, value in sys.modules.items() if owned(name)}
    old_path = sys.path[:]
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, protocol["frozen_source"])
    driver_name, loader_name = "_physical_ga_main_export_driver", "_physical_ga_main_baseline_loader"
    try:
        sources.file(protocol["driver"])
        driver = module_at(protocol["driver"]["path"], driver_name)
        expected_core = Path(protocol["frozen_source"]) / "experiments_v2/physical_ga_initial_study.py"
        if Path(driver.core.__file__).resolve() != expected_core.resolve():
            raise ValueError("main driver imported a non-frozen core")
        sources.file(protocol["baseline_loader"])
        baseline = module_at(protocol["baseline_loader"]["path"], loader_name)
        yield driver, baseline
    finally:
        for name in list(sys.modules):
            if owned(name) or name in (driver_name, loader_name):
                del sys.modules[name]
        sys.modules.update(saved)
        sys.path[:] = old_path


def validate_inventory(p, inventory):
    jobs, circuits = p["jobs"], p["circuits"]
    if (p["protocol_id"] != PROTOCOL_ID or p["arms"] != ["ga_h2"] or p["seeds"] != [0, 1, 2]
            or len(jobs) != 507 or len(circuits) != 169
            or Counter(j["origin"] for j in jobs) != {"fresh": 480, "reused": 27}):
        raise ValueError("main matrix must retain 480 fresh and all 27 pilot outcomes")
    ids = [(j["input_sha256"], j["seed"]) for j in jobs]
    if (len(set(ids)) != len(ids) or len({j["job_id"] for j in jobs}) != len(jobs)
            or {j["job_id"] for j in jobs} != {
            f"{sha[:16]}-s{seed}-ga_h2" for sha, seed in ids}):
        raise ValueError("duplicate or inconsistent main identity")
    by_sha = {c["input_sha256"]: c for c in circuits}
    if len(by_sha) != 169 or set(ids) != {(sha, seed) for sha in by_sha for seed in p["seeds"]}:
        raise ValueError("main matrix does not cover every canonical input and seed")
    labels = {}
    for c in circuits:
        for label in c["labels"]:
            key = label["dataset"], label["circuit"]
            if key in labels or label["canonical_sha256"] != c["input_sha256"]:
                raise ValueError("duplicate or inconsistent display alias")
            expected = inventory[key[0]][key[1]]
            if any(label[k] != value for k, value in expected.items()):
                raise ValueError("display inventory differs from the original baseline inventory")
            labels[key] = label
    expected_labels = {(d, c) for d, values in inventory.items() for c in values}
    if set(labels) != expected_labels or len(labels) != 172:
        raise ValueError("the full 172 display-input inventory is required")
    return by_sha


def bind_protocol(p, path, sources):
    for key in ("driver", "python", "architecture", "model"):
        sources.file(p[key])
    sources.file(path)
    sources.file(Path(path).with_name("protocol.sha256.json"))
    for relative, ref in p["source_files"].items():
        sources.file({"path": str(Path(p["frozen_source"]) / relative), "sha256": ref["sha256"]})
    for c in p["circuits"]:
        sources.file({"path": c["input"], "sha256": c["input_sha256"]})
    sources.file({"path": p["native"]["extension_path"], "sha256": p["native"]["extension_sha256"]})
    if p["native"].get("wheel_registration_path"):
        sources.file(p["native"]["wheel_registration_path"])


def terminal_inventory(root, jobs):
    """A started-only claim is not a terminal receipt, even if called interrupted."""
    available, missing = [], []
    for job in jobs:
        path = Path(root) / "receipts" / (job["job_id"] + ".json")
        (available if path.is_file() else missing).append(job)
    return available, missing


def check_completion(p, protocol_path, fresh_rows, sources):
    root = Path(protocol_path).parent
    execution = sources.json(root / "execution.json")
    completion = sources.json(root / "execution_completion.json")
    expected_pin = {"path": str(Path(protocol_path).resolve()), "sha256": sha256(protocol_path)}
    for value in (execution, completion):
        if value.get("protocol") != expected_pin:
            raise ValueError("supervisor receipt belongs to another protocol")
    if (execution.get("fresh_planned") != 480 or execution.get("reused_fixed") != 27
            or execution.get("max_workers") != p["max_workers"] or completion.get("paused") is not False
            or completion.get("fresh_terminal") != 480
            or completion.get("fresh_statuses") != dict(Counter(r["status"] for r in fresh_rows))
            or completion.get("reused_statuses") != {"success": 24, "timeout": 3}):
        raise ValueError("supervisor did not close the complete prespecified main matrix")
    return {"execution": sources.file(root / "execution.json"),
            "completion": sources.file(root / "execution_completion.json")}


def bind_receipt(root, job, row, protocol_sha, sources):
    receipt_ref = sources.file(Path(root) / "receipts" / (job["job_id"] + ".json"))
    receipt = sources.json(receipt_ref)
    if (receipt.get("protocol_sha256") != protocol_sha or receipt.get("status") != row["status"]
            or receipt.get("origin") != "fresh"
            or any(receipt.get(k) != job[k] for k in ("job_id", "input_sha256", "seed", "arm"))):
        raise ValueError("terminal receipt identity/status changed")
    if row["status"] == "success" and receipt.get("returncode") != 0:
        raise ValueError("success lacks a zero worker return code")
    for field in ("result", "failure"):
        if receipt.get(field):
            sources.file(receipt[field])
    started = Path(root) / "receipts" / (job["job_id"] + ".started.json")
    if started.exists():
        sources.file(started)
    # Preserve any partial selection/trace generated before a terminal failure.
    if row["status"] != "success":
        job_root = Path(root) / "jobs" / job["job_id"]
        for name in ("setting.json", "selection.json", "dynamic_rng.json", "native_trace.json.gz", "failure.json"):
            path = job_root / name
            if path.is_file():
                sources.file(path)
    return receipt_ref


def failure_evidence(row, circuit, receipt_ref, sources):
    receipt = sources.json(receipt_ref)
    failure_ref, failure = None, {}
    if receipt.get("failure"):
        failure_ref = sources.file(receipt["failure"])
        failure = sources.json(failure_ref)
        if any(failure.get(k) != row[k] for k in ("job_id", "input_sha256", "seed", "arm")):
            raise ValueError("failure artifact identity differs from its receipt")
    note = ""
    if (row["status"] == "program_error" and circuit["gates_1q"] == circuit["gates_2q"] == 0
            and failure.get("exception_type") == "AttributeError"
            and "zzx_initial_lookahead_report" in failure.get("message", "")):
        note = "空门序列导致初始化报告字段缺失"
    return {"source_failure": failure_ref, "diagnostic_note": note,
            "failure_source_path": (failure_ref or receipt_ref)["path"] if row["status"] != "success" else None,
            "failure_source_sha256": (failure_ref or receipt_ref)["sha256"] if row["status"] != "success" else None,
            "failure_source_kind": ("failure_artifact" if failure_ref else "terminal_receipt") if row["status"] != "success" else None,
            "failure_exception_type": failure.get("exception_type"),
            "failure_message": failure.get("message")}


def normalize_observation(baseline, row, job, circuit, model, source_protocol, receipt_ref, diagnostics=None):
    """Adapt only identity vocabulary; validate the saved score under the old model."""
    source_status = row["status"]
    identity = {**job, "canonical_sha256": job["input_sha256"],
                **{k: circuit[k] for k in ("qubits", "gates_1q", "gates_2q")}}
    result = row.get("result")
    if source_status == "success":
        observation = baseline.validate_default_score(
            {**result, "canonical_job_id": job["job_id"]}, identity, model)
    else:
        if source_status not in baseline.TERMINAL_FAILURES:
            raise ValueError("non-terminal/unrecognized new outcome: " + source_status)
        observation = baseline.validated_record({"dataset": job["dataset"], "circuit": job["circuit"],
            "canonical_sha256": job["input_sha256"], "canonical_job_id": job["job_id"],
            "seed": job["seed"], "status": source_status, "metrics": {}})
    observation.update(main_origin=job["origin"], source_origin=result.get("origin") if result else "fresh",
        source_status=source_status, input_sha256=job["input_sha256"],
        source_protocol=source_protocol, source_receipt=receipt_ref,
        source_result_protocol_sha256=result.get("protocol_sha256") if result else None,
        source_snapshot_sha256=result.get("source_snapshot_sha256") if result else None,
        quality_classification=(result.get("quality_classification") if result else source_status))
    observation.update(diagnostics or {})
    return observation


def expand_aliases(observations, circuits):
    expanded = []
    for observation in observations:
        for label in circuits[observation["canonical_sha256"]]["labels"]:
            expanded.append({**observation, "dataset": label["dataset"], "circuit": label["circuit"]})
    return expanded


def fixed_mapping_timing(sources, reference=TIMING_AUDIT):
    """Historical fixed-layout remaining compile stages, never new GA runtime."""
    audit = sources.json(reference)
    expected_protocol = {"circuits": 12, "methods": ["M3", "M4"], "repetitions": [0, 1, 2],
                         "seed": 0, "workers": 1}
    if (audit["schema"] != "fixed-mapping-timing-stage-audit-v1"
            or audit["timing_protocol"] != expected_protocol
            or audit["stage_fields_complete_M4_records"] != 36
            or audit["mapping_parity"]["all_equal"] is not True):
        raise ValueError("fixed-layout timing audit contract differs")
    sources.file(audit["source_timing_report"])
    mapping_members = {}
    for comparison in audit["mapping_parity"]["comparisons"]:
        if comparison["all_six_M3_M4_repetitions_equal"] is not True:
            raise ValueError("fixed-layout timing mapping parity failed")
        for member in comparison["members"]:
            if member["method"] == "M4":
                mapping_members[member["manifest"]["path"]] = member
    stages, grouped, seen = [], {}, set()
    for record in audit["stage_records"]:
        ref = sources.file(record["manifest"])
        manifest = sources.json(ref)
        key = (record["dataset"], record["circuit"], record["repetition"])
        if key in seen:
            raise ValueError("duplicate fixed-layout timing repetition")
        seen.add(key)
        member = mapping_members[record["manifest"]["path"]]
        if (member["manifest"] != record["manifest"]
                or manifest["input_sha256"] != member["input_sha256"]
                or any(manifest[k] != record[k] for k in ("dataset", "circuit", "seed", "repetition"))
                or manifest["method"] != "M4" or manifest["seed"] != 0
                or manifest["run_kind"] != "timing" or manifest["concurrency_limit"] != 1
                or manifest["status"] != "success" or manifest["exit_code"] != 0
                or any(manifest[k] != value for k, value in record["stages"].items())):
            raise ValueError("historical timing identity/stage field drift")
        post = manifest["full_compile_ns"] - manifest["initial_placement_ns"]
        search = manifest["search_kernel_ns"]
        if post != record["derived_post_initial_compile_ns"] or not 0 <= search <= post or post <= 0:
            raise ValueError("invalid historical post-initial timing decomposition")
        run = {"dataset": key[0], "circuit": key[1], "repetition": key[2],
               "post_initial_seconds": post / 1e9, "search_share": search / post,
               "manifest": ref, "stages_ns": record["stages"]}
        stages.append(run)
        grouped.setdefault(key[:2], []).append(run)
    if len(seen) != 36 or len(grouped) != 12 or len(mapping_members) != 36:
        raise ValueError("fixed-layout timing must retain 12 circuits times 3 repetitions")
    per_circuit = []
    for key, runs in sorted(grouped.items()):
        if {r["repetition"] for r in runs} != {0, 1, 2}:
            raise ValueError("missing fixed-layout timing repetition")
        per_circuit.append({"dataset": key[0], "circuit": key[1],
            "post_initial_median_seconds": statistics.median(r["post_initial_seconds"] for r in runs),
            "search_share_median": statistics.median(r["search_share"] for r in runs)})
    post_median = statistics.median(r["post_initial_median_seconds"] for r in per_circuit)
    share_mean = statistics.mean(r["search_share_median"] for r in per_circuit)
    if (not math.isclose(post_median, 21.3076912085, rel_tol=0, abs_tol=1e-10)
            or not math.isclose(share_mean, .8830283402766648, rel_tol=0, abs_tol=1e-14)):
        raise ValueError("historical fixed-layout timing aggregate drift")
    return {"audit": sources.file(reference), "circuit_N": 12, "repetitions_per_circuit": 3,
        "post_initial_median_seconds": post_median, "search_share_mean": share_mean,
        "scope": "Historical SA-initialized M4 runs under saved fixed starting mappings; remaining full compilation after initial placement. Not new physical-GA total compilation time.",
        "aggregation": "Within each run subtract initial placement from full compile and divide search kernel by that remainder. Take each circuit's 3-repetition medians; then across 12 circuits take the time median and arithmetic mean of the search shares.",
        "nested_stage_policy": audit["nested_timing"], "per_circuit": per_circuit,
        "runs": sorted(stages, key=lambda r: (r["dataset"], r["circuit"], r["repetition"]))}


def fixed_mapping_budget_timing(sources, reference=BUDGET_TIMING_AUDIT):
    """Recompute historical concurrent sensitivity ratios from 36 manifests."""
    audit = sources.json(reference)
    variants = ("paper_sensitivity_default", "paper_sensitivity_budget_192", "paper_sensitivity_return_4_2")
    if (audit["schema"] != "fixed-mapping-budget-timing-v1" or audit["status"] != "complete"
            or audit["records_n"] != 36 or audit["distinct_circuits_n"] != 12
            or audit["all_stage_fields_valid"] is not True or audit["all_initial_mappings_match"] is not True
            or audit["derivation_script"]["sha256"] != BUDGET_TIMING_SCRIPT_SHA256
            or audit["timing_context"]["concurrency_limits"] != {"4": 36}
            or audit["timing_context"]["run_kinds"] != {"ablation": 36}):
        raise ValueError("historical budget timing audit contract differs")
    sources.file(audit["derivation_script"])
    sources.file(audit["original_sensitivity_csv"])
    groups = {}
    for record in audit["records"]:
        manifest = sources.json(record["manifest"])
        fields = ("dataset", "circuit", "input_sha256", "config_sha256", "model_sha256", "architecture_sha256",
                  "native_wheel_sha256", "seed", "repetition", "status", "full_compile_ns", "initial_placement_ns",
                  "concurrency_limit", "run_kind")
        if (any(manifest[k] != record[k] for k in fields) or record["seed"] != 0 or record["repetition"] != 0
                or record["status"] != "success" or manifest["method"] != "M4" or manifest["exit_code"] != 0
                or manifest["ablation_variant"] != record["variant"] or record["variant"] not in variants
                or record["concurrency_limit"] != 4 or record["run_kind"] != "ablation"):
            raise ValueError("historical budget timing manifest identity/field drift")
        mapping_hash = hashlib.sha256(json.dumps(record["initial_locs"], sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if mapping_hash != record["initial_mapping_sha256"]:
            raise ValueError("historical budget timing saved mapping hash differs")
        post = manifest["full_compile_ns"] - manifest["initial_placement_ns"]
        if not 0 <= manifest["initial_placement_ns"] < manifest["full_compile_ns"] or post != record["post_initial_compile_ns"]:
            raise ValueError("historical budget timing stage subtraction differs")
        group = groups.setdefault((record["dataset"], record["circuit"]), {})
        if record["variant"] in group:
            raise ValueError("duplicate historical budget timing configuration")
        group[record["variant"]] = record
    if len(audit["records"]) != 36 or len(groups) != 12 or any(set(g) != set(variants) for g in groups.values()):
        raise ValueError("historical budget timing requires 12 complete three-configuration groups")
    comparisons = {c["variant"]: c for c in audit["comparisons"]}
    if len(audit["comparisons"]) != 2 or set(comparisons) != set(variants[1:]):
        raise ValueError("historical budget timing comparison set differs")
    ratios = {}
    for variant in variants[1:]:
        per_circuit = []
        for key, group in sorted(groups.items()):
            base, other = group[variants[0]], group[variant]
            if any(base[k] != other[k] for k in ("input_sha256", "model_sha256", "architecture_sha256",
                                                "native_wheel_sha256", "initial_mapping_sha256")):
                raise ValueError("historical budget timing fixed-mapping pair differs")
            per_circuit.append(other["post_initial_compile_ns"] / base["post_initial_compile_ns"])
        value = math.exp(math.fsum(math.log(r) for r in per_circuit) / 12)
        comparison = comparisons[variant]
        if (comparison["n_circuits"] != 12 or comparison["same_initial_mapping_pairs"] != 12
                or comparison["reference"] != variants[0]
                or not math.isclose(value, comparison["post_initial_time_ratio_geomean"], rel_tol=1e-12)):
            raise ValueError("historical budget timing geometric mean differs")
        ratios[variant] = value
    return {"audit": sources.file(reference), "derivation_script": sources.file(audit["derivation_script"]),
        "original_sensitivity_csv": sources.file(audit["original_sensitivity_csv"]), "circuit_N": 12,
        "budget_post_initial_time_ratio": ratios[variants[1]], "return_post_initial_time_ratio": ratios[variants[2]],
        "timing_context": audit["timing_context"], "aggregation": audit["derivation_rule"],
        "quantity": audit["quantity"], "physical_replay": False,
        "verification": "36 manifest hashes and timing fields rechecked; per-run stage differences and 12 paired ratios independently recomputed. Saved mapping equality is bound by the pinned derivation audit; traces are not reread.",
        "scope": "Observed historical four-worker ablation time ratios, separate from the serial fixed-layout timing group and from new physical-GA end-to-end compilation time."}


def render_exports(baseline, baselines, observations, inventory, provenance):
    """Reuse exact baseline/common-cohort/alias/median logic, not old sample counts."""
    outputs = baseline.render_exports(baselines, observations, inventory, provenance)
    metadata = json.loads(outputs.pop("default_initial_values.json"))
    metadata.update(export_schema=EXPORT_SCHEMA,
                    publication_status="independently_verified_complete_physical_ga_main_matrix",
                    method_schema_aliases={"Default": "physical_prefix_ga_Hinit2_dynamic_H8"})
    metadata["macros"] = {"GAMain" + k.removeprefix("Default"): v for k, v in metadata["macros"].items()}
    for dataset, dataset_name in (("zac18", "ZAC"), ("qmap154", "QMAP")):
        for method, baseline_name in (("M1", "ZAC"), ("M2", "ICCAD")):
            comparison = metadata["datasets"][dataset]["comparisons"][method]
            for outcome in ("wins", "ties", "losses"):
                metadata["macros"][f"GAMain{dataset_name}Vs{baseline_name}{outcome.capitalize()}"] = str(comparison[outcome])
    if "historical_fixed_layout_timing" in provenance:
        timing = provenance["historical_fixed_layout_timing"]
        metadata["historical_fixed_layout_timing"] = timing
        metadata["macros"].update(GAMainFixedLayoutCircuitN=str(timing["circuit_N"]),
            GAMainPostInitialMedianSeconds=f"{timing['post_initial_median_seconds']:.2f}",
            GAMainSearchSharePercent=f"{timing['search_share_mean'] * 100:.2f}")
    if "historical_fixed_mapping_budget_timing" in provenance:
        timing = provenance["historical_fixed_mapping_budget_timing"]
        metadata["historical_fixed_mapping_budget_timing"] = timing
        metadata["macros"].update(GAMainBudgetTimingN=str(timing["circuit_N"]),
            GAMainBudgetPostInitialTimeRatio=f"{timing['budget_post_initial_time_ratio']:.4f}",
            GAMainReturnPostInitialTimeRatio=f"{timing['return_post_initial_time_ratio']:.4f}")
    metadata["unchanged_old_studies"] = {"dynamic_ablation_initializer": "original_SA",
        "strict_timing_initializer": "original_SA", "not_results_of_physical_ga_main": True}
    metadata["author_attention"] = [s.replace("default_common_valid", "ga_main_common_valid")
                                    for s in metadata["author_attention"]]
    outputs.pop("default_initial_values.tex")
    outputs["ga_main_values.tex"] = ("% Independent physical_ga_main_v1 export; existing paper values are unchanged.\n"
        + "".join(rf"\newcommand{{\{k}}}{{{v}}}" + "\n" for k, v in metadata["macros"].items()))
    outputs["representative_cases.tex"] = outputs["representative_cases.tex"].replace(
        r"\DefaultRepresentativeCircuitRows", r"\GAMainRepresentativeCircuitRows")
    outputs["representative_cases.csv"] = baseline.csv_text(metadata["representative_cases"])
    outputs["ga_main_values.json"] = canonical(metadata)
    return outputs


def outcome_csv(baseline, observations, *, ood=False):
    selected = (r for r in observations if r["status"] == "success" and r["fidelity_ood"]) if ood else (
        r for r in observations if r["status"] != "success")
    rows = sorted(selected, key=lambda r: (r["canonical_sha256"], r["seed"]))
    if rows:
        return baseline.csv_text(rows)
    output = io.StringIO(newline="")
    csv.writer(output).writerow(["dataset", "circuit", "canonical_sha256", "input_sha256", "seed",
        "source_status", "main_origin", "source_origin", "fidelity_ood", "diagnostic_note",
        "failure_source_path", "failure_source_sha256", "failure_source_kind"])
    return output.getvalue()


def historical_failure_context(observations, sources):
    selected = [r for r in observations if r.get("diagnostic_note") == "空门序列导致初始化报告字段缺失"]
    if not selected:
        return []
    ref = sources.file(OLD_MAIN_CONTEXT)
    with Path(ref["path"]).open(newline="", encoding="utf-8") as stream:
        old_rows = list(csv.DictReader(stream))
    context = []
    for digest in sorted({r["canonical_sha256"] for r in selected}):
        for row in old_rows:
            if row["canonical_sha256"] != digest:
                continue
            context.append({"dataset": row["dataset"], "circuit": row["circuit"], "canonical_sha256": digest,
                "source": ref, "old_common_fidelity": row["common_fidelity"] == "True",
                "old_seed_status": {method: json.loads(row[method])["seed_status"] for method in ("M1", "M2", "Default")},
                "use": "Diagnostic historical context only; not used to select the new main cohort."})
    return context


def compute_exports(protocol_path=DEFAULT_PROTOCOL, *, repo=REPO, audit_only=False, audit_limit=6):
    protocol_path, repo = Path(protocol_path).resolve(), Path(repo).resolve()
    sources = Sources(repo)
    sidecar = sources.json(protocol_path.with_name("protocol.sha256.json"))
    if sidecar.get("sha256") != PROTOCOL_SHA256:
        raise ValueError("main protocol differs from the externally pinned study seal")
    p = sources.json(sidecar)
    if Path(sidecar["path"]).resolve() != protocol_path:
        raise ValueError("protocol sidecar points elsewhere")
    fresh_jobs = [j for j in p["jobs"] if j["origin"] == "fresh"]
    available, missing = terminal_inventory(protocol_path.parent, fresh_jobs)
    if not audit_only and (missing or not (protocol_path.parent / "execution_completion.json").is_file()):
        raise ValueError(f"main matrix is not closed: {len(missing)} missing fresh receipts or supervisor completion missing")
    with frozen_modules(p, sources) as (driver, baseline):
        p = driver.load_protocol(protocol_path)
        pilot_path = Path(p["source_pilot_protocol"]["path"])
        sources.file(p["source_pilot_protocol"])
        pilot = driver.ORIGINAL_PILOT_LOAD(pilot_path)
        bind_protocol(p, protocol_path, sources)
        bind_protocol(pilot, pilot_path, sources)
        sources.file(p["source_default_protocol"])
        baselines, inventory, baseline_provenance = baseline.load_accepted_baselines(repo,
            accepted_reference=p["baseline_provenance"]["accepted_final_manifest"])
        if baseline_provenance != p["baseline_provenance"] or len(baselines) != 344:
            raise ValueError("original baseline closure or policy changed")
        if (baseline_provenance["accepted_model_sha256"] != p["model"]["sha256"]
                or baseline_provenance["accepted_architecture_sha256"] != p["architecture"]["sha256"]):
            raise ValueError("new method and original baselines use different model/hardware")
        for key in ("accepted_final_manifest", "accepted_quality_source", "accepted_freeze"):
            sources.file(baseline_provenance[key])
        for ref in baseline_provenance["baseline_records"]:
            sources.file(ref)
        # This only extends the new compiler's failure vocabulary. Baselines
        # have already been loaded under their unchanged original policy.
        baseline.TERMINAL_FAILURES = baseline.TERMINAL_FAILURES | NEW_FAILURES
        circuits = validate_inventory(p, inventory)
        model = sources.json(p["model"])
        reused_jobs = [j for j in p["jobs"] if j["origin"] == "reused"]
        pilot_jobs = {j["job_id"]: j for j in pilot["jobs"] if j["arm"] == "ga_h2"}
        if set(pilot_jobs) != {j["job_id"] for j in reused_jobs} or len(pilot_jobs) != 27:
            raise ValueError("all 27 pilot GA-H2 identities must be reused")
        if audit_only:
            if type(audit_limit) is not int or audit_limit < 1:
                raise ValueError("audit-limit must be positive")
            chosen_fresh, chosen_reused = available[:audit_limit], reused_jobs[:audit_limit]
        else:
            chosen_fresh, chosen_reused = fresh_jobs, reused_jobs
        fresh_rows, fresh_refs = driver.core.collect(protocol_path, {**p, "jobs": chosen_fresh},
                                                       verify_trace=False)
        pilot_rows, pilot_refs = driver.core.collect(pilot_path,
            {**pilot, "jobs": [pilot_jobs[j["job_id"]] for j in chosen_reused]}, verify_trace=False)
        if not audit_only and Counter(r["status"] for r in pilot_rows) != {"success": 24, "timeout": 3}:
            raise ValueError("pilot successes/timeouts were selected or changed")
        for ref in [*fresh_refs, *pilot_refs]:
            sources.file(ref)
        # Pilot scheduling was amended; bind its preserved scheduling context.
        pilot_scheduling = []
        for path in sorted((pilot_path.parent / "parallel_dispatch_v1").glob("*.json")):
            pilot_scheduling.append(sources.file(path))
        if not audit_only:
            for ref in p["reuse_evidence"]:
                sources.file(ref)
        row_index = {r["job_id"]: r for r in [*fresh_rows, *pilot_rows]}
        observations = []
        for job in [*chosen_fresh, *chosen_reused]:
            row = row_index[job["job_id"]]
            source_path = pilot_path if job["origin"] == "reused" else protocol_path
            source_ref = sources.file(source_path)
            receipt_ref = bind_receipt(source_path.parent, job, row, source_ref["sha256"], sources)
            if job["origin"] == "reused":
                sources.file(job["reuse_receipt"])
                wrapper_path = protocol_path.parent / "reuse_receipts" / (job["job_id"] + ".json")
                wrapper = sources.json(wrapper_path)
                if (wrapper.get("source_receipt") != job["reuse_receipt"]
                        or wrapper.get("source_protocol") != p["source_pilot_protocol"]
                        or wrapper.get("protocol_sha256") != sidecar["sha256"]
                        or wrapper.get("status") != row["status"] or wrapper.get("timing_is_fresh") is not False
                        or any(wrapper.get(k) != job[k] for k in ("job_id", "input_sha256", "seed", "arm", "origin"))):
                    raise ValueError("reuse wrapper relabelled or substituted pilot evidence")
            circuit = circuits[job["input_sha256"]]
            diagnostics = failure_evidence(row, circuit, receipt_ref, sources)
            observations.append(normalize_observation(baseline, row, job, circuit,
                                                       model, source_ref, receipt_ref, diagnostics))
        compact = {"protocol_id": p["protocol_id"], "complete": not missing,
            "planned_canonical_jobs": 507, "fresh_receipts_available": len(available),
            "fresh_receipts_missing": len(missing), "checked_fresh": len(fresh_rows),
            "checked_reused": len(pilot_rows), "checked_status_counts": dict(Counter(r["status"] for r in observations)),
            "verified_baseline_records": len(baselines), "physical_replay": False}
        if audit_only:
            sources.finish()
            return None, {**compact, "statistics_exported": False, "read_only": True}
        supervision = check_completion(p, protocol_path, fresh_rows, sources)
        expanded = expand_aliases(observations, circuits)
        if len(observations) != 507 or len(expanded) != 516:
            raise ValueError("final export requires 507 canonical/516 display-seed observations")
        generator = sources.file(Path(__file__))
        timing = fixed_mapping_timing(sources)
        budget_timing = fixed_mapping_budget_timing(sources)
        old_failure_context = historical_failure_context(observations, sources)
        provenance = {"protocol": sources.file(protocol_path), "pilot_protocol": sources.file(pilot_path),
            "generator": generator, "baseline_loader": sources.file(p["baseline_loader"]),
            "baselines": baseline_provenance, "supervision": supervision, "pilot_scheduling": pilot_scheduling,
            "source_snapshot_sha256": p["source_snapshot_sha256"], "pilot_source_snapshot_sha256": pilot["source_snapshot_sha256"],
            "canonical_jobs": 507, "display_records": 516, "fresh_jobs": 480, "reused_jobs": 27,
            "verification": "Validated worker evidence + hash closure; independently recomputed saved-score consistency, eligibility, aggregates and macros. No export-stage trace replay or physical simulation.",
            "physical_replay": False,
            "canonical_status_counts": dict(Counter(r["status"] for r in observations)),
            "canonical_completed_ood_N": sum(r["status"] == "success" and r["fidelity_ood"] for r in observations),
            "display_status_counts": dict(Counter(r["status"] for r in expanded)),
            "timing_policy": p["timing_policy"], "reuse_policy": p["reuse_policy"],
            "baseline_policy": p["baseline_policy"], "historical_fixed_layout_timing": timing,
            "historical_fixed_mapping_budget_timing": budget_timing,
            "historical_failure_context": old_failure_context,
            "source_files": sources.finish()}
        outputs = render_exports(baseline, baselines, expanded, inventory, provenance)
        outputs["canonical_runs.csv"] = baseline.csv_text(sorted(observations,
            key=lambda r: (r["canonical_sha256"], r["seed"])))
        outputs["failures.csv"] = outcome_csv(baseline, observations)
        outputs["completed_ood.csv"] = outcome_csv(baseline, observations, ood=True)
        outputs["provenance.json"] = canonical({**provenance,
            "output_files": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in sorted(outputs.items())}})
        sources.finish()
        return outputs, compact


def write_or_check(outputs, destination, *, protocol_path=DEFAULT_PROTOCOL, check=False):
    destination, study_root = Path(destination).resolve(), Path(protocol_path).resolve().parent
    allowed = [study_root / "paper_exports", study_root / "publication"]
    if not any(destination == base or destination.is_relative_to(base) for base in allowed):
        raise ValueError("output must be paper_exports/publication or a new version below the new study root")
    differences = [name for name, content in outputs.items() if not (destination / name).is_file()
                   or (destination / name).read_text(encoding="utf-8") != content]
    if check:
        return {"status": "fail" if differences else "pass", "read_only": True, "different_exports": differences}
    if destination.exists():
        raise FileExistsError("export directory already exists; use --check or a new versioned directory")
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in outputs.items():
        with (destination / name).open("x", encoding="utf-8") as stream:
            stream.write(content)
    return {"status": "pass", "output_root": str(destination),
            "files": {name: sha256(destination / name) for name in sorted(outputs)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-root", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--audit-only", action="store_true")
    parser.add_argument("--audit-limit", type=int, default=6,
                        help="maximum fresh and maximum reused records in read-only audit mode")
    args = parser.parse_args(argv)
    outputs, report = compute_exports(args.protocol, audit_only=args.audit_only, audit_limit=args.audit_limit)
    if not args.audit_only:
        destination = args.output_root or args.protocol.resolve().parent / "paper_exports"
        report = write_or_check(outputs, destination, protocol_path=args.protocol, check=args.check)
    print(canonical(report), end="")
    return int(report.get("status") == "fail")


if __name__ == "__main__":
    raise SystemExit(main())
