#!/usr/bin/env python3
"""Export the sealed linked-39 initialization study, independently of old values.

No compiler or research module is imported. Metrics are recomputed from every
prespecified circuit/seed receipt and result, checked against sealed reports,
and exported only after the complete evidence chain passes. --check never writes.
Unfinished jobs remain explicit; only complete three-seed circuits enter means.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics


STUDY_RELATIVE = Path("ZAC_zzx/results/initial_lookahead_v1/"
    "dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1")
PILOT_RELATIVE = Path("ZAC_zzx/results/initial_lookahead_v1/"
    "nine-family-h0-h2-sa-k4-rho07-b32-seed012-v1")
PROTOCOL_SHA256 = "0cb1d3f834f456fc1f5d78c16029178bd7afe923b075fc2f441551d469d7935f"
PILOT_PROTOCOL_SHA256 = "3f99d0207dcbf87ea44b12810bfeac0f5272f544013d72dffa9802a7342f8f69"
PILOT_SUMMARY_SHA256 = "c4c020e9fb0bf5b4cd8af2027e761df45db28eb1edad7aac7121009470b12477"
# Final reports sealed after all 96 extension jobs and the cross-phase audit.
RAW_SUMMARY_SHA256 = "e2c16121a5488ce59b8026e7cb759316e56e98b3228875be4b851723f81bfdaf"
SUMMARY_SHA256 = "1fe755f84c98fc6ed00f5313c7eb8aba934550d87edf092c8b1e2c12186b4ed3"
NEW32_SUMMARY_SHA256 = "22bd28f5d81a6fac0c8d9211ebdb8899a34f9281d4a1f082dd31db6fa706c8db"
ANALYSIS_PROVENANCE_SHA256 = "e8762b5b9be353b27487a9250a86277c720e35edf7c43fcd183941ba370608ab"
ARMS = ("sa_reference", "h0", "h2")
SEEDS = [0, 1, 2]
PERCENT_FIELDS = ("InitExtFidelityGain", "InitExtBatchReduction", "InitExtVsSAGain")
COUNT_FIELDS = ("InitExtTargetN", "InitExtCompleteN", "InitExtTimeouts")
DERIVED_FILENAMES = ("initial_lookahead_extension_values.tex",
    "paper_initial_lookahead_extension_values.json",
    "paper_initial_lookahead_extension_provenance.json")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False,
        allow_nan=False) + "\n"


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def compare(expected, actual, context):
    """Compare recomputed fields, allowing unrelated phase-local timing fields."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{context}: expected an object")
        for key, value in expected.items():
            require(key in actual, f"{context}: missing {key}")
            compare(value, actual[key], f"{context}/{key}")
    elif isinstance(expected, float):
        require(finite(actual) and math.isclose(expected, actual, rel_tol=1e-10,
            abs_tol=1e-12), f"{context}: recomputed metric mismatch")
    else:
        require(type(expected) is type(actual) and expected == actual,
            f"{context}: value or type mismatch")


