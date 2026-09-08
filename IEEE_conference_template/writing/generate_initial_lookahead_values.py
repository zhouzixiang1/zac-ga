#!/usr/bin/env python3
"""Generate separate manuscript values from the sealed initialization study.

This reads existing evidence only. It never invokes a compiler, reruns an
experiment, or modifies the frozen main-result values. Both input hashes are
pinned so that a different study cannot silently replace this bounded sample.
Use ``--check`` for a read-only freshness check of the two derived value files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics


STUDY_RELATIVE = Path("ZAC_zzx/results/initial_lookahead_v1/nine-family-h0-h2-sa-k4-rho07-b32-seed012-v1")
PROTOCOL_SHA256 = "3f99d0207dcbf87ea44b12810bfeac0f5272f544013d72dffa9802a7342f8f69"
SUMMARY_SHA256 = "c4c020e9fb0bf5b4cd8af2027e761df45db28eb1edad7aac7121009470b12477"
ARMS = ("sa_reference", "h0", "h2")
COUNT_FIELDS = ("InitStudyN", "InitCompleteN", "InitTimeouts")
PERCENT_FIELDS = ("InitFidelityGain", "InitBatchReduction", "InitVsSAGain")
DERIVED_FILENAMES = ("initial_lookahead_values.tex", "paper_initial_lookahead_values.json")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_documents(protocol, summary):
    """Verify scope and recompute the circuit-level paired estimands."""
    if protocol["protocol_id"] != "initial-physical-prefix-nine-family-seed3-v1":
        raise ValueError("unexpected initialization study protocol")
    expected = [row["circuit"] for row in protocol["circuits"]]
    rows = summary["per_circuit"]
    if len(expected) != 9 or len(set(expected)) != 9 or [row["circuit"] for row in rows] != expected:
        raise ValueError("all nine prespecified circuits, in protocol order, must be retained")
    if (protocol["seeds"] != [0, 1, 2] or protocol["horizons"] != [0, 2]
            or protocol["candidates"] != 4 or protocol["rho"] != .7
            or protocol["rollout_evaluations"] != 32 or protocol["dynamic_horizon"] != 8
            or protocol["timeout_per_paired_job_s"] != 600 or len(protocol["job_order"]) != 27):
        raise ValueError("initialization or final dynamic comparison controls changed")
    if (summary["source_stable"] is not True or summary["formal_paper_result"] is not False
            or summary["protocol_sha256"] != PROTOCOL_SHA256):
        raise ValueError("study lacks matching stable-source provenance")
    if summary["job_statuses"] != {"failed": 0, "missing": 0, "success": 26, "timeout": 1}:
        raise ValueError("all 27 outcomes, including the one timeout, must be retained")
    if (summary["physical_checks"] != {"completed_arm_count": 78, "fidelity_ood_arm_count": 0,
                                       "physically_valid_arm_count": 78}
            or summary["input_ledger_mismatch_jobs"]):
        raise ValueError("physical or input-ledger verification differs from the sealed study")
    complete = [row for row in rows if row["complete"]]
    if len(complete) != 8 or summary["overall"]["complete_circuits"] != 8:
        raise ValueError("primary mean requires exactly the eight complete circuits")
    for row in rows:
        expected_runs = 3 if row["complete"] else 2
        if any(row["arms"][arm]["valid_runs"] != expected_runs for arm in ARMS):
            raise ValueError("within-circuit seed completeness changed")
        if not row["complete"] and (row["circuit"] != "ising_n42"
                                     or row["job_statuses"] != ["success", "timeout", "success"]):
            raise ValueError("the incomplete Ising circuit must remain explicit")
    def mean(arm, key):
        return statistics.mean(row["arms"][arm][key] for row in complete)
    values = {
        "InitStudyN": len(rows), "InitCompleteN": len(complete),
        "InitTimeouts": summary["job_statuses"]["timeout"],
        "InitFidelityGain": math.expm1(statistics.mean(
            row["arms"]["h2"]["mean_log_fidelity"] - row["arms"]["h0"]["mean_log_fidelity"]
            for row in complete)) * 100,
        "InitBatchReduction": 100 * (1 - mean("h2", "mean_move_batches") / mean("h0", "mean_move_batches")),
        "InitVsSAGain": math.expm1(statistics.mean(
            row["arms"]["h2"]["mean_log_fidelity"] - row["arms"]["sa_reference"]["mean_log_fidelity"]
            for row in complete)) * 100,
    }
    comparisons = summary["overall"]["comparisons"]
    references = {
        "InitFidelityGain": comparisons["h2_vs_h0"]["fidelity_relative_gain_percent"],
        "InitBatchReduction": comparisons["h2_vs_h0"]["move_batches_reduction_percent"],
        "InitVsSAGain": comparisons["h2_vs_sa_reference"]["fidelity_relative_gain_percent"],
    }
    for key, expected_value in references.items():
        if not math.isfinite(values[key]) or not math.isclose(values[key], expected_value, rel_tol=1e-10, abs_tol=1e-12):
            raise ValueError(f"recomputed circuit-level metric disagrees with summary: {key}")
    return values


def load_verified(project_root):
    study = Path(project_root) / STUDY_RELATIVE
    for filename, expected in (("protocol.json", PROTOCOL_SHA256), ("summary.json", SUMMARY_SHA256)):
        if sha256(study / filename) != expected:
            raise ValueError(f"sealed initialization input hash mismatch: {filename}")
    protocol = json.loads((study / "protocol.json").read_text())
    summary = json.loads((study / "summary.json").read_text())
    return study, protocol, summary, validate_documents(protocol, summary)


def render_tex(values, generator_sha256=None):
    generator_sha256 = generator_sha256 or sha256(__file__)
    lines = ["% Generated by writing/generate_initial_lookahead_values.py; do not edit.",
             "% Independent initialization study; not the frozen main-result table.",
             f"% Protocol SHA-256: {PROTOCOL_SHA256}", f"% Summary SHA-256: {SUMMARY_SHA256}",
             f"% Generator SHA-256: {generator_sha256}",
             "% F: geometric ratio of circuit-level seed-mean log fidelity; batches: ratio of arithmetic means.",
             "% Primary cohort: 8 complete circuits; all 9 remain in the study, including one seed timeout."]
    for key, value in values.items():
        text = str(value) if key in COUNT_FIELDS else f"{value:.5f}\\%"
        lines.append(f"\\newcommand{{\\{key}}}{{{text}}}")
    lines.append("% Two-decimal display macros for manuscript prose; full precision remains in JSON.")
    for key in PERCENT_FIELDS:
        lines.append(f"\\newcommand{{\\{key}Rounded}}{{{values[key]:.2f}\\%}}")
    return "\n".join(lines) + "\n"


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def expected_exports(project_root):
    """Build deterministic export bytes in memory; this function never writes."""
    study, protocol, summary, values = load_verified(Path(project_root).resolve())
    generator_sha256 = sha256(__file__)
    tex = render_tex(values, generator_sha256)
    output = {"study": str(study), "protocol_sha256": PROTOCOL_SHA256,
              "summary_sha256": SUMMARY_SHA256, "generator_sha256": generator_sha256,
              "run_source_sha256": protocol["repository"]["source_sha256"],
              "separate_from_frozen_main_results": True,
              "analysis_unit": "circuit; seeds 0, 1, 2 averaged within circuit",
              "primary_cohort": [row["circuit"] for row in summary["per_circuit"] if row["complete"]],
              "all_study_circuits": [row["circuit"] for row in summary["per_circuit"]],
              "incomplete_circuits": [row["circuit"] for row in summary["per_circuit"] if not row["complete"]],
              "timeout_policy": protocol["timeout_policy"],
              "metric_units": {key: "percent" for key in PERCENT_FIELDS},
              "values": values, "rounded_percent_values": {key: f"{value:.5f}"
                 for key, value in values.items() if key in PERCENT_FIELDS},
              "display_percent_values": {key + "Rounded": f"{values[key]:.2f}" for key in PERCENT_FIELDS}}
    exports = {DERIVED_FILENAMES[0]: tex, DERIVED_FILENAMES[1]: json_text(output)}
    historical = study / "analysis_provenance.json"
    provenance = {"export_schema": "initial-lookahead-paper-export-v2",
                  "generator_sha256": generator_sha256,
                  "generator_test_sha256": sha256(Path(__file__).with_name("test_generate_initial_lookahead_values.py")),
                  "protocol_sha256": PROTOCOL_SHA256, "summary_sha256": SUMMARY_SHA256,
                  "run_source_sha256": protocol["repository"]["source_sha256"],
                  "historical_analysis_provenance": str(historical),
                  "historical_analysis_provenance_sha256": sha256(historical),
                  "historical_analysis_provenance_modified": False,
                  "note": "This is the current paper-export version, not a replacement for the historical post-run audit.",
                  "display_percent_decimal_places": 2, "audit_percent_decimal_places": 5,
                  "derived_file_sha256": {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                                          for name, text in exports.items()}}
    return exports, provenance, values


def check_exports(paper_root, exports):
    """Compare without creating directories, repairing files or changing mtimes."""
    stale = []
    for name, expected in exports.items():
        path = Path(paper_root) / name
        if not path.is_file():
            stale.append({"file": name, "reason": "missing"})
        elif path.read_bytes() != expected.encode("utf-8"):
            stale.append({"file": name, "reason": "stale"})
    return stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--paper-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="read-only freshness check; return nonzero without writing when stale")
    args = parser.parse_args(argv)
    try:
        exports, provenance, values = expected_exports(args.project_root)
        if args.check:
            stale = check_exports(args.paper_root, exports)
            print(json.dumps({"status": "failed" if stale else "passed", "read_only": True,
                              "stale_files": stale, "paper_root": str(args.paper_root)}, indent=2))
            return 1 if stale else 0
        args.paper_root.mkdir(parents=True, exist_ok=True)
        for name, text in exports.items():
            (args.paper_root / name).write_text(text, encoding="utf-8")
        (args.paper_root / "paper_export_provenance.json").write_text(json_text(provenance), encoding="utf-8")
        print(json.dumps({"status": "generated", "values": values, "paper_root": str(args.paper_root)}, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "failed", "read_only": args.check,
                          "error": f"{type(error).__name__}: {error}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
