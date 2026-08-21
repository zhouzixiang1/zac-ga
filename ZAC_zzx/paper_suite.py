"""新电路·论文程序三方套件：一条命令跑 ZAC 原程序 + ICCAD 论文管线 + 遗传(硬层)。

口径（paper-programs-only 规则）：
  ZAC   = HPCA'25 原程序（ZAC/run.py + repro_paper.json 同款配置：
          full_architecture + maximalis_sort + 自带判分器）
  ICCAD = 论文复现管线（opt3/u1-u2 转译 + 论文参数 + NavizEvaluator，
          experiments/run_qmap.py 的 preprocess/make_compiler）
  遗传  = ZAC_zzx（GA 初始化 + 鬼点硬保证层 + 鬼点罚前瞻）

用法：
  ZAC/.venv/bin/python ZAC_zzx/paper_suite.py --qasm <目录或文件...> --tag newsuite
输出：ZAC_zzx/results/<tag>/{zac,zzx}/… + iccad.json + 对比表.md
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/Users/zhouzixiang/Desktop/zac")
ZAC = ROOT / "ZAC"
ZZX = ROOT / "ZAC_zzx"
VENV = ZAC / ".venv/bin/python"
QMAP_VENV = ROOT / ".venv_qmap/bin/python"


def collect_qasms(paths):
    out = []
    for p in paths:
        p = Path(p).resolve()          # 绝对路径：三个引擎 cwd 各不相同
        if p.is_dir():
            out += sorted(str(x.resolve()) for x in p.glob("*.qasm"))
        elif p.is_file():
            out.append(str(p))
    return out


def run_zac(qasms, outdir):
    """ZAC 原程序：repro_paper.json 同款配置，仅换电路与输出目录。"""
    setting = {
        "qasm_list": qasms,
        "zac_setting": [{
            "arch_spec": "hardware_spec/full_architecture.json",
            "dependency": True,
            "dir": str(outdir / "zac") + "/",
            "routing_strategy": "maximalis_sort",
            "scheduling": "asap",
            "trivial_placement": False,
            "dynamic_placement": True,
            "use_window": True,
            "window_size": 1000,
            "reuse": True,
            "use_verifier": True,
        }],
        "simulation": True,
        "animation": False,
    }
    f = outdir / "zac_setting.json"
    json.dump(setting, open(f, "w"), indent=1)
    subprocess.run([VENV, "run.py", str(f)], cwd=ZAC, check=True,
                   capture_output=True)


def run_iccad(qasms, outdir):
    """ICCAD 论文管线：与 run_qmap.py 完全同款（预处理/参数/评测器）。"""
    script = r'''
import sys, json
sys.path.insert(0, "experiments")
from run_qmap import preprocess, make_compiler, ZAC_ARCH, NavizEvaluator
from spec_convert import convert
from mqt.qmap.na.zoned import ZonedNeutralAtomArchitecture
spec = convert(json.load(open(ZAC_ARCH)))
arch = ZonedNeutralAtomArchitecture.from_json_string(json.dumps(spec))
ev = NavizEvaluator(spec)
out = []
for path in json.load(open(sys.argv[1])):
    from qiskit import QuantumCircuit
    qc = preprocess(__import__("pathlib").Path(path))
    for kind in ("agnostic", "astar"):
        c = make_compiler(kind, arch, "qasmbench")
        code = c.compile(qc)
        stats = c.stats()
        code = "\n".join(l for l in code.splitlines() if not l.startswith("@+ u"))
        ev.reset()
        m = ev.evaluate(code)
        out.append({"qasm": path, "config": kind, "steps": m["rearrangement_steps"],
                    "rearr_ms": round(m["rearrangement_duration"], 2),
                    "layers": m["two_qubit_gate_layer"],
                    "max_gates": m["max_two_qubit_gates"],
                    "total_ms": stats.get("totalTime")})
        print(path.split("/")[-1], kind, m["rearrangement_steps"], flush=True)
json.dump(out, open(sys.argv[2], "w"), indent=1)
'''
    fin, fout = outdir / "qasms.json", outdir / "iccad.json"
    json.dump(qasms, open(fin, "w"))
    subprocess.run([QMAP_VENV, "-c", script, str(fin), str(fout)], cwd=ROOT,
                   check=True)


def run_zzx(qasms, outdir):
    """遗传（我们）：GA 初始化 + 硬保证层 + 鬼点罚（前瞻席配方）。"""
    setting = {
        "qasm_list": qasms,
        "zac_setting": [{
            "arch_spec": "hardware_spec/zac_arch_repro.json",
            "dependency": True, "routing_strategy": "coloring", "scheduling": "asap",
            "trivial_placement": False, "dynamic_placement": True,
            "use_window": True, "window_size": 1000, "reuse": True,
            "use_verifier": True, "seed": 0,
            "placer": "resident", "engine": "ga", "init_engine": "ga",
            "w_ghost": 1.0, "w_ord": 0.0,
            "dir": str(outdir / "zzx") + "/",
        }],
        "simulation": True, "animation": False,
    }
    f = outdir / "zzx_setting.json"
    json.dump(setting, open(f, "w"), indent=1)
    subprocess.run([VENV, "run.py", str(f)], cwd=ZZX, check=True,
                   capture_output=True)


def summarize(outdir):
    sys.path.insert(0, str(ZZX))
    from verify_batches import verify
    lines = ["# 新电路·论文程序三方对比\n",
             "| circuit | ZAC Steps | ZAC 保真度 | ICCAD Steps(agn/ast) | "
             "ICCAD rearr ms | 遗传 Steps | 遗传 保真度 | 遗传 鬼点 |",
             "|---|---|---|---|---|---|---|---|"]
    iccad = {}
    for r in json.load(open(outdir / "iccad.json")):
        name = Path(r["qasm"]).stem.replace("_transpiled", "")
        iccad.setdefault(name, {})[r["config"]] = r
    for cp in sorted((outdir / "zac" / "code").glob("*_code.json")):
        name = cp.stem.replace("_code", "").replace("_transpiled", "")
        code = json.load(open(cp))
        zsteps = sum(1 for i in code["instructions"]
                     if i["type"] == "rearrangeJob")
        raw = cp.stem.replace("_code", "")
        zfid = json.load(open(outdir / "zac" / "fidelity" / f"{raw}_fidelity.json"))
        ic = iccad.get(name, {})
        ag = ic.get("agnostic", {})
        asr = ic.get("astar", {})
        zp = outdir / "zzx" / "code" / cp.name
        if zp.exists():
            zc = json.load(open(zp))
            xsteps = sum(1 for i in zc["instructions"]
                         if i["type"] == "rearrangeJob")
            raw2 = zp.stem.replace("_code", "")
            xfid = json.load(open(outdir / "zzx" / "fidelity" /
                                  f"{raw2}_fidelity.json"))
            ghost = verify(zp, None)["stats"]["ghost_hits"]
            xcell = f"{xsteps} | {xfid['cir_fidelity']:.4f} | {ghost}"
        else:
            xcell = "— | — | —"
        lines.append(
            f"| {name} | {zsteps} | {zfid['cir_fidelity']:.4f} | "
            f"{ag.get('steps', '—')}/{asr.get('steps', '—')} | "
            f"{asr.get('rearr_ms', '—')} | {xcell} |")
    f = outdir / "对比表.md"
    open(f, "w").write("\n".join(lines) + "\n")
    print(f"\n== {f}")
    print("\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--qasm", nargs="+", required=True)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()
    qasms = collect_qasms(a.qasm)
    assert qasms, "没有找到 qasm"
    outdir = ZZX / "results" / a.tag
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"[1/3] ZAC 原程序（{len(qasms)} 电路）…")
    run_zac(qasms, outdir)
    print("[2/3] ICCAD 论文管线…")
    run_iccad(qasms, outdir)
    print("[3/3] 遗传（硬层）…")
    run_zzx(qasms, outdir)
    summarize(outdir)
