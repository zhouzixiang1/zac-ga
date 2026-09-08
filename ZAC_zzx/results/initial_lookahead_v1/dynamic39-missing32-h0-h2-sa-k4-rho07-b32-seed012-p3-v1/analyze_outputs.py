"""Post-run evidence audit; never executes a compiler or rewrites run evidence.

This analysis file is deliberately outside the runtime source snapshot. The
protocol and raw summaries are immutable; exports are newly created files.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import statistics

from evaluation import normalize_zair, score_trace, validate_trace_physics
from experiments_v2.initial_lookahead_batch import ARMS, collect, summarize_records, valid_result
from experiments_v2.initial_lookahead_runner import REPO, file_hash, logical_trace_receipt, write_json
from zzx.initial_lookahead import candidate_pool, stable_hash


ROOT = Path(__file__).resolve().parent
PROTOCOL_SHA = "0cb1d3f834f456fc1f5d78c16029178bd7afe923b075fc2f441551d469d7935f"
PILOT_PROTOCOL_SHA = "3f99d0207dcbf87ea44b12810bfeac0f5272f544013d72dffa9802a7342f8f69"
PILOT_SUMMARY_SHA = "c4c020e9fb0bf5b4cd8af2027e761df45db28eb1edad7aac7121009470b12477"


def read(path):
    return json.loads(Path(path).read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit_selection(selection, base, horizon, seed, total_layers):
    require(selection["config"] == {"candidates": 4, "horizon": horizon, "rho": .7,
            "rollout_evaluations": 32, "seed": seed}, "selection config mismatch")
    pool = candidate_pool(base, count=4, seed=seed)
    require(selection["candidate_count"] == len(pool) and
            selection["candidate_pool_sha256"] == stable_hash(pool), "candidate pool mismatch")
    require(selection["rollout_config"]["lookahead_horizon"]["max_horizon"] == 0 and
            selection["rollout_config"]["max_unique_evaluations"] == 32, "inner lookahead/budget mismatch")
    require(len(selection["candidates"]) == len(pool), "missing candidate")
    for index, candidate in enumerate(selection["candidates"]):
        require(candidate["candidate"] == index and candidate["mapping"] == [list(site) for site in pool[index]] and
                candidate["mapping_sha256"] == stable_hash(pool[index]), "candidate mapping mismatch")
        if candidate["status"] != "success":
            continue
        require(candidate["actual_total_layers"] == total_layers and candidate["internal_horizon"] == 0 and
                candidate["layers_scored"] == min(total_layers, horizon + 1), "false prefix termination")
        previous = 0.
        for layer, row in enumerate(candidate["rows"]):
            require(row["layer"] == layer and math.isclose(row["weight"], .7 ** layer), "wrong prefix weight")
            require(row["validation"]["ok"] and row["validation"]["ghost_hits"] == 0, "invalid candidate physics")
            require(math.isclose(row["cumulative_nll"], -row["score"]["log_fidelity"], abs_tol=1e-12) and
                    math.isclose(row["increment_nll"], row["cumulative_nll"] - previous, abs_tol=1e-12) and
                    math.isclose(row["weighted_increment_nll"], row["weight"] * row["increment_nll"], abs_tol=1e-12),
                    "prefix cost decomposition mismatch")
            previous = row["cumulative_nll"]
        require(math.isclose(candidate["weighted_nll"], sum(r["weighted_increment_nll"] for r in candidate["rows"]),
                             abs_tol=1e-12), "candidate weighted cost mismatch")
    valid = [r for r in selection["candidates"] if r["status"] == "success"]
    best = min(valid, key=lambda r: (r["weighted_nll"], r["candidate"]))
    require(selection["selected_candidate"] == best["candidate"] and
            selection["selected_mapping_sha256"] == best["mapping_sha256"] and
            selection["failed_candidates"] == len(pool) - len(valid), "wrong selected candidate")


def audit_phase(root, protocol, names):
    root = Path(root).resolve()
    records = collect(root, protocol)
    arch = read(protocol["architecture_path"])
    require(file_hash(protocol["architecture_path"]) == protocol["architecture_sha256"], "architecture changed")
    require(file_hash(protocol["config_path"]) == protocol["config_sha256"], "config changed")
    require(file_hash(protocol["python"]) == protocol["python_sha256"], "Python runtime changed")
    require(file_hash(protocol["native"]["extension_path"]) == protocol["native"]["extension_sha256"],
            "native extension changed")
    base_setting = read(protocol["config_path"])["base_config"]["zac_setting"][0]
    metadata = {r["circuit"]: r for r in protocol["circuits"]}
    audit = {"jobs": 0, "completed_arms": 0, "physically_valid_arms": 0,
             "logical_sequences_valid": 0, "ood_arms": 0, "same_map_h0_h2": 0,
             "complete_paired_jobs": 0, "failures": [], "evidence_hashes": {}}
    for name in names:
        entry = metadata[name]
        require(file_hash(entry["input"]) == entry["input_sha256"], f"input changed: {name}")
        for seed in protocol["seeds"]:
            job_id = f"{name}-s{seed}"
            job = root / "jobs" / job_id
            receipt_path = root / "receipts" / f"{job_id}.json"
            receipt = read(receipt_path)
            record = records[name, seed]
            audit["jobs"] += 1
            audit["evidence_hashes"][str(receipt_path)] = file_hash(receipt_path)
            for evidence in ("protocol.json", "summary.json", "failure.json", "base_mapping.json",
                             "h0_selection.json", "h2_selection.json", "sealed_choices.json"):
                evidence_path = job / evidence
                if evidence_path.exists():
                    audit["evidence_hashes"][str(evidence_path)] = file_hash(evidence_path)
            if receipt["status"] != "success":
                audit["failures"].append({"circuit": name, "seed": seed,
                    "status": receipt["status"], "last_started_stage": receipt.get("last_started_stage"),
                    "elapsed_s": receipt["elapsed_s"], "source_receipt": str(receipt_path)})
            job_protocol = read(job / "protocol.json")
            require(job_protocol["repository"]["source_sha256"] == protocol["repository"]["source_sha256"],
                    f"job/source mismatch: {job_id}")
            require(job_protocol["input_sha256"] == entry["input_sha256"], f"job/input mismatch: {job_id}")
            expected_setting = deepcopy(base_setting)
            expected_setting.update(seed=seed, name=name, dir=str(job) + "/",
                arch_spec=protocol["architecture_path"], use_verifier=False, resyn=False)
            expected_setting.pop("init_strategy", None)
            expected_setting.pop("initial_lookahead", None)
            require(job_protocol["dynamic_setting"] == expected_setting, f"dynamic config mismatch: {job_id}")
            require(job_protocol["native"] == protocol["native"] and
                    job_protocol["config_sha256"] == protocol["config_sha256"] and
                    job_protocol["architecture_sha256"] == protocol["architecture_sha256"] and
                    job_protocol["python"] == protocol["python"], f"runtime identity mismatch: {job_id}")
            require(job_protocol["initial_configs"] == [{"candidates": 4, "horizon": h, "rho": .7,
                    "rollout_evaluations": 32, "seed": seed} for h in (0, 2)], "job initializer config mismatch")
            require(job_protocol["selection_uses_full_compile_results"] is False, "post-hoc selection")
            if (job / "base_mapping.json").exists():
                base = read(job / "base_mapping.json")
                require(stable_hash(base["mapping"]) == base["mapping_sha256"], "base mapping hash changed")
                for h in (0, 2):
                    path = job / f"h{h}_selection.json"
                    if path.exists():
                        audit_selection(read(path), base["mapping"], h, seed, base["total_layers"])
            if (job / "sealed_choices.json").exists():
                seal = read(job / "sealed_choices.json")
                choices = {r["variant"]: r for r in seal["choices"]}
                require(seal["selection_uses_full_compile_results"] is False, "post-hoc seal")
                require(choices["sa_reference"]["candidate"] == 0 and
                        choices["sa_reference"]["mapping_sha256"] == base["mapping_sha256"], "SA seal mismatch")
                for h in (0, 2):
                    selection = read(job / f"h{h}_selection.json")
                    require(choices[f"h{h}"]["candidate"] == selection["selected_candidate"] and
                            choices[f"h{h}"]["mapping_sha256"] == selection["selected_mapping_sha256"] and
                            seal["candidate_pool_sha256"] == selection["candidate_pool_sha256"], "selection/seal mismatch")
                init = record["initialization_audit"]
                require(init and all(init[key] for key in
                    ("same_pool", "same_rollout_policy", "same_common_prefix", "choices_sealed")),
                    f"initialization control mismatch: {job_id}")
                if record["accepted"] and all(valid_result(record.get(a, {})) for a in ARMS):
                    audit["complete_paired_jobs"] += 1
                    audit["same_map_h0_h2"] += choices["h0"]["mapping_sha256"] == choices["h2"]["mapping_sha256"]
                for path in (job / "base_mapping.json", job / "sealed_choices.json",
                             job / "h0_selection.json", job / "h2_selection.json"):
                    audit["evidence_hashes"][str(path)] = file_hash(path)
            else:
                choices = {}
            for arm in ARMS:
                if arm not in record:
                    continue
                result = record[arm]
                require(result["dynamic_setting_sha256"] == stable_hash(expected_setting), "result/config mismatch")
                trace_path = job / f"{arm}_native.json"
                result_path = job / f"{arm}_result.json"
                trace = read(trace_path)
                require(stable_hash(trace["instructions"]) == result["native_instruction_sha256"],
                        f"instruction hash changed: {job_id}/{arm}")
                init_locs = trace["instructions"][0]["init_locs"]
                mapping = [r[1:] for r in sorted(init_locs, key=lambda r: r[0])]
                mapping_sha = stable_hash(mapping)
                require(mapping_sha == result["selected_mapping_sha256"] == choices[arm]["mapping_sha256"],
                        f"initial mapping mismatch: {job_id}/{arm}")
                events = tuple(normalize_zair(trace, architecture=arch))
                physics = validate_trace_physics(events, n_qubits=entry["qubits"])
                score = score_trace(events, n_qubits=entry["qubits"]).to_dict()
                require(physics["ok"] and physics["ghost_hits"] == 0, f"physics mismatch: {job_id}/{arm}")
                for key in ("fidelity", "log_fidelity", "move_batches", "move_time_us"):
                    a, b = score[key], result["score"][key]
                    require(a == b if a is None or b is None else math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12),
                            f"score mismatch: {job_id}/{arm}/{key}")
                # The old pilot did not export the compiled-schedule ledger.
                # Recheck its per-atom canonical operation sequence here; the
                # new phase additionally checked the actual compiler schedule.
                layers = [e.gate_pairs for e in events if e.kind == "two_qubit_gate"]
                logical = logical_trace_receipt(entry["input"], entry["qubits"], events, layers)
                if "logical_validation" in result:
                    require(result["logical_validation"]["ok"] and
                            result["logical_validation"]["observed_layer_ledger_sha256"] ==
                            logical["observed_layer_ledger_sha256"], "logical ledger changed")
                audit["completed_arms"] += 1
                audit["physically_valid_arms"] += 1
                audit["logical_sequences_valid"] += 1
                audit["ood_arms"] += bool(score["ood"])
                audit["evidence_hashes"][str(trace_path)] = file_hash(trace_path)
                audit["evidence_hashes"][str(result_path)] = file_hash(result_path)
    return records, audit


def quality_only(report):
    for row in report["per_circuit"]:
        for arm in row["arms"].values():
            for key in list(arm):
                if key.endswith("_s"):
                    del arm[key]
    for arm in report["overall"]["arms"].values():
        for key in list(arm):
            if key.endswith("_s"):
                del arm[key]
    for comparison in report["overall"]["comparisons"].values():
        comparison.pop("end_to_end_time_ratio", None)
    report["timing_policy"] = "No serial-pilot/parallel-extension timing pooling. Quality aggregation only."
    return report


def add_common_seed_comparisons(report, protocol, records):
    for row in report["per_circuit"]:
        name = row["circuit"]
        seeds = [s for s in protocol["seeds"] if records[name, s]["accepted"] and
                 all(valid_result(records[name, s].get(a, {})) for a in ARMS)]
        row["common_valid_paired_seeds"] = seeds
        row["common_seed_comparisons"] = {}
        if not seeds:
            continue
        for treatment, control in (("h2", "h0"), ("h2", "sa_reference")):
            diffs = [records[name, s][treatment]["score"]["log_fidelity"] -
                     records[name, s][control]["score"]["log_fidelity"] for s in seeds]
            means = {a: statistics.mean(records[name, s][a]["score"]["move_batches"] for s in seeds)
                     for a in (treatment, control)}
            row["common_seed_comparisons"][f"{treatment}_vs_{control}"] = {
                "fidelity_relative_gain_percent": 100 * math.expm1(statistics.mean(diffs)),
                "move_batches_reduction_percent": 100 * (1 - means[treatment] / means[control]),
                "descriptive_only": len(seeds) != 3}


def markdown(report, title):
    overall = report["overall"]
    lines = [f"# {title}", "", f"预定 {overall['planned_circuits']} 项；完整三种子 {overall['complete_circuits']} 项。",
        "", "分析单位为电路：先在电路内平均种子的 log fidelity，再跨完整电路计算几何保真度；批次为算术均值之比。",
        "不完整项保留；其下表比较仅使用各臂共同有效种子，标为描述性，不进入主汇总。串行先导和并行扩展的墙钟时间不合并。", ""]
    for key, values in overall["comparisons"].items():
        lines.append(f"- {key}: fidelity {values['fidelity_relative_gain_percent']:+.8f}%; batch reduction {values['move_batches_reduction_percent']:+.8f}%; fidelity W/T/L {values['fidelity_win_tie_loss']}; batch W/T/L {values['batch_win_tie_loss']}.")
    lines += ["", "| Circuit | Source | Qubits | Seeds/status | H2/H0 fidelity gain % | H2/H0 batch reduction % | H2/SA fidelity gain % |",
              "|---|---|---:|---|---:|---:|---:|"]
    for row in report["per_circuit"]:
        comparisons = row["common_seed_comparisons"]
        h0, sa = comparisons.get("h2_vs_h0", {}), comparisons.get("h2_vs_sa_reference", {})
        def fmt(obj, key):
            return f"{obj[key]:+.6f}" if key in obj else "—"
        status = "3/3 complete" if row["complete"] else f"{len(row['common_valid_paired_seeds'])}/3 descriptive; " + ", ".join(row["job_statuses"])
        lines.append(f"| {row['circuit']} | [{row.get('source_phase', 'new')}]({row['source_root']}/protocol.json) | {row['qubits']} | {status} | {fmt(h0, 'fidelity_relative_gain_percent')} | {fmt(h0, 'move_batches_reduction_percent')} | {fmt(sa, 'fidelity_relative_gain_percent')} |")
    return "\n".join(lines) + "\n"


def main():
    require(file_hash(ROOT / "protocol.json") == PROTOCOL_SHA, "extension protocol changed")
    protocol = read(ROOT / "protocol.json")
    raw = read(ROOT / "summary.json")  # A running or incomplete queue cannot export.
    require(raw["source_stable"] and sum(raw["job_statuses"].values()) == 96, "queue incomplete or source changed")
    pilot_root = Path(protocol["pilot_root"])
    require(file_hash(pilot_root / "protocol.json") == PILOT_PROTOCOL_SHA, "pilot protocol changed")
    require(file_hash(pilot_root / "summary.json") == PILOT_SUMMARY_SHA, "pilot summary changed")
    pilot = read(pilot_root / "protocol.json")
    current = protocol["repository"]["source_files"]
    require(all(file_hash(REPO / path) == value for path, value in current.items()), "sealed runtime source changed")
    new_records, new_audit = audit_phase(ROOT, protocol, protocol["cohort"]["new"])
    old_records, old_audit = audit_phase(pilot_root, pilot, protocol["cohort"]["reused"])
    independent = summarize_records(protocol, new_records)
    require(independent["overall"] == raw["overall"], "raw summary and recomputation disagree")
    metadata = {r["circuit"]: {**r, "source_root": str(ROOT), "source_phase": "parallel_extension"}
                for r in protocol["circuits"]}
    metadata.update({r["circuit"]: {**r, "source_root": str(pilot_root), "source_phase": "serial_pilot"}
                     for r in pilot["circuits"] if r["circuit"] in protocol["cohort"]["reused"]})
    merged_protocol = {**protocol, "protocol_id": "linked-target39-quality-analysis-v1",
                       "circuits": [metadata[name] for name in protocol["cohort"]["target"]]}
    merged_records = {**new_records, **{key: row for key, row in old_records.items()
                                      if key[0] in protocol["cohort"]["reused"]}}
    linked = quality_only(summarize_records(merged_protocol, merged_records))
    for row in independent["per_circuit"]:
        row.update(source_root=str(ROOT), source_phase="parallel_extension")
    add_common_seed_comparisons(independent, protocol, new_records)
    add_common_seed_comparisons(linked, merged_protocol, merged_records)
    sources = {"extension_protocol_sha256": PROTOCOL_SHA, "extension_summary_sha256": file_hash(ROOT / "summary.json"),
               "pilot_protocol_sha256": PILOT_PROTOCOL_SHA, "pilot_summary_sha256": PILOT_SUMMARY_SHA,
               "analysis_source_sha256": file_hash(__file__),
               "analysis_test_sha256": file_hash(ROOT / "test_analyze_outputs.py")}
    old_sources = pilot["repository"]["source_files"]
    shared = set(old_sources) & set(current)
    changed = sorted(p for p in shared if old_sources[p] != current[p])
    require(changed == ["ZAC_zzx/experiments_v2/initial_lookahead_runner.py"], "unexpected cross-phase algorithm change")
    provenance = {"sources": sources, "new_phase_audit": new_audit, "linked_pilot7_audit": old_audit,
        "cross_phase_source_comparison": {"shared_files": len(shared), "byte_identical_files": len(shared)-len(changed),
            "changed_files": changed, "old_source_sha256": pilot["repository"]["source_sha256"],
            "new_source_sha256": protocol["repository"]["source_sha256"],
            "scope": "Runner differences add snapshot scope, stage/CPU/RSS observations and logical trace assertions. Initializer, dynamic compiler, scoring, native extension, configuration and architecture unchanged; timing not pooled."},
        "timing_policy": protocol["timing_policy"], "old_pilot_extra_circuits_excluded": protocol["cohort"]["pilot_outside_target"]}
    independent.update(sources=sources, audit=new_audit, execution_phase="parallel_extension_only",
                       job_statuses=dict(Counter(r["status"] for r in new_records.values())),
                       execution_wall_s=raw["execution_wall_s"], workers=3)
    linked.update(sources=sources, job_statuses=dict(Counter(r["status"] for r in merged_records.values())),
                  source_links={"parallel_extension": str(ROOT), "serial_pilot7": str(pilot_root)},
                  old_pilot_extra_circuits_excluded=protocol["cohort"]["pilot_outside_target"])
    for name, report, title in (("new32_analysis", independent, "新增32电路：独立扩展"),
                                ("linked39_analysis", linked, "预定39电路：旧7与新增32来源链接汇总")):
        write_json(ROOT / f"{name}.json", report)
        with (ROOT / f"{name}.md").open("x", encoding="utf-8") as stream:
            stream.write(markdown(report, title))
    provenance["export_files"] = {name: file_hash(ROOT / name) for name in (
        "new32_analysis.json", "new32_analysis.md", "linked39_analysis.json", "linked39_analysis.md")}
    write_json(ROOT / "postrun_analysis_provenance.json", provenance)
    print(json.dumps({"new32": independent["overall"], "linked39": linked["overall"],
                      "new_statuses": independent["job_statuses"], "linked_statuses": linked["job_statuses"]}, indent=2))


if __name__ == "__main__":
    main()
