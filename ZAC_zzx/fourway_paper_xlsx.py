"""四方法对比·论文对齐版：ICCAD 与 ZAC 两列全部使用论文自己的管线，零自研内容。

ICCAD 列 = run_qmap 复现管线（论文预处理 opt3/u1-u2 基 + 论文参数 + 论文自己的
  NavizEvaluator 计数），并排论文 Table I 原值作对照（13/15 精确一致）；论文不报
  保真度/时长，故 ICCAD 无这些列。3 个 Table I 外电路（bv14/bv19/ghz23）为同
  管线补跑（qmap_extra3.json）。
ZAC 列 = HPCA'25 原程序冻结真值（ZAC/result/zac/repro_fixed，12/18 与论文逐位
  一致），指标读其自带判分器与产物。
遗传列 = 我们的（GA 初始化 + 鬼点硬保证层），仅供对照——不参与"论文对齐"主张。

用法：.venv_qmap/bin/python fourway_paper_xlsx.py
输出：results/fourway/四方法对比_论文对齐.xlsx
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
ZZX = ROOT / "ZAC_zzx"
ZAC = ROOT / "ZAC/result/zac/repro_fixed"
OUT = ZZX / "results/fourway/四方法对比_论文对齐.xlsx"


def zac_rows():
    rows = {}
    for cp in sorted((ZAC / "code").glob("*_code.json")):
        n = cp.stem.replace("_code", "").replace("_transpiled", "")
        code = json.load(open(cp))
        steps = sum(1 for i in code["instructions"] if i["type"] == "rearrangeJob")
        move = sum(i["end_time"] - i["begin_time"] for i in code["instructions"]
                   if i["type"] == "rearrangeJob")
        raw = cp.stem.replace("_code", "")
        fid = json.load(open(ZAC / "fidelity" / f"{raw}_fidelity.json"))
        t = json.load(open(ZAC / "time" / f"{raw}_time.json"))
        rows[n] = (steps, round(move, 1), round(fid["cir_fidelity"], 4),
                   round(t["total"], 2))
    return rows


def main():
    # ---- ZAC（论文原程序）----
    z = zac_rows()
    # ---- ICCAD（论文管线）：15 复现 + 3 补跑 ----
    iccad = {}
    for r in csv.DictReader(open(ROOT / "experiments/results/qmap_qasmbench.csv")):
        if r["status"] != "ok":
            continue
        key = f"{r['circuit']}_n{r['qubits']}".replace("swaptest_n", "swap_test_n")
        d = iccad.setdefault(key, {})
        d[r["config"]] = (int(r["steps"]), float(r["rearr_ms"]),
                          float(r["total_time"]) if r["total_time"] else None,
                          int(r["two_qubit_gate_layer"]),
                          int(r["max_gates_in_layer"]))
    for r in json.load(open(ROOT / "experiments/results/qmap_extra3.json")):
        key = f"{r['circuit']}_n{r['qubits']}"
        iccad.setdefault(key, {})[r["config"]] = (
            r["steps"], r["rearr_ms"], r["total_ms"],
            r["layers"], r["max_gates"])
    # ---- 论文 Table I 原值 ----
    truth = {}
    for r in csv.DictReader(open(ROOT / "experiments/paper_truth/qmap_table1.csv")):
        if not r["qubits"]:
            continue
        key = f"{r['circuit']}_n{r['qubits']}".replace("swaptest_n", "swap_test_n")
        truth[key] = r
    # ---- 遗传（我们）----
    def gene(dirn):
        rows = {}
        for cp in sorted((ZZX / f"results/{dirn}/code").glob("*_code.json")):
            n = cp.stem.replace("_code", "").replace("_transpiled", "")
            code = json.load(open(cp))
            steps = sum(1 for i in code["instructions"]
                        if i["type"] == "rearrangeJob")
            raw = cp.stem.replace("_code", "")
            fid = json.load(open(ZZX / f"results/{dirn}/fidelity/{raw}_fidelity.json"))
            t = json.load(open(ZZX / f"results/{dirn}/time/{raw}_time.json"))
            rows[n] = (steps, round(fid["cir_fidelity"], 4), round(t["total"], 2))
        return rows
    nl, lk = gene("nolook_ga"), gene("main_ga")

    common = [n for n in z if n in iccad and n in nl and n in lk]
    common.sort()

    wb = Workbook()
    ws = wb.active
    ws.title = "论文对齐"
    note = ("口径：ZAC 列 = HPCA'25 原程序冻结真值（12/18 与论文逐位一致）；"
            "ICCAD 列 = 论文复现管线（opt3/u1-u2 预处理、论文参数、论文自己的"
            " NavizEvaluator 计数；15 电路 13/15 与 Table I 步数精确一致，qft 两例"
            " 输入电路漂移；bv14/bv19/ghz23 为 Table I 外同管线补跑）；论文不报"
            " ICCAD 保真度/时长，故无此列。遗传列 = GA初始化+鬼点硬保证层（我们"
            "的方法，仅供对照）。Table I 原值列供逐行核对。")
    cols = ["circuit", "q", "2q门", "Layers", "Max 2Q/Layer",
            "ZAC Steps", "ZAC move μs", "ZAC 保真度", "ZAC 编译s",
            "ICCAD Steps(ra/agn)", "ICCAD Steps(rw/astar)", "ICCAD rearr ms", "ICCAD 编译s",
            "Table I ra_steps", "Table I rw_steps",
            "遗传(无前瞻) Steps", "遗传(前瞻) Steps", "遗传 保真度", "遗传 编译s"]
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
    for n in common:
        ag = iccad[n]["agnostic"]
        asr = iccad[n]["astar"]
        t = truth.get(n)
        ws.append([n,
                   int(t["qubits"]) if t else "", int(t["two_qubit_gates"]) if t else "",
                   ag[3], ag[4],
                   z[n][0], z[n][1], z[n][2], z[n][3],
                   ag[0], asr[0], asr[1],
                   round(asr[2] / 1000, 3) if asr[2] else "",
                   int(t["ra_steps"]) if t else "—", int(t["rw_steps"]) if t else "—",
                   nl[n][0], lk[n][0], lk[n][1], lk[n][2]])
    ws.column_dimensions["A"].width = 22
    for c in range(2, len(cols) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13
    ws.freeze_panes = "A3"
    wb.save(OUT)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
