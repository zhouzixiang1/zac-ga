"""ZAC_zzx 五方对比表：ZAC 真值 / GA v1a / FABLE / ZAC_zzx-B(penalty) / ZAC_zzx-A(ga)。

全部只读——旧实验的数据一个字节不动。指标：
    duration ratio（对 ZAC 归一，<1 = 更快）、重排批数、平均原子/批、
    fidelity、保真度 geomean；ZAC_zzx 两行额外汇总"χ 预演 vs 实际批数"
    一致率（放置预演账本 vs 路由账本对账）。

跑法：ZAC/.venv/bin/python compare.py [结果目录1 结果目录2]
    默认读 results/repro_penalty 与 results/repro_ga。
"""
from __future__ import annotations

import csv
import json
import sys
from functools import reduce
from pathlib import Path

ZNEW_ROOT = Path(__file__).resolve().parent
ZAC_ROOT = ZNEW_ROOT.parent

SYSTEMS = [  # (标签, 结果目录, 是否本实验)
    ("ZAC 真值", ZAC_ROOT / "ZAC/result/zac/repro_fixed", False),
    ("GA v1a", ZAC_ROOT / "GA/results/repro_ga", False),
    ("FABLE", ZAC_ROOT / "FABLE/results/repro_fable", False),
    ("ZAC_zzx-B", ZNEW_ROOT / "results/repro_penalty", True),
    ("ZAC_zzx-A", ZNEW_ROOT / "results/repro_ga", True),
]


def load_dir(d: Path) -> dict[str, dict]:
    """{电路名: {duration, fidelity, batches, atoms}}；读 code + fidelity。"""
    out = {}
    if not d.is_dir():
        return out
    for fp in sorted((d / "code").glob("*_code.json")):
        name = fp.name.split("_code")[0].replace("_transpiled", "")
        code = json.load(open(fp))
        jobs = [i for i in code["instructions"] if i.get("type") == "rearrangeJob"]
        atoms = sum(len(j["aod_qubits"]) for j in jobs)
        entry = {"duration": code["runtime"], "batches": len(jobs),
                 "atoms_per_batch": atoms / max(1, len(jobs))}
        fid_fp = d / "fidelity" / f"{fp.name.split('_code')[0]}_fidelity.json"
        if fid_fp.is_file():
            entry["fidelity"] = json.load(open(fid_fp))["cir_fidelity"]
        out[name] = entry
    return out


def ledger_stats(d: Path) -> str:
    """ZAC_zzx 批次账本：χ 预演 vs 实际执行的一致率（一行摘要）。"""
    rows = []
    for fp in sorted((d / "time").glob("*_batch_ledger.json")):
        led = json.load(open(fp))
        executed_out = {}
        for r in led["route_log"]:
            if r["phase"] == "out":
                executed_out.setdefault(r["layer"], 0)
                executed_out[r["layer"]] += r["batches"]
        # place_gate 对每层可能有两个预演（复用/不复用两世界），取"与实际相等
        # 的那个世界存在"为准；实际执行的路由一定对应被 filter_mapping 选中的世界
        hit = total = 0
        layer_chi = {}
        for layer, chi, _, _ in led["placer_preview"]:
            layer_chi.setdefault(int(layer), []).append(int(chi))
        for layer, chis in layer_chi.items():
            if layer in executed_out:
                total += 1
                if executed_out[layer] in chis:
                    hit += 1
        rows.append((hit, total))
    hit = sum(h for h, _ in rows)
    total = sum(t for _, t in rows)
    if total == 0:
        return "（无账本）"
    return f"预演χ可对上实际批数的层：{hit}/{total}"


def geomean(xs: list[float]) -> float:
    return reduce(lambda a, b: a * b, xs) ** (1 / len(xs))


