"""四方法对比表生成器：ZAC原始 / ICCAD / 遗传不加前瞻 / 遗传加前瞻。

数据集：Sheet1 = ZAC 数据集（hpca 18）；Sheet2 = ICCAD 数据集（qmap examples 154）。
指标（每方法四列）：保真度、move 批次、move 时间（纯搬运执行）、编译时间（求出该解）。

方法与数据来源：
  ZAC原始      hpca = ZAC/result/zac/repro_fixed（冻结真值）；iccad = qmap_suite/zac
  ICCAD        results/fourway/qmap_{hpca,iccad}.json（astar 优先回落 agnostic）
  遗传(无前瞻)  γ0=0：hpca = results/nolook；iccad = qmap_suite/zzx_nolook
  遗传(前瞻)    γ0=0.5 主配置：hpca = results/main；iccad = qmap_suite/zzx

用法：.venv_qmap/bin/python fourway_table.py   # openpyxl 装在 .venv_qmap
输出：results/fourway/四方法对比.xlsx + 四方法对比.md
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
ZZX = ROOT / "ZAC_zzx"
TRUTH = ROOT / "ZAC" / "result" / "zac" / "repro_fixed"
OUT = ZZX / "results" / "fourway"

METHODS = [("zac", "ZAC原始"), ("qmap", "ICCAD"),
           ("nolook", "遗传(无前瞻)"), ("look", "遗传(前瞻)")]

NOTE = (
    "四方法×四指标。保真度=五项模型同判分（0.9997^1q × 0.995^2q × 0.999^(2×人次) × Π(1−idle/T)，"
    "同架构 ZAC repro）。move批=搬运班次数（ZAC/遗传=rearrangeJob 条数；ICCAD=load…store 作业数，"
    "口径近似，跨方法只作参考）。move时间=纯搬运执行时长，不含门时间（ZAC/遗传=Σ搬送批起止区间；"
    "ICCAD=Σ作业 2×15μs+√(d/0.00275)）。编译时间=电脑求出该解的耗时（ZAC/遗传=编译流水线 total；"
    "ICCAD=transpile+compile 实测，不含 python 启动）。遗传(前瞻)=γ0=0.5 主配置；遗传(无前瞻)=γ0=0"
    "（下次使用锚点项归零，其余设置全同）。ICCAD 配置=astar 优先、超时回落 agnostic。"
    "汇总行只统计四法数据齐全的电路（N 见行内标注）。"
)


# ---------- 指标提取 ----------

def zair(code_dir: Path, stem: str):
    """ZAC/遗传系产物 → 四指标（code/fidelity/time 三件套在 code_dir 的兄弟目录）。"""
    try:
        code = json.load(open(code_dir / f"{stem}_code.json"))
        fid = json.load(open(code_dir.parent / "fidelity" / f"{stem}_fidelity.json"))
        t = json.load(open(code_dir.parent / "time" / f"{stem}_time.json"))["total"]
    except FileNotFoundError:
        return None
    jobs = [i for i in code["instructions"] if i["type"] == "rearrangeJob"]
    return {"fidelity": fid["cir_fidelity"],
            "batches": len(jobs),
            "move_time": round(sum(i["end_time"] - i["begin_time"] for i in jobs), 1),
            "compile_s": t}


def mval(row: dict, key: str, field: str):
    """统一取值：row[key] 方法在该指标上的数（缺数据=None）。"""
    if key == "qmap":
        q = row.get("qmap")
        if not (q and q.get("ok")):
            return None
        if field == "compile_s":
            return q["compile_ms"] / 1000
        return q.get(field)
    m = row.get(key)
    return None if m is None else m.get(field)


def gm(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


# ---------- 数据装载 ----------

def build_rows(hpca: bool):
    rows = []
    if hpca:
        qmap_rows = {r["name"]: r["qmap"] for r in json.load(open(OUT / "qmap_hpca.json"))}
        for f in sorted((ZZX / "benchmark" / "hpca").glob("*.qasm")):
            stem = f.stem
            truth = zair(TRUTH / "code", stem)
            rows.append({
                "name": stem,
                "q": (truth or {}).get("qubits")
                     or len(json.load(open(TRUTH / "code" / f"{stem}_code.json"))
                            ["instructions"][0]["init_locs"]),
                "zac": truth,
                "qmap": qmap_rows.get(stem),
                "nolook": zair(ZZX / f"results/nolook{suffix}" / "code", stem),
                "look": zair(ZZX / f"results/main{suffix}" / "code", stem)})
    else:
        qmap_rows = {r["name"]: r["qmap"] for r in json.load(open(OUT / "qmap_iccad.json"))}
        for rec in json.load(open(ZZX / "results" / "qmap_suite" / "summary.json")):
            stem = rec["name"]
            rows.append({
                "name": stem, "q": rec.get("qubits"),
                "zac": zair(ZZX / "results" / "qmap_suite" / "zac" / "code", stem),
                "qmap": qmap_rows.get(stem),
                "nolook": zair(ZZX / f"results/qmap_suite/zzx_nolook{suffix}" / "code", stem),
                "look": zair(ZZX / f"results/qmap_suite/zzx{suffix}" / "code" if suffix else ZZX / "results" / "qmap_suite" / "zzx" / "code", stem)})
    return rows


def complete(row):
    return all(mval(row, key, "fidelity") is not None for key, _ in METHODS)


# ---------- 表格与摘要 ----------

FIELDS = [("fidelity", "保真度", "mean"),
          ("batches", "move批", "sum"),
          ("move_time", "move时间μs", "sum"),
          ("compile_s", "编译s", "sum")]


def fill_sheet(ws, rows, title):
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDEBF7")
    ws.append([f"{title}　{NOTE}"])
    ws["A1"].font = Font(italic=True, size=9, color="666666")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=2 + 4 * len(METHODS))
    hdr = ["circuit", "q"]
    for _, label in METHODS:
        hdr += [f"{label}\n保真度", f"{label}\nmove批", f"{label}\nmove时间μs", f"{label}\n编译s"]
    ws.append(hdr)
    for c in range(1, len(hdr) + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(wrap_text=True, horizontal="center")
    for r in rows:
        line = [r["name"], r["q"]]
        for key, _ in METHODS:
            line += [mval(r, key, "fidelity"), mval(r, key, "batches"),
                     mval(r, key, "move_time"),
                     round(mval(r, key, "compile_s"), 2) if mval(r, key, "compile_s") else None]
        ws.append(line)
    full = [r for r in rows if complete(r)]
    ws.append([])
    ws.append([f"汇总（四法齐全 N={len(full)}/{len(rows)}）"] + [""] * (len(hdr) - 1))
    ws.cell(row=ws.max_row, column=1).font = bold
    for field, label, how in FIELDS:
        line = [label, ""]
        for key, _ in METHODS:
            vs = [v for v in (mval(r, key, field) for r in full) if v is not None]
            cell = (round(sum(vs) / len(vs), 4) if how == "mean" else round(sum(vs), 1)) \
                if vs else None
            line.append(cell)
            line += ["", "", ""]
        ws.append(line[:len(hdr)])
    for field in ("batches", "move_time", "compile_s"):
        line = [f"geomean /ZAC：{field}", ""]
        for key, _ in METHODS:
            if key == "zac":
                line += [1.0, "", "", ""]
                continue
            g = gm([mval(r, key, field) / mval(r, "zac", field) for r in full
                    if mval(r, key, field) and mval(r, "zac", field)])
            line += [round(g, 3) if g else None, "", "", ""]
        ws.append(line[:len(hdr)])
    for c, w in zip(range(1, len(hdr) + 1), [30, 5] + [11, 9, 13, 9] * len(METHODS)):
        ws.column_dimensions[get_column_letter(c)].width = w


def md_summary(rows, title):
    full = [r for r in rows if complete(r)]
    out = [f"## {title}（四法齐全 N={len(full)}/{len(rows)}）", "",
           "| 指标 | " + " | ".join(l for _, l in METHODS) + " |",
           "|" + "---|" * (len(METHODS) + 1)]
    for field, label, how in FIELDS:
        cells = []
        for key, _ in METHODS:
            vs = [v for v in (mval(r, key, field) for r in full) if v is not None]
            cells.append(f"{sum(vs)/len(vs):.4f}" if how == "mean" else f"{sum(vs):.1f}")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    for field in ("batches", "move_time", "compile_s"):
        cells = ["1.000"]
        for key, _ in METHODS[1:]:
            g = gm([mval(r, key, field) / mval(r, "zac", field) for r in full
                    if mval(r, key, field) and mval(r, "zac", field)])
            cells.append(f"{g:.3f}" if g else "—")
        out.append(f"| geomean {field}/ZAC | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main():
    # 变体选择：默认 = SA 初始化的 zzx；传 "ga" = GA 初始化（gainit.py 替换 SA 后）
    ga = len(sys.argv) > 1 and sys.argv[1] == "ga"
    if ga:
        global ZZX
        suffix = "_ga"
        out_xlsx = OUT / "四方法对比_ga.xlsx"
        out_md = OUT / "四方法对比_ga.md"
    else:
        suffix = ""
        out_xlsx = OUT / "四方法对比.xlsx"
        out_md = OUT / "四方法对比.md"
    rows1 = build_rows(hpca=True)
    rows2 = build_rows(hpca=False)
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "ZAC数据集18"
    fill_sheet(ws1, rows1, "ZAC 数据集（hpca 18 电路）")
    fill_sheet(wb.create_sheet("ICCAD数据集"), rows2, "ICCAD 数据集（qmap examples）")
    wb.save(out_xlsx)
    with open(out_md, "w") as f:
        f.write("# 四方法对比（ZAC原始 / ICCAD / 遗传无前瞻 / 遗传前瞻）\n\n")
        f.write(md_summary(rows1, "ZAC 数据集（hpca 18）") + "\n")
        f.write(md_summary(rows2, "ICCAD 数据集（qmap examples）"))
    print(f"✅ {out_xlsx}")
    print(md_summary(rows1, "ZAC数据集"))
    print(md_summary(rows2, "ICCAD数据集"))


if __name__ == "__main__":
    main()
