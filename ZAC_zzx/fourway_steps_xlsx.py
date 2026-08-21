"""四方法对比·硬层版 Excel：Num. Rearr. Steps 用真值（仿 ICCAD'25 Table I
逐电路绝对数），不用比例；move/保真度/编译同为真值列。

Sheet1 = ZAC 数据集（hpca 18，四方法全齐；遗传两席 = GA 初始化+鬼点硬保证层）
Sheet2 = ICCAD 数据集（qmap examples；遗传列为 qmap_suite 旧内核，未硬化——
         表头 NOTE 注明，硬化重跑属遗留项）

用法：.venv_qmap/bin/python fourway_steps_xlsx.py
输出：results/fourway/四方法对比_硬层.xlsx
"""
from __future__ import annotations

import json
from math import exp, log
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
ZZX = ROOT / "ZAC_zzx"
ZAC = ROOT / "ZAC/result/zac/repro_fixed"
OUT = ZZX / "results/fourway/四方法对比_硬层.xlsx"


def zair(code_dir, fid_dir, time_dir):
    """ZAC/遗传系产物 → {电路: (steps, move, fid, compile, ghost)}。"""
    rows = {}
    for cp in sorted(Path(code_dir).glob("*_code.json")):
        n = cp.stem.replace("_code", "")
        key = n.replace("_transpiled", "")
        code = json.load(open(cp))
        steps = sum(1 for i in code["instructions"] if i["type"] == "rearrangeJob")
        move = sum(i["end_time"] - i["begin_time"] for i in code["instructions"]
                   if i["type"] == "rearrangeJob")
        fp = Path(fid_dir) / f"{n}_fidelity.json"
        tp = Path(time_dir) / f"{n}_time.json"
        ryd = [len(i["gates"]) for i in code["instructions"]
               if i["type"] == "rydberg"]
        rows[key] = (steps, round(move, 1),
                     round(json.load(open(fp))["cir_fidelity"], 4) if fp.exists() else None,
                     round(json.load(open(tp))["total"], 2) if tp.exists() else None,
                     len(ryd), max(ryd) if ryd else 0)
    return rows


def gm(vals):
    return exp(sum(map(log, vals)) / len(vals)) if vals else float("nan")


