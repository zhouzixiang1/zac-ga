"""M5 结果表生成器：主表 + 验收清单 + 前瞻标定 + 种子稳健性 + 决策分布。

用法：ZAC/.venv/bin/python ZAC_zzx/make_table.py
（读取 results/{main,a1,b_rerun,a_rerun,gamma_*,lumped,seed_*} 与 ZAC 冻结真值
  ZAC/result/zac/repro_fixed，输出 results/ 下三个 md：
    comparison_table.md  18 电路 × 四系统主表（ZAC_zzx/A1/B重跑/A重跑）
    acceptance.md        验收清单：回归/验证器/时长门槛/编译帽/保真度分解/决策分布
    calibration.md       γ0 前瞻标定（0/0.25/0.5 + lumped）+ 3 种子极差
  分母口径：全部 duration ratio 以冻结 ZAC 真值为分母；B/A 重跑是同会话
  的 ZAC_new 对照（控制变量：同 venv、同架构、同判分器）。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRUTH = ROOT.parent / "ZAC" / "result" / "zac" / "repro_fixed"
ZNEW_B = ROOT.parent / "ZAC_new" / "results" / "repro_penalty"          # ZAC_new-B 存档（跨会话回归参照）
SERIAL = {"bv_n14_transpiled", "bv_n19_transpiled", "bv_n30_transpiled", "bv_n70_transpiled",
          "cat_n22_transpiled", "cat_n35_transpiled", "ghz_n23", "ghz_n40_transpiled",
          "ghz_n78_transpiled", "wstate_n27_transpiled"}


def load_fid(d: Path, name: str):
    p = d / f"fidelity/{name}_fidelity.json"
    return json.load(open(p)) if p.exists() else None


def load_time(d: Path, name: str):
    p = d / f"time/{name}_time.json"
    return json.load(open(p)) if p.exists() else None


def batches(d: Path, name: str):
    p = d / f"time/{name}_batch_ledger.json"
    if not p.exists():
        return None, None
    led = json.load(open(p))
    jobs = sum(e["batches"] for e in led.get("route_log", []))
    dec = led.get("placer_preview", [])
    return jobs, dec


def geomean(xs):
    xs = [x for x in xs if x]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else float("nan")


def circuit_names(d: Path):
    return sorted(p.name[:-len("_fidelity.json")] for p in (d / "fidelity").glob("*_fidelity.json"))


def main():
    main_d = ROOT / "results/main"
    names = circuit_names(main_d)
    dirs = {"ZAC_zzx(主=stay+ga)": "results/main", "A1(stay+match)": "results/a1",
            "B重跑(batch+penalty)": "results/b_rerun", "A重跑(batch+ga)": "results/a_rerun"}

    rows = {}
    for n in names:
        z = load_fid(TRUTH, n)
        if not z or z["cir_duration"] <= 0:
            continue
        row = {"truth": z["cir_duration"], "fid": {}}
        for label, rel in dirs.items():
            f = load_fid(ROOT / rel, n)
            row[label] = f["cir_duration"] / z["cir_duration"] if f else None
            if f:
                row["fid"][label] = f["cir_fidelity"]
        jb, dec = batches(main_d, n)
        row["main_batches"] = jb
        b_jobs, _ = batches(ROOT / "results/b_rerun", n)
        row["b_batches"] = b_jobs
        row["dec"] = dec
        t = load_time(main_d, n)
        ta = load_time(ROOT / "results/a_rerun", n)
        row["compile"] = (t or {}).get("total")
        row["a_compile"] = (ta or {}).get("total")
        rows[n] = row

    # ---------------------------------------------------------------- 主表
    lines = ["# ZAC_zzx 主表：18 电路 × 四系统（分母 = 冻结 ZAC 真值）", "",
             "| 电路 | " + " | ".join(dirs) + " | ZAC时长μs | 批数 B→主 | 主编译s |",
             "|---" * (len(dirs) + 4) + "|"]
    for n, r in rows.items():
        cells = " | ".join(f"{r.get(l, float('nan')):.3f}" if r.get(l) else "—" for l in dirs)
        bb = r["b_batches"] if r["b_batches"] is not None else "—"
        lines.append(f"| {n} | {cells} | {r['truth']:.0f} | {bb}→{r['main_batches']} | "
                     f"{r['compile']:.1f} |" if r["compile"] else f"| {n} | {cells} | {r['truth']:.0f} | {bb}→{r['main_batches']} | — |")
    for label in dirs:
        g = geomean([r.get(label) for r in rows.values()])
        lines.append(f"\n**{label} geomean = {g:.3f}**")
    serial_g = geomean([r.get("ZAC_zzx(主=stay+ga)") for n, r in rows.items() if n in SERIAL])
    lines.append(f"\n**串行族 geomean（bv×4, cat×2, ghz×3, wstate）= {serial_g:.3f}**")
    (ROOT / "results/comparison_table.md").write_text("\n".join(lines))

    # ---------------------------------------------------------------- 验收清单
    acc = ["# 验收清单", ""]
    # 1) 回归：B 重跑 vs ZAC_new 存档（规范化指令流）
    same = 0
    tot = 0
    for n in names:
        a = ROOT / f"results/b_rerun/code/{n}_code.json"
        b = ZNEW_B / f"code/{n}_code.json"
        if not (a.exists() and b.exists()):
            continue
        tot += 1
        ia = [(i.get("type"), i.get("id")) for i in json.load(open(a))["instructions"]]
        ib = [(i.get("type"), i.get("id")) for i in json.load(open(b))["instructions"]]
        same += (ia == ib)
    acc.append(f"1. 回归：B重跑 ≡ ZAC_new 存档（规范化流）：{same}/{tot} 条完全一致"
               f"（历史已知 13/18 与真值同构，其余为着色路由结构性差异）")
    # 2) 验证器
    ok = 0
    for n in names:
        p = main_d / f"code/{n}_code.json"
        q = next((ROOT / "benchmark/hpca").glob(f"{n}.qasm"))
        r = subprocess.run([sys.executable, str(ROOT / "verify_batches.py"), str(p),
                            f"--qasm={q}"], capture_output=True, text=True)
        ok += (r.returncode == 0)
    acc.append(f"2. 验证器 8 查（含 --qasm 语义查）：{ok}/{len(names)} 全绿")
    # 3) 时长门槛
    main_g = geomean([r.get("ZAC_zzx(主=stay+ga)") for r in rows.values()])
    worst = max(rows.items(), key=lambda kv: kv[1].get("ZAC_zzx(主=stay+ga)") or 0)
    acc.append(f"3. 时长：geomean={main_g:.3f}（门槛≤0.92 {'✓' if main_g <= 0.92 else '✗'} /"
               f" 目标≤0.85 {'✓' if main_g <= 0.85 else '✗'}）；串行族={serial_g:.3f}（≤0.85"
               f" {'✓' if serial_g <= 0.85 else '✗'}）")
    acc.append(f"   单电路最差：{worst[0]}={worst[1]['ZAC_zzx(主=stay+ga)']:.3f}（≤1.05 门槛）")
    over = [n for n, r in rows.items()
            if r["main_batches"] is not None and r["b_batches"] is not None
            and isinstance(r["b_batches"], int) and r["b_batches"] > 0
            and r["main_batches"] > 1.25 * r["b_batches"]]
    acc.append(f"   批数哨兵（≤1.25×B）：{'无超限 ✓' if not over else '超限 ' + str(over)}")
    # 4) 编译时间
    total_compile = sum(r["compile"] or 0 for r in rows.values())
    acc.append(f"4. 编译：全套 Σ={total_compile:.0f}s（≤900s {'✓' if total_compile <= 900 else '✗'}）")
    viol = [n for n, r in rows.items() if r["compile"] and r["a_compile"]
            and r["compile"] > 3 * r["a_compile"]]
    acc.append(f"   单电路 ≤3×A重跑：{'全部满足 ✓' if not viol else '超限 ' + str(viol)}")
    # 5) 保真度（只报分解）
    lines5 = []
    for n in ("ising_n42", "qft_n18_transpiled", "ghz_n78_transpiled"):
        r = rows.get(n)
        if r:
            lines5.append(f"   {n}: 主={r['fid'].get('ZAC_zzx(主=stay+ga)', 0):.4f} "
                          f"B={r['fid'].get('B重跑(batch+penalty)', 0):.4f}")
    acc.append("5. 保真度（只报不设线；transfer/coherence 是时长单调函数）：" + "\n".join(lines5))
    # 6) 决策分布
    stay = ret = 0
    for r in rows.values():
        for d in (r["dec"] or []):
            stay += d.get("stay", 0)
            ret += d.get("return", 0) + d.get("forced_e2", 0) + d.get("capacity", 0)
    acc.append(f"6. 决策分布：STAY={stay} / RETURN={ret}"
               f"（留座占比 {stay / max(1, stay + ret) * 100:.1f}%）")
    (ROOT / "results/acceptance.md").write_text("\n".join(acc))

    # ---------------------------------------------------------------- 标定与种子
    extra = ["# 前瞻标定（γ0）与种子稳健性（3 哨兵）", "",
             "| 电路 | γ0=0 | γ0=0.25 | γ0=0.5(默认) | lumped(A3') | seed0 | seed1 | seed2 |",
             "|---|---|---|---|---|---|---|---|"]
    sent = ["qft_n29_transpiled", "ising_n98_transpiled", "ghz_n78_transpiled"]
    for n in sent:
        z = load_fid(TRUTH, n)
        cells = []
        for rel in ("results/gamma_0", "results/gamma_025", "results/main",
                    "results/lumped", "results/main", "results/seed_1", "results/seed_2"):
            f = load_fid(ROOT / rel, n)
            cells.append(f"{f['cir_duration'] / z['cir_duration']:.3f}" if f else "—")
        extra.append(f"| {n} | " + " | ".join(cells) + " |")
    for n in sent:
        vals = []
        for rel in ("results/main", "results/seed_1", "results/seed_2"):
            f = load_fid(ROOT / rel, n)
            z = load_fid(TRUTH, n)
            if f:
                vals.append(f["cir_duration"] / z["cir_duration"])
        if vals:
            rng = max(vals) - min(vals)
            extra.append(f"\n- {n}: seed 极差 = {rng:.3f}（验收 ≤0.03 {'✓' if rng <= 0.03 else '✗'}）"
                         f"  均值 {sum(vals) / len(vals):.3f}")
    (ROOT / "results/calibration.md").write_text("\n".join(extra))
    print("\n".join(acc))
    print("\n表格已写入 results/{comparison_table,acceptance,calibration}.md")


if __name__ == "__main__":
    main()
