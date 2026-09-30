#!/usr/bin/env python3
"""Derive Figure 7 from the sealed five-horizon quality study, without experiments.

Generation writes only figures/data/horizon_trends.dat and its provenance JSON.
--check recomputes source hashes, three-seed medians and accepted aggregate macros.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

from paper_paths import PAPER_ROOT, artifact_path, tool_path

ROOT = PAPER_ROOT
HORIZONS = (0, 1, 2, 4, 8)
WORDS = ("Zero", "One", "Two", "Four", "Eight")
SOURCE = "paper_horizon_extension_values.json"
MACROS = "horizon_extension_values.tex"
PROTOCOL_SHA256 = "d899e7995eb252e51e19543bae98321979f7ff163f7858c10633ffaa017c643e"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def same(a, b):
    return (isinstance(a, (int, float)) and isinstance(b, (int, float))
            and math.isfinite(a) and math.isfinite(b)
            and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-14))


def derive(source, macro_text):
    require(source["protocol_sha256"] == PROTOCOL_SHA256, "Unexpected study protocol")
    require(source["horizons"] == list(HORIZONS) and source["seeds"] == [0, 1, 2],
            "Expected five horizons and three seeds")
    require(source["separate_from_frozen_main_results"] is True, "Independent-study scope missing")
    cohort = [row for row in source["per_circuit"] if row["complete"]]
    keys = [(row["dataset"], row["circuit"]) for row in cohort]
    require(len(keys) == len(set(keys)) == 10, "Expected ten distinct complete circuits")
    require(keys == [(r["dataset"], r["circuit"]) for r in source["primary_cohort"]],
            "Primary cohort and complete rows differ")
    require(source["values"]["HorizonExtCompleteN"] == 10, "Aggregate cohort count differs")
    jobs = defaultdict(list)
    for row in source["job_outcomes"]:
        jobs[(row["dataset"], row["circuit"], row["horizon"])].append(row)
    circuits = []
    for index, row in enumerate(cohort):
        require(set(row["horizons"]) == {str(h) for h in HORIZONS}, "Missing horizon")
        medians = {}
        for h in HORIZONS:
            runs = jobs[(row["dataset"], row["circuit"], h)]
            require(len(runs) == 3 and {r["seed"] for r in runs} == {0, 1, 2},
                    "Each complete circuit/horizon needs exactly three seeds")
            require(all(r["status"] == "success" and r["fidelity_ood"] is False
                        and not r["exclusions"] for r in runs), "Invalid run in common cohort")
            medians[h] = {}
            for metric, key in (("fidelity", "median_fidelity"), ("move_batches", "median_move_batches")):
                require(all(isinstance(r[metric], (int, float)) and math.isfinite(r[metric])
                            and r[metric] > 0 for r in runs), "Nonpositive or nonfinite quality metric")
                median = statistics.median(r[metric] for r in runs)
                require(same(median, row["horizons"][str(h)][key]), "Per-circuit median differs")
                medians[h][metric] = median
        series = {}
        for h in HORIZONS:
            series[h] = {}
            for metric, key in (("fidelity", "fidelity_ratio_vs_h8"), ("move_batches", "move_batches_ratio_vs_h8")):
                ratio = medians[h][metric] / medians[8][metric]
                require(same(ratio, row["horizons"][str(h)][key]), "Per-circuit H8 ratio differs")
                series[h][metric] = ratio
        circuits.append({"index": index, "dataset": row["dataset"], "circuit": row["circuit"],
                         "ratios": series})
    macros = dict(re.findall(r"\\newcommand\{\\(\w+)\}\{([^}]+)\}", macro_text))
    rows, checks = [], []
    for h, word in zip(HORIZONS, WORDS):
        row = {"horizon": h}
        for metric, prefix, suffix in (("fidelity", "f", "FidelityRatio"),
                                       ("move_batches", "b", "BatchRatio")):
            values = [c["ratios"][h][metric] for c in circuits]
            row.update({f"{prefix}{i}": value for i, value in enumerate(values)})
            mean = math.exp(math.fsum(map(math.log, values)) / len(values))
            row[f"{prefix}_mean"] = mean
            name = f"HorizonExt{word}{suffix}"
            require(same(mean, source["values"][name]), "Geometric mean differs from accepted export")
            require(macros.get(name) == f"{mean:.4f}", "Displayed aggregate macro differs")
            checks.append({"macro": name, "recomputed": mean,
                           "accepted": source["values"][name], "displayed": macros[name]})
        rows.append(row)
    return rows, circuits, checks


def generate(root=ROOT):
    raw = {name: artifact_path(root, name).read_bytes() for name in (SOURCE, MACROS)}
    rows, circuits, checks = derive(json.loads(raw[SOURCE]), raw[MACROS].decode())
    columns = ["horizon", *[f"f{i}" for i in range(10)], "f_mean",
               *[f"b{i}" for i in range(10)], "b_mean"]
    data = " ".join(columns) + "\n"
    for row in rows:
        data += " ".join(str(row[c]) if c == "horizon" else format(row[c], ".17g") for c in columns) + "\n"
    provenance = {"schema": "five-horizon-circuit-trends-v1", "protocol_sha256": PROTOCOL_SHA256,
                  "sources": [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                              for name, content in raw.items()],
                  "generator": {"path": "writing/" + Path(__file__).name,
                                "sha256": hashlib.sha256(tool_path(root, __file__).read_bytes()).hexdigest()},
                  "data_sha256": hashlib.sha256(data.encode()).hexdigest(),
                  "columns": columns, "circuits": circuits, "aggregate_checks": checks,
                  "analysis": "At each H, take each circuit's three-seed metric median and divide by its H8 median; geometric means weight the ten circuits equally.",
                  "scope": "Independent fixed-starting-layout horizon study, not the physical-GA main quality cohort; no timing inference.",
                  "window_boundary": "H is a configured upper bound. Equal H4/H8 results do not establish saturation; available layers and scale budgets can limit both configurations."}
    return {artifact_path(root, "figures/data/horizon_trends.dat"): data,
            artifact_path(root, "figures/data/horizon_trends.provenance.json"): json.dumps(provenance, sort_keys=True, indent=2,
                ensure_ascii=False, allow_nan=False) + "\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    for path, content in generate(args.paper_root.resolve()).items():
        if args.check:
            require(path.is_file() and path.read_text() == content, "Stale horizon figure input: " + str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print("Horizon trends: 10 circuits, 5 horizons, 3 seeds; " + ("verified" if args.check else "generated"))


if __name__ == "__main__":
    main()