def validate_protocols(protocol, pilot):
    controls = {"seeds": SEEDS, "horizons": [0, 2], "candidates": 4, "rho": .7,
        "rollout_evaluations": 32, "dynamic_horizon": 8,
        "timeout_per_paired_job_s": 600}
    for phase, document in (("extension", protocol), ("pilot", pilot)):
        compare(controls, document, f"{phase}/controls")
        require(all(type(seed) is int for seed in document["seeds"]),
            f"{phase}: seed identities must be integers")
        names = [row["circuit"] for row in document["circuits"]]
        require(len(names) == len(set(names)), f"{phase}: duplicate circuits")
        expected_jobs = [{"circuit": name, "seed": seed, "job_id": f"{name}-s{seed}"}
            for name in names for seed in SEEDS]
        require(document["job_order"] == expected_jobs
            and all(type(j["seed"]) is int for j in document["job_order"]),
            f"{phase}: incorrect or missing prespecified seed jobs")
    require(protocol["protocol_id"] == "initial-physical-prefix-dynamic39-missing32-v1"
        and pilot["protocol_id"] == "initial-physical-prefix-nine-family-seed3-v1",
        "unexpected initialization protocol")
    require(protocol["formal_paper_result"] is False and protocol["workers"] == 3,
        "extension status or execution phase changed")
    target, new, reused = (protocol["cohort"][key] for key in ("target", "new", "reused"))
    require(len(target) == len(set(target)) == 39 and len(new) == len(set(new)) == 32
        and len(reused) == len(set(reused)) == 7
        and not set(new) & set(reused) and set(target) == set(new) | set(reused),
        "fixed target must contain all 39 circuits: new 32 plus reused 7")
    require([r["circuit"] for r in protocol["circuits"]] == new,
        "extension circuit order differs from its prespecified cohort")
    old = {r["circuit"] for r in pilot["circuits"]}
    require(len(old) == 9 and set(reused) <= old
        and sorted(old - set(reused)) == protocol["cohort"]["pilot_outside_target"]
        == ["cm82a_208", "ex1_226"], "pilot coverage or excluded identities changed")


def valid_result(result):
    score = result.get("score", {})
    return (result.get("status") == "success"
        and result.get("validation", {}).get("ok") is True
        and result["validation"].get("ghost_hits") == 0
        and score.get("ood") is False and finite(score.get("log_fidelity")))


def aggregate(rows):
    complete = [row for row in rows if row["complete"]]
    result = {"planned_circuits": len(rows), "complete_circuits": len(complete),
        "analysis_unit": "circuit (three seeds averaged within circuit)",
        "arms": {}, "comparisons": {}}
    if not complete:
        return result
    for arm in ARMS:
        result["arms"][arm] = {key: statistics.mean(row["arms"][arm][key]
            for row in complete) for key in
            ("mean_log_fidelity", "mean_move_batches", "mean_move_time_us")}
        result["arms"][arm]["geometric_fidelity"] = math.exp(
            result["arms"][arm]["mean_log_fidelity"])
    for treatment, control in (("h2", "h0"), ("h0", "sa_reference"), ("h2", "sa_reference")):
        logs = [row["arms"][treatment]["mean_log_fidelity"]
            - row["arms"][control]["mean_log_fidelity"] for row in complete]
        batches = [row["arms"][control]["mean_move_batches"]
            - row["arms"][treatment]["mean_move_batches"] for row in complete]
        def wtl(values):
            return {"wins": sum(x > 1e-12 for x in values),
                "ties": sum(abs(x) <= 1e-12 for x in values),
                "losses": sum(x < -1e-12 for x in values)}
        baseline = result["arms"][control]["mean_move_batches"]
        require(baseline > 0, "batch reduction denominator must be positive")
        result["comparisons"][f"{treatment}_vs_{control}"] = {
            "fidelity_relative_gain_percent": 100 * math.expm1(statistics.mean(logs)),
            "move_batches_reduction_percent": 100 * (1
                - result["arms"][treatment]["mean_move_batches"] / baseline),
            "fidelity_win_tie_loss": wtl(logs), "batch_win_tie_loss": wtl(batches)}
    return result


