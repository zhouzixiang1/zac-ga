"""仿 Physics-Layer-Compiler examples/compare_zac.py 的表样：
ZAC vs ZAC_zzx 逐电路对比（q / 时长 / ratio / 保真度 / mv 搬运批 / pulse 轮数 / 1qS 单比特批）。

用法：ZAC/.venv/bin/python ZAC_zzx/compare_table.py [hpca|qmap]
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRUTH = ROOT.parent / "ZAC" / "result" / "zac" / "repro_fixed"
HPCA_MAIN = ROOT / "results" / "main"


def load(code_dir: Path, name: str):
    fid = json.load(open(code_dir.parent / "fidelity" / f"{name}_fidelity.json"))
    code = json.load(open(code_dir / f"{name}_code.json"))
    mv = sum(1 for i in code["instructions"] if i["type"] == "rearrangeJob")
    pulse = sum(1 for i in code["instructions"] if i["type"] == "rydberg")
    q1 = sum(1 for i in code["instructions"] if i["type"] == "1qGate")
    q = len(code["instructions"][0]["init_locs"])
    return fid["cir_duration"], fid["cir_fidelity"], mv, pulse, q1, q


def table(rows, title):
    print(title)
    print("circuit              q   ZAC us     zzx us   ratio   ZAC fid  zzx fid   mv(Z→z)  pulse  1qS")
    print("-" * 100)
    ratios = []
    for name, z, x in rows:
        dz, fz, mvz, pz, q1z, q = z
        dx, fx, mvx, px, q1x, _ = x
        r = dx / dz
        ratios.append(r)
        print(f"{name:20s}{q:3d} {dz:10.1f} {dx:10.1f}   {r:5.2f}   {fz:7.4f}  {fx:7.4f}"
              f"   {mvz:3d}→{mvx:<3d}  {pz:4d}  {q1z:3d}")
    g = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
    fz_mean = sum(z[1] for _, z, _ in rows) / len(rows)
    fx_mean = sum(x[1] for _, _, x in rows) / len(rows)
    worst = sorted(rows, key=lambda t: t[2][0] / t[1][0])[-5:]
    print("-" * 100)
    print(f"ZAC_zzx: n {len(rows)}  geomean zzx/ZAC time ratio {g:.3f}   "
          f"mean fidelity ZAC {fz_mean:.4f}  zzx {fx_mean:.4f}")
    print("worst ratios: " + ", ".join(f"{n} {x[0]/z[0]:.2f}x" for n, z, x in worst))
    return g


def hpca():
    rows = []
    for f in sorted((TRUTH / "fidelity").glob("*_fidelity.json")):
        name = f.name[:-len("_fidelity.json")]
        try:
            z = load(TRUTH / "code", name)
            x = load(HPCA_MAIN / "code", name)
        except FileNotFoundError:
            continue
        rows.append((name, z, x))
    return table(rows, "ZAC config: qasm_sa_1000_reuse   vs   ZAC_zzx(stay+ga+coloring)   "
                       "[cost model: ZAC simulator]\n")


def qmap():
    summary = json.load(open(ROOT / "results" / "qmap_suite" / "summary.json"))
    rows = []
    for rec in summary:
        if not (rec.get("zac", {}).get("ok") and rec.get("zzx", {}).get("ok")):
            continue
        z, x = rec["zac"], rec["zzx"]
        rows.append((rec["name"],
                     (z["duration"], z["fidelity"], z["batches"], z["rounds"], z.get("transfers"), rec["qubits"]),
                     (x["duration"], x["fidelity"], x["batches"], x["rounds"], x.get("transfers"), rec["qubits"])))
    out = ROOT / "results" / "qmap_suite" / "comparison_table.txt"
    with open(out, "w") as f:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            table(rows, f"qmap examples 套件（{len(rows)} 个配对案例，同架构同判分）\n")
        f.write(buf.getvalue())
    print(f"[qmap] {len(rows)} 对已写入 {out.relative_to(ROOT)}（套件仍在跑，完成后重跑本脚本刷新）")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "hpca"
    if which == "hpca":
        hpca()
    else:
        qmap()
