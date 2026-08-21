"""四方法 × ICCAD'25 Table I 全指标对齐表。

Table I 的每电路指标：place 时间 / route 时间 / Num. Rearr. Steps / rearr 时间
（ra=routing-agnostic、rw=routing-aware 两配置）×电路属性（q / 2q门 / layers /
max gates per layer）。本表给五个方法块，同一套指标：

  ZAC原始     place=initial+intermediate placement（原程序 SA+逐轮放置；
              其路由计时混在放置内，route 列恒≈0，见表注）
  ICCAD ra    论文管线 3.2.0（qmap_table1_v32.json，单位已修为真 ms）
  ICCAD rw    同上（astar = 论文主方法）
  遗传无前瞻  results/nolook_ga（GA 初始化+硬保证层）
  遗传前瞻    results/main_ga（同上，鬼点罚）

Steps/rearr 与 Table I 同义：一步=一次完整 AOD 重排循环；rearr=纯搬运执行时长。
单位全按 Table I：时间一律 ms（Steps 为整数）。

用法：.venv_qmap/bin/python table1_align.py
输出：results/fourway/四方法_对齐TableI.xlsx + .md
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
ZZX = ROOT / "ZAC_zzx"
ZAC = ROOT / "ZAC" / "result" / "zac" / "repro_fixed"
OUT = ZZX / "results" / "fourway"


def zair_block(code_dir: Path, stem: str):
    """ZAC/遗传系 → (place_ms, route_ms, steps, rearr_ms)。stem 无后缀时自动回退。"""
    for cand in (stem, stem.replace("_transpiled", "")):
        try:
            code = json.load(open(code_dir / f"{cand}_code.json"))
            t = json.load(open(code_dir.parent / "time" / f"{cand}_time.json"))
            stem = cand
            break
        except FileNotFoundError:
            code = None
    if code is None:
        return None
    jobs = [i for i in code["instructions"] if i["type"] == "rearrangeJob"]
    place = t.get("initial placement", 0) + t.get("intermediate placement", 0)
    route = t.get("routing", 0)
    return (round(place * 1000, 1), round(route * 1000, 1), len(jobs),
            round(sum(i["end_time"] - i["begin_time"] for i in jobs) / 1000, 2))


def gm(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def main():
    # ---- ICCAD 论文管线（真 ms）----
    iccad = {}
    for r in json.load(open(ROOT / "experiments/results/qmap_table1_v32.json")):
        key = f"{r['circuit']}_n{r['qubits']}".replace("swaptest_n", "swap_test_n")
        iccad.setdefault(key, {})[r["config"]] = (
            r["place_ms"], r["route_ms"], int(r["steps"]),
            round(r["rearr_us"] / 1000, 2), r["layers"], r["max_gates"])
    # ---- 电路属性：Table I 原值（15）+ 3 补跑电路的手填 ----
    truth, props = {}, {}
    for r in csv.DictReader(open(ROOT / "experiments/paper_truth/qmap_table1.csv")):
        if not r["qubits"]:
            continue
        key = f"{r['circuit']}_n{r['qubits']}".replace("swaptest_n", "swap_test_n")
        truth[key] = r
        props[key] = (int(r["qubits"]), int(r["two_qubit_gates"]))
    for k, qg in {"bv_n14": (14, 13), "bv_n19": (19, 18), "ghz_n23": (23, 22)}.items():
        props.setdefault(k, qg)

    rows = []
    for name in sorted(iccad):
        n = name                                            # bv_n14 形式
        stem = n + "_transpiled"                            # zair_block 自带回退
        z = zair_block(ZAC / "code", stem)
        nl = zair_block(ZZX / "results/nolook_ga/code", stem)
        lk = zair_block(ZZX / "results/main_ga/code", stem)
        ag, rw = iccad[n]["agnostic"], iccad[n]["astar"]
        q, g2 = props.get(n, ("", ""))
        rows.append({"n": n, "q": q, "g2": g2, "layers": ag[4], "maxg": ag[5],
                     "z": z, "ra": ag, "rw": rw, "nl": nl, "lk": lk})

    # ---- Sheet2：ICCAD 数据集（qmap examples 154）----
    rows2 = []
    if (ROOT / "experiments/results/qmap_iccad_v32.json").exists():
        cap = json.load(open(ROOT / "experiments/results/qmap_iccad_v32.json"))
        suite = {r["name"]: r for r in json.load(
            open(ZZX / "results/qmap_suite/summary.json"))}
        for n in sorted(cap):
            v = cap[n]
            if "astar" not in v or "agnostic" not in v:
                continue
            rec = suite.get(n, {})
            ra, rw = v["agnostic"], v["astar"]
            rows2.append({
                "n": n, "q": v.get("qubits", rec.get("qubits")),
                "g2": rec.get("gates2q", ""),
                "layers": ra.get("layers", ""), "maxg": ra.get("max_gates", ""),
                "z": zair_block(ZZX / "results/qmap_suite/zac/code", n),
                "ra": (ra["place_ms"], ra["route_ms"], ra["steps"], ra["rearr_ms"]),
                "rw": (rw["place_ms"], rw["route_ms"], rw["steps"], rw["rearr_ms"]),
                "nl": zair_block(ZZX / "results/qmap_suite/zzx_hard_nl/code", n),
                "lk": zair_block(ZZX / "results/qmap_suite/zzx_hard/code", n)})

    # ---- xlsx：双 sheet（发射函数共用） ----
    def emit(ws, note, rws):
        cols = ["circuit", "q", "2q门", "layers", "max门/层",
                "ZAC Place", "ZAC Route", "ZAC Steps", "ZAC Rearr",
                "rw Place", "rw Route", "rw Steps", "rw Rearr",
                "NL Place", "NL Route", "NL Steps", "NL Rearr",
                "LK Place", "LK Route", "LK Steps", "LK Rearr"]
        bold, fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
        ws.append([note])
        ws["A1"].font = Font(italic=True, size=9, color="666666")
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
        ws.append(cols)
        for c in range(1, len(cols) + 1):
            cell = ws.cell(row=2, column=c)
            cell.font = bold
            cell.fill = fill
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
        for r in rws:
            line = [r["n"], r["q"], r["g2"], r["layers"], r["maxg"]]
            for blk in (r["z"], r["rw"], r["nl"], r["lk"]):
                line += list(blk) if blk else ["—"] * 4
            ws.append(line)

        def col(key, idx):
            return [r[key][idx] for r in rws if r[key]]

        ws.append([])
        for label, idx in (("Σ Place ms", 0), ("Σ Route ms", 1),
                           ("Σ Steps", 2), ("Σ Rearr ms", 3)):
            line = [label]
            for key in ("z", "rw", "nl", "lk"):
                v = col(key, idx)
                line.append(round(sum(v), 1) if v else "—")
            ws.append(line)
        for label, idx in (("geomean Steps/ZAC", 2), ("geomean Rearr/ZAC", 3)):
            line = [label, 1.0]
            for key in ("rw", "nl", "lk"):
                # 行内配对：两法同电路都齐才入对（缺失行会错位 zip，已修）
                pairs = [(r[key][idx], r["z"][idx]) for r in rws if r[key] and r["z"]]
                g = gm([a / b for a, b in pairs if a and b])
                line.append(round(g, 3) if g else "—")
            ws.append(line)
        for c, w in zip(range(1, len(cols) + 1), [18, 5, 6, 7, 8] + [9, 9, 8, 9] * 4):
            ws.column_dimensions[get_column_letter(c)].width = w
        return col

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "ZAC数据集18"
    note1 = ("四方法 × ICCAD'25 Table I 全指标：ZAC原始 / ICCAD rw（论文主方法）/"
             "遗传无前瞻(NL, w_ghost=0) / 遗传前瞻(LK, 鬼点罚 w_ghost=1)，遗传两席均"
             "GA 初始化+硬保证层。每方法四列：Place ms / Route ms / Num. Rearr. Steps / "
             "Rearr ms。"
             "ICCAD=论文管线 mqt.qmap 3.2.0（Steps 26/30 与 Table I 逐位一致；时间单位坑已勘误："
             "stats() 实为 μs，此处已修为真 ms）。ZAC/遗传：Place=初始布局+逐轮放置、Route=路由"
             "（ZAC 原程序路由计时混在放置内故 Route≈0）；Steps=rearrangeJob 条数（与 Table I "
             "一步=一次完整 AOD 重排循环同义）；Rearr=Σ批起止区间。")
    col1 = emit(ws1, note1, rows)
    mds = [md_block("ZAC 数据集（hpca 18）", rows)]
    if rows2:
        ws2 = wb.create_sheet("ICCAD数据集154")
        note2 = ("ICCAD 数据集（qmap examples 154）× Table I 全指标。ICCAD=3.2.0 论文管线全量"
                 "采集（capture_iccad_v32.py，单位已修）；ZAC=qmap_suite/zac；遗传=硬化套件"
                 "（zzx_hard/zzx_hard_nl，GA 初始化+鬼点硬保证层）。超时/失败以 — 表示；"
                 "geomean 按与 ZAC 成对齐全的电路计算（各法 N 见 Σ 行可推）。")
        emit(ws2, note2, rows2)
        mds.append(md_block("ICCAD 数据集（qmap examples 154）", rows2))
    wb.save(OUT / "四方法_对齐TableI.xlsx")
    open(OUT / "四方法_对齐TableI.md", "w").write(
        "# 四方法 × Table I 全指标对齐（双数据集）\n\n" + "\n\n".join(mds))
    print("\n\n".join(mds))


def md_block(title, rows):
    col = lambda key, idx: [r[key][idx] for r in rows if r[key]]

    def s(key, idx):
        v = col(key, idx)
        return sum(v) if v else float("nan")
    lines = [f"## {title}", "",
             "| 指标 | ZAC原始 | ICCAD rw | 遗传NL | 遗传LK |",
             "|---|---|---|---|---|",
             f"| Σ Place ms | {s('z',0):.0f} | {s('rw',0):.0f} | {s('nl',0):.0f} | {s('lk',0):.0f} |",
             f"| Σ Route ms | {s('z',1):.0f} | {s('rw',1):.0f} | {s('nl',1):.0f} | {s('lk',1):.0f} |",
             f"| Σ Steps | {s('z',2):.0f} | {s('rw',2):.0f} | {s('nl',2):.0f} | {s('lk',2):.0f} |",
             f"| Σ Rearr ms | {s('z',3):.0f} | {s('rw',3):.0f} | {s('nl',3):.0f} | {s('lk',3):.0f} |"]
    for label, idx in (("Steps", 2), ("Rearr", 3)):
        cells = ["1.000"]
        for key in ("rw", "nl", "lk"):
            pairs = [(r[key][idx], r["z"][idx]) for r in rows if r[key] and r["z"]]
            g = gm([a / b for a, b in pairs if a and b])
            cells.append(f"{g:.3f} (N={len(pairs)})" if g else "—")
        lines.append(f"| geomean {label}/ZAC | " + " | ".join(cells) + " |")
    lines += ["", "（ZAC 的 Route≈0 系原程序计时口径：路由混在放置内；Place 列含其全部编译。）"]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
