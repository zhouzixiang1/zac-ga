"""qmap 案例全套对比：ZAC 原版 vs ZAC_zzx，同一架构、同一判分。

案例源：qmap-main/examples/*.qasm（RevLib 等 139 个）。
规模横跨 5 ~ 224k 个 2q 门——按规模分档限时（ZAC 框架在十万门级本身
就不可编译，探针如实记录超时）：
    小型  2q ≤ 300      → 每配置 300s
    中型  300 < 2q ≤1500 → 每配置 600s
    超大  2q > 1500      → 每配置 60s 探针

输出：results/qmap_suite/summary.json + 控制台进度。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QMAP = ROOT.parent / "qmap-main" / "examples"
PY = str(ROOT.parent / "ZAC" / ".venv" / "bin" / "python")

CONFIGS = {
    "zac": {  # ZAC 原版最佳配置（= 冻结真值同款：SA 初始 + maximalis_sort + 复用）
        "placer": "zac", "routing_strategy": "maximalis_sort",
        "trivial_placement": False, "dynamic_placement": True,
        "use_window": True, "window_size": 1000, "reuse": True,
    },
    "zzx": {  # ZAC_zzx 主配置（驻留 + GA 分相位着色 + 着色路由 + γ0 前瞻锚点）
        "placer": "resident", "engine": "ga", "routing_strategy": "coloring",
        "trivial_placement": False, "dynamic_placement": True,
        "use_window": True, "window_size": 1000, "reuse": True,
        "seed": 0, "w_batch": 1.57, "gamma0": 0.5,
    },
    "zzx_nolook": {  # 同主配置但 γ0=0：前瞻锚点项全部归零（不加前瞻消融）
        "placer": "resident", "engine": "ga", "routing_strategy": "coloring",
        "trivial_placement": False, "dynamic_placement": True,
        "use_window": True, "window_size": 1000, "reuse": True,
        "seed": 0, "w_batch": 1.57, "gamma0": 0.0,
    },
    "zzx_ga": {  # 主配置 + GA 初始布局（换掉 SA）
        "placer": "resident", "engine": "ga", "routing_strategy": "coloring",
        "trivial_placement": False, "dynamic_placement": True,
        "use_window": True, "window_size": 1000, "reuse": True,
        "seed": 0, "w_batch": 1.57, "gamma0": 0.5, "init_engine": "ga",
    },
    "zzx_nolook_ga": {  # 无前瞻 + GA 初始布局
        "placer": "resident", "engine": "ga", "routing_strategy": "coloring",
        "trivial_placement": False, "dynamic_placement": True,
        "use_window": True, "window_size": 1000, "reuse": True,
        "seed": 0, "w_batch": 1.57, "gamma0": 0.0, "init_engine": "ga",
    },
    "zzx_hard": {  # 前瞻席（鬼点罚）+ GA 初始 + 硬保证层（= main_ga 同款）
        "placer": "resident", "engine": "ga", "routing_strategy": "coloring",
        "trivial_placement": False, "dynamic_placement": True,
        "use_window": True, "window_size": 1000, "reuse": True,
        "seed": 0, "w_batch": 1.57, "init_engine": "ga",
        "w_ghost": 1.0, "w_ord": 0.0,
    },
    "zzx_hard_nl": {  # 无前瞻席 + GA 初始 + 硬保证层（= nolook_ga 同款：w_ghost=0）
        "placer": "resident", "engine": "ga", "routing_strategy": "coloring",
        "trivial_placement": False, "dynamic_placement": True,
        "use_window": True, "window_size": 1000, "reuse": True,
        "seed": 0, "w_batch": 1.57, "init_engine": "ga",
        "w_ghost": 0.0, "w_ord": 0.0,
    },
}


def tier(n2: int) -> int:
    if n2 <= 300:
        return 300
    if n2 <= 1500:
        return 600
    return 60


def main():
    tags = []
    if "--tags" in sys.argv:                # 只跑指定配置（如 --tags zzx_nolook）
        tags = sys.argv[sys.argv.index("--tags") + 1].split(",")
    from qiskit import QuantumCircuit   # 探尺寸（用 qmap venv 外的 ZAC venv 也有 qiskit）
    cases = []
    for f in sorted(QMAP.glob("*.qasm")):
        try:
            qc = QuantumCircuit.from_qasm_file(str(f))
            n2 = sum(1 for inst in qc.data if len(inst.qubits) == 2)
            cases.append((f, qc.num_qubits, n2))
        except Exception as e:
            cases.append((f, -1, -1))
    cases.sort(key=lambda t: (t[2] < 0, t[2]))
    print(f"共 {len(cases)} 个案例", flush=True)

    out_dir = ROOT / "results" / "qmap_suite"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for idx, (f, nq, n2) in enumerate(cases):
        rec = {"name": f.stem, "qubits": nq, "gates2q": n2, "tier": tier(n2)}
        for tag, cfg in CONFIGS.items():
            if tags and tag not in tags:
                continue
            tmo = tier(n2) if n2 >= 0 else 30
            d = {"qasm_list": [str(f)], "zac_setting": [dict(
                    cfg, dependency=True, scheduling="asap", use_verifier=True,
                    dir=f"results/qmap_suite/{tag}/", arch_spec="hardware_spec/zac_arch_repro.json")],
                 "simulation": True, "animation": False}
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
                json.dump(d, tf)
                cfg_path = tf.name
            t0 = time.time()
            try:
                r = subprocess.run([PY, "run.py", cfg_path], cwd=str(ROOT),
                                   capture_output=True, text=True, timeout=tmo)
                ok = "Traceback" not in r.stderr + r.stdout
                rec[tag] = {"ok": ok, "wall": round(time.time() - t0, 1)}
            except subprocess.TimeoutExpired:
                rec[tag] = {"ok": False, "timeout": True, "wall": tmo}
            Path(cfg_path).unlink()
            # 读取产物
            try:
                fid = json.load(open(out_dir / tag / "fidelity" / f"{f.stem}_fidelity.json"))
                code = json.load(open(out_dir / tag / "code" / f"{f.stem}_code.json"))
                rec[tag].update(
                    duration=fid["cir_duration"], fidelity=fid["cir_fidelity"],
                    batches=sum(1 for i in code["instructions"] if i["type"] == "rearrangeJob"),
                    transfers=sum(len(i.get("aod_qubits", [])) for i in code["instructions"]
                                  if i["type"] == "rearrangeJob"),
                    rounds=sum(1 for i in code["instructions"] if i["type"] == "rydberg"))
            except FileNotFoundError:
                rec[tag]["ok"] = False
        summary.append(rec)
        if tags:
            t0rec = next((rec[t] for t in tags if t in rec), {})
            msg = f"{'+'.join(tags)}={'ok' if t0rec.get('ok') else '失败'} wall={t0rec.get('wall')}"
        else:
            z, x = rec.get("zac", {}), rec.get("zzx", {})
            if z.get("ok") and x.get("ok"):
                msg = f"ratio={x['duration']/z['duration']:.3f} 批 {z['batches']}→{x['batches']} 人次 {z['transfers']}→{x['transfers']}"
            else:
                msg = f"zac={'ok' if z.get('ok') else '失败'} zzx={'ok' if x.get('ok') else '失败'}"
        print(f"[{idx+1:3d}/{len(cases)}] {f.stem:32s} 2q={n2:6d} {msg}", flush=True)
        summary_name = "summary.json" if not tags else "summary_" + "_".join(tags) + ".json"
        with open(out_dir / summary_name, "w") as fp:
            json.dump(summary, fp, indent=1)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
