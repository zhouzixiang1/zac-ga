"""GA entry point: same flow as ZAC/run.py but instantiates ZAC_GA.

Paths inside the experiment spec are resolved relative to the repo root
(parent of GA/), so configs stay location-independent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "ZAC"))

from zac.ds.architecture import Architecture  # noqa: E402
from zac.simulator.simulator import Simulator  # noqa: E402
from zga.zac_ga import ZAC_GA  # noqa: E402


def resolve(p: str) -> str:
    q = Path(p)
    return str(q if q.is_absolute() else ROOT / q)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exp_spec", metavar="S", type=str, help="experiment specification")
    args = parser.parse_args()
    with open(args.exp_spec) as f:
        exp_spec = json.load(f)

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

    dict_arch = {}
    for benchmark in benchmark_set:
        print("==============================================")
        print(f"Compile circuit {benchmark}")
        filename = benchmark.split("/")[-1].split(".")[0]
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
            s["dir"] = resolve(zac_setting.get("dir", "GA/results/")) + "/"

            compiler = ZAC_GA()
            compiler.parse_setting(s)
            compiler.set_architecture_spec_path(zac_setting["arch_spec"])
            compiler.set_architecture(arch)
            compiler.set_program(benchmark)
            for sub in ("code", "time", "fidelity"):
                os.makedirs(s["dir"] + sub, exist_ok=True)
            code_dict = compiler.solve(save_file=True)
            with open(compiler.code_filename) as f:
                json.load(f)
            if exp_spec.get("simulation", False):
                simulator = Simulator()
                simulator.set_arch_spec(spec)
                simulator.parse(compiler.code_filename)
                fidelity_result = simulator.simulate()
                out = s["dir"] + f"fidelity/{filename}_fidelity.json"
                with open(out, "w") as f:
                    json.dump(fidelity_result, f, indent=2)
            if exp_spec.get("animation", False):
                os.makedirs(s["dir"] + "animation", exist_ok=True)
                compiler.animate(code_dict, output=s["dir"] + f"animation/{filename}.mp4")
