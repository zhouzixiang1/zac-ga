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
    # qmap 3.2.0（论文版本）全量重采：18 电路 ×2 映射，步数 26/30 与
    # Table I 精确一致；rearr 原始单位 μs，行构造时 ÷1000 对齐论文 ms。
    # ⚠ 单位坑（已修，2026-08-21）：stats().placeTime/routeTime/totalTime 实测
    # 也是 μs（wall 2.1ms ↔ 1727），qmap_table1_v32.json 的 place_ms/route_ms/
    # total_ms 已就地 ÷1000 为真 ms——重新采集时必须同样换算
    iccad = {}
    for r in json.load(open(ROOT / "experiments/results/qmap_table1_v32.json")):
        key = f"{r['circuit']}_n{r['qubits']}".replace(
            "swaptest_n", "swap_test_n")
        iccad.setdefault(key, {})[r["config"]] = (
            int(r["steps"]), float(r["rearr_us"]),
            r.get("place_ms"), r.get("route_ms"), r.get("total_ms"),
            int(r["layers"]), int(r["max_gates"]))
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
            "ICCAD 列 = **mqt.qmap 3.2.0（论文版本）**论文管线全量重采（opt3/u1-u2 "
            "预处理、论文参数、NavizEvaluator）：步数 26/30 与 Table I 精确一致"
            "（qft 两例输入电路漂移），place/route/rearr 为本机 3.2.0 实测"
            "（rearr 已换算 ms 对齐论文单位）；bv14/bv19/ghz23 为 Table I 外同管线"
            "补跑。注意 .venv_qmap 当前为 3.5.0（步数漂移 13/30），论文对齐一律"
            "用 3.2.0。论文不报 ICCAD 保真度，故无此列。遗传列 = GA初始化+鬼点"
            "硬保证层（我们的方法，仅供对照）。")
    cols = ["circuit", "q", "2q门", "Layers", "Max 2Q/Layer",
            "ZAC Steps", "ZAC move μs", "ZAC 保真度", "ZAC 编译s",
            "ICCAD ra place_ms", "ICCAD ra route_ms", "ICCAD ra Steps", "ICCAD ra rearr_ms",
            "ICCAD rw place_ms", "ICCAD rw route_ms", "ICCAD rw Steps", "ICCAD rw rearr_ms",
            "Table I ra_place", "Table I ra_route", "Table I ra_steps", "Table I ra_rearr",
            "Table I rw_place", "Table I rw_route", "Table I rw_steps", "Table I rw_rearr",
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
        def fnum(v, nd=2):
            return round(float(v), nd) if v not in (None, "") else "—"
        # iccad 元组序: [0]steps [1]rearr_ms [2]place_ms [3]route_ms
        #               [4]total_ms [5]layers [6]max_gates
        _fill = {"bv_n14": (14, 13), "bv_n19": (19, 18), "ghz_n23": (23, 22)}
        _q = int(t["qubits"]) if t else _fill.get(n, ("", ""))[0]
        _g = int(t["two_qubit_gates"]) if t else _fill.get(n, ("", ""))[1]
        ws.append([n,
                   _q, _g,
                   ag[5], ag[6],
                   z[n][0], z[n][1], z[n][2], z[n][3],
                   fnum(ag[2]), fnum(ag[3]), ag[0], fnum(ag[1] / 1000, 3),
                   fnum(asr[2]), fnum(asr[3]), asr[0], fnum(asr[1] / 1000, 3),
                   fnum(t["ra_place_ms"]) if t else "—",
                   fnum(t["ra_route_ms"]) if t else "—",
                   int(t["ra_steps"]) if t else "—",
                   fnum(t["ra_rearr_ms"]) if t else "—",
                   fnum(t["rw_place_ms"]) if t else "—",
                   fnum(t["rw_route_ms"]) if t else "—",
                   int(t["rw_steps"]) if t else "—",
                   fnum(t["rw_rearr_ms"]) if t else "—",
                   nl[n][0], lk[n][0], lk[n][1], lk[n][2]])
    ws.column_dimensions["A"].width = 22
    for c in range(2, len(cols) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13
    ws.freeze_panes = "A3"
    wb.save(OUT)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
