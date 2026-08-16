"""三方对比 xlsx 生成器：Sheet1 = hpca 18，Sheet2 = qmap examples 全套。

读取 results/three_way/{sheet1,sheet2}.json，输出 results/three_way/三方对比.xlsx
表头/口径注记写在每 sheet 顶部；尾部 geomean + 平均保真度 + 最差名单。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
OUT = ROOT / "ZAC_zzx" / "results" / "three_way"

HDR = ["circuit", "q", "2q门", "轮数",
       "ZAC μs", "qmap μs", "zzx μs",
       "qmap/ZAC", "zzx/ZAC",
       "ZAC fid", "qmap fid", "zzx fid",
       "mv Z→q→z", "人次 Z→q→z", "qmap配置"]
NOTE = ("判分口径：三法同架构（ZAC repro）、同判分器（ZAC simulator 五项模型）；"
        "qmap 列为 ICCAD'25 路由感知编译（astar 优先，失败回落 agnostic，配置见末列），"
        "其调度按 compare_zac 同款顺序时间戳约定计价（job=2×15+√(d/0.00275)，"
        "1q 指令 52μs，rydberg 0.36μs），测量/栅栏操作不入编译；"
        "时长为顺序保守上界，zzx/ZAC 为真实判分时长。")


def fill_sheet(ws, rows):
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDEBF7")
    ws.append([NOTE])
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HDR))
    ws.append(HDR)
    for c in range(1, len(HDR) + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")

    rq, rz = [], []
    fids = {"ZAC fid": [], "qmap fid": [], "zzx fid": []}
    for r in rows:
        z, q, x = r.get("zac"), r.get("qmap"), r.get("zzx")
        q_ok = bool(q and q.get("ok"))
        z_ok = bool(z and z.get("duration"))
        x_ok = bool(x and x.get("duration"))
        row = [r["name"],
               (z or {}).get("qubits", (x or {}).get("qubits", (q or {}).get("qubits"))),
               (z or {}).get("rounds") and None or (r.get("gates2q") or (z or {}).get("gates2q")),
               (z or {}).get("rounds") or (x or {}).get("rounds") or (q or {}).get("rounds"),
               z_ok and z["duration"], q_ok and q["duration"], x_ok and x["duration"],
               z_ok and q_ok and round(q["duration"] / z["duration"], 3),
               z_ok and x_ok and round(x["duration"] / z["duration"], 3),
               z_ok and round(z["fidelity"], 4), q_ok and round(q["fidelity"], 4),
               x_ok and round(x["fidelity"], 4),
               f"{z.get('batches','—')}→{q_ok and q['batches'] or '—'}→{x_ok and x['batches'] or '—'}",
               f"{z.get('transfers','—')}→{q_ok and q['transfers'] or '—'}→{x_ok and x.get('transfers') or '—'}",
               q.get("config") if q else "—"]
        ws.append(row)
        if z_ok and q_ok:
            rq.append(q["duration"] / z["duration"])
        if z_ok and x_ok:
            rz.append(x["duration"] / z["duration"])
        for k, ok, src in (("ZAC fid", z_ok, z), ("qmap fid", q_ok, q), ("zzx fid", x_ok, x)):
            if ok:
                fids[k].append(src["fidelity"])

    n = len(rows)
    gq = math.exp(sum(map(math.log, rq)) / len(rq)) if rq else None
    gz = math.exp(sum(map(math.log, rz)) / len(rz)) if rz else None
    ws.append([])
    ws.append(["汇总",
               f"配对 qmap/ZAC={len(rq)}/{n}  zzx/ZAC={len(rz)}/{n}",
               f"geomean qmap/ZAC = {gq:.3f}" if gq else "qmap 无完整对",
               f"geomean zzx/ZAC = {gz:.3f}" if gz else "zzx 无完整对",
               "", "",
               f"平均保真度 ZAC {sum(fids['ZAC fid'])/max(1,len(fids['ZAC fid'])):.4f}"
               f"  qmap {sum(fids['qmap fid'])/max(1,len(fids['qmap fid'])):.4f}"
               f"  zzx {sum(fids['zzx fid'])/max(1,len(fids['zzx fid'])):.4f}"])
    for c in range(1, len(HDR) + 1):
        ws.cell(row=ws.max_row, column=c).font = bold
    widths = [24, 5, 7, 6, 11, 11, 11, 9, 9, 9, 9, 9, 14, 16, 11]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def main():
    wb = Workbook()
    s1 = json.load(open(OUT / "sheet1.json"))
    fill_sheet(wb.active, s1)
    wb.active.title = "Sheet1_hpca18"
    ws2 = wb.create_sheet("Sheet2_qmap_examples")
    fill_sheet(ws2, json.load(open(OUT / "sheet2.json")))
    path = OUT / "三方对比.xlsx"
    wb.save(path)
    print("saved", path)


if __name__ == "__main__":
    main()
