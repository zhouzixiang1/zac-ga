"""Unified summary: ZAC vs GA (ZAIR metrics) vs qmap Table I reproduction.

Usage: python3 experiments/summarize.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
from parse_zair import load_run  # noqa: E402


def main() -> None:
    zac = load_run(ROOT / "ZAC" / "result" / "zac" / "repro_fixed")
    ga_dir = ROOT / "GA" / "results" / "repro_ga"
    ga = load_run(ga_dir) if (ga_dir / "code").exists() else {}
    qmap_rows = list(csv.DictReader(open(ROOT / "experiments" / "results" / "qmap_qasmbench.csv"))) \
        if (ROOT / "experiments" / "results" / "qmap_qasmbench.csv").exists() else []
    qmap = {(r["circuit"], int(r["qubits"])): r for r in qmap_rows if r.get("status") == "ok"}

    truth = {(r["circuit"], int(r["qubits"])): r
             for r in csv.DictReader(open(ROOT / "experiments" / "paper_truth" / "qmap_table1.csv"))
             if r["qubits"] and r["source"] == "qasmbench"}

    print("=" * 100)
    print(f"{'circuit':16s} | {'ZAC steps':>9s} {'dur(us)':>9s} {'fid':>7s} | "
          f"{'GA steps':>8s} {'dur(us)':>9s} {'fid':>7s} | {'astar steps':>11s} {'agnostic':>8s}")
    print("-" * 100)
    for name in sorted(zac, key=lambda c: (c.split("_n")[0], int(c.split("_n")[1].split("_")[0]))):
        z = zac[name]
        g = ga.get(name, {})
        base = name.replace("_transpiled", "")
        n = int(base.split("_n")[1])
        bench = base.split("_n")[0]
        t = truth.get((bench, n))
        astar_steps = f"{'–':>11s}"
        agn_steps = f"{'–':>8s}"
        if t:
            agn_steps = f"{t['ra_steps']:>8s}"
            astar_steps = f"{t['rw_steps']:>11s}"
        gs = f"{g.get('steps', '–'):>8}"
        gd = f"{g.get('duration_us', 0):>9.1f}" if g else f"{'–':>9}"
        gf = f"{g.get('fidelity', 0):>7.4f}" if g else f"{'–':>7}"
        print(f"{base:16s} | {z['steps']:>9d} {z['duration_us']:>9.1f} {z['fidelity']:>7.4f} | "
              f"{gs} {gd} {gf} | {astar_steps} {agn_steps}")

    # aggregate
    if ga:
        import math
        ratios = []
        for name, z in zac.items():
            if name in ga and ga[name].get("duration_us"):
                ratios.append(ga[name]["duration_us"] / z["duration_us"])
        if ratios:
            geo = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
            print("-" * 100)
            print(f"GA/ZAC duration geomean: {geo:.3f}  (n={len(ratios)})")
            print(f"GA mean fidelity: {sum(g['fidelity'] for g in ga.values() if g.get('fidelity'))/len([g for g in ga.values() if g.get('fidelity')]):.4f}"
                  f"  ZAC mean fidelity: {sum(z['fidelity'] for z in zac.values())/len(zac):.4f}")


if __name__ == "__main__":
    main()
