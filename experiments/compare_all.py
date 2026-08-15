"""统一对比表：两篇论文的结果 vs 用户的 GA / FABLE（同一架构、同一指标口径）。

参与方（全部用本机复现数据，保证同尺子测量）：
    ZAC          《Reuse-Aware Compilation...》(HPCA'25) 的结果
                 = M1 复现的 ZAC 原版（与论文 CSV 16/18 逐位一致）
    qmap-agnostic ICCAD'25《Routing-Aware Placement》的 ZAC C++ 复刻版
    qmap-astar   ICCAD'25 的 A* 路由感知放置（论文主方法）
                 （两者 = M2 复现，qmap 3.2.0，与 Table I 12/15 步数精确一致）
    GA v1a       用户的遗传放置器（GA/ 文件夹，ICCAD 评估+朴素变异）
    FABLE        用户的 Fable 想法放置器（FABLE/ 文件夹，最大链+冲突边+结构化算子）

指标口径（M2/M4 已对齐）：
    步数   = 重排指令条数（ZAC 系=rearrangeJob 数；qmap 系=naviz 复合 job 数）
    重排时长 = Σ 各重排 job 的起止跨度，运动模型同源（√(d/0.00275)+15μs/装卸）
    总时长/保真度 = ZAC 模拟器（仅 ZAC 系有——qmap 论文不报这两项）

输出：results/compare_all.md + .csv；stdout 打印 markdown。
跑法：ZAC/.venv/bin/python experiments/compare_all.py
"""
from __future__ import annotations

import csv
import json
from functools import reduce
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments"


def geomean(xs):
    return reduce(lambda a, b: a * b, xs) ** (1 / len(xs))


def zair_metrics(code_dir: Path) -> dict[str, dict]:
    """ZAIR 代码 JSON → {电路: {steps, rearr_us}}。"""
    out = {}
    for fp in sorted(Path(code_dir).glob("*_code.json")):
        name = fp.name.split("_code.json")[0].replace("_transpiled", "")
        d = json.load(open(fp))
        jobs = [i for i in d["instructions"] if i["type"] == "rearrangeJob"]
        out[name] = {
            "steps": len(jobs),
            "rearr_us": sum(i["end_time"] - i["begin_time"] for i in jobs),
        }
    return out


def fid_metrics(fid_dir: Path) -> dict[str, tuple[float, float]]:
    out = {}
    for fp in sorted(Path(fid_dir).glob("*_fidelity.json")):
        j = json.load(open(fp))
        out[fp.name.split("_fidelity")[0].replace("_transpiled", "")] = (
            j["cir_duration"], j["cir_fidelity"])
    return out


# ------------------------------------------------------------------ 加载
zac_m = zair_metrics(ROOT / "ZAC/result/zac/repro_fixed/code")
ga_m = zair_metrics(ROOT / "GA/results/repro_ga/code")
fb_m = zair_metrics(ROOT / "FABLE/results/repro_fable/code")
zac_f = fid_metrics(ROOT / "ZAC/result/zac/repro_fixed/fidelity")
ga_f = fid_metrics(ROOT / "GA/results/repro_ga/fidelity")
fb_f = fid_metrics(ROOT / "FABLE/results/repro_fable/fidelity")

qmap = {}          # (name, config) -> (steps, rearr_us)
with open(EXP / "results/qmap_qasmbench.csv") as f:
    for row in csv.DictReader(f):
        c = row["circuit"].replace("swaptest", "swap_test")
        name = f"{c}_n{row['qubits']}"
        qmap[(name, row["config"])] = (int(row["steps"]), float(row["rearr_ms"]))

# 共同电路：qmap 15 行 ∩ ZAC 18
circuits = sorted({n for n, _ in qmap} & set(zac_m) & set(ga_m) & set(fb_m))

