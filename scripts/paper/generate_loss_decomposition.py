#!/usr/bin/env python3
"""Derive an additive loss decomposition from the accepted GA publication export.

This does not run experiments or alter their exports. Without --check, only the
two dedicated figures/data/physical_ga_loss_decomposition outputs are written.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import statistics

from paper_paths import PAPER_ROOT, artifact_path, tool_path

EXPORT_RELATIVE = Path("ZAC_zzx/results/physical_ga_main_v1/paper_exports")
OUTPUT_STEM = "physical_ga_loss_decomposition"
DATASETS = ("zac18", "qmap154")
BASELINES = ("M1", "M2")
FACTORS = ("log_atom_transfer", "log_idle_excitation", "log_coherence_linear")
TOLERANCE = 1e-10


def finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Decomposition requires finite logarithmic metrics")
    return result


def metrics(value):
    return json.loads(value) if isinstance(value, str) else value


def is_true(value):
    return value is True or value == "True"


def derive(units, runs, metadata):
    """Select the actual median-logF run, then decompose its paired differences."""
    grouped = defaultdict(list)
    for run in runs:
        grouped[(run["dataset"], run["canonical_sha256"])].append(run)
    paired = defaultdict(list)
    selected, seen, circuit_errors = [], set(), []
    for unit in sorted(units, key=lambda row: (DATASETS.index(row["dataset"]), row["canonical_sha256"])):
        key = (unit["dataset"], unit["canonical_sha256"])
        if key in seen:
            raise ValueError("Duplicate canonical analysis unit: " + str(key))
        seen.add(key)
        candidates = grouped[key]
        if len(candidates) != 3 or {int(row["seed"]) for row in candidates} != {0, 1, 2}:
            raise ValueError("Each analysis unit requires exactly seeds 0, 1, 2: " + str(key))
        if any(row["status"] != "success" or not is_true(row["fidelity_valid"])
               or is_true(row["fidelity_ood"]) for row in candidates):
            raise ValueError("The fixed common cohort contains an invalid run: " + str(key))
        middle = statistics.median(finite(metrics(row["metrics"])["log_fidelity"])
                                   for row in candidates)
        chosen = min((row for row in candidates
                      if finite(metrics(row["metrics"])["log_fidelity"]) == middle),
                     key=lambda row: int(row["seed"]))
        ga = metrics(chosen["metrics"])
        target = metrics(unit["Default"])
        match_error = abs(middle - finite(target["log_fidelity"]))
        if match_error > TOLERANCE:
            raise ValueError("Median logF does not match the main-table analysis unit: " + str(key))
        members = json.loads(unit["circuits"]) if isinstance(unit["circuits"], str) else unit["circuits"]
        if int(unit["member_N"]) != len(members) or not members:
            raise ValueError("Inconsistent canonical alias membership: " + str(key))
        selected.append({"dataset": key[0], "canonical_sha256": key[1],
                         "seed": int(chosen["seed"]), "circuits": members,
                         "member_N": len(members), "log_fidelity": middle,
                         "analysis_unit_match_error": match_error})
        for baseline in BASELINES:
            reference = metrics(unit[baseline])
            parts = [finite(ga[factor]) - finite(reference[factor]) for factor in FACTORS]
            net = middle - finite(reference["log_fidelity"])
            error = abs(math.fsum(parts) - net)
            if error > TOLERANCE:
                raise ValueError("Run factors do not add to paired logF: " + str((key, baseline)))
            circuit_errors.append(error)
            paired[(key[0], baseline)].append((*parts, net))
    rows, identities = [], []
    for dataset in DATASETS:
        expected_n = int(metadata["datasets"][dataset]["common_canonical_N"])
        for baseline in BASELINES:
            observations = paired[(dataset, baseline)]
            if not observations or len(observations) != expected_n:
                raise ValueError("Analysis cohort count differs from the main table: " + dataset)
            means = [math.fsum(row[index] for row in observations) / expected_n for index in range(4)]
            methods = metadata["datasets"][dataset]["methods"]
            expected_net = (finite(methods["Default"]["log_fidelity_mean"])
                            - finite(methods[baseline]["log_fidelity_mean"]))
            sum_error = abs(math.fsum(means[:3]) - expected_net)
            net_error = abs(means[3] - expected_net)
            if max(sum_error, net_error) > TOLERANCE:
                raise ValueError("Decomposition differs from main-table aggregate: " + str((dataset, baseline)))
            rows.append({"dataset": dataset, "baseline": baseline, "N": expected_n,
                         **dict(zip(("transfer", "excitation", "coherence"), means[:3])),
                         "net": expected_net})
            identities.append({"dataset": dataset, "baseline": baseline,
                               "main_table_log_ratio": expected_net,
                               "component_sum_error": sum_error,
                               "paired_mean_error": net_error})
    return rows, {"selected_runs": selected,
                  "max_analysis_unit_match_error": max(row["analysis_unit_match_error"] for row in selected),
                  "max_per_circuit_additive_error": max(circuit_errors),
                  "aggregate_identity_checks": identities}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def generate(paper_root, project_root):
    source_root = project_root / EXPORT_RELATIVE
    sources, payloads = [], {}
    for name in ("analysis_units.csv", "canonical_runs.csv", "ga_main_values.json"):
        path = source_root / name
        content = path.read_bytes()
        sources.append({"path": path.relative_to(project_root).as_posix(), "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest()})
        payloads[name] = (json.loads(content) if name.endswith(".json")
                          else list(csv.DictReader(io.StringIO(content.decode("utf-8")))))
    rows, identity = derive(payloads["analysis_units.csv"], payloads["canonical_runs.csv"],
                            payloads["ga_main_values.json"])
    text = "dataset baseline N transfer excitation coherence net\n"
    for row in rows:
        text += (f"{row['dataset']} {row['baseline']} {row['N']} "
                 + " ".join(format(row[name], ".17g") for name in ("transfer", "excitation", "coherence", "net"))
                 + "\n")
    script = tool_path(paper_root, __file__)
    provenance = {"schema": "physical-ga-median-run-loss-decomposition-v1",
                  "sources": sources,
                  "generator": {"path": "writing/" + script.name,
                                "sha256": hashlib.sha256(script.read_bytes()).hexdigest()},
                  "selection": "For each accepted canonical unit choose the actual run with median log_fidelity among seeds 0, 1, 2; ties use the smallest seed. All three factors come from that same run.",
                  "baseline": "Use each canonical analysis unit's existing M1/M2 log factors, preserving the accepted alias aggregation.",
                  "aggregation": "Each canonical unit has weight 1. Average the paired log-factor differences; their sum equals the difference of main-table mean log fidelities.",
                  "interpretation": "Positive values are model-estimated loss reductions; negative values are added loss. These are model components, not independent causal ablations.",
                  "tolerance": TOLERANCE,
                  "data_sha256": hashlib.sha256(text.encode()).hexdigest(),
                  "rows": rows, **identity}
    return {artifact_path(paper_root, "figures/data/" + OUTPUT_STEM + ".dat"): text,
            artifact_path(paper_root, "figures/data/" + OUTPUT_STEM + ".provenance.json"): json_text(provenance)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", type=Path, default=PAPER_ROOT)
    parser.add_argument("--project-root", type=Path, default=PAPER_ROOT.parent)
    parser.add_argument("--check", action="store_true", help="Recompute and compare without writing")
    args = parser.parse_args(argv)
    outputs = generate(args.paper_root.resolve(), args.project_root.resolve())
    if args.check:
        mismatches = [str(path) for path, content in outputs.items()
                      if not path.is_file() or path.read_text(encoding="utf-8") != content]
        if mismatches:
            parser.exit(1, "Loss-decomposition outputs differ: " + ", ".join(mismatches) + "\n")
    else:
        for path, content in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print("Loss decomposition: 4 comparisons, 135 canonical median runs; "
          + ("verified" if args.check else "generated"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
