"""ICCAD 数据集（qmap examples 154）论文管线采集：mqt.qmap 3.2.0 × ra/rw 双配置。

产出 Table I 全指标：place_ms / route_ms / steps / rearr_ms（+ layers/max_gates）。
⚠ 单位：stats().placeTime/routeTime/totalTime 实测为 μs（2026-08-21 勘误），
此处统一 ÷1000 存真 ms；NavizEvaluator rearr 同为 μs，÷1000。

用法：.venv_qmap32/bin/python capture_iccad_v32.py     # 断点续跑
输出：experiments/results/qmap_iccad_v32.json
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
PY32 = str(ROOT / ".venv_qmap32/bin/python")
OUT = ROOT / "experiments/results/qmap_iccad_v32.json"
EXAMPLES = ROOT / "qmap-main/examples"

SCRIPT = r'''
import sys, json, time
sys.path.insert(0, "experiments")
from run_qmap import preprocess, make_compiler, ZAC_ARCH
from spec_convert import convert
from mqt.qmap.na.zoned import ZonedNeutralAtomArchitecture
from pathlib import Path
spec = convert(json.load(open(ZAC_ARCH)))
arch = ZonedNeutralAtomArchitecture.from_json_string(json.dumps(spec))
from naviz_eval import NavizEvaluator
ev = NavizEvaluator(spec)
recs = json.load(open(sys.argv[1]))     # [{"path":..., "name":...}, ...]
out = {}
if len(sys.argv) > 2 and Path(sys.argv[2]).exists():   # 断点续跑
    out = json.load(open(sys.argv[2]))
for k, rec in enumerate(recs):
    if rec["name"] in out:
        continue
    from qiskit import QuantumCircuit
    qc = preprocess(Path(rec["path"]))
    row = {"qubits": qc.num_qubits}
    for kind in ("agnostic", "astar"):
        c = make_compiler(kind, arch, "qasmbench")
        t0 = time.perf_counter()
        code = c.compile(qc)
        wall = time.perf_counter() - t0
        st = c.stats()
        code = "\n".join(l for l in code.splitlines() if not l.startswith("@+ u"))
        ev.reset()
        m = ev.evaluate(code)
        row[kind] = {"place_ms": round(st.get("placementTime", 0) / 1000, 3),
                     "route_ms": round(st.get("routingTime", 0) / 1000, 3),
                     "total_ms": round(st.get("totalTime", 0) / 1000, 3),
                     "steps": m["rearrangement_steps"],
                     "rearr_ms": round(m["rearrangement_duration"] / 1000, 2),
                     "layers": m["two_qubit_gate_layer"],
                     "max_gates": m["max_two_qubit_gates"]}
    out[rec["name"]] = row
    print(f'[{k+1}/{len(recs)}] {rec["name"]} ra={row["agnostic"]["steps"]}步 '
          f'rw={row["astar"]["steps"]}步 rw编译{row["astar"]["total_ms"]}ms', flush=True)
    json.dump(out, open(sys.argv[2], "w"), indent=1)
print("CAPTURE_DONE", flush=True)
'''


def main():
    recs = [{"name": f.stem, "path": str(f)} for f in sorted(EXAMPLES.glob("*.qasm"))]
    ROOT.joinpath("experiments/results").mkdir(exist_ok=True)
    tf = ROOT / "experiments/results/_iccad_capture_list.json"
    json.dump(recs, open(tf, "w"))
    r = subprocess.run([PY32, "-c", SCRIPT, str(tf), str(OUT)], cwd=str(ROOT))
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
