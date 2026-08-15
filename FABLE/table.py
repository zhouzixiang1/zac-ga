"""生成正式结果对比表（Markdown + CSV）：四方 × 18 电路。

    ZAC 真值      HPCA'25 AE 数据（判卷标准）
    FABLE(本实验) Fable 想法 + ZAC 底盘（results/repro_fable, w_conf=0.25）
    GA v1a        同底盘同预算 + ICCAD 评估（GA/results/repro_ga，只读）
    Fable 原生    用户笔记 compiler_search（paper_truth/fable_notes.csv）

输出：results/comparison_table.md 与 .csv；stdout 打印 markdown。
跑法：ZAC/.venv/bin/python FABLE/table.py
"""
from __future__ import annotations

import csv
import json
from functools import reduce
from pathlib import Path

FABLE_ROOT = Path(__file__).resolve().parent
ZAC_ROOT = FABLE_ROOT.parent


def load_fid(d: Path) -> dict[str, tuple[float, float]]:
    out = {}
    for fp in sorted(d.glob("*_fidelity.json")):
        j = json.load(open(fp))
        out[fp.name.split("_fidelity")[0].replace("_transpiled", "")] = (
            j["cir_duration"], j["cir_fidelity"])
    return out


def load_steps(d: Path) -> dict[str, int]:
    """从 ZAIR 代码 JSON 数重排批次（rearrangeJob 条数）。"""
    out = {}
    for fp in sorted(d.glob("*_code.json")):
        j = json.load(open(fp))
        out[fp.name.split("_code.json")[0].replace("_transpiled", "")] = sum(
            1 for i in j["instructions"] if i["type"] == "rearrangeJob")
    return out


def geomean(xs):
    return reduce(lambda a, b: a * b, xs) ** (1 / len(xs))


truth = {}
with open(ZAC_ROOT / "experiments/paper_truth/zac.csv") as f:
    for row in csv.DictReader(f):
        truth[row["circuit"]] = (float(row["duration_us"]), float(row["fidelity"]))
notes = {}
with open(FABLE_ROOT / "paper_truth/fable_notes.csv") as f:
    for row in csv.DictReader(f):
        notes[row["circuit"]] = row
ours = load_fid(FABLE_ROOT / "results/repro_fable/fidelity")
ga = load_fid(ZAC_ROOT / "GA/results/repro_ga/fidelity")
steps_zac = load_steps(ZAC_ROOT / "ZAC/result/zac/repro_fixed/code")
steps_ours = load_steps(FABLE_ROOT / "results/repro_fable/code")
steps_ga = load_steps(ZAC_ROOT / "GA/results/repro_ga/code")

circuits = sorted(c for c in truth if c in ours)
rows, r_o, r_g, r_n = [], [], [], []
for c in circuits:
    z_us, z_fid = truth[c]
    o_us, o_fid = ours[c]
    g_us, g_fid = ga[c]
    n_us, n_fid = float(notes[c]["fable_search_us"]), float(notes[c]["fable_search_fid"])
    q = notes[c]["q"]
    ro, rg, rn = o_us / z_us, g_us / z_us, n_us / z_us
    r_o.append(ro); r_g.append(rg); r_n.append(rn)
    rows.append({
        "circuit": c, "q": q,
        "zac_us": z_us, "fable_us": o_us, "ga_us": g_us, "notes_us": n_us,
        "ratio_fable": round(ro, 3), "ratio_ga": round(rg, 3), "ratio_notes": round(rn, 3),
        "steps_zac": steps_zac.get(c, ""), "steps_fable": steps_ours.get(c, ""),
        "steps_ga": steps_ga.get(c, ""),
        "fid_zac": round(z_fid, 4), "fid_fable": round(o_fid, 4),
        "fid_ga": round(g_fid, 4), "fid_notes": round(n_fid, 4),
    })

n = len(rows)
summary = {
    "geomean_ratio_fable": round(geomean(r_o), 3),
    "geomean_ratio_ga": round(geomean(r_g), 3),
    "geomean_ratio_notes": round(geomean(r_n), 3),
    "mean_fid_fable": round(sum(r["fid_fable"] for r in rows) / n, 4),
    "mean_fid_ga": round(sum(r["fid_ga"] for r in rows) / n, 4),
    "mean_fid_notes": round(sum(r["fid_notes"] for r in rows) / n, 4),
    "mean_fid_zac": round(sum(r["fid_zac"] for r in rows) / n, 4),
}

# ---- markdown ----
md = ["# FABLE 结果对比表（18 电路，qasm_sa_1000_reuse 同口径）", "",
      "| 电路 | q | ZAC μs(批次) | FABLE μs(批次) | **比** | GA v1a μs | 比 | Fable原生 μs | 比 | 保真度 ZAC/FABLE/GA/原生 |",
      "|---|--:|---|---|--:|---|--:|---|--:|---|"]
for r in rows:
    md.append(
        f"| {r['circuit']} | {r['q']} "
        f"| {r['zac_us']:.0f} ({r['steps_zac']}) "
        f"| {r['fable_us']:.0f} ({r['steps_fable']}) "
        f"| **{r['ratio_fable']:.2f}** "
        f"| {r['ga_us']:.0f} | {r['ratio_ga']:.2f} "
        f"| {r['notes_us']:.0f} | {r['ratio_notes']:.2f} "
        f"| {r['fid_zac']:.3f}/{r['fid_fable']:.3f}/{r['fid_ga']:.3f}/{r['fid_notes']:.3f} |")
md += ["",
       f"**geomean 时长比 vs ZAC：FABLE {summary['geomean_ratio_fable']:.3f}｜GA v1a {summary['geomean_ratio_ga']:.3f}｜Fable原生 {summary['geomean_ratio_notes']:.3f}**",
       f"**平均保真度：ZAC {summary['mean_fid_zac']:.4f}｜FABLE {summary['mean_fid_fable']:.4f}｜GA v1a {summary['mean_fid_ga']:.4f}｜Fable原生 {summary['mean_fid_notes']:.4f}**"]

out_md = FABLE_ROOT / "results/comparison_table.md"
out_md.write_text("\n".join(md) + "\n")

with open(FABLE_ROOT / "results/comparison_table.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    w.writerow({"circuit": "GEOMEAN/MEAN",
                "ratio_fable": summary["geomean_ratio_fable"],
                "ratio_ga": summary["geomean_ratio_ga"],
                "ratio_notes": summary["geomean_ratio_notes"],
                "fid_zac": summary["mean_fid_zac"],
                "fid_fable": summary["mean_fid_fable"],
                "fid_ga": summary["mean_fid_ga"],
                "fid_notes": summary["mean_fid_notes"]})

print("\n".join(md))
print(f"\n[saved] {out_md}")
print(f"[saved] {out_md.with_suffix('.csv')}")
