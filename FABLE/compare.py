"""FABLE 对比表：本文件夹结果 vs ZAC 真值 vs GA v1a vs Fable 笔记原生数据。

四方同台（全部只读，不改任何旧实验的数据）：
    ZAC 真值      experiments/paper_truth/zac.csv（HPCA'25 AE 数据，判卷标准）
    FABLE(本实验) FABLE/results/repro_fable/fidelity/*.json（Fable 想法上 ZAC 底盘）
    GA v1a        GA/results/repro_ga/fidelity/*.json（同底盘、同预算、ICCAD 评估）
    Fable 原生    FABLE/paper_truth/fable_notes.csv（用户 Windows 机器上的自建管线，
                  compiler_search 列，geomean 0.938 —— 本实验要追的目标）

跑法：ZAC/.venv/bin/python FABLE/compare.py
"""
from __future__ import annotations

import csv
import json
import sys
from functools import reduce
from pathlib import Path

FABLE_ROOT = Path(__file__).resolve().parent
ZAC_ROOT = FABLE_ROOT.parent


def load_fidelity_dir(d: Path) -> dict[str, tuple[float, float]]:
    """{电路名: (duration_us, fidelity)}；去掉 _transpiled 后缀。"""
    out = {}
    for fp in sorted(d.glob("*_fidelity.json")):
        with open(fp) as f:
            j = json.load(f)
        name = fp.name.split("_fidelity")[0].replace("_transpiled", "")
        out[name] = (j["cir_duration"], j["cir_fidelity"])
    return out


def geomean(xs: list[float]) -> float:
    return reduce(lambda a, b: a * b, xs) ** (1 / len(xs))


def main() -> None:
    truth: dict[str, tuple[float, float]] = {}
    with open(ZAC_ROOT / "experiments/paper_truth/zac.csv") as f:
        for row in csv.DictReader(f):
            truth[row["circuit"]] = (float(row["duration_us"]), float(row["fidelity"]))

    ours = load_fidelity_dir(FABLE_ROOT / "results/repro_fable/fidelity")
    ga = load_fidelity_dir(ZAC_ROOT / "GA/results/repro_ga/fidelity")

    notes: dict[str, tuple[float, float, float, float]] = {}
    with open(FABLE_ROOT / "paper_truth/fable_notes.csv") as f:
        for row in csv.DictReader(f):
            notes[row["circuit"]] = (float(row["zac_us"]), float(row["fable_search_us"]),
                                     float(row["zac_fid"]), float(row["fable_search_fid"]))

    circuits = [c for c in truth if c in ours]
    missing = set(truth) - set(ours)
    if missing:
        print(f"[warn] 本实验缺 {len(missing)} 个电路: {sorted(missing)}\n")

    header = (f"{'circuit':<14}{'ZAC μs':>10}{'FABLEμs':>9}{'ratio':>7}"
              f"{'GAμs':>9}{'ratio':>7}{'笔记μs':>9}{'ratio':>7}"
              f"{'fid本':>7}{'fid真':>7}{'fid笔记':>8}")
    print(header)
    print("-" * len(header))
    r_ours, r_ga, r_notes = [], [], []
    fid_ours, fid_zac, fid_notes = [], [], []
    for c in sorted(circuits):
        z_dur, z_fid = truth[c]
        o_dur, o_fid = ours[c]
        g_dur = ga[c][0] if c in ga else float("nan")
        n_zdur, n_fdur, n_zfid, n_ffid = notes[c]
        row_r = [o_dur / z_dur, g_dur / z_dur, n_fdur / n_zdur]
        r_ours.append(row_r[0]); r_ga.append(row_r[1]); r_notes.append(row_r[2])
        fid_ours.append(o_fid); fid_zac.append(z_fid); fid_notes.append(n_ffid)
        print(f"{c:<14}{z_dur:>10.1f}{o_dur:>9.1f}{row_r[0]:>7.2f}"
              f"{g_dur:>9.1f}{row_r[1]:>7.2f}{n_fdur:>9.1f}{row_r[2]:>7.2f}"
              f"{o_fid:>7.3f}{z_fid:>7.3f}{n_ffid:>8.3f}")

    n = len(circuits)
    if n:
        print("-" * len(header))
        print(f"geomean duration ratio vs ZAC:  FABLE(本实验) {geomean(r_ours):.3f}"
              f"   GA v1a {geomean(r_ga):.3f}   Fable笔记 {geomean(r_notes):.3f}   (n={n})")
        print(f"mean fidelity:                  FABLE(本实验) {sum(fid_ours)/n:.4f}"
              f"   ZAC真值 {sum(fid_zac)/n:.4f}   Fable笔记 {sum(fid_notes)/n:.4f}")


if __name__ == "__main__":
    sys.exit(main())