def circuit_row(entry, jobs, phase, source_root):
    require([job["seed"] for job in jobs] == SEEDS, "wrong within-circuit seed order")
    row = {**entry, "source_phase": phase, "source_root": str(source_root),
        "complete": all(job["accepted"] and all(valid_result(job["results"].get(a, {}))
            for a in ARMS) for job in jobs),
        "planned_seeds": SEEDS, "job_statuses": [job["status"] for job in jobs], "arms": {}}
    common = [job for job in jobs if job["accepted"] and
        all(valid_result(job["results"].get(a, {})) for a in ARMS)]
    row["common_valid_paired_seeds"] = [job["seed"] for job in common]
    row["common_seed_comparisons"] = {}
    for arm in ARMS:
        available = [job["results"][arm] for job in jobs if arm in job["results"]]
        valid = [result for result in available if valid_result(result)]
        arm_row = {"completed_runs": len(available), "valid_runs": len(valid),
            "selected_candidates": [r.get("selected_candidate") for r in available]}
        if valid:
            arm_row.update({out: statistics.mean(r["score"][key] for r in valid)
                for out, key in (("mean_log_fidelity", "log_fidelity"),
                ("mean_move_batches", "move_batches"), ("mean_move_time_us", "move_time_us"))})
            arm_row["geometric_fidelity"] = math.exp(arm_row["mean_log_fidelity"])
        row["arms"][arm] = arm_row
    if common:
        for treatment, control in (("h2", "h0"), ("h2", "sa_reference")):
            means = {a: statistics.mean(j["results"][a]["score"]["move_batches"]
                for j in common) for a in (treatment, control)}
            require(means[control] > 0, "descriptive batch denominator must be positive")
            row["common_seed_comparisons"][f"{treatment}_vs_{control}"] = {
                "fidelity_relative_gain_percent": 100 * math.expm1(statistics.mean(
                    j["results"][treatment]["score"]["log_fidelity"]
                    - j["results"][control]["score"]["log_fidelity"] for j in common)),
                "move_batches_reduction_percent": 100 * (1 - means[treatment] / means[control]),
                "descriptive_only": len(common) != 3}
    return row


def validate_documents(protocol, pilot, linked, new_report, records, roots):
    validate_protocols(protocol, pilot)
    metadata = {r["circuit"]: r for r in protocol["circuits"] + pilot["circuits"]}
    target = protocol["cohort"]["target"]
    require(set(records) == {(name, seed) for name in target for seed in SEEDS},
        "records must retain all 117 prespecified paired jobs")
    rows = []
    for name in target:
        phase = "parallel_extension" if name in protocol["cohort"]["new"] else "serial_pilot"
        rows.append(circuit_row(metadata[name], [records[name, s] for s in SEEDS],
            phase, roots[phase]))
    require([r["circuit"] for r in linked["per_circuit"]] == target,
        "linked summary omitted or reordered prespecified circuits")
    require(linked["protocol_id"] == "linked-target39-quality-analysis-v1"
        and linked["formal_paper_result"] is False, "wrong linked analysis identity")
    for actual, expected in zip(linked["per_circuit"], rows):
        compare(expected, actual, f"linked/{expected['circuit']}")
        require(all(not key.endswith("_s") for arm in actual["arms"].values() for key in arm),
            "cross-phase wall-clock times must not be pooled")
    statuses = dict(Counter(record["status"] for record in records.values()))
    require(linked["job_statuses"] == statuses, "linked outcomes, including failures, changed")
    overall = aggregate(rows)
    compare(overall, linked["overall"], "linked/overall")
    require(all("end_to_end_time_ratio" not in c for c in linked["overall"]["comparisons"].values())
        and all(not key.endswith("_s") for arm in linked["overall"]["arms"].values() for key in arm),
        "cross-phase compilation timing must remain separate")
    stages = {}
    for phase, names, report in (
        ("parallel_extension", protocol["cohort"]["new"], new_report),
        ("serial_pilot", protocol["cohort"]["reused"], None)):
        selected = [row for row in rows if row["circuit"] in names]
        # Reports preserve protocol order rather than interleaved target order.
        selected = sorted(selected, key=lambda row: names.index(row["circuit"]))
        stages[phase] = {"circuits": names, "overall": aggregate(selected),
            "job_statuses": dict(Counter(records[n, s]["status"] for n in names for s in SEEDS))}
        if report is not None:
            require([r["circuit"] for r in report["per_circuit"]] == names,
                "new-32 phase coverage changed")
            for actual, expected in zip(report["per_circuit"], selected):
                compare(expected, actual, f"new32/{expected['circuit']}")
            compare(stages[phase]["overall"], report["overall"], "new32/overall")
            require(stages[phase]["job_statuses"] == report["job_statuses"],
                "new-32 phase outcomes changed")
    require(overall["complete_circuits"] > 0, "no complete circuits for primary metrics")
    comparisons = overall["comparisons"]
    values = {"InitExtTargetN": len(rows), "InitExtCompleteN": overall["complete_circuits"],
        "InitExtTimeouts": statuses.get("timeout", 0),
        "InitExtFidelityGain": comparisons["h2_vs_h0"]["fidelity_relative_gain_percent"],
        "InitExtBatchReduction": comparisons["h2_vs_h0"]["move_batches_reduction_percent"],
        "InitExtVsSAGain": comparisons["h2_vs_sa_reference"]["fidelity_relative_gain_percent"]}
    return values, rows, stages, overall


