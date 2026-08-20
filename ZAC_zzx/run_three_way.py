"""三方对比表（ZAC / ICCAD-qmap / ZAC_zzx）驱动：跑 qmap 计分 + 汇总双 sheet xlsx。

Sheet1 = hpca 18 电路（ZAC=冻结真值, zzx=results/main, qmap=astar 优先超时回落 agnostic）
Sheet2 = qmap examples 全套（ZAC/zzx=results/qmap_suite 套件, qmap 同上策略：
        仅对 ≤300 门的电路尝试 astar——大电路的 A* 放置在大存储架构上
        会指数爆炸，早前实测 ising_n42 就要 500s+）
运行：.venv_qmap/bin/python ZAC_zzx/run_three_way.py  （末尾自动等后台套件 ALL_DONE）
产出：results/three_way/{sheet1,sheet2}.json → build_xlsx.py 生成 三方对比.xlsx
口径：qmap 列见 qmap_score.py 模块头（顺序时间戳 + 五项直算，保守上界）；
      ZAC/zzx 列零处理，直接读各自判分产物。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
ZZX = ROOT / "ZAC_zzx"
TRUTH = ROOT / "ZAC" / "result" / "zac" / "repro_fixed"
QMAP_PY = str(ROOT / ".venv_qmap" / "bin" / "python")
OUT = ZZX / "results" / "three_way"


def zair_metrics(code_path: Path):
    fid = json.load(open(code_path.parent.parent / "fidelity" / (code_path.name[:-10] + "_fidelity.json")))
    code = json.load(open(code_path))
    return {
        "duration": fid["cir_duration"], "fidelity": fid["cir_fidelity"],
        "batches": sum(1 for i in code["instructions"] if i["type"] == "rearrangeJob"),
        "transfers": sum(len(i.get("aod_qubits", [])) for i in code["instructions"]
                         if i["type"] == "rearrangeJob"),
        "rounds": sum(1 for i in code["instructions"] if i["type"] == "rydberg"),
        "qubits": len(code["instructions"][0]["init_locs"]),
    }


def qmap_score(qasm: Path, config: str, timeout: int):
    try:
        r = subprocess.run([QMAP_PY, str(ZZX / "qmap_score.py"), str(qasm), config],
                           capture_output=True, text=True, timeout=timeout)
        line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if line.startswith("{"):
            d = json.loads(line)
            d["ok"] = True
            return d
        return {"ok": False, "config": config, "err": (r.stderr or line)[-120:]}
    except (subprocess.TimeoutExpired, Exception) as e:
        return {"ok": False, "config": config, "err": f"{type(e).__name__}"}


def score_with_fallback(qasm: Path, astar_budget: int | None):
    if astar_budget:
        r = qmap_score(qasm, "astar", astar_budget)
        if r.get("ok"):
            return r
    r = qmap_score(qasm, "agnostic", 300)
    return r


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    # ---------------- Sheet 1：hpca 18 ----------------
    sheet1 = []
    for f in sorted((ZZX / "benchmark" / "hpca").glob("*.qasm")):
        name = f.stem
        try:
            z = zair_metrics(TRUTH / "code" / f"{name}_code.json")
        except FileNotFoundError:
            continue
        try:
            x = zair_metrics(ZZX / "results" / "main" / "code" / f"{name}_code.json")
        except FileNotFoundError:
            x = None
        q = score_with_fallback(f, astar_budget=600)
        sheet1.append({"name": name, "zac": z, "zzx": x, "qmap": q})
        m = (f"{q['duration']:>9.1f}us fid {q['fidelity']:.4f}" if q.get("ok")
             else f"失败[{q.get('config')}]{q.get('err','')[:40]}")
        print(f"[S1] {name:26s} qmap({q.get('config','-')}): {m}", flush=True)
    json.dump(sheet1, open(OUT / "sheet1.json", "w"), indent=1, ensure_ascii=False)

    # ---------------- 等后台套件收尾 ----------------
    log = "/tmp/qmap_suite.log"
    while True:
        txt = open(log).read() if Path(log).exists() else ""
        if "ALL_DONE" in txt:
            break
        print("…等后台 ZAC/zzx 套件收尾", flush=True)
        time.sleep(60)
    summary = json.load(open(ZZX / "results" / "qmap_suite" / "summary.json"))

    # ---------------- Sheet 2：qmap examples 全套 ----------------
    sheet2 = []
    for rec in summary:
        qasm = next((ROOT / "qmap-main" / "examples").glob(f"{rec['name']}.qasm"))
        astar_budget = 600 if rec["gates2q"] <= 300 else None
        q = score_with_fallback(qasm, astar_budget)
        sheet2.append({"name": rec["name"], "gates2q": rec["gates2q"],
                       "zac": rec.get("zac"), "zzx": rec.get("zzx"), "qmap": q})
        print(f"[S2] {rec['name']:32s} 2q={rec['gates2q']:6d} "
              f"qmap({q.get('config','-')}): {'ok' if q.get('ok') else q.get('err','')[:40]}",
              flush=True)
    json.dump(sheet2, open(OUT / "sheet2.json", "w"), indent=1, ensure_ascii=False)
    print("THREE_WAY_DONE", flush=True)


if __name__ == "__main__":
    main()
