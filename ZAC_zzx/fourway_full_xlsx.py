"""四方法 × 四指标 · 双数据集版：每数据集一个 sheet，四方法并排，无论文原值列。

指标（每方法 4 列）：保真度 / move批次(Steps) / move时间 μs / 编译时间 s。
ICCAD 另给 ra(agnostic) 的 Steps 一列（rw=论文方法为准，其余指标取 rw）。

Sheet1 ZAC数据集（18）：ZAC=原程序冻结真值；ICCAD=mqt.qmap 3.2.0（论文版本，
  Steps 26/30 与 Table I 精确一致；move=rearr（NavizEvaluator 论文口径）；
  保真度=同公式对其 3.2.0 NA 码直算 na_score.py——论文不报）；遗传=GA初始化+
  鬼点硬保证层（前瞻席=鬼点罚）。
Sheet2 ICCAD数据集（154，qmap examples）：ICCAD=results/fourway/qmap_iccad.json
  （当时 3.5.0/统一预处理管线）；ZAC=qmap_suite/zac（133 完成）；遗传=
  qmap_suite/{zzx_ga,zzx_nolook_ga}（119 完成，旧内核未硬化——NOTE 注明）。

用法：.venv_qmap/bin/python fourway_full_xlsx.py
输出：results/fourway/四方法_四指标_双数据集.xlsx
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
ZACD = ROOT / "ZAC/result/zac/repro_fixed"
OUT = ZZX / "results/fourway/四方法_四指标_双数据集.xlsx"

HDR = ["circuit", "q", "2q门", "Layers", "Max 2Q/Layer",
       "ZAC Steps", "ZAC move μs", "ZAC 保真度", "ZAC 编译s",
       "ICCAD Steps(ra)", "ICCAD Steps(rw)", "ICCAD move μs", "ICCAD 保真度", "ICCAD 编译s",
       "无前瞻 Steps", "无前瞻 move μs", "无前瞻 保真度", "无前瞻 编译s",
       "前瞻 Steps", "前瞻 move μs", "前瞻 保真度", "前瞻 编译s"]
# 每方法的 (steps, move, fid, 编译) 列位（0 基）
COLS = {"ZAC": (5, 6, 7, 8), "ICCAD": (10, 11, 12, 13),
        "NL": (14, 15, 16, 17), "LK": (18, 19, 20, 21)}


def zair(code_dir, fid_dir, time_dir):
    """产物目录 → {电路: (steps, move, fid, 编译, 轮数, 层宽, q数)}。"""
    rows = {}
    for cp in sorted(Path(code_dir).glob("*_code.json")):
        n = cp.stem.replace("_code", "").replace("_transpiled", "")
        code = json.load(open(cp))
        steps = sum(1 for i in code["instructions"] if i["type"] == "rearrangeJob")
        move = sum(i["end_time"] - i["begin_time"] for i in code["instructions"]
                   if i["type"] == "rearrangeJob")
        ryd = [len(i["gates"]) for i in code["instructions"] if i["type"] == "rydberg"]
        nq = len(code["instructions"][0]["init_locs"])
        raw = cp.stem.replace("_code", "")
        fp = Path(fid_dir) / f"{raw}_fidelity.json"
        tp = Path(time_dir) / f"{raw}_time.json"
        rows[n] = (steps, round(move, 1),
                   round(json.load(open(fp))["cir_fidelity"], 4) if fp.exists() else None,
                   round(json.load(open(tp))["total"], 2) if tp.exists() else None,
                   len(ryd), max(ryd) if ryd else 0, nq)
    return rows


def gm(v):
    return exp(sum(map(log, v)) / len(v)) if v else None


def emit(ws, note, rows):
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDEBF7")
    ws.append([note])
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HDR))
    ws.append(HDR)
    for c in range(1, len(HDR) + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for r in rows:
        ws.append(r)

    def nums(c):
        return [r[c] for r in rows if isinstance(r[c], (int, float))]

    def ratio_gm(c, base):
        pairs = [(r[c], r[base]) for r in rows
                 if isinstance(r[c], (int, float)) and isinstance(r[base], (int, float))]
        vals = [a / b for a, b in pairs if b]
        return round(gm(vals), 3) if vals else "—"

    def total(c):
        v = nums(c)
        return round(sum(v), 1) if v else "—"

    def mean(c):
        v = nums(c)
        return round(sum(v) / len(v), 4) if v else "—"

    def row_sum(label, cells):
        ws.append([label] + cells)

    row_sum("合计 Steps", ["", "", "", "",
                           total(5), "", "", "", "", total(10), "", "", "",
                           total(14), "", "", "", total(18), "", "", ""])
    row_sum("Steps/ZAC geomean", ["", "", "", "",
                                  1.0, "", "", "", "", ratio_gm(10, 5), "", "", "",
                                  ratio_gm(14, 5), "", "", "", ratio_gm(18, 5), "", "", ""])
    row_sum("平均保真度", ["", "", "", "",
                           "", "", mean(7), "", "", "", "", mean(12), "",
                           "", "", mean(16), "", "", "", mean(20), ""])
    row_sum("move/ZAC geomean", ["", "", "", "",
                                 "", 1.0, "", "", "", "", ratio_gm(11, 6), "", "",
                                 "", ratio_gm(15, 6), "", "", "", ratio_gm(19, 6), ""])
    row_sum("编译总s", ["", "", "", "",
                       "", "", "", total(8), "", "", "", "", total(13),
                       "", "", "", total(17), "", "", "", total(21)])
    for rr in range(ws.max_row - 4, ws.max_row + 1):
        for c in range(1, len(HDR) + 1):
            ws.cell(row=rr, column=c).font = bold
    ws.column_dimensions["A"].width = 22
    for c in range(2, len(HDR) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 12.5
    ws.freeze_panes = "A3"


def main():
    wb = Workbook()
    # ---------------- Sheet1 ----------------
    z = zair(ZACD / "code", ZACD / "fidelity", ZACD / "time")
    nl = zair(ZZX / "results/nolook_ga/code", ZZX / "results/nolook_ga/fidelity",
              ZZX / "results/nolook_ga/time")
    lk = zair(ZZX / "results/main_ga/code", ZZX / "results/main_ga/fidelity",
              ZZX / "results/main_ga/time")
    v32 = {}
    for r in json.load(open(ROOT / "experiments/results/qmap_table1_v32.json")):
        v32.setdefault(f"{r['circuit']}_n{r['qubits']}".replace(
            "swaptest_n", "swap_test_n"), {})[r["config"]] = r
    scored = {}
    sf = ROOT / "experiments/results/qmap_v32_scored.json"
    if sf.exists():
        for r in json.load(open(sf)):
            scored[(r["circuit"].replace("swaptest_n", "swap_test_n"),
                    r["config"])] = r
    rows1 = []
    for n in sorted(z):
        if n not in v32 or n not in nl or n not in lk:
            continue
        ag, asr = v32[n]["agnostic"], v32[n]["astar"]
        sc = scored.get((n, "astar"), {})
        g2 = scored.get((n, "agnostic"), {}).get("n2q_total", "")
        rows1.append([
            n, v32[n]["astar"]["qubits"], g2, ag["layers"], ag["max_gates"],
            z[n][0], z[n][1], z[n][2], z[n][3],
            ag["steps"], asr["steps"], round(asr["rearr_us"], 1),
            sc.get("fid", "—"), round(asr["total_ms"] / 1000, 3),  # json 已修为真 ms（stats 原始单位 μs）
            nl[n][0], nl[n][1], nl[n][2], nl[n][3],
            lk[n][0], lk[n][1], lk[n][2], lk[n][3]])
    ws = wb.active
    ws.title = "ZAC数据集"
    note1 = ("四方法 × 四指标（保真度/move批次/move时间/编译）。ZAC=HPCA'25 原程序"
             "冻结真值；ICCAD=mqt.qmap 3.2.0（论文版本，Steps 26/30 与 Table I "
             "精确一致；move=rearr（论文 NavizEvaluator）；保真度=同公式对其 3.2.0 "
             "输出直算（na_score.py，论文不报该指标））；遗传=GA初始化+鬼点硬保证层"
             "（前瞻席=鬼点罚，鬼点全 0）。ICCAD Steps 另给 ra 列，其余指标取 rw。")
    emit(ws, note1, rows1)

    # ---------------- Sheet2 ----------------
    qmi = {r["name"].replace("_transpiled", ""): r["qmap"]
           for r in json.load(open(ZZX / "results/fourway/qmap_iccad.json"))}
    z2 = zair(ZZX / "results/qmap_suite/zac/code",
              ZZX / "results/qmap_suite/zac/fidelity",
              ZZX / "results/qmap_suite/zac/time")
    n2g = zair(ZZX / "results/qmap_suite/zzx_nolook_ga/code",
               ZZX / "results/qmap_suite/zzx_nolook_ga/fidelity",
               ZZX / "results/qmap_suite/zzx_nolook_ga/time")
    l2g = zair(ZZX / "results/qmap_suite/zzx_ga/code",
               ZZX / "results/qmap_suite/zzx_ga/fidelity",
               ZZX / "results/qmap_suite/zzx_ga/time")
    rows2 = []
    for n in sorted(qmi):
        q = qmi[n]
        zr, nr, lr = z2.get(n), n2g.get(n), l2g.get(n)

        def cell4(r):
            return list(r[:4]) if r else ["—"] * 4

        attr = lr or nr or zr
        rows2.append([
            n, q.get("qubits"), q.get("gates2q"),
            attr[4] if attr else "", attr[5] if attr else "",
            *cell4(zr),
            q["batches"], q["batches"], round(q["move_time"], 1),
            q["fidelity"], round(q["compile_ms"] / 1000, 3),
            *cell4(nr), *cell4(lr)])
    ws2 = wb.create_sheet("ICCAD数据集")
    note2 = ("qmap examples 154 例。ICCAD=当时管线（3.5.0/统一预处理，含保真度"
             "直算）；ZAC=qmap_suite/zac（133 完成）；遗传=qmap_suite/{zzx_ga,"
             "zzx_nolook_ga}（119 完成）——旧内核（无硬保证层），硬化+3.2.0 重跑"
             "属遗留项。列义同 Sheet1。")
    emit(ws2, note2, rows2)

    wb.save(OUT)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