def load_verified(project_root):
    project_root = Path(project_root).resolve()
    study, pilot_root = project_root / STUDY_RELATIVE, project_root / PILOT_RELATIVE
    pins = [(study / "protocol.json", PROTOCOL_SHA256),
        (study / "summary.json", RAW_SUMMARY_SHA256),
        (study / "linked39_analysis.json", SUMMARY_SHA256),
        (study / "new32_analysis.json", NEW32_SUMMARY_SHA256),
        (study / "postrun_analysis_provenance.json", ANALYSIS_PROVENANCE_SHA256),
        (pilot_root / "protocol.json", PILOT_PROTOCOL_SHA256),
        (pilot_root / "summary.json", PILOT_SUMMARY_SHA256)]
    require(all(isinstance(pin, str) and len(pin) == 64 for _, pin in pins),
        "final extension reports are not yet sealed; no manuscript values may be generated")
    hashes = {}
    for path, expected in pins:
        require(sha256(path) == expected, f"sealed input hash mismatch: {path.name}")
        hashes[str(path)] = expected
    def read(path, evidence=None, required=False):
        path = Path(path)
        digest = sha256(path)
        if required:
            require(str(path) in evidence, f"input absent from sealed post-run audit: {path}")
        if evidence is not None and str(path) in evidence:
            require(digest == evidence[str(path)], f"seed evidence hash mismatch: {path}")
        hashes[str(path)] = digest
        return json.loads(path.read_text())
    protocol, pilot = read(study / "protocol.json"), read(pilot_root / "protocol.json")
    validate_protocols(protocol, pilot)
    require(Path(protocol["pilot_root"]).resolve() == pilot_root, "pilot source path changed")
    raw, old_raw = read(study / "summary.json"), read(pilot_root / "summary.json")
    linked, new_report = read(study / "linked39_analysis.json"), read(study / "new32_analysis.json")
    provenance = read(study / "postrun_analysis_provenance.json")
    expected_sources = {"extension_protocol_sha256": PROTOCOL_SHA256,
        "extension_summary_sha256": RAW_SUMMARY_SHA256,
        "pilot_protocol_sha256": PILOT_PROTOCOL_SHA256, "pilot_summary_sha256": PILOT_SUMMARY_SHA256,
        "analysis_source_sha256": sha256(study / "analyze_outputs.py"),
        "analysis_test_sha256": sha256(study / "test_analyze_outputs.py")}
    for label, doc in (("linked", linked), ("new32", new_report), ("provenance", provenance)):
        require(doc["sources"] == expected_sources, f"{label}: analysis source chain mismatch")
    require(raw["source_stable"] is True and raw["protocol_sha256"] == PROTOCOL_SHA256
        and old_raw["source_stable"] is True, "source stability not established")
    records = {}
    for phase, root, document, names, audit_key in (
        ("parallel_extension", study, protocol, protocol["cohort"]["new"], "new_phase_audit"),
        ("serial_pilot", pilot_root, pilot, protocol["cohort"]["reused"], "linked_pilot7_audit")):
        audit = provenance[audit_key]
        require(audit["jobs"] == len(names) * 3
            and audit["completed_arms"] == audit["physically_valid_arms"]
            == audit["logical_sequences_valid"] and audit["ood_arms"] == 0,
            f"{phase}: incomplete physical/logical post-run audit")
        evidence = audit["evidence_hashes"]
        metadata = {r["circuit"]: r for r in document["circuits"]}
        for name in names:
            for seed in SEEDS:
                job_id = f"{name}-s{seed}"
                job = root / "jobs" / job_id
                receipt = read(root / "receipts" / f"{job_id}.json", evidence, True)
                require(receipt["circuit"] == name and type(receipt["seed"]) is int
                    and receipt["seed"] == seed and receipt["job_id"] == job_id,
                    f"incorrect circuit/seed receipt identity: {job_id}")
                require(receipt["status"] in ("success", "timeout", "memory_limit", "failed"),
                    f"unsealed or unsupported job outcome: {job_id}")
                jp = read(job / "protocol.json")
                require(type(jp["dynamic_setting"]["seed"]) is int
                    and jp["dynamic_setting"]["seed"] == seed
                    and jp["input_sha256"] == metadata[name]["input_sha256"]
                    and jp["repository"]["source_sha256"] == document["repository"]["source_sha256"],
                    f"job seed/input/source identity mismatch: {job_id}")
                expected_configs = [{"candidates": 4, "horizon": h, "rho": .7,
                    "rollout_evaluations": 32, "seed": seed} for h in (0, 2)]
                require(jp["initial_configs"] == expected_configs
                    and jp["selection_uses_full_compile_results"] is False,
                    f"job selection controls changed: {job_id}")
                completed = read(job / "summary.json") if (job / "summary.json").exists() else {}
                if receipt["status"] == "success":
                    require(completed.get("source_stable") is True,
                        f"successful receipt lacks stable completed job: {job_id}")
                result = {"seed": seed, "status": receipt["status"],
                    "accepted": receipt["status"] == "success" and completed.get("source_stable") is True,
                    "results": {}}
                for arm in ARMS:
                    path = job / f"{arm}_result.json"
                    if not path.exists():
                        require(not result["accepted"], f"missing completed seed result: {job_id}/{arm}")
                        continue
                    value = read(path, evidence, True)
                    expected_horizon = None if arm == "sa_reference" else int(arm[1:])
                    require(value["variant"] == arm and value["horizon"] == expected_horizon
                        and value["dynamic_setting_sha256"] == stable_hash(jp["dynamic_setting"]),
                        f"result arm/seed configuration mismatch: {job_id}/{arm}")
                    score = value["score"]
                    require(value["n_qubits"] == metadata[name]["qubits"]
                        and score["counts"]["one_qubit_gates"] == metadata[name]["gates_1q"]
                        and score["counts"]["two_qubit_gates"] == metadata[name]["gates_2q"],
                        f"input ledger mismatch: {job_id}/{arm}")
                    if valid_result(value):
                        require(finite(score["move_batches"]) and score["move_batches"] >= 0
                            and finite(score["move_time_us"]) and score["move_time_us"] >= 0,
                            f"nonfinite or negative physical metric: {job_id}/{arm}")
                    result["results"][arm] = value
                records[name, seed] = result
    roots = {"parallel_extension": study, "serial_pilot": pilot_root}
    values, rows, stages, overall = validate_documents(protocol, pilot, linked, new_report, records, roots)
    require({k: v for k, v in raw["job_statuses"].items() if v}
        == stages["parallel_extension"]["job_statuses"], "raw queue outcomes disagree")
    compare(stages["parallel_extension"]["overall"], raw["overall"], "raw extension/overall")
    old_by_name = {r["circuit"]: r for r in old_raw["per_circuit"]}
    for row in rows:
        if row["source_phase"] == "serial_pilot":
            compare({k: row[k] for k in ("complete", "planned_seeds", "job_statuses", "arms")},
                old_by_name[row["circuit"]], f"pilot/{row['circuit']}")
    return protocol, values, rows, stages, overall, records, hashes


