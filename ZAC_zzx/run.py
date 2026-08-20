"""ZAC_zzx 入口（前台）：与 GA/FABLE 的 run.py 流程相同，仅创建 ZAC_zzx。

用法：
    ZAC/.venv/bin/python ZAC_zzx/run.py ZAC_zzx/exp_setting/zzx_toy_none.json  # 回归冒烟
    ZAC/.venv/bin/python ZAC_zzx/run.py ZAC_zzx/exp_setting/zzx_main.json      # 18 电路主配置
（只需要一个装了 qiskit/scipy/rustworkx/matplotlib 的 Python 3.10 环境）

每个电路的产物落盘到 dir/ 下三个子目录：
    code/      ZAIR 指令流 JSON（内嵌 gate_ledger = 重综合后的实际门列表，
               供 verify_batches.py ⑦ 语义查对账——直读 QASM 会差在共享
               重综合层，ZAC 真值同样如此，非编译 bug）
    fidelity/  ZAC 判分器五项保真度分解 + duration
    time/      编译耗时分解（SA/GA 放置/路由）+ 批次账本（route_log 每相位
               的批数与解法；placer_preview = 决策分布统计）
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

            # 语义查参照：把重综合后的实际 2q 门列表嵌进 code JSON——
            # --qasm 直读会差在共享的重综合层（ZAC 真值同样"缺门"，4 电路实证）
            gate_ledger = {}
            for g0, g1 in compiler.g_q:
                gate_ledger.setdefault(g0, []).append(g1)
                gate_ledger.setdefault(g1, []).append(g0)
            with open(compiler.code_filename) as f:
                code_json = json.load(f)
            code_json["gate_ledger"] = gate_ledger
            with open(compiler.code_filename, "w") as f:
                json.dump(code_json, f, indent=1)

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