def fill_sheet(ws, title, circuits, cols, summary_label, summary_fn):
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDEBF7")
    ws.append([title])
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for r in circuits:
        ws.append(r)
    row = ws.append(summary_label()) and ws.max_row or ws.max_row
    for c in range(1, len(cols) + 1):
        ws.cell(row=2, column=c)
    for col, width in ((1, 22),):
        ws.column_dimensions[get_column_letter(col)].width = width
    for col in range(2, len(cols) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 12.5
    ws.freeze_panes = "A3"


def main():
    zac = zair(ZAC / "code", ZAC / "fidelity", ZAC / "time")
    nl = zair(ZZX / "results/nolook_ga/code", ZZX / "results/nolook_ga/fidelity",
              ZZX / "results/nolook_ga/time")
    lk = zair(ZZX / "results/main_ga/code", ZZX / "results/main_ga/fidelity",
              ZZX / "results/main_ga/time")
    qm = {r["name"].replace("_transpiled", ""): r["qmap"]
          for r in json.load(open(ZZX / "results/fourway/qmap_hpca.json"))}

    wb = Workbook()
    # ---------------- Sheet1：ZAC 数据集 ----------------
    ws = wb.active
    ws.title = "ZAC数据集"
    cols = ["circuit", "q", "2q门", "Num. Layers", "Max 2Q-Gates in Layer",
            "ZAC Steps", "ICCAD Steps", "遗传(无前瞻) Steps", "遗传(前瞻) Steps",
            "ZAC move μs", "ICCAD move μs", "遗传(无前瞻) move", "遗传(前瞻) move",
            "ZAC fid", "ICCAD fid", "遗传(无前瞻) fid", "遗传(前瞻) fid",
            "ZAC 编译s", "ICCAD 编译s", "遗传(无前瞻) 编译s", "遗传(前瞻) 编译s"]
    note = ("Num. Rearr. Steps 为真值（ICCAD'25 Table I 同口径：一次完整 AOD "
            "重排循环=1步；ZAC/遗传=rearrangeJob 条数）。遗传两席=GA初始化+"
            "鬼点硬保证层（前瞻席=鬼点罚 w_ghost=1）；ICCAD=astar 优先回落 "
            "agnostic；同架构同判分器。")
    common = [n for n in zac if n in qm and n in nl and n in lk]
    common.sort()
    rows = []
    for n in common:
        q = qm[n]
        rows.append([n, q["qubits"], q["gates2q"], lk[n][4], lk[n][5],
                     zac[n][0], q["batches"], nl[n][0], lk[n][0],
                     zac[n][1], round(q["move_time"], 1), nl[n][1], lk[n][1],
                     zac[n][2], q["fidelity"], nl[n][2], lk[n][2],
                     zac[n][3], round(q["compile_ms"] / 1000, 2),
                     nl[n][3], lk[n][3]])
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDEBF7")
    ws.append([note])
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    # 汇总：真值合计 + 比例 geomean（步数合计=总班次，另给 /ZAC geomean 参考）
    z_steps = sum(zac[n][0] for n in common)
    q_steps = sum(qm[n]["batches"] for n in common)
    n_steps = sum(nl[n][0] for n in common)
    l_steps = sum(lk[n][0] for n in common)
    ws.append(["合计 Steps（真值）", "", "", z_steps, q_steps, n_steps, l_steps])
    ws.append(["Steps/ZAC geomean", "", "",
               1.0, round(gm([qm[n]["batches"] / zac[n][0] for n in common]), 3),
               round(gm([nl[n][0] / zac[n][0] for n in common]), 3),
               round(gm([lk[n][0] / zac[n][0] for n in common]), 3)])
    ws.append(["平均保真度", "", "",
               round(sum(zac[n][2] for n in common) / len(common), 4),
               round(sum(qm[n]["fidelity"] for n in common) / len(common), 4),
               round(sum(nl[n][2] for n in common) / len(common), 4),
               round(sum(lk[n][2] for n in common) / len(common), 4)])
    ws.append(["编译总s", "", "",
               round(sum(zac[n][3] for n in common), 1),
               round(sum(qm[n]["compile_ms"] for n in common) / 1000, 1),
               round(sum(nl[n][3] for n in common), 1),
               round(sum(lk[n][3] for n in common), 1)])
    for rr in range(ws.max_row - 3, ws.max_row + 1):
        for c in range(1, len(cols) + 1):
            ws.cell(row=rr, column=c).font = bold
    ws.column_dimensions["A"].width = 22
    for c in range(2, len(cols) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13
    ws.freeze_panes = "A3"

    # ---------------- Sheet2：ICCAD 数据集（qmap examples） ----------------
    ws2 = wb.create_sheet("ICCAD数据集")
    qmi = {r["name"].replace("_transpiled", ""): r["qmap"]
           for r in json.load(open(ZZX / "results/fourway/qmap_iccad.json"))}
    z2 = zair(ZZX / "results/qmap_suite/zac/code",
              ZZX / "results/qmap_suite/zac/fidelity",
              ZZX / "results/qmap_suite/zac/time") if (
        ZZX / "results/qmap_suite/zac/code").exists() else {}
    x2 = zair(ZZX / "results/qmap_suite/zzx/code",
              ZZX / "results/qmap_suite/zzx/fidelity",
              ZZX / "results/qmap_suite/zzx/time") if (
        ZZX / "results/qmap_suite/zzx/code").exists() else {}
    cols2 = ["circuit", "q", "2q门", "Num. Layers", "Max 2Q-Gates in Layer",
             "ICCAD Steps", "ZAC Steps", "遗传zzx Steps(旧内核)",
             "ICCAD move μs", "ZAC move μs", "遗传zzx move(旧内核)",
             "ICCAD fid", "ZAC fid", "遗传zzx fid(旧内核)"]
    note2 = ("qmap examples 全套；遗传列=qmap_suite 旧内核（无硬保证层，"
             "硬化重跑属遗留项）；ZAC 仅收录可完成电路。Steps 同 Table I 口径。")
    ws2.append([note2])
    ws2["A1"].font = Font(italic=True, size=9, color="666666")
    ws2.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols2))
    ws2.append(cols2)
    for c in range(1, len(cols2) + 1):
        cell = ws2.cell(row=2, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    n_rows = 0
    for n in sorted(qmi):
        q = qmi[n]
        zr = z2.get(n)
        xr = x2.get(n)
        ws2.append([n, q.get("qubits"), q.get("gates2q"),
                    q.get("rounds"), xr[5] if xr else "—",
                    q["batches"], zr[0] if zr else "—", xr[0] if xr else "—",
                    round(q["move_time"], 1),
                    zr[1] if zr else "—", xr[1] if xr else "—",
                    q["fidelity"], zr[2] if zr else "—", xr[2] if xr else "—"])
        n_rows += 1
    ws2.append([f"共 {n_rows} 例（qmap 全部成功；ZAC/遗传列数=各自完成数）"])
    ws2.column_dimensions["A"].width = 24
    for c in range(2, len(cols2) + 1):
        ws2.column_dimensions[get_column_letter(c)].width = 14
    ws2.freeze_panes = "A3"

    wb.save(OUT)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