# ------------------------------------------------------------------ 表
md = [
    "# 两篇论文 vs GA / FABLE 统一对比表",
    "",
    "同一架构（ZAC 1AOD spec）、同一批电路（QASMBench 15 个共有电路）、同一指标口径。",
    "步数=重排指令数；重排时长=Σ job 跨度（μs）；总时长/保真度仅 ZAC 系（qmap 论文不报）。",
    "",
    "| 电路 | ZAC步/重排μs | GA步/重排μs | FABLE步/重排μs | agnostic步/重排μs | astar步/重排μs | 总时长μs Z/G/F | 保真度 Z/G/F |",
    "|---|---|---|---|---|---|---|---|",
]
csv_rows = []
agg = {k: [] for k in ("r_steps_ga", "r_steps_fb", "r_steps_as",
                       "r_rearr_ga", "r_rearr_fb", "r_rearr_as")}
for c in circuits:
    z, g, f_ = zac_m[c], ga_m[c], fb_m[c]
    ag = qmap[(c, "agnostic")]
    as_ = qmap[(c, "astar")]
    zf, gf, ff = zac_f[c], ga_f[c], fb_f[c]
    md.append(
        f"| {c} | {z['steps']}/{z['rearr_us']:.0f} "
        f"| {g['steps']}/{g['rearr_us']:.0f} ({g['steps']/z['steps']:.2f}) "
        f"| {f_['steps']}/{f_['rearr_us']:.0f} ({f_['steps']/z['steps']:.2f}) "
        f"| {ag[0]}/{ag[1]:.0f} ({ag[0]/z['steps']:.2f}) "
        f"| {as_[0]}/{as_[1]:.0f} ({as_[0]/z['steps']:.2f}) "
        f"| {zf[0]:.0f}/{gf[0]:.0f}/{ff[0]:.0f} "
        f"| {zf[1]:.3f}/{gf[1]:.3f}/{ff[1]:.3f} |")
    agg["r_steps_ga"].append(g["steps"] / z["steps"])
    agg["r_steps_fb"].append(f_["steps"] / z["steps"])
    agg["r_steps_as"].append(as_[0] / z["steps"])
    agg["r_rearr_ga"].append(g["rearr_us"] / z["rearr_us"])
    agg["r_rearr_fb"].append(f_["rearr_us"] / z["rearr_us"])
    agg["r_rearr_as"].append(as_[1] / z["rearr_us"])
    csv_rows.append({
        "circuit": c,
        "zac_steps": z["steps"], "ga_steps": g["steps"], "fable_steps": f_["steps"],
        "agnostic_steps": ag[0], "astar_steps": as_[0],
        "zac_rearr_us": round(z["rearr_us"], 1), "ga_rearr_us": round(g["rearr_us"], 1),
        "fable_rearr_us": round(f_["rearr_us"], 1),
        "agnostic_rearr_us": round(ag[1], 1), "astar_rearr_us": round(as_[1], 1),
        "zac_total_us": round(zf[0], 1), "ga_total_us": round(gf[0], 1),
        "fable_total_us": round(ff[0], 1),
        "zac_fid": round(zf[1], 4), "ga_fid": round(gf[1], 4), "fable_fid": round(ff[1], 4),
    })

md += ["",
       f"**步数 geomean 比 vs ZAC（n={len(circuits)}）：GA {geomean(agg['r_steps_ga']):.3f}｜"
       f"FABLE {geomean(agg['r_steps_fb']):.3f}｜astar {geomean(agg['r_steps_as']):.3f}**",
       f"**重排时长 geomean 比 vs ZAC：GA {geomean(agg['r_rearr_ga']):.3f}｜"
       f"FABLE {geomean(agg['r_rearr_fb']):.3f}｜astar {geomean(agg['r_rearr_as']):.3f}**",
       f"**总时长 geomean 比 vs ZAC（ZAC 系 18 电路口径）：GA 0.948｜FABLE 0.942**"]

out_md = EXP / "results/compare_all.md"
out_md.write_text("\n".join(md) + "\n")
with open(EXP / "results/compare_all.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(csv_rows[0]))
    w.writeheader()
    w.writerows(csv_rows)
    w.writerow({"circuit": "GEOMEAN",
                **{f"{k}_steps": round(geomean([r[f"{k}_steps"] / r["zac_steps"]
                                                for r in csv_rows]), 3)
                   for k in ("ga", "fable", "agnostic", "astar")}})

print("\n".join(md))
print(f"\n[saved] {out_md}")
print(f"[saved] {out_md.with_suffix('.csv')}")
