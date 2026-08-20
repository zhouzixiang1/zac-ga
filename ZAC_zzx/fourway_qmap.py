"""四方法对比 · qmap 重计分：给 ICCAD 方法补 move_time（纯搬运时长）与编译时间。

复用 three_way 的同款策略（astar 优先、超时回落 agnostic），但调用扩展后的
qmap_score.py（新增 move_time / compile_ms 字段）。逐电路落盘，断点续跑。

用法：.venv_qmap/bin/python fourway_qmap.py            # 两个数据集全跑
      .venv_qmap/bin/python fourway_qmap.py hpca        # 只跑 ZAC 数据集
输出：results/fourway/qmap_{hpca,iccad}.json
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
ZZX = ROOT / "ZAC_zzx"
QMAP_PY = str(ROOT / ".venv_qmap" / "bin" / "python")
OUT = ZZX / "results" / "fourway"


def qmap_score(qasm: Path, config: str, timeout: int):
    t0 = time.time()
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
    return qmap_score(qasm, "agnostic", 300)


def run_dataset(tag: str, cases: list, resume_path: Path):
    done = {}
    if resume_path.exists():                       # 断点续跑
        done = {r["name"]: r for r in json.load(open(resume_path))}
    rows = []
    for name, qasm, astar_budget in cases:
        if name in done and done[name].get("qmap", {}).get("ok"):
            rows.append(done[name])
            print(f"[{tag}] {name:32s} 已有，跳过", flush=True)
            continue
        q = score_with_fallback(qasm, astar_budget)
        rows.append({"name": name, "qmap": q})
        m = (f"{q['duration']:>9.1f}us move={q['move_time']:>9.1f} "
             f"编译={q['compile_ms']/1000:>7.1f}s 批={q['batches']}" if q.get("ok")
             else f"失败[{q.get('config')}]{q.get('err','')[:40]}")
        print(f"[{tag}] {name:32s} qmap({q.get('config','-')}): {m}", flush=True)
        json.dump(rows, open(resume_path, "w"), indent=1, ensure_ascii=False)
    json.dump(rows, open(resume_path, "w"), indent=1, ensure_ascii=False)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    which = sys.argv[1] if len(sys.argv) > 1 else "all"

    # ---- ZAC 数据集：hpca 18（astar 600s → agnostic 300s，同 three_way S1）----
    if which in ("all", "hpca"):
        cases = [(f.stem, f, 600) for f in sorted((ZZX / "benchmark" / "hpca").glob("*.qasm"))]
        run_dataset("hpca", cases, OUT / "qmap_hpca.json")

    # ---- ICCAD 数据集：qmap examples（astar 仅 2q≤300，同 three_way S2）----
    if which in ("all", "iccad"):
        summary = json.load(open(ZZX / "results" / "qmap_suite" / "summary.json"))
        cases = [(rec["name"],
                  next((ROOT / "qmap-main" / "examples").glob(f"{rec['name']}.qasm")),
                  600 if rec["gates2q"] <= 300 else None)
                 for rec in summary]
        run_dataset("iccad", cases, OUT / "qmap_iccad.json")
    print("FOURWAY_QMAP_DONE", flush=True)


if __name__ == "__main__":
    main()