def main() -> None:
    if len(sys.argv) > 1:  # 允许换本实验目录（消融用）
        SYSTEMS[3] = ("ZAC_zzx-B", ZNEW_ROOT / sys.argv[1], True)
        SYSTEMS[4] = ("ZAC_zzx-A", ZNEW_ROOT / sys.argv[2], True)
    data = {label: load_dir(d) for label, d, _ in SYSTEMS}
    truth = data[SYSTEMS[0][0]]
    circuits = [c for c in truth if all(c in data[l] for l, _, _ in SYSTEMS)]

    print(f"共 {len(circuits)} 个电路同台（缺 {18 - len(circuits)} 个）\n")

    # ---------- 主表：duration ratio / 批数 ----------
    header = f"|{'电路':16s}|" + "|".join(
        f"{label} ratio|批|原子/批|" for label, _, _ in SYSTEMS) + "|"
    lines = [header, "|" + "---|" * (1 + 3 * len(SYSTEMS))]
    for c in sorted(circuits):
        z = truth[c]["duration"]
        row = f"|{c:16s}"
        for label, _, _ in SYSTEMS:
            e = data[label][c]
            row += f"|{e['duration'] / z:.3f}|{e['batches']}|{e['atoms_per_batch']:.2f}|"
        lines.append(row + "|")
    geo = f"|{'GEOMEAN':16s}"
    for label, _, _ in SYSTEMS:
        ratios = [data[label][c]["duration"] / truth[c]["duration"] for c in circuits]
        batches = sum(data[label][c]["batches"] for c in circuits)
        geo += f"|**{geomean(ratios):.3f}**|{batches}||"
    lines.append(geo + "|")

    # ---------- 保真度表 ----------
    flines = [f"|{'电路':16s}|" + "|".join(f"{l}|" for l, _, _ in SYSTEMS) + "|",
              "|" + "---|" * (1 + len(SYSTEMS))]
    for c in sorted(circuits):
        row = f"|{c:16s}"
        for label, _, _ in SYSTEMS:
            f_ = data[label][c].get("fidelity")
            row += f"|{f_:.4f}|" if f_ else "|—|"
        flines.append(row + "|")
    fgeo = f"|{'GEOMEAN':16s}"
    for label, _, _ in SYSTEMS:
        fs = [data[label][c]["fidelity"] for c in circuits if data[label][c].get("fidelity")]
        fgeo += f"|**{geomean(fs):.4f}**|" if fs else "|—|"
    flines.append(fgeo + "|")

    md = ("# ZAC_zzx 五方对比（18 电路，全部同底盘只换放置/路由）\n\n"
          "## 时长 ratio（对 ZAC 归一，<1 更快）与批次\n\n"
          + "\n".join(lines) + "\n\n## 保真度\n\n" + "\n".join(flines) + "\n\n"
          "## χ 预演对账（ZAC_zzx 专属）\n\n"
          + f"- ZAC_zzx-B: {ledger_stats(SYSTEMS[3][1])}\n"
          + f"- ZAC_zzx-A: {ledger_stats(SYSTEMS[4][1])}\n")

    out_md = ZNEW_ROOT / "results/comparison_table.md"
    out_md.write_text(md)
    print(md)

    # CSV（供后续画图/表格复用）
    with open(ZNEW_ROOT / "results/comparison_table.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["circuit"] + [f"{l}_ratio" for l, _, _ in SYSTEMS]
                   + [f"{l}_batches" for l, _, _ in SYSTEMS]
                   + [f"{l}_fidelity" for l, _, _ in SYSTEMS])
        for c in sorted(circuits):
            w.writerow([c]
                       + [f"{data[l][c]['duration'] / truth[c]['duration']:.4f}" for l, _, _ in SYSTEMS]
                       + [data[l][c]["batches"] for l, _, _ in SYSTEMS]
                       + [data[l][c].get("fidelity", "") for l, _, _ in SYSTEMS])
    print(f"\n已写 {out_md} 与 results/comparison_table.csv")


if __name__ == "__main__":
    main()
