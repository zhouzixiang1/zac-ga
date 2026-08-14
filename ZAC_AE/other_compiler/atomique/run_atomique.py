import argparse
import os
import pdb
from copy import deepcopy
from multiprocessing import Pool

import numpy as np
import yaml
from torchpack.utils.config import configs

from benchmarks.benchmark_set import BenchmarkSets
from hyperparams import HyperParamSets
from utils import count_1q_2q_gates, get_n2q_interation_stats


def worker(args):
    configs, benchmark_sets, hyperparam_sets, result_path = args
    printing = False
    if configs.compiler.name == "fpqac_generic":
        from compilers.FPQAC.fpqac_generic_compiler import FPQACGenericCompiler

        compiler = FPQACGenericCompiler(configs)
    else:
        raise NotImplementedError(f"Compiler {configs.compiler.name} not implemented")

    if printing:
        print(f"number of parameter sets: {len(hyperparam_sets.all_sets)}")
    all_res = compiler.run(
        benchmark_sets=benchmark_sets, hyperparam_sets=hyperparam_sets
    )
    return all_res


def main(args_config, args_pdb, opts, printing=False, multiprocessing=True):
    configs.load(args_config, recursive=True)
    configs.update(opts)

    if args_pdb:
        pdb.set_trace()

    result_path = "../../result/atomique/"
    # result_path = args_config.replace("configs", "results").replace(".yml", "")
    configs.result_path = result_path

    os.makedirs(result_path, exist_ok=True)

    print("Loading Hyperparams...")
    hyperparam_sets = HyperParamSets(configs)
    print(f"Number of parameter sets: {len(hyperparam_sets.all_sets)}")

    print("Loading Benchmarks...")
    benchmark_sets = BenchmarkSets(configs)
    # if configs.get("count_gates", False):
    #     for benchmark in benchmark_sets.all_benchmarks:
    #         n_1q_gate, n_2q_gate = count_1q_2q_gates(benchmark.circ)
    #         stats = get_n2q_interation_stats(benchmark.circ)
    #         avg_2q = stats["n_2q_gate_qubit_list_mean"]
    #         avg_inter = stats["interact_qubit_list_mean"]
    #         print(f"& {n_2q_gate} & {n_1q_gate} & {avg_2q:.1f} & {avg_inter:.1f}")

    print("Start Compiling...")

    if multiprocessing:
        args = []
        empty_benchmark_set = deepcopy(benchmark_sets)
        empty_benchmark_set.all_benchmarks = []
        for i in range(len(benchmark_sets.all_benchmarks)):
            new_benchmark_set = deepcopy(empty_benchmark_set)
            new_benchmark_set.all_benchmarks = [benchmark_sets.all_benchmarks[i]]
            args.append((configs, new_benchmark_set, hyperparam_sets, result_path))
        with Pool() as pool:
            all_res = pool.map(worker, args)

        all_res = [item for sublist in all_res for item in sublist]
    else:
        all_res = worker((configs, benchmark_sets, hyperparam_sets, result_path))

    def numpy_array_representer(dumper, data):
        return dumper.represent_list(data.tolist())

    yaml.add_representer(np.ndarray, numpy_array_representer)
    yaml.dump(all_res, open(f"{hyperparam_sets.configs.result_path}/res.yml", "w"))
    all_fid_list = []
    all_2q_list = []
    all_depth_list = []
    all_time_list = []
    for res in all_res:
        all_fid_list.append(res["fidelity"]["total_fidelity"])
        all_time_list.append(res["time"]["total_time"])
        if "circ_stats" in res.keys():
            all_2q_list.append(res["circ_stats"]["n_2q_gate"])
            all_depth_list.append(res["circ_stats"]["n_2q_layer"])
            all_time_list.append(res["time"]["total_time"])
        # print(res['fidelity']['total_fidelity'])
        if printing:
            print(res)
            print("\n")
    if printing:
        print(all_fid_list)
        print(all_2q_list)
        print(all_depth_list)
        print(all_time_list)
    return all_res


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", metavar="FILE", help="config file")
    parser.add_argument("--run-dir", metavar="DIR", help="run directory")
    parser.add_argument("--pdb", action="store_true", help="pdb")

    args, opts = parser.parse_known_args()
    main(args.config, args.pdb, opts)
    print("[INFO] Finish Compilation")
