"""ZAC_zzx 入口（前台）：与 GA/FABLE 的 run.py 流程相同，仅创建 ZAC_zzx。

用法：
    ZAC/.venv/bin/python ZAC_zzx/run.py ZAC_zzx/exp_setting/zac_zzx_toy.json   # 冒烟
    ZAC/.venv/bin/python ZAC_zzx/run.py ZAC_zzx/exp_setting/zac_zzx_repro.json # 18 电路
（只需要一个装了 qiskit/scipy/rustworkx/matplotlib 的 Python 3.10 环境）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ZAC_zzx 文件夹自身：本地 zac/（ZAC 源码副本）和 zzx/（实验包）都从这里找
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from zac.ds.architecture import Architecture  # noqa: E402  硬件模型（本地副本）
from zac.simulator.simulator import Simulator  # noqa: E402 保真度模拟器（本地副本）
from zzx.zac_zzx import ZAC_zzx  # noqa: E402     换过发动机的 ZAC


def resolve(p: str) -> str:
    """把任务单里的相对路径锚到 ZAC_zzx 文件夹——换机器、换目录都不会迷路。"""
    q = Path(p)
    return str(q if q.is_absolute() else ROOT / q)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exp_spec", metavar="S", type=str, help="experiment specification")
    args = parser.parse_args()
    with open(args.exp_spec) as f:
        exp_spec = json.load(f)

    # ---- 收集要编译的电路（支持单文件或整个目录）----
    benchmark_set = []
    for name in exp_spec["qasm_list"]:
        name = resolve(name)
        if os.path.isfile(name):
            benchmark_set.append(name)
        elif os.path.isdir(name):
            for filename in sorted(os.listdir(name)):
                fp = os.path.join(name, filename)
                if os.path.isfile(fp):
                    benchmark_set.append(fp)

    # 架构对象较重（预处理耗时），同一份 spec 只解析一次、多电路复用
    dict_arch = {}
    for benchmark in benchmark_set:
        print("==============================================")
        print(f"Compile circuit {benchmark}")
        filename = benchmark.split("/")[-1].split(".")[0]   # 电路名（去扩展名）

        for zac_setting in exp_spec["zac_setting"]:
            if zac_setting["arch_spec"] in dict_arch:
                arch, spec = dict_arch[zac_setting["arch_spec"]]
            else:
                with open(resolve(zac_setting["arch_spec"])) as f:
                    spec = json.load(f)
                arch = Architecture(spec)
                arch.preprocessing()
                dict_arch[zac_setting["arch_spec"]] = (arch, spec)

            s = dict(zac_setting)
            s["name"] = filename
            s["dir"] = resolve(zac_setting.get("dir", "results/")) + "/"

            # 创建编译器并跑完整流水线（解析→调度→点名→布局(ZAC_zzx在这里)→路由→校验）
            compiler = ZAC_zzx()
            compiler.parse_setting(s)     # placer=batch/zac + engine 旋钮 + coloring 路由
            compiler.set_architecture_spec_path(zac_setting["arch_spec"])
            compiler.set_architecture(arch)
            compiler.set_program(benchmark)   # qiskit 解析+重综合
            for sub in ("code", "time", "fidelity"):
                os.makedirs(s["dir"] + sub, exist_ok=True)
            code_dict = compiler.solve(save_file=True)    # 主入口；结果落盘 ZAIR JSON

            # 批次账本落盘：χ 预演（batch 模式=数值行）/ 决策分布（resident 模式=字典行）
            preview = compiler.zzx_placer_preview
            if preview and isinstance(preview[0], (list, tuple)):
                preview = [list(map(float, row)) for row in preview]
            ledger = {
                "placer_preview": preview,
                "route_log": compiler.zzx_route_log,
            }
            with open(s["dir"] + f"time/{filename}_batch_ledger.json", "w") as f:
                json.dump(ledger, f, indent=1)

            if exp_spec.get("simulation", False):
                # 用同一套硬件参数给指令流打保真度分（与 ZAC 原版同一把尺子）
                simulator = Simulator()
                simulator.set_arch_spec(spec)
                simulator.parse(compiler.code_filename)
                fidelity_result = simulator.simulate()
                out = s["dir"] + f"fidelity/{filename}_fidelity.json"
                with open(out, "w") as f:
                    json.dump(fidelity_result, f, indent=2)

            if exp_spec.get("animation", False):
                # 生成原子搬运 mp4 动画（需要 ffmpeg）
                os.makedirs(s["dir"] + "animation", exist_ok=True)
                compiler.animate(code_dict, output=s["dir"] + f"animation/{filename}.mp4")
