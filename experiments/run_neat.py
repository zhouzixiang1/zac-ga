"""Run NEAT on Table 2 codes and compare with paper (DAC'26).

Metric semantics calibrated against the authors' stored results:
- depth  = number of non-empty gate layers in full_schedule
- #move  = number of stage transitions where >=1 ancilla changes position
- dist   = total euclidean distance of all ancilla movements (grid units)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from math import dist
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archive" / "neat" / "evaluation"))

from codes import REGISTRY  # noqa: E402
from loguru import logger  # noqa: E402
from neat.auto_atom_compiler import AutoAtomCompiler  # noqa: E402

TRUTH = ROOT / "experiments" / "paper_truth" / "neat_table2.csv"
RESULTS = ROOT / "experiments" / "results"

# Table 2 code -> registry key
TABLE2 = {
    "C4": "C4Code", "C6": "C6Code", "Steane": "SteaneCode", "Shor": "Shor",
    "3DColor": "color_3d", "Surface7": "SurfaceCode_7", "Surface13": "SurfaceCode_13",
}


def metrics_of(schedule: dict, locations: dict) -> tuple[int, int, float]:
    layers = schedule["full_schedule"]["layers"]
    depth = sum(1 for lay in layers if lay["gates"])
    trajs = locations["x_visit_locations"] + locations["z_visit_locations"]
    n_stages = len(trajs[0]) if trajs else 0
    moves = sum(
        1 for k in range(n_stages - 1)
        if any(t[k] != t[k + 1] for t in trajs)
    )
    total = sum(
        dist(a, b)
        for t in trajs for a, b in zip(t, t[1:]) if a != b
    )
    return depth, moves, total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="C4,C6,Steane,Shor,3DColor")
    ap.add_argument("--lb-timeout", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=7200)
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    truth = {r["code"]: r for r in csv.DictReader(open(TRUTH))}
    rows = []
    for name in args.codes.split(","):
        key = TABLE2[name]
        code = REGISTRY[key]()
        logger.info(f"compiling {name} ({key}) ...")
        m = AutoAtomCompiler(code=code)
        lb = m.compile_smt_visit(timeout=args.lb_timeout).depth
        res = m.compile_map_with_visit(
            timeout=args.timeout, max_attempts=lb + 4, min_attempts=lb,
        )
        if not res:
            print(f"[FAIL] {name}: no solution")
            continue
        schedule, locations = res
        out = RESULTS / f"neat_{name}"
        out.mkdir(exist_ok=True)
        schedule.to_json_file(out / "schedule.json")
        locations.to_json_file(out / "locations.json")
        d, mv, td = metrics_of(json.load(open(out / "schedule.json")),
                               json.load(open(out / "locations.json")))
        t = truth.get(name, {})
        rows.append({"code": name, "depth": d, "paper_depth": t.get("neat_depth"),
                     "moves": mv, "paper_moves": t.get("neat_n_move"),
                     "dist": round(td, 3), "paper_dist": t.get("neat_total_distance")})
        print(f"[OK] {name}: depth={d} (paper {t.get('neat_depth')})  "
              f"moves={mv} (paper {t.get('neat_n_move')})  "
              f"dist={td:.3f} (paper {t.get('neat_total_distance')})", flush=True)
        with open(RESULTS / "neat_runs.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