def expected_exports(project_root):
    protocol, values, rows, stages, overall, records, hashes = load_verified(project_root)
    generator_hash = sha256(__file__)
    lines = ["% Generated by writing/generate_initial_lookahead_extension_values.py; do not edit.",
        "% Linked target39 quality-only study; distinct from frozen main and nine-family results.",
        f"% Protocol SHA-256: {PROTOCOL_SHA256}", f"% Linked summary SHA-256: {SUMMARY_SHA256}",
        f"% Generator SHA-256: {generator_hash}",
        "% Three seeds averaged within each complete circuit; no cross-phase wall-time pooling."]
    for key in COUNT_FIELDS + PERCENT_FIELDS:
        value = str(values[key]) if key in COUNT_FIELDS else f"{values[key]:.5f}\\%"
        lines.append(f"\\newcommand{{\\{key}}}{{{value}}}")
    for key in PERCENT_FIELDS:
        lines.append(f"\\newcommand{{\\{key}Rounded}}{{{values[key]:.2f}\\%}}")
    output = {"export_schema": "initial-lookahead-linked39-paper-v1",
        "protocol_sha256": PROTOCOL_SHA256, "summary_sha256": SUMMARY_SHA256,
        "generator_sha256": generator_hash, "values": values, "overall": overall,
        "separate_from_frozen_main_results": True, "separate_from_nine_family_values": True,
        "analysis_unit": "circuit; mean of log fidelity and batch counts across seeds 0, 1, 2",
        "timing_policy": "Quality only; serial-pilot and parallel-extension wall times are not pooled.",
        "seed_semantics": protocol["sa_seed_semantics"],
        "all_study_circuits": [r["circuit"] for r in rows],
        "primary_cohort": [r["circuit"] for r in rows if r["complete"]],
        "incomplete_circuits": [r for r in rows if not r["complete"]],
        "old_pilot_outside_target": protocol["cohort"]["pilot_outside_target"],
        "phases": stages, "per_circuit": rows,
        "job_outcomes": [{"circuit": name, "seed": seed, "status": records[name, seed]["status"],
            "accepted": records[name, seed]["accepted"],
            "available_arms": list(records[name, seed]["results"])}
            for name in protocol["cohort"]["target"] for seed in SEEDS],
        "display_percent_values": {key + "Rounded": f"{values[key]:.2f}" for key in PERCENT_FIELDS}}
    exports = {DERIVED_FILENAMES[0]: "\n".join(lines) + "\n", DERIVED_FILENAMES[1]: json_text(output)}
    provenance = {"export_schema": "initial-lookahead-linked39-provenance-v1",
        "generator_sha256": generator_hash,
        "generator_test_sha256": sha256(Path(__file__).with_name(
            "test_generate_initial_lookahead_extension_values.py")),
        "protocol_sha256": PROTOCOL_SHA256, "summary_sha256": SUMMARY_SHA256,
        "input_sha256": hashes, "recomputed_from_all_prespecified_seed_results": True,
        "source_evidence_modified": False,
        "derived_file_sha256": {name: hashlib.sha256(text.encode()).hexdigest()
            for name, text in exports.items()}}
    exports[DERIVED_FILENAMES[2]] = json_text(provenance)
    return exports, values


def check_exports(paper_root, exports):
    stale = []
    for name, expected in exports.items():
        path = Path(paper_root) / name
        if not path.is_file():
            stale.append({"file": name, "reason": "missing"})
        elif path.read_bytes() != expected.encode():
            stale.append({"file": name, "reason": "stale"})
    return stale


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
            print(json_text({"status": "failed" if stale else "passed",
                "read_only": True, "stale_files": stale}), end="")
            return int(bool(stale))
        args.paper_root.mkdir(parents=True, exist_ok=True)
        for name, text in exports.items():
            (args.paper_root / name).write_text(text, encoding="utf-8")
        print(json_text({"status": "generated", "values": values}), end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        print(json_text({"status": "failed", "read_only": args.check,
            "error": f"{type(error).__name__}: {error}"}), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
